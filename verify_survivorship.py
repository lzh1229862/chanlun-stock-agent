"""verify_survivorship.py —— 存活着偏差对比：退市股 vs 存活股（T24 / ADR-027）。

### 问的问题

我们的 45 只样本**全部是活下来的股票**。规则如果对「会死的股票」表现差很多，
前面所有数字都是乐观的。退市股拉进来之后，直接比。

### 口径（两个群体用**同一套函数**算，保证内部一致）

- 结构/信号：与主流水线同一套参数（min_bi=5 / max_bi=5000 / power_tol=0.03）
- 入场：**确认日次一交易日开盘**（ADR-011 的 confirm_open），两个群体一致
- 超额 = 信号收益 − **同一只股票同期无条件基准**（逐日开盘买入 + 持有 w 日）
- 退市股额外施加**选项 (c)**：丢弃落在「退市前 N 个月」窗口内的信号（视为 ST 期）

### 为什么要重算存活股，而不直接用库里的数字

库里那批经过完整 F3 过滤（ST / 次新 / 涨跌停），退市股没法完全复现那套过滤。
**两个群体走同一条代码路径**才可比。重算结果可与库里数字对照，作为一致性抽查。

### 用法

    python verify_survivorship.py --smoke 5      # 先抽 5 只用小样本验证流程
    python verify_survivorship.py                # 全量对比（先在后台把数据拉齐）
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd

import backtest as bt
import verify_edge as ve
from confirm_dates import confirmation_delay
from delisted_pool import ST_WINDOW_MONTHS, is_in_st_window, load_delisted, load_pool
from min_loop import MAX_BI_NUM, MIN_BI_LEN, build_signals
from structure_gap import structure_at

WINDOWS = (5, 10, 20)
MIN_BARS = 250
WARMUP = 120          # 左侧预热 bar 数（结构要够长）


def qfq(df):
    q = df.sort_values("date").reset_index(drop=True).copy()
    if "qfq_factor" not in q.columns:
        q["qfq_factor"] = 1.0
    for c in ("open", "high", "low", "close"):
        q[c] = q[c] / q["qfq_factor"]
    return q


def unconditional(bars, window):
    """逐日开盘买入、持有 window 个交易日到收盘 —— 与 backtest.evaluate 的口径一致。"""
    o, c = bars["open"].to_numpy(), bars["close"].to_numpy()
    n = len(bars)
    if n <= window:
        return None
    fwd = c[window - 1:] / o[:n - window + 1] - 1.0
    return float(fwd.mean()) if len(fwd) else None


def cohort_signals(bars, code, windows=WINDOWS, st_cut=None):
    """算一只股票所有信号的超额。返回 (records, baseline, note)。

    records: [{"signal_date","signal_type","window","ret","exc"}, ...]
    """
    q = qfq(bars)
    if len(q) < MIN_BARS:
        return [], {}, "K 线不足 %d" % len(q)
    pos = {str(d.date()): i for i, d in enumerate(q["date"])}
    try:
        bis, zss = structure_at(code, q, len(q) - 1)
    except Exception as e:
        return [], {}, "结构失败 %s" % type(e).__name__
    recs = build_signals(bis, zss)
    if not recs:
        return [], {}, "无信号"

    base = {w: unconditional(q, w) for w in windows}
    base = {w: v for w, v in base.items() if v is not None}
    if not base:
        return [], {}, "基准算不出"

    out = []
    for r in recs:
        i = pos.get(r["date"])
        if i is None or i < WARMUP:
            continue
        if st_cut and is_in_st_window(r["date"], st_cut):
            continue                      # 选项 (c)：退市前 ST 窗口内丢弃
        try:
            delay = confirmation_delay(q, i, code, r["date"], r["type"])
        except Exception:
            continue
        if delay is None:
            continue
        i_entry = i + delay + 1
        d = bt.direction_of(r["type"])
        for w in windows:
            if w not in base:
                continue
            m = bt.evaluate(q, i_entry, w, d)
            if m is None:
                continue
            out.append({"signal_date": r["date"], "signal_type": r["type"], "window": w,
                        "ret": m["return_pct"], "exc": m["return_pct"] - base[w]})
    return out, base, "%d 信号" % len(recs)


def summarize(name, all_recs, windows=WINDOWS):
    rows = []
    for w in windows:
        sub = [r for r in all_recs if r["window"] == w]
        if len(sub) < 20:
            continue
        rets = [r["ret"] for r in sub]
        excs = [r["exc"] for r in sub]
        mean = sum(rets) / len(rets)
        exc = sum(excs) / len(excs)
        _m, lo, hi = ve.mean_ci(rets)
        rows.append({"cohort": name, "window": w, "n": len(sub), "mean": mean, "exc": exc,
                     "lo": lo, "hi": hi, "rets": rets, "excs": excs})
    return rows


def fmt_rows(rows):
    out = []
    for r in rows:
        print("  %-10s %2d 日  n=%-5d 均值=%-9s 超额=%-9s  均值CI [%s, %s]"
              % (r["cohort"], r["window"], r["n"], ve.pct(r["mean"]), ve.pct(r["exc"]),
                 ve.pct(r["lo"]), ve.pct(r["hi"])))
        out.append(r)
    return out


def run_survivors(codes, windows=WINDOWS, verbose=True):
    from storage_kline import load_kline
    recs = []
    for c in codes:
        try:
            df = load_kline(c)
        except Exception:
            continue
        if df.empty:
            continue
        r, _b, note = cohort_signals(df, c, windows)
        recs += r
        if verbose:
            print("    存活 %s  %s" % (c, note), flush=True)
    return recs


def run_delisted(pool, windows=WINDOWS, verbose=True):
    recs, done, empty = [], 0, 0
    for s in pool:
        df = load_delisted(s["code"])
        if df.empty:
            empty += 1
            continue
        r, _b, note = cohort_signals(df, s["code"], windows, st_cut=s["delist_date"])
        recs += r
        done += 1
        if verbose and done % 20 == 0:
            print("    退市已算 %d 只（无数据 %d）…" % (done, empty), flush=True)
    if verbose:
        print("    退市有数据 %d 只 / 无数据 %d 只" % (done, empty))
    return recs


def compare(surv, dels, windows=WINDOWS):
    print()
    print("=== 分组汇总 ===")
    a = fmt_rows(summarize("存活股", surv, windows))
    b = fmt_rows(summarize("退市股", dels, windows))
    print()
    print("=== 差值（退市 − 存活），Welch 95%CI ===")
    for w in windows:
        x = next((r for r in a if r["window"] == w), None)
        y = next((r for r in b if r["window"] == w), None)
        if not x or not y:
            continue
        d, lo, hi = ve.welch_diff_ci(y["rets"], x["rets"])
        de, loe, hie = ve.welch_diff_ci(y["excs"], x["excs"])
        print("  %2d 日  超额差 %-9s CI [%s, %s]  %s    (均值差 %s)"
              % (w, ve.pct(de), ve.pct(loe), ve.pct(hie),
                 "显著差异 ★" if (loe > 0 or hie < 0) else "不显著",
                 ve.pct(d)))
    return a, b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", type=int, default=0, help="只抽 N 只存活股 + N 只退市股试跑")
    a = ap.parse_args()

    import watchlist_store as ws
    pool = load_pool()
    from storage_signal import connect
    with connect() as conn:
        allc = sorted({r[0] for r in conn.execute("SELECT DISTINCT stock_code FROM signals")})

    if a.smoke:
        surv_codes = allc[:a.smoke]
        dl_pool = [s for s in pool if not load_delisted(s["code"]).empty][:a.smoke]
    else:
        surv_codes = allc
        dl_pool = pool

    print("=== 幸存者偏差对比 ===")
    print("  存活股 %d 只 / 退市股 %d 只（池 %d，已取数）"
          % (len(surv_codes), len(dl_pool), len(pool)))
    print("  ST 窗口 = 退市前 %d 个月（选项 c）" % ST_WINDOW_MONTHS)
    print()
    print("  算存活股…")
    surv = run_survivors(surv_codes, verbose=bool(a.smoke))
    print("  存活股信号 %d 条" % len(surv))
    print("  算退市股…")
    dels = run_delisted(dl_pool, verbose=True)
    print("  退市股信号 %d 条" % len(dels))
    if len(surv) < 20 or len(dels) < 20:
        print()
        print("  ⚠️ 样本太少（存活 %d / 退市 %d），先跑 --smoke 或等数据拉齐"
              % (len(surv), len(dels)))
        return 1
    a_, b_ = compare(surv, dels)
    Path("config/survivorship_result.json").write_text(json.dumps(
        {"windows": list(WINDOWS), "st_window_months": ST_WINDOW_MONTHS,
         "survivors": [{k: v for k, v in r.items() if k not in ("rets", "excs")} for r in a_],
         "delisted": [{k: v for k, v in r.items() if k not in ("rets", "excs")} for r in b_],
         "n_surv_stocks": len(surv_codes), "n_delist_stocks": len(dl_pool)},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print("  结果已存 config/survivorship_result.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
