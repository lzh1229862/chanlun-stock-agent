"""signal_features.py —— 为每条信号提取特征（T16-1）。

⚠️ 为什么不能用全序列结构
    缠论的笔/中枢是**顺序构造**的。用整段历史算出来的「信号日附近的中枢」，
    可能包含信号日之后才成形的那一笔 —— 那就是未来函数，会让所有后续分析虚高。
    所以每条信号都用**只截到信号日为止**的 K 线重算结构（实测 12ms/条，4200 条约 50 秒）。

产出
    data/signal_features.parquet（不进版本库），一行一条信号。

特征分三类
    结构   position / zs_gap_days / zs_width_pct / zs_n / bi_bars / bi_pct
    量价   vol_ratio_20 / amount_yi / mom_20 / vola_20 / dist_ma60
    上下文 confirm_delay / board / regime

用法
    python signal_features.py              # 重建全部
    python signal_features.py --code 600519
"""
import argparse
import sys
import time
from pathlib import Path

import pandas as pd

import market_regime as mr
import storage_index as si
import storage_kline as sk
import storage_signal as ss
from min_loop import MAX_BI_NUM, MIN_BI_LEN, build_zs

OUT = Path("data/signal_features.parquet")
NO_ZS = "无中枢"


def board_of(code):
    if code.startswith("688"):
        return "科创板"
    if code.startswith("30"):
        return "创业板"
    if code.startswith("60"):
        return "沪主板"
    return "深主板"


def qfq_bars(code):
    q = sk.load_kline(code)
    if q.empty:
        return q
    q = q.sort_values("date").reset_index(drop=True).copy()
    for c in ("open", "high", "low", "close"):
        q[c] = q[c] / q["qfq_factor"]
    return q


def macd_hist(q, fast=12, slow=26, sig=9):
    """MACD 柱（DIF - DEA）。

    **只用传入的 bars** —— 调用方负责不要给未来数据。这里刻意不用 czsc 的 MACD，
    因为它挂在 CZSC 对象上、和我们「按前缀重算」的口径不好对齐。
    """
    c = q["close"]
    dif = c.ewm(span=fast, adjust=False).mean() - c.ewm(span=slow, adjust=False).mean()
    dea = dif.ewm(span=sig, adjust=False).mean()
    return dif - dea


def bi_area(q, hist, b):
    """一笔覆盖区间内 |MACD 柱| 之和 —— 缠论里「力度」的一种连续度量。"""
    j = q.index[q["date"] == pd.Timestamp(b.sdt).normalize()]
    k = q.index[q["date"] == pd.Timestamp(b.edt).normalize()]
    if not len(j) or not len(k):
        return None
    seg = hist.iloc[int(j[0]):int(k[0]) + 1]
    return float(seg.abs().sum()) if len(seg) else None


def struct_at(code, q, i):
    """只用到第 i 根为止的 K 线算笔与中枢。"""
    import czsc
    sub = q.iloc[:i + 1]
    s = sub.rename(columns={"volume": "vol"}).copy()
    s["dt"] = pd.to_datetime(s["date"])
    s["symbol"] = code
    b = czsc.format_standard_kline(s, freq=czsc.Freq.D)
    cz = czsc.CZSC(b, min_bi_len=MIN_BI_LEN, max_bi_num=MAX_BI_NUM)
    bis = list(cz.bi_list)
    return bis, build_zs(bis)


