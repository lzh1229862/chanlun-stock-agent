"""verify_robustness.py —— 参数敏感性与信号稳定性（T11-1 / T11-2）

两个「证伪」用的检查，共用同一套「按给定参数在给定 K 线上重算信号」的核心，
避免两边口径各写一份、悄悄漂移。

    sens  参数敏感性   min_bi_len / 中枢延伸上限 / POWER_TRADE 各扫一遍，看：
                       · 信号集合换掉多少（Jaccard 重合度）
                       · 多出来 / 消失的信号，表现比原来的好还是差
                       如果参数一动信号就换一批，说明现在的「准确率」是数值敏感拟合出来的，
                       而不是结构本身稳健。

    stab  信号稳定性   前缀重算：K 线增量更新后，**已确认的历史信号不应被改写**。
                       缠论实现最容易在这里出问题 —— 新来一根 K 线，昨日的笔被重新划分，
                       于是几天前发过的信号「消失」了。这个可以直接测、零外界依赖。

为什么先做这两件事
    它们的作用是**证伪**，不是调优。在拿参数去「提高准确率」之前，必须先知道
    结论对参数有多敏感；否则调出来的好看数字分不清是规则变好了还是参数碰巧。

入场口径说明（重要）
    本文件**不用**回测的「确认日次一交易日开盘」 —— 那需要对每条信号跑前缀重算，代价极高。
    统一用「信号日次一交易日开盘」。因为敏感性与稳定性只要求**跨参数、跨前缀一致**，
    不要求与 backtest 表可比。**这里的收益数字不要和 verify_edge.py 的输出直接比。**

运行
    python verify_robustness.py sens            # 参数敏感性
    python verify_robustness.py stab            # 信号稳定性
    python verify_robustness.py all             # 两个都跑
    python verify_robustness.py sens --quick    # 只取 3 只股票
    python verify_robustness.py sens --window 20
"""
import argparse
import sys
from datetime import datetime

import pandas as pd

import backtest as bt
import confirm_dates as cd
from min_loop import MAX_BI_NUM, MAX_ZS_BIS, MIN_BI_LEN, POWER_TOL, build_signals, build_zs
from storage_kline import load_kline
from storage_signal import connect
from verify_edge import mean_ci, pct, pc0, unconditional

BASELINE = {"min_bi_len": MIN_BI_LEN, "max_zs_bis": MAX_ZS_BIS, "power_tol": POWER_TOL}

# 扫描范围。基准值由 min_loop 的常量决定，写在这里只是为了让输出能标注哪些是当前值。
SWEEPS = [
    ("min_bi_len", [3, 4, 5, 6, 7, 8], "笔的最小长度（czsc min_bi_len）"),
    ("max_zs_bis", [5, 7, 9, 11, 13], "中枢延伸上限（笔数）"),
    ("power_tol", [0.0, 0.01, 0.03, 0.05, 0.10], "背驰力度容差 POWER_TOL"),
]

STAB_SETTLE_LAG = 20   # 检验点往前多少个交易日算「已确认」
STAB_CHECKPOINTS = [60, 40, 20]


# ==================== 核心：在给定 K 线上按给定参数算信号 ====================

def signals_on(bars_qfq, code, min_bi_len=MIN_BI_LEN, max_zs_bis=MAX_ZS_BIS,
               power_tol=POWER_TOL):
    """给定（可截断的）前复权 K 线 + 参数 -> {(日期, 类型)}。

    **不改动任何全局常量** —— 参数全部显式传下去，所以同一个进程里能安全地反复调不同取值。
    """
    import czsc
    q = bars_qfq.rename(columns={"volume": "vol"}).copy()
    q["dt"] = pd.to_datetime(q["date"])
    q["symbol"] = code
    b = czsc.format_standard_kline(q, freq=czsc.Freq.D)
    cz = czsc.CZSC(b, min_bi_len=min_bi_len, max_bi_num=MAX_BI_NUM)
    bis = list(cz.bi_list)
    zss = build_zs(bis, max_bis=max_zs_bis)
    return {(x["date"], x["type"]) for x in build_signals(bis, zss, power_tol=power_tol)}


