"""verify_edge.py —— 信号有效性对照：基准 / 成本 / 置信区间 / 分布（T10-1）

回答一个之前一直没回答的问题：
    **信号之后的收益，比「同一只股票随便哪天入场」到底好多少？**

为什么必须先做这一步
    实测发现 12 只样本股的 5 日**无条件**平均收益本身就是 +0.12% ~ +1.49%。
    也就是说「信号后平均 +0.96%」里有一大半是市场给的（beta），不是缠论规则给的。
    不先扣掉这个基准，任何「胜率提升」都可能是行情变了，而不是规则变好了。

输出四件事
    1. 超额收益   信号均值 − 同股票同期无条件基准均值
    2. 扣费净值   扣掉佣金 / 印花税 / 过户费 / 滑点之后还剩多少
    3. 置信区间   均值用 t 区间、胜率用 Wilson 区间；样本不足直接告警
    4. 收益分布   盈亏比 / 最大连续亏损 / 分位数

**不改动任何分析逻辑**，只读 backtest 表 + 本地 Parquet。
入场口径复用 backtest.evaluate，与信号回测完全一致（确认日次一交易日开盘）。

运行
    python verify_edge.py                      # 默认 signal 口径、5/10/20 日
    python verify_edge.py --scope day
    python verify_edge.py --window 5
    python verify_edge.py --zero-cost          # 不计任何交易成本
    python verify_edge.py --slippage 0.001     # 单边滑点 0.1%
"""
import argparse
import sys
from statistics import mean, stdev

import backtest as bt
import storage_signal as ss

# 往返成本假设（单边）
DEFAULTS = dict(commission=0.00025,   # 券商佣金 万 2.5
                stamp=0.0005,         # 印花税 千 0.5（仅卖出）
                transfer=0.00001,     # 过户费 十万分之 1（单边）
                slippage=0.0005)      # 滑点 万 5（单边）

# 双侧 95% t 临界值（df 1~30，超过用正态 1.96）
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
       8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
       15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080,
       22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048,
       29: 2.045, 30: 2.042}


def t_crit(df):
    return T95.get(df, 1.96) if df <= 30 else 1.96


def mean_ci(vals):
    """均值 + 双侧 95% 区间。n < 2 返回 None。"""
    n = len(vals)
    if n < 2:
        return (mean(vals), None, None) if n == 1 else (None, None, None)
    m = mean(vals)
    half = t_crit(n - 1) * stdev(vals) / (n ** 0.5)
    return m, m - half, m + half


def wilson(k, n, z=1.96):
    """胜率的 Wilson 区间 —— 小样本比正态近似靠谱得多。"""
    if n == 0:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return (c - s) / d, (c + s) / d


def profit_factor(vals):
    """盈亏比 = 盈利总和 / 亏损总和。无亏损返回 inf。"""
    up = sum(v for v in vals if v > 0)
    dn = -sum(v for v in vals if v < 0)
    if dn == 0:
        return float("inf") if up > 0 else 0.0
    return up / dn


def max_losing_streak(dates_and_wins):
    """按入场日排序后的最大连续亏损笔数（忽略持仓重叠，仅作参考）。"""
    best = cur = 0
    for _, w in sorted(dates_and_wins):
        cur = cur + 1 if not w else 0
        best = max(best, cur)
    return best


