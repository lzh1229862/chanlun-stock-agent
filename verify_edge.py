"""verify_edge.py —— 信号有效性对照：基准 / 成本 / 置信区间 / 分布 / 样本外（T10-1）

回答一个之前一直没回答的问题：
    **信号之后的收益，比「同一只股票随便哪天入场」到底好多少？**

为什么必须先做这一步
    实测发现 12 只样本股的 5 日**无条件**平均收益本身就是 +0.12% ~ +1.49%。
    也就是说「信号后平均 +0.96%」里有一大半是市场给的（beta），不是缠论规则给的。
    不先扣掉这个基准，任何「胜率提升」都可能是行情变了，而不是规则变好了。

五组输出
    表 A  超额收益   信号均值 − 同股票同期无条件基准均值
    表 B  信号质量   胜率 Wilson 区间 / 基准胜率 / 盈亏比 / 最大连亏 / 最差 5%
    表 C  样本外切分 按入场日切成「观察期 / 验证期」，两段结论是否一致   ← 过拟合时间维度的主检测
    表 D  时间分块   把样本期等分成 N 段，看超额在各 regme 下是否稳定
    汇总  买点 / 卖点分开 + 扣费净值

**不改动任何分析逻辑**，只读 backtest 表 + 本地 Parquet。
入场口径复用 backtest.evaluate，与信号回测完全一致（确认日次一交易日开盘）。

重要：所有基准都在**该子样本自己的时间跨度内**重算 —— 否则用牛市的基准去衡量熊市的信号，
      会得出完全错误的结论。

运行
    python verify_edge.py                      # 默认 signal 口径、5/10/20 日
    python verify_edge.py --split 0.6           # 追加样本外切分（前 60% 观察 / 后 40% 验证）
    python verify_edge.py --blocks 4            # 追加 4 段时间分块
    python verify_edge.py --scope day
    python verify_edge.py --window 5 --zero-cost
    python verify_edge.py --slippage 0.001
"""
import argparse
import sys
from datetime import datetime, timedelta
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

_ORDER = {"第一类买点": 0, "第一类卖点": 1, "第二类买点": 2,
          "第二类卖点": 3, "第三类买点": 4, "第三类卖点": 5}


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


def welch_diff_ci(a, b):
    """两组均值之差的 95% 区间（Welch）。

    分组对照必须看**差值**是否显著 —— 「配合组超额 +0.85%、不配合组 +0.40%」
    听起来配合更好，但两个区间各自都很宽时，差值区间大概率跨 0，那就什么也没证明。
    """
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return None, None, None
    m1, m2 = mean(a), mean(b)
    v1, v2 = stdev(a) ** 2, stdev(b) ** 2
    se2 = v1 / n1 + v2 / n2
    if se2 <= 0:
        return m1 - m2, m1 - m2, m1 - m2
    df = se2 * se2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
    d = m1 - m2
    half = t_crit(int(max(df, 1.0))) * se2 ** 0.5
    return d, d - half, d + half


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


def type_order(t):
    return (_ORDER.get(t, 99), t)


def all_types(rows):
    return sorted({r["signal_type"] for r in rows}, key=type_order)


# ==================== 样本切分（纯函数，便于测试）====================

def split_point(rows, frac):
    """按入场日切分：返回切分日期，使 frac 比例的**不同入场日**落在它之前。"""
    dates = sorted({r["entry_date"] for r in rows})
    if len(dates) < 2:
        return None
    return dates[min(int(len(dates) * frac), len(dates) - 2)]


def partition(rows, cut):
    """cut 之前的算观察期，之后的算验证期。"""
    return [r for r in rows if r["entry_date"] <= cut], [r for r in rows if r["entry_date"] > cut]