def qfq_bars(code):
    """前复权 K 线（与信号生成同一口径）。"""
    df = load_kline(code)
    if df.empty:
        return df
    q = df.sort_values("date").reset_index(drop=True).copy()
    for c in ("open", "high", "low", "close"):
        q[c] = q[c] / q["qfq_factor"]
    return q


def universe(limit=None):
    """本地有 Parquet 的股票（取回测表里出现过的代码，覆盖面比当前股票池广）。"""
    conn = connect()
    try:
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT stock_code FROM backtest ORDER BY stock_code")]
    finally:
        conn.close()
    codes = [c for c in codes if not qfq_bars(c).empty]
    return codes[:limit] if limit else codes


def jaccard(a, b):
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def forward_returns(sigset, bars, window):
    """入场 = 信号日次一交易日开盘，方向调整后收益。"""
    pos = {str(d.date()): i for i, d in enumerate(bars["date"])}
    out = []
    for d, t in sorted(sigset):
        i = pos.get(d)
        if i is None or i + 1 >= len(bars):
            continue
        r = bt.evaluate(bars, i + 1, window, bt.direction_of(t))
        if r:
            out.append(r["return_pct"])
    return out


def excess_of(rets, codes_bars, window):
    """扣掉「同股票无条件基准」之后的均值。基准按各股自己的全区间算。"""
    if not rets:
        return None, None, None
    bases = []
    for code, bars in codes_bars:
        lo = str(bars["date"].iloc[0].date())
        hi = str(bars["date"].iloc[-1].date())
        b = unconditional(code, window, lo, hi)
        if b[0] is not None:
            bases.append(b[0])
    if not bases:
        return None, None, None
    base = sum(bases) / len(bases)
    m, lo, hi = mean_ci(rets)
    return m - base, lo - base, hi - base


# ==================== 口径自检 ====================

def selfcheck(codes):
    """signals_on(默认参数) 必须与 confirm_dates.compute_signals 完全一致。

    两边各写一份「怎么把 K 线喂给 czsc」，一旦漂移，后面所有结论都是假的。
    """
    for c in codes:
        q = qfq_bars(c)
        if q.empty:
            continue
        mine = signals_on(q, c)
        theirs = cd.compute_signals(q, c)
        if mine != theirs:
            print("  ✗ 口径不一致 %s：本文件 %d 条 / confirm_dates %d 条，差集 %s"
                  % (c, len(mine), len(theirs), sorted(mine ^ theirs)[:5]))
            return False
    print("  ✓ 口径自检通过：signals_on() 与 confirm_dates.compute_signals() 完全一致（%d 只）"
          % len(codes))
    return True


# ==================== 参数敏感性 ====================

def run_sens(args):
    codes = universe(args.quick and 3 or None)
    print("标的 %d 只 · 入场口径 = 信号日次一交易日开盘（与 backtest 表口径不同，只做跨参数比较）"
          % len(codes))
    print("基准参数 min_bi_len=%d / 中枢上限=%d 笔 / POWER_TOL=%.2f"
          % (MIN_BI_LEN, MAX_ZS_BIS, POWER_TOL))
    print()
    print("【口径自检】")
    if not selfcheck(codes):
        return 1

    bars = {c: qfq_bars(c) for c in codes}
    base_sets = {c: signals_on(bars[c], c) for c in codes}
    base_all = set().union(*base_sets.values())
    print("  基准信号总数（去重后按 (日期, 类型)）: %d" % len(base_all))

    for key, values, label in SWEEPS:
        print()
        print("【%s 扫描】%s" % (key, label))
        print("  %-11s %-7s %-9s %-9s %-6s %-6s %-10s %-20s %s"
              % (key, "信号数", "与基准重合", "Jaccard", "新增", "消失",
                 "超额(%d日)" % args.window, "超额 95%CI", "判断"))
        for v in values:
            kw = dict(BASELINE)
            kw[key] = v
            sets = {c: signals_on(bars[c], c, **kw) for c in codes}
            allsig = set().union(*sets.values())
            inter = len(allsig & base_all)
            jac = jaccard(allsig, base_all)
            rets = []
            for c in codes:
                rets += forward_returns(sets[c], bars[c], args.window)
            exc, lo, hi = excess_of(rets, list(bars.items()), args.window)
            mark = "  ← 当前" if v == BASELINE[key] else ""
            if jac >= 0.9:
                verdict = "稳定"
            elif jac >= 0.6:
                verdict = "有变化"
            else:
                verdict = "⚠ 数值敏感"
            print("  %-11s %-7d %-9d %-9.3f %-6d %-6d %-10s %-20s %s%s"
                  % (v, len(allsig), inter, jac, len(allsig - base_all),
                     len(base_all - allsig), pct(exc), "[%s, %s]" % (pct(lo), pct(hi)),
                     verdict, mark))

    print()
    print("怎么读")
    print("  · Jaccard = |交集| / |并集|，1.0 表示信号集合完全没变")
    print("  · 「新增/消失」是相对基准参数的对称差；两者都很大 = 换了一批信号，不是补充")
    print("  · 如果某个参数只要动一点 Jaccard 就崩，那么基于它的「胜率提升」不可信 ——")
    print("    那不是规则变好，是参数碰巧")
    return 0