def quantile(vals, q):
    s = sorted(vals)
    if not s:
        return None
    i = (len(s) - 1) * q
    lo, hi = int(i), min(int(i) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


_ORDER = {"第一类买点": 0, "第一类卖点": 1, "第二类买点": 2,
          "第二类卖点": 3, "第三类买点": 4, "第三类卖点": 5}


def type_order(t):
    return (_ORDER.get(t, 99), t)


def all_types(rows):
    return sorted({r["signal_type"] for r in rows}, key=type_order)


_BASE_CACHE = {}


def unconditional(code, window, lo_date, hi_date):
    """该股在「信号入场日跨度内」的无条件 N 日多头收益（与信号同一入场口径）。

    返回 (均值, 胜率, 样本数)。做空基准 = 取负，胜率 = 1 - 多头胜率。
    """
    key = (code, window, lo_date, hi_date)
    if key in _BASE_CACHE:
        return _BASE_CACHE[key]
    bars = bt.get_bars(code)
    rets = []
    for i in range(len(bars)):
        r = bt.evaluate(bars, i, window, 1)
        if r and lo_date <= r["entry_date"] <= hi_date:
            rets.append(r["return_pct"])
    out = (mean(rets), sum(1 for v in rets if v > 0) / len(rets), len(rets)) if rets else (None, None, 0)
    _BASE_CACHE[key] = out
    return out


def load_rows(scope):
    conn = ss.connect()
    try:
        cur = conn.execute(
            "SELECT stock_code, signal_type, entry_date, window, return_pct, is_win "
            "FROM backtest WHERE scope=? ORDER BY entry_date", (scope,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def cost_per_round_trip(a):
    return a.commission * 2 + a.stamp + a.transfer * 2 + a.slippage * 2


def pct(x, nd=2):
    return "—" if x is None else "%+.*f%%" % (nd, 100 * x)


def pc0(x):
    return "—" if x is None else "%.1f%%" % (100 * x)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["signal", "day"], default="signal")
    ap.add_argument("--window", type=int, default=None, help="只看某个窗口")
    ap.add_argument("--min-n", type=int, default=30, help="样本量告警阈值")
    ap.add_argument("--zero-cost", action="store_true", help="不计任何交易成本")
    for k in DEFAULTS:
        ap.add_argument("--" + k, type=float, default=None)
    a = ap.parse_args()
    if a.zero_cost:
        for k in DEFAULTS:
            setattr(a, k, 0.0)
    else:
        for k in DEFAULTS:
            if getattr(a, k) is None:
                setattr(a, k, DEFAULTS[k])

    rows = load_rows(a.scope)
    if not rows:
        print("backtest 表里没有 %s 口径的数据。先跑 python backtest.py" % a.scope)
        return 1

    windows = [a.window] if a.window else bt.WINDOWS
    cost = cost_per_round_trip(a)

    print("=" * 108)
    print("信号有效性对照   口径=%s   窗口=%s" % (a.scope, "/".join(str(w) for w in windows)))
    print("成本假设（往返）= 佣金 %.4f%%x2 + 印花税 %.3f%% + 过户费 %.4f%%x2 + 滑点 %.3f%%x2 = %.3f%%"
          % (100 * a.commission, 100 * a.stamp, 100 * a.transfer, 100 * a.slippage, 100 * cost))
    print("基准 = 同一只股票、同一入场口径、在信号入场日跨度内**每一个交易日**都入场")
    print("信号入场日跨度 = %s ~ %s"
          % (min(r["entry_date"] for r in rows), max(r["entry_date"] for r in rows)))
    print("=" * 108)

    print()
    print("【表 A】超额收益：信号 vs 无条件基准")
    print("  %-11s %-4s %-5s %-10s %-10s %-10s %-21s %-10s %s"
          % ("类型", "窗口", "样本", "信号均值", "基准均值", "超额", "超额 95%CI", "扣费净值", "判断"))
    flags = []
    for w in windows:
        for t in all_types(rows):
            sub = [r for r in rows if r["window"] == w and r["signal_type"] == t]
            if not sub:
                continue
            d = bt.direction_of(t)
            rets = [r["return_pct"] for r in sub]
            m, lo, hi = mean_ci(rets)
            codes = {r["stock_code"] for r in sub}
            bases = []
            for c in codes:
                ds = [r["entry_date"] for r in sub if r["stock_code"] == c]
                b = unconditional(c, w, min(ds), max(ds))
                if b[0] is not None:
                    bases.append((b[0], len(ds)))
            # 基准按该股信号条数加权；买点用多头基准，卖点取负
            base = d * sum(v * n for v, n in bases) / sum(n for _, n in bases)
            # 超额区间：把基准当常数，平移信号均值的区间即可
            exc, exc_lo, exc_hi = m - base, lo - base, hi - base
            net = m - cost
            if len(sub) < a.min_n:
                judge = "⚠ 样本不足"
            elif exc_lo <= 0 <= exc_hi:
                judge = "超额不显著"
            elif net <= 0:
                judge = "扣费后为负"
            else:
                judge = "✓ 正超额"
            if judge != "✓ 正超额":
                flags.append((t, w, judge))
            print("  %-11s %-4s %-5d %-10s %-10s %-10s [%s, %s] %-10s %s"
                  % (t, w, len(sub), pct(m), pct(base), pct(exc),
                     pct(exc_lo, 2), pct(exc_hi, 2), pct(net), judge))

    print()
    print("【表 B】信号质量")
    print("  %-11s %-4s %-5s %-19s %-10s %-10s %-8s %-8s %-9s"
          % ("类型", "窗口", "样本", "胜率 95%CI", "基准胜率", "超额胜率", "盈亏比", "最大连亏", "最差 5%"))
    for w in windows:
        for t in all_types(rows):
            sub = [r for r in rows if r["window"] == w and r["signal_type"] == t]
            if not sub:
                continue
            d = bt.direction_of(t)
            rets = [r["return_pct"] for r in sub]
            k = sum(1 for v in rets if v > 0)
            plo, phi = wilson(k, len(sub))
            bases = []
            for c in {r["stock_code"] for r in sub}:
                ds = [r["entry_date"] for r in sub if r["stock_code"] == c]
                b = unconditional(c, w, min(ds), max(ds))
                if b[1] is not None:
                    bases.append((b[1], len(ds)))
            bwr = sum(v * n for v, n in bases) / sum(n for _, n in bases)
            bwr = bwr if d > 0 else 1 - bwr
            print("  %-11s %-4s %-5d [%s, %s] %-10s %-10s %-8.2f %-8d %-9s"
                  % (t, w, len(sub), pc0(plo), pc0(phi), pc0(bwr), pc0(k / len(sub) - bwr),
                     profit_factor(rets),
                     max_losing_streak([(r["entry_date"], r["is_win"]) for r in sub]),
                     pct(quantile(rets, 0.05))))

    print()
    print("【汇总】买点 / 卖点分开看（把窗口混在一起会互相抵消）")
    print("  %-5s %-4s %-5s %-10s %-21s %-10s %-10s %s"
          % ("方向", "窗口", "样本", "均值", "均值 95%CI", "基准", "超额", "扣费后"))
    for w in windows:
        for label, want, d in (("买点", "买", 1), ("卖点", "卖", -1)):
            sub = [r for r in rows if r["window"] == w and want in r["signal_type"]]
            if not sub:
                continue
            rets = [r["return_pct"] for r in sub]
            m, lo, hi = mean_ci(rets)
            bases = []
            for c in {r["stock_code"] for r in sub}:
                ds = [r["entry_date"] for r in sub if r["stock_code"] == c]
                b = unconditional(c, w, min(ds), max(ds))
                if b[0] is not None:
                    bases.append((b[0], len(ds)))
            base = d * sum(v * n for v, n in bases) / sum(n for _, n in bases)
            print("  %-5s %-4s %-5d %-10s [%s, %s] %-10s %-10s %s"
                  % (label, w, len(sub), pct(m), pct(lo), pct(hi), pct(base),
                     pct(m - base), pct(m - cost)))

    print()
    print("=" * 108)
    print("需要留意（%d 项）：" % len(flags))
    for t, w, j in flags:
        print("  %-11s %2d日  ->  %s" % (t, w, j))
    print()
    print("怎么看这些数字")
    print("  · 「信号均值」为正不代表规则有效 —— 要看「超额」（扣掉同股票随机入场的部分）")
    print("  · 超额 95%CI 跨 0 = 统计上分不清是规则还是运气，样本量不足时尤其如此")
    print("  · 「扣费净值」才是真实到手；5 日窗口的往返成本约 0.2%，会吃掉大部分小额超额")
    print("  · 这一版**没有做市场状态分层**，样本期若整体上行，买点会系统性好看")
    print("  · 非投资建议；样本单一、无滑点建模校验，不能当作策略有效性证据")
    return 0


if __name__ == "__main__":
    sys.exit(main())
