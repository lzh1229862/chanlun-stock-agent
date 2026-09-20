"""verify_level.py —— 级别共振：日线信号 × 周线状态（T12-1）

缠论的本质是**级别递归**，而本项目只用日线。这是补这块的第一步：
把日线重采样成周线（**零新数据依赖**），在周线上也算一套笔/中枢，然后问：

    **日线信号，如果周线方向也配合，超额是不是真的更高？**

为什么「共振」听起来对
    大级别定方向、小级别找买卖点 —— 这是缠论级别递归的直接推论。
    但「听起来对」不算数：必须看「配合组 vs 不配合组」的**差值置信区间**。
    两组各自区间都很宽时，差值大概率跨 0，那就什么也没证明。

⚠️ 防未来函数（这一步最容易做错）
    周线要「当周走完」才知道。所以对信号日 D，只能用
    **结束日严格早于 D 所在周周一** 的周线 —— 也就是上周五及更早。
    用当周（还没走完）的周线算状态，就是未来函数，会把结果吹得很漂亮。

三个条件（都按信号方向对称）
    W1  周线最后一笔方向与信号同向
    W2  周线收盘在最近周线中枢的上沿之上（买点）/ 下沿之下（卖点）
    W3  周线收盘在 20 周均线之上（买点）/ 之下（卖点）

入场口径与 verify_robustness 一致：信号日次一交易日开盘（不做确认延迟前缀重算）。

运行
    python verify_level.py                  # 默认 5 日窗口
    python verify_level.py --window 20
    python verify_level.py --quick          # 只取 3 只股票
"""
import argparse
import sys

import pandas as pd

import backtest as bt
from verify_edge import mean_ci, pct, unconditional, welch_diff_ci
from verify_robustness import qfq_bars, signals_on, universe

CONDITIONS = [
    ("W1 周线笔同向", "周线最后一笔方向与信号一致"),
    ("W2 周线中枢", "买点：收盘在周线中枢上沿之上；卖点：在下沿之下"),
    ("W3 周线MA20", "买点：收盘在 20 周均线之上；卖点：之下"),
]


# ==================== 周线重采样（纯函数）====================

def weekly_bars(q):
    """日线（前复权）-> 周线。date 取该周**最后一个交易日**（不是周五标签）。

    用最后一个真实交易日而不是日历周五，是因为「这周走完了吗」的判断
    必须基于真实交易日 —— 遇到长假周，周五根本不是交易日。
    """
    if q.empty:
        return q
    d = q.copy()
    d["wk"] = d["date"].dt.to_period("W-SUN")     # 周一~周日为一周
    g = d.groupby("wk")
    return pd.DataFrame({
        "date": g["date"].max(),
        "open": g["open"].first(), "high": g["high"].max(),
        "low": g["low"].min(), "close": g["close"].last(),
        "volume": g["volume"].sum(), "amount": g["amount"].sum(),
    }).reset_index(drop=True)


def week_start(d):
    """d 所在周的周一。"""
    t = pd.Timestamp(d)
    return t - pd.Timedelta(days=t.weekday())


def weekly_index_for(wbars, d):
    """最后一个「结束日 < d 所在周周一」的周线下标；没有可用的返回 None。

    这一句就是防未来函数的关键：严格早于本周一，也就是上周五及更早。
    """
    if wbars.empty:
        return None
    ok = wbars.index[wbars["date"] < week_start(d)]
    return int(ok[-1]) if len(ok) else None


def weekly_structure(wbars):
    """在给定周线序列上算笔与中枢。"""
    import czsc
    from min_loop import MAX_BI_NUM, MIN_BI_LEN, build_zs
    if len(wbars) < 10:
        return [], []
    w = wbars.rename(columns={"volume": "vol"}).copy()
    w["dt"] = pd.to_datetime(w["date"])
    w["symbol"] = "W"
    b = czsc.format_standard_kline(w, freq=czsc.Freq.W)
    cz = czsc.CZSC(b, min_bi_len=MIN_BI_LEN, max_bi_num=MAX_BI_NUM)
    bis = list(cz.bi_list)
    return bis, build_zs(bis)