# ==================== 信号稳定性 ====================

def run_stab(args):
    codes = universe(args.quick and 3 or None)
    print("标的 %d 只 · 检查点 = 倒数 %s 个交易日 · 已确认判定 = 检验点往前 %d 个交易日"
          % (len(codes), STAB_CHECKPOINTS, STAB_SETTLE_LAG))
    print("问的是：**已经确认过的历史信号，在加入新 K 线后会不会被改写/消失**")
    print()

    tot_settled = tot_lost = tot_new = 0
    rows = []
    for c in codes:
        q = qfq_bars(c)
        n = len(q)
        if n < max(STAB_CHECKPOINTS) + STAB_SETTLE_LAG + 30:
            rows.append((c, "K 线不足", 0, 0, 0, "跳过"))
            continue
        final = signals_on(q, c)
        for back in STAB_CHECKPOINTS:
            t = n - back
            settle_date = str(q["date"].iloc[t - STAB_SETTLE_LAG].date())
            prefix = q.iloc[:t].reset_index(drop=True)
            early = {s for s in signals_on(prefix, c) if s[0] <= settle_date}
            late = {s for s in final if s[0] <= settle_date}
            lost = early - late
            new = late - early
            tot_settled += len(early)
            tot_lost += len(lost)
            tot_new += len(new)
            if not lost and not new:
                verdict = "✓ 稳定"
            else:
                verdict = "⚠ 被改写"
            rows.append((c, str(q["date"].iloc[t].date()), len(early), len(lost), len(new), verdict))

    print("  %-8s %-12s %-10s %-6s %-6s %s"
          % ("代码", "检查点", "已确认信号", "丢失", "新增", "结论"))
    for c, chk, nset, lost, new, verdict in rows:
        print("  %-8s %-12s %-10s %-6s %-6s %s" % (c, chk, nset, lost, new, verdict))

    print()
    print("=" * 100)
    print("汇总：共检查 %d 条「已确认」历史信号，其中 **%d 条被改写消失**、%d 条新出现。"
          % (tot_settled, tot_lost, tot_new))
    if tot_settled == 0:
        print("  没有可比对的信号，结论无意义。")
    elif tot_lost == 0 and tot_new == 0:
        print("  ✓ 历史信号完全稳定：K 线增长不会改写已确认的记录，可以放心增量使用。")
    else:
        rate = 100.0 * tot_lost / tot_settled
        print("  ⚠ 有 %.1f%% 的已确认信号在后续计算中消失 —— 这意味着**回测里统计到的信号，"
              % rate)
        print("    有一部分在实盘当时并不存在**。任何基于这些信号的胜率都被高估了。")
        print("    这也是「缠绕论实现」最容易出问题的地方，值得单独查清是哪一类信号被改写。")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["sens", "stab", "all"])
    ap.add_argument("--window", type=int, default=5)
    ap.add_argument("--quick", action="store_true", help="只取 3 只股票")
    args = ap.parse_args()

    if args.mode in ("sens", "all"):
        print("=" * 100)
        print("参数敏感性")
        print("=" * 100)
        rc = run_sens(args)
        if rc:
            return rc
    if args.mode in ("stab", "all"):
        print()
        print("=" * 100)
        print("信号稳定性")
        print("=" * 100)
        run_stab(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
