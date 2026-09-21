"""verify_minute.py —— 60 分钟级别的「级别共振」验证（T21 / ADR-025）。

### 问的问题

日线买点出现时，如果 **60 分钟级别也同向**，后续表现会更好吗？

（ADR-019 已经测过**周线**同向 —— 结论是「反而更差」。60 分钟是另一个方向，不能外推。）

### 样本受限于数据源

新浪的 60 分钟接口**固定只给最近 1970 根 ≈ 2 年**（见 storage_minute 的说明）。
所以只有 **2024-09 之后**的日线信号能参与检验，而且还要给 60 分钟结构留出预热期。
结果：可用样本约 700 条量级、只覆盖单一市场环境（ADR-020 记录那段以下行为主）。
**结论只能当线索，不能当定论。**

### 口径

- 60 分钟状态 = 该**确认日收盘（15:00）**为止的最后一笔方向（"同向" = 与信号方向一致）
- 日线信号的三日期 / 超额口径与 verify_edge 一致（复用 verify_hypotheses 的机器）
- 基准在**本期自己的跨度内**重算

### 用法

    python verify_minute.py --fetch        # 先把股票池的 60 分钟数据拉下来
    python verify_minute.py                # 跑共振验证
"""
import argparse
import sys

import pandas as pd

import storage_minute as sm
import verify_hypotheses as vh
from min_loop import MAX_BI_NUM, MIN_BI_LEN, build_zs

MIN_BARS = 240          # 60 分钟少于这个数不判状态（约 60 个交易日预热）
DOWN = "Down"


def is_down(bi):
    return str(bi.direction) == DOWN


def dir_at(code, day):
    """60 分钟级别在 day 收盘时的最后一笔方向。返回 "up"/"down"/None。"""
    d = sm.load_minute(code)
    if d.empty:
        return None
    cutoff = pd.Timestamp(day) + pd.Timedelta(hours=15)
    sub = d[d["dt"] <= cutoff]
    if len(sub) < MIN_BARS:
        return None
    import czsc
    s = sub.rename(columns={"volume": "vol"}).copy()
    s["symbol"] = code
    b = czsc.format_standard_kline(s, freq=czsc.Freq.F60)
    cz = czsc.CZSC(b, min_bi_len=MIN_BI_LEN, max_bi_num=MAX_BI_NUM)
    bis = list(cz.bi_list)
    if not bis:
        return None
    return "down" if is_down(bis[-1]) else "up"


def fetch_pool(codes=None, verbose=True):
    import watchlist_store as ws
    codes = codes or ws.load_watchlist()
    print("=== 拉 60 分钟数据（新浪，固定给最近 1970 根）===")
    for c in codes:
        src, err = sm.ensure_minute(c, refresh=True)
        d = sm.load_minute(c)
        print("  %-8s %-8s %4d 根  %s ~ %s  %s"
              % (c, src or "FAIL", len(d),
                 str(d["dt"].min())[:16] if not d.empty else "—",
                 str(d["dt"].max())[:16] if not d.empty else "—", err or ""), flush=True)
    return codes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--codes", default=None)
    ap.add_argument("--window", type=int, default=5)
    a = ap.parse_args()
    codes = [c.strip() for c in a.codes.split(",")] if a.codes else None

    if a.fetch:
        fetch_pool(codes)
        return 0

    # 默认用「signals 库里所有、且本地有 60 分钟数据」的股票 —— 样本越大越好。
    # 不用自选股池：那只覆盖 10 只，2 年窗口下分组会小到没法判断。
    if not codes:
        import storage_signal as ss
        with ss.connect() as conn:
            allc = [r[0] for r in conn.execute("SELECT DISTINCT stock_code FROM signals")]
        import watchlist_store as ws
        codes = sorted(c for c in set(allc) | set(ws.load_watchlist())
                       if not sm.load_minute(c).empty) or ws.load_watchlist()
    m = vh.load_merged()
    m = m[m["window"] == a.window]
    m = m[m["stock_code"].isin(codes)]
    if m.empty:
        print("没有数据")
        return 1

    print("=== 60 分钟级别共振验证（窗口 %d 日）===" % a.window)
    cache = {}
    rows = []
    for r in m.itertuples():
        key = (r.stock_code, r.confirm_date)
        if key not in cache:
            cache[key] = dir_at(r.stock_code, r.confirm_date)
        d60 = cache[key]
        if d60 is None:
            continue
        want = "up" if "买点" in r.signal_type else "down"
        rows.append({**r._asdict(), "d60": d60, "same": d60 == want})
    if not rows:
        print("  没有可用样本（60 分钟数据缺失或预热不足）")
        return 1
    sub = pd.DataFrame(rows)
    print("  可用样本 %d 条 / %d 只   确认日 %s ~ %s"
          % (len(sub), sub["stock_code"].nunique(),
             sub["confirm_date"].min(), sub["confirm_date"].max()))
    print("  覆盖区间仅约 2 年 —— 单一市场环境，结论只能当线索")
    print()

    base = vh.baseline_per_code(sub, a.window)
    for lab, mask in (("60分钟同向", sub["same"]),
                      ("60分钟反向", ~sub["same"])):
        g = sub[mask]
        if len(g) < 30:
            print("  %-12s n=%d 太少" % (lab, len(g)))
            continue
        rets, exc = vh.excess_of(g, base, a.window)
        print("  %-12s n=%-5d 超额 %s   胜率 %.0f%%"
              % (lab, len(g), vh.pct(exc),
                 100.0 * sum(1 for x in rets if x > 0) / len(rets)))
    a_g = sub[sub["same"]]
    b_g = sub[~sub["same"]]
    if len(a_g) >= 30 and len(b_g) >= 30:
        ra, ea = vh.excess_of(a_g, base, a.window)
        rb, eb = vh.excess_of(b_g, base, a.window)
        d, dlo, dhi = vh.welch_diff_ci(ra, rb)
        de = ea - eb
        dlo, dhi = dlo + (de - d), dhi + (de - d)
        print()
        print("  同向 − 反向 = %s   CI [%s, %s]   %s"
              % (vh.pct(de), vh.pct(dlo), vh.pct(dhi),
                 "不跨0 ✓" if (dlo > 0 or dhi < 0) else "跨0（不显著）"))
        cut = vh.ve.split_point([{"entry_date": x} for x in sub["entry_date"]], 0.5)
        hh = []
        for x1, y1 in ((a_g[a_g["entry_date"] <= cut], b_g[b_g["entry_date"] <= cut]),
                       (a_g[a_g["entry_date"] > cut], b_g[b_g["entry_date"] > cut])):
            if len(x1) < 20 or len(y1) < 20:
                hh.append(None)
                continue
            _, e1 = vh.excess_of(x1, base, a.window)
            _, e2 = vh.excess_of(y1, base, a.window)
            hh.append(e1 - e2)
        print("  分半: %s" % " / ".join(vh.pct(x) if x is not None else "—" for x in hh))
    print()
    print("  ⚠️ 样本仅约 2 年、单一市场环境；且 60 分钟数据只有 2 年历史无法延长。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