def state_at(wbars, j, cache):
    """第 j 周为止的周线状态（只用到 wbars[:j+1]，不偷看后面）。"""
    if j in cache:
        return cache[j]
    sub = wbars.iloc[:j + 1]
    bis, zss = weekly_structure(sub)
    close = float(sub["close"].iloc[-1])
    ma20 = float(sub["close"].tail(20).mean()) if len(sub) >= 20 else None
    st = {"bi_dir": str(bis[-1].direction) if bis else None,
          "zg": zss[-1]["zg"] if zss else None,
          "zd": zss[-1]["zd"] if zss else None,
          "close": close, "ma20": ma20}
    cache[j] = st
    return st


def group_of(cond, st, direction):
    """返回 True（配合）/ False（不配合）/ None（无法判定）。"""
    if st is None:
        return None
    if cond == "W1 周线笔同向":
        if not st.get("bi_dir"):
            return None
        return st["bi_dir"] == ("向上" if direction > 0 else "向下")
    if cond == "W2 周线中枢":
        if st.get("zg") is None:
            return None
        return st["close"] > st["zg"] if direction > 0 else st["close"] < st["zd"]
    if cond == "W3 周线MA20":
        if st.get("ma20") is None:
            return None
        return st["close"] > st["ma20"] if direction > 0 else st["close"] < st["ma20"]
    return None


# ==================== 分组统计 ====================

def group_stats(sigs, bars_map, window):
    """sigs = [(code, date, type)]，同类同方向。基准在该组自己的日期跨度内按股重算。"""
    by_code = {}
    for code, d, t in sigs:
        by_code.setdefault(code, []).append((d, t, bt.direction_of(t)))
    rets, bases = [], []
    for code, items in by_code.items():
        bars = bars_map[code]
        pos = {str(x.date()): i for i, x in enumerate(bars["date"])}
        rr = []
        for d, t, dr in items:
            i = pos.get(d)
            if i is None or i + 1 >= len(bars):
                continue
            r = bt.evaluate(bars, i + 1, window, dr)
            if r:
                rr.append(r["return_pct"])
        if not rr:
            continue
        rets += rr
        ds = [d for d, _, _ in items]
        b = unconditional(code, window, min(ds), max(ds))
        if b[0] is not None:
            bases.append((items[0][2] * b[0], len(rr)))
    if len(rets) < 2 or not bases:
        return None          # n<2 算不出区间，直接当样本不足
    base = sum(v * n for v, n in bases) / sum(n for _, n in bases)
    m, lo, hi = mean_ci(rets)
    return {"n": len(rets), "mean": m, "base": base, "exc": m - base,
            "exc_lo": lo - base, "exc_hi": hi - base, "rets": rets}