def features_row(code, q, i, i_asof, sig, reg_df):
    """i = 信号日下标（量价口径）；i_asof = 确认日下标（结构口径）。

    ⚠️ 结构必须读在**确认日**：实测信号日当天那根触发笔**还没成形**
    （例：600519 信号日 2015-03-06，但只用当天为止的数据，最后一笔结束在 2015-02-27）。
    这正是 ADR-011 的确认延迟 —— 笔要靠后续 K 线才能确认。用信号日读结构会读了个空。
    """
    bis, zss = struct_at(code, q, i_asof)
    close = float(q.at[i, "close"])
    date = str(q.at[i, "date"].date())

    # --- 结构位置（相对**信号日为止**的最近中枢）---
    z = zss[-1] if zss else None
    if z is None:
        pos, gap, zw, zn = NO_ZS, None, None, None
    else:
        pos = "中枢上方" if close > z["zg"] else ("中枢下方" if close < z["zd"] else "中枢内部")
        k = q.index[q["date"] == pd.Timestamp(z["edt"]).normalize()]
        gap = int(i - k[0]) if len(k) else None
        zw = (z["zg"] - z["zd"]) / close if close else None
        zn = z["n"]

    # --- 触发笔（signal_date = BI.edt）---
    # 注意：czsc 的笔端点**带时间分量**，必须 normalize 后才能和 Parquet 的零点日期比。
    bi_bars = bi_pct = None
    bi_idx = None
    for k in range(len(bis) - 1, -1, -1):
        b = bis[k]
        if str(pd.Timestamp(b.edt).date()) != date:
            continue
        bi_idx = k
        j = q.index[q["date"] == pd.Timestamp(b.sdt).normalize()]
        if len(j):
            bi_bars = i - int(j[0]) + 1
            bi_pct = abs(float(b.change))
        break

    # --- 背驰强度：触发笔 ÷ 上一同向笔 的 MACD 面积比（<1 表示背驰）---
    # 已有的力度判定是 power_price / power_volume 的**硬阈值**（min_loop.POWER_TOL），
    # 这里补一个**连续量**，才有可能用来排序。
    # MACD 只算到**确认日**为止（与结构口径一致），不碰未来数据。
    area_cur = area_prev = area_ratio = None
    if bi_idx is not None:
        hist = macd_hist(q.iloc[:i_asof + 1])
        area_cur = bi_area(q, hist, bis[bi_idx])
        for m in range(bi_idx - 1, -1, -1):
            if str(bis[m].direction) == str(bis[bi_idx].direction):
                area_prev = bi_area(q, hist, bis[m])
                break
        if area_cur is not None and area_prev:
            area_ratio = area_cur / area_prev

    # --- 量价 ---
    win = q.iloc[max(0, i - 19):i + 1]
    vol_ratio = float(q.at[i, "volume"]) / float(win["volume"].mean()) if win["volume"].mean() else None
    amount_yi = float(q.at[i, "amount"]) / 1e8
    if i >= 20:
        mom20 = close / float(q.at[i - 20, "close"]) - 1
    else:
        mom20 = None
    rets = q["close"].iloc[max(0, i - 19):i + 1].pct_change().dropna()
    vola20 = float(rets.std()) * (244 ** 0.5) if len(rets) > 2 else None
    if i >= 59:
        ma60 = float(q["close"].iloc[i - 59:i + 1].mean())
        dist60 = close / ma60 - 1 if ma60 else None
    else:
        dist60 = None

    return {"stock_code": code, "signal_date": date, "signal_type": sig,
            "position": pos, "zs_gap_days": gap, "zs_width_pct": zw, "zs_n": zn,
            "bi_bars": bi_bars, "bi_pct": bi_pct,
            "macd_area_cur": area_cur, "macd_area_prev": area_prev,
            "macd_area_ratio": area_ratio,
            "vol_ratio_20": vol_ratio, "amount_yi": amount_yi, "mom_20": mom20,
            "vola_20": vola20, "dist_ma60": dist60,
            "board": board_of(code), "regime": mr.regime_on(reg_df, date)}


def build(codes=None, verbose=True):
    conn = ss.connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT stock_code, signal_date, signal_type, confirm_date FROM signals "
        "WHERE is_tradable=1 ORDER BY stock_code, signal_date")]
    conn.close()
    if codes:
        rows = [r for r in rows if r["stock_code"] in codes]

    idx, _, _ = si.ensure_index("000300")
    reg_df = mr.with_indicators(idx) if idx is not None else pd.DataFrame()

    out, t0 = [], time.time()
    cur, q, pos = None, None, None
    for n, r in enumerate(rows, 1):
        c = r["stock_code"]
        if c != cur:
            q = qfq_bars(c)
            pos = {str(d.date()): i for i, d in enumerate(q["date"])} if not q.empty else {}
            cur = c
        i = pos.get(r["signal_date"]) if pos else None
        i_asof = pos.get(r["confirm_date"]) if (pos and r.get("confirm_date")) else None
        if i is None or i_asof is None:
            continue          # 还没确认的信号不进特征表：没法谈"当时可知什么"
        rec = features_row(c, q, i, i_asof, r["signal_type"], reg_df)
        rec["confirm_delay"] = i_asof - i     # 确认延迟 = 确认日 − 信号日（交易日数）
        out.append(rec)
        if verbose and n % 500 == 0:
            print("    %d/%d  %.0fs" % (n, len(rows), time.time() - t0), flush=True)
    if verbose:
        print("  完成 %d 条，用时 %.0fs" % (len(out), time.time() - t0))
    return pd.DataFrame(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default=None, help="只算某只（逗号分隔）")
    a = ap.parse_args()
    codes = [c.strip() for c in a.code.split(",")] if a.code else None
    print("=== 提取信号特征（每条只用<到信号日为止>的 K 线）===")
    df = build(codes)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, index=False)
    print("  写入 %s（%d 行 x %d 列）" % (OUT, len(df), len(df.columns)))
    print("  列:", list(df.columns))
    return 0


if __name__ == "__main__":
    sys.exit(main())