def assign_blocks(rows, n):
    """把入场日跨度等分成 n 段，返回 {行序号: 段号}（0 起）。段内按日历跨度均分。"""
    if n < 2:
        return {i: 0 for i in range(len(rows))}
    days = sorted({r["entry_date"] for r in rows})
    if len(days) < n:
        return {i: 0 for i in range(len(rows))}
    d0 = datetime.strptime(days[0], "%Y-%m-%d")
    d1 = datetime.strptime(days[-1], "%Y-%m-%d")
    span = (d1 - d0) / n
    out = {}
    for i, r in enumerate(rows):
        d = datetime.strptime(r["entry_date"], "%Y-%m-%d")
        k = int((d - d0) / span) if span.total_seconds() > 0 else 0
        out[i] = min(max(k, 0), n - 1)
    return out


# ==================== 基准 ====================

_BASE_CACHE = {}


def unconditional(code, window, lo_date, hi_date):
    """该股在给定日期跨度内的无条件 N 日多头收益（与信号同一入场口径）。

    返回 (均值, 胜率, 样本数)。做空基准 = 均值取负、胜率 = 1 - 多头胜率。
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


def stats_for(sub, window, cost):
    """一批同类型同窗口的回测行 -> 有效性指标。

    基准在**这一批自己的时间跨度内**按股票分别重算，再按信号条数加权。
    """
    if not sub:
        return None
    d = bt.direction_of(sub[0]["signal_type"])
    rets = [r["return_pct"] for r in sub]
    m, lo, hi = mean_ci(rets)
    bases = []
    for c in {r["stock_code"] for r in sub}:
        ds = [r["entry_date"] for r in sub if r["stock_code"] == c]
        b = unconditional(c, window, min(ds), max(ds))
        if b[0] is not None:
            bases.append((b[0], b[1], len(ds)))
    if not bases:
        return None
    tot = sum(n for _, _, n in bases)
    base = d * sum(v * n for v, _, n in bases) / tot
    base_wr = sum(v * n for _, v, n in bases) / tot
    if d < 0:
        base_wr = 1 - base_wr
    k = sum(1 for v in rets if v > 0)
    return {"n": len(sub), "dir": d, "mean": m, "lo": lo, "hi": hi,
            "base": base, "exc": m - base, "exc_lo": lo - base, "exc_hi": hi - base,
            "net": m - cost, "wr": k / len(sub), "wr_ci": wilson(k, len(sub)),
            "base_wr": base_wr, "pf": profit_factor(rets),
            "streak": max_losing_streak([(r["entry_date"], r["is_win"]) for r in sub]),
            "p05": quantile(rets, 0.05)}


def judge(s, cost, min_n):
    if s is None:
        return "—"
    if s["n"] < min_n:
        return "⚠ 样本不足"
    if s["exc_lo"] <= 0 <= s["exc_hi"]:
        return "超额不显著"
    if s["net"] <= 0:
        return "扣费后为负"
    return "✓ 正超额" if s["exc"] > 0 else "✗ 负超额"


# ==================== 输出 ====================

def pct(x, nd=2):
    return "—" if x is None else "%+.*f%%" % (nd, 100 * x)


def pc0(x):
    return "—" if x is None else "%.1f%%" % (100 * x)


def num2(x):
    return "—" if x is None else "%.2f" % x


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


def groups(rows, windows):
    """按 (窗口, 类型) 切片，保持稳定顺序。"""
    for w in windows:
        for t in all_types(rows):
            sub = [r for r in rows if r["window"] == w and r["signal_type"] == t]
            if sub:
                yield w, t, sub


def table_ab(rows, windows, cost, min_n):
    flags = []
    print()
    print("【表 A】超额收益：信号 vs 无条件基准")
    print("  %-11s %-4s %-5s %-10s %-10s %-10s %-20s %-10s %s"
          % ("类型", "窗口", "样本", "信号均值", "基准均值", "超额", "超额 95%CI", "扣费净值", "判断"))
    for w, t, sub in groups(rows, windows):
        s = stats_for(sub, w, cost)
        if s is None:
            continue
        j = judge(s, cost, min_n)
        if j != "✓ 正超额":
            flags.append((t, w, j))
        print("  %-11s %-4s %-5d %-10s %-10s %-10s [%s, %s] %-10s %s"
              % (t, w, s["n"], pct(s["mean"]), pct(s["base"]), pct(s["exc"]),
                 pct(s["exc_lo"]), pct(s["exc_hi"]), pct(s["net"]), j))

    print()
    print("【表 B】信号质量")
    print("  %-11s %-4s %-5s %-18s %-10s %-10s %-8s %-8s %-9s"
          % ("类型", "窗口", "样本", "胜率 95%CI", "基准胜率", "超额胜率", "盈亏比", "最大连亏", "最差 5%"))
    for w, t, sub in groups(rows, windows):
        s = stats_for(sub, w, cost)
        if s is None:
            continue
        print("  %-11s %-4s %-5d [%s, %s] %-10s %-10s %-8s %-8d %-9s"
              % (t, w, s["n"], pc0(s["wr_ci"][0]), pc0(s["wr_ci"][1]), pc0(s["base_wr"]),
                 pc0(s["wr"] - s["base_wr"]),
                 "inf" if s["pf"] == float("inf") else "%.2f" % s["pf"],
                 s["streak"], pct(s["p05"])))
    return flags


def table_split(rows, windows, cost, min_n, frac):
    """表 C：样本外切分 —— 过拟合『时间』的主检测。"""
    cut = split_point(rows, frac)
    if cut is None:
        print()
        print("【表 C】样本外切分：入场日不足，跳过。")
        return
    a_rows, b_rows = partition(rows, cut)
    print()
    print("【表 C】样本外切分（按入场日，前 %.0f%% 观察 / 后 %.0f%% 验证）"
          % (100 * frac, 100 * (1 - frac)))
    print("  切分点 = %s 之后为验证期；基准在各期**自己的时间跨度内**重算" % cut)
    print("  %-11s %-4s %-30s %-30s %s"
          % ("类型", "窗口", "观察期  n / 超额 / 95%CI", "验证期  n / 超额 / 95%CI", "一致性"))
    same, diff = 0, []
    for w, t in ((w, t) for w in windows for t in all_types(rows)):
        sa = stats_for([r for r in a_rows if r["window"] == w and r["signal_type"] == t], w, cost)
        sb = stats_for([r for r in b_rows if r["window"] == w and r["signal_type"] == t], w, cost)
        if sa is None or sb is None:
            continue
        if sa["exc"] * sb["exc"] < 0:
            verdict = "⚠ 方向相反"
            diff.append((t, w, verdict))
        elif sa["exc_lo"] > 0 or sa["exc_hi"] < 0:
            if sb["exc_lo"] > 0 or sb["exc_hi"] < 0:
                verdict = "两段一致显著"
                same += 1
            else:
                verdict = "验证期不显著"
        else:
            verdict = "两段都不显著"
        print("  %-11s %-4s %-30s %-30s %s"
              % (t, w,
                 "%d / %s / [%s, %s]" % (sa["n"], pct(sa["exc"]), pct(sa["exc_lo"]), pct(sa["exc_hi"])),
                 "%d / %s / [%s, %s]" % (sb["n"], pct(sb["exc"]), pct(sb["exc_lo"]), pct(sb["exc_hi"])),
                 verdict))
    print()
    print("  小结：两段一致显著 %d 个组合，方向相反 %d 个。" % (same, len(diff)))
    print("        「方向相反」= 这套规则在这两段行情里给出相反结论，说明它没有跨 regime 的稳定性。")


def table_blocks(rows, windows, cost, n):
    """表 D：把样本期等分成 n 段，看超额在各 regime 下是否稳定。"""
    idx = assign_blocks(rows, n)
    print()
    print("【表 D】时间分块（入场日跨度等分 %d 段）—— 结论跨 regime 稳不稳" % n)
    print("  %-5s %-4s %-5s %-5s %-10s %-10s %-10s %-10s"
          % ("方向", "窗口", "段", "样本", "均值", "基准", "超额", "扣费后"))
    for w in windows:
        for label, want, d in (("买点", "买", 1), ("卖点", "卖", -1)):
            for k in range(n):
                sub = [r for i, r in enumerate(rows)
                       if idx[i] == k and r["window"] == w and want in r["signal_type"]]
                if not sub:
                    continue
                s = stats_for(sub, w, cost)
                if s is None:
                    continue
                print("  %-5s %-4s %-5d %-5d %-10s %-10s %-10s %-10s"
                      % (label, w, k + 1, s["n"], pct(s["mean"]), pct(s["base"]),
                         pct(s["exc"]), pct(s["net"])))
        print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["signal", "day"], default="signal")
    ap.add_argument("--window", type=int, default=None, help="只看某个窗口")
    ap.add_argument("--min-n", type=int, default=30, help="样本量告警阈值")
    ap.add_argument("--split", type=float, default=None, metavar="FRAC",
                    help="样本外切分比例，如 0.6 = 前 60%% 观察 / 后 40%% 验证")
    ap.add_argument("--blocks", type=int, default=None, metavar="N",
                    help="把样本期等分成 N 段做 regime 稳健性检查")
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

    print("=" * 112)
    print("信号有效性对照   口径=%s   窗口=%s" % (a.scope, "/".join(str(w) for w in windows)))
    print("成本假设（往返）= 佣金 %.4f%%x2 + 印花税 %.3f%% + 过户费 %.4f%%x2 + 滑点 %.3f%%x2 = %.3f%%"
          % (100 * a.commission, 100 * a.stamp, 100 * a.transfer, 100 * a.slippage, 100 * cost))
    print("基准 = 同一只股票、同一入场口径、在信号入场日跨度内**每一个交易日**都入场")
    print("信号入场日跨度 = %s ~ %s"
          % (min(r["entry_date"] for r in rows), max(r["entry_date"] for r in rows)))
    print("=" * 112)

    flags = table_ab(rows, windows, cost, a.min_n)

    print()
    print("【汇总】买点 / 卖点分开看（把窗口混在一起会互相抵消）")
    print("  %-5s %-4s %-5s %-10s %-21s %-10s %-10s %s"
          % ("方向", "窗口", "样本", "均值", "均值 95%CI", "基准", "超额", "扣费后"))
    for w in windows:
        for label, want in (("买点", "买"), ("卖点", "卖")):
            sub = [r for r in rows if r["window"] == w and want in r["signal_type"]]
            s = stats_for(sub, w, cost)
            if s is None:
                continue
            print("  %-5s %-4s %-5d %-10s [%s, %s] %-10s %-10s %s"
                  % (label, w, s["n"], pct(s["mean"]), pct(s["lo"]), pct(s["hi"]),
                     pct(s["base"]), pct(s["exc"]), pct(s["net"])))

    if a.split:
        table_split(rows, windows, cost, a.min_n, a.split)
    if a.blocks:
        table_blocks(rows, windows, cost, a.blocks)

    print()
    print("=" * 112)
    print("需要留意（表 A 中非『正超额』的 %d 项）：" % len(flags))
    for t, w, j in flags:
        print("  %-11s %2d日  ->  %s" % (t, w, j))
    print()
    print("怎么看这些数字")
    print("  · 「信号均值」为正不代表规则有效 —— 要看「超额」（扣掉同股票随机入场的部分）")
    print("  · 超额 95%CI 跨 0 = 统计上分不清是规则还是运气，样本量不足时尤其如此")
    print("  · 「扣费净值」才是真实到手；5 日窗口的往返成本约 0.2%，会吃掉大部分小额超额")
    print("  · 表 C / 表 D 才回答「会不会只是这段行情碰巧」——只看表 A 容易自我说服")
    print("  · 仍未做「point-in-time 股票池」，随机抽现有股票也消除不了幸存者偏差")
    print("  · 非投资建议；不能当作策略有效性证据")
    return 0


if __name__ == "__main__":
    sys.exit(main())