def verdict(d, lo, hi, n1, n2):
    if d is None:
        return "样本不足"
    if n1 < 10 or n2 < 10:
        return "样本不足"
    if lo <= 0 <= hi:
        return "不显著"
    return "✓ 配合更好" if d > 0 else "✗ 配合更差"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=5)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--min-n", type=int, default=10)
    a = ap.parse_args()

    codes = universe(3 if a.quick else None)
    bars_map = {c: qfq_bars(c) for c in codes}
    wk_map = {c: weekly_bars(bars_map[c]) for c in codes}

    print("=" * 112)
    print("级别共振：日线信号 × 周线状态   窗口=%d 日   标的 %d 只" % (a.window, len(codes)))
    print("周线只用「结束日严格早于信号周周一」的那些 —— 当周还没走完，用它就是未来函数")
    for c in codes[:3]:
        w = wk_map[c]
        print("  %s 周线 %d 根（%s ~ %s）" % (c, len(w), w["date"].iloc[0].date(), w["date"].iloc[-1].date()))
    if len(codes) > 3:
        print("  …（其余 %d 只略）" % (len(codes) - 3))
    print("=" * 112)

    # 给每条信号打好 (code, date, type) 并确定周线状态
    records = []
    for c in codes:
        w = wk_map[c]
        cache = {}
        for d, t in sorted(signals_on(bars_map[c], c)):
            i = weekly_index_for(w, d)
            records.append({"code": c, "date": d, "type": t,
                            "dir": bt.direction_of(t),
                            "state": state_at(w, i, cache) if i is not None else None})
    types = sorted({r["type"] for r in records})
    print()
    print("信号总数 %d 条（日线结构信号，未过滤）" % len(records))

    print()
    print("【分组对照】每个条件把同类信号切成「配合 / 不配合」两组")
    print("  %-14s %-11s %-5s %-6s %-10s %-7s %-10s %-10s %-19s %s"
          % ("条件", "类型", "配合n", "占比", "配合超额", "不配合n", "不配合超额",
             "差值", "差值 95%CI", "判断"))
    rows = []
    for cname, _ in CONDITIONS:
        for t in types:
            sigs = [(r["code"], r["date"], r["type"]) for r in records if r["type"] == t]
            hit, miss = [], []
            for r in records:
                if r["type"] != t:
                    continue
                g = group_of(cname, r["state"], r["dir"])
                if g is None:
                    continue
                (hit if g else miss).append((r["code"], r["date"], r["type"]))
            sa = group_stats(hit, bars_map, a.window)
            sb = group_stats(miss, bars_map, a.window)
            if sa is None or sb is None:
                continue
            # 差值必须比**超额**之差，不能比原始均值之差：
            # 两组时间跨度不同 -> 基准不同 -> 原始均值之差里混着 beta
            d_raw, lo_raw, hi_raw = welch_diff_ci(sa["rets"], sb["rets"])
            if d_raw is None:
                d = lo = hi = None
            else:
                half = (hi_raw - lo_raw) / 2
                d = sa["exc"] - sb["exc"]
                lo, hi = d - half, d + half
            v = verdict(d, lo, hi, sa["n"], sb["n"])
            rows.append((cname, t, sa, sb, d, lo, hi, v))
            print("  %-14s %-11s %-6d %-6s %-10s %-7d %-10s %-10s %-19s %s"
                  % (cname, t, sa["n"], "%d%%" % round(100 * sa["n"] / max(sa["n"] + sb["n"], 1)),
                     pct(sa["exc"]), sb["n"], pct(sb["exc"]), pct(d),
                     "[%s, %s]" % (pct(lo), pct(hi)), v))

    print()
    print("=" * 112)
    good = [r for r in rows if r[7].startswith("✓")]
    bad = [r for r in rows if r[7].startswith("✗")]
    print("结论：%d 个组合里，「配合组显著更好」%d 个、「配合组显著更差」%d 个，"
          "其余差值不显著或样本不足。" % (len(rows), len(good), len(bad)))
    for r in good:
        print("  ✓ %s / %s  差值 %s CI [%s, %s]" % (r[0], r[1], pct(r[4]), pct(r[5]), pct(r[6])))
    for r in bad[:5]:
        print("  ✗ %s / %s  差值 %s CI [%s, %s]" % (r[0], r[1], pct(r[4]), pct(r[5]), pct(r[6])))
    if not good and not bad:
        print("  → 没有任何条件能显著拉开差距。**「级别共振」在这个样本里没有得到支持。**")
    print()
    print("怎么读")
    print("  · 看「差值」和它的 CI，不看两组各自的超额 —— 各自区间宽时差值几乎一定跨 0")
    print("  · 本轮只加了**周线**（日线重采样，零新数据）；60 分钟级别需要新数据源，尚未做")
    print("  · 周线笔/中枢用的仍是 min_bi_len=5；而 ADR-005 补记已说明它是个分档开关")
    print("  · 样本期单一上行、n 很小，结论不能当策略有效性证据")
    return 0


if __name__ == "__main__":
    sys.exit(main())
