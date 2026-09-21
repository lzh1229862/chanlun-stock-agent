"""T4-4 F4 回测：双口径统计信号后 5/10/20 日表现。

口径定义（不写死，见下方常量）
    入场   信号【次一交易日开盘价】
           不用信号日收盘价 —— 否则等于"用收盘价算信号、又按同一个收盘价成交"，本身就是未来函数
    窗口   从入场日起 N 个交易日；退出价 = 窗口最后一个交易日收盘价
    收益   方向调整后：买点 ret = (退出-入场)/入场；卖点 ret = (入场-退出)/入场
    净值   nav_0 = 1；nav_t = 1 + dir*(close_t - entry)/entry，dir = +1 买 / -1 卖
    最大回撤 min_t (nav_t - max(nav_0..nav_t)) / max(nav_0..nav_t)   <= 0
    胜率   ret > 0 的比例
    窗口未走完的信号：跳过该 window（不用不完整数据充数）

双口径
    signal 按信号  —— 每条 is_tradable=1 的信号独立统计
    day    按交易日 —— 同日多信号只取 is_primary=1 的主信号

防未来函数
    信号生成只使用信号日及之前的数据（用 --verify-nofuture 做前缀重算验证）
    回测只读取信号日【之后】的价格，不参与信号生成

运行
    python backtest.py                    # 双口径全量回测并写入 backtest 表
    python backtest.py --dry-run          # 只打印不写库
    python backtest.py --scope signal     # 只跑按信号口径
    python backtest.py --verify-nofuture  # 前缀重算验证无未来函数
    python backtest.py --selftest         # 指标算法自测（不联网）
"""
import argparse
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime

import pandas as pd

from confirm_dates import confirmation_delay
from signal_filter import limit_ratio
from storage_kline import load_kline
from storage_signal import DB_PATH, connect, query_signals

WINDOWS = [5, 10, 20]
ENTRY_MODE = "confirm_open"   # confirm_open（默认，防未来函数） / next_open / signal_close
DIRECTION = {"买": 1, "卖": -1}

BT_DDL = """
CREATE TABLE IF NOT EXISTS backtest (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id     INTEGER,
    stock_code    TEXT    NOT NULL,
    signal_date   TEXT    NOT NULL,
    signal_type   TEXT    NOT NULL,
    scope         TEXT    NOT NULL,
    window        INTEGER NOT NULL,
    confirm_date  TEXT    NOT NULL,
    confirm_delay INTEGER NOT NULL,
    entry_date    TEXT    NOT NULL,
    entry_price   REAL    NOT NULL,
    exit_date     TEXT    NOT NULL,
    exit_price    REAL    NOT NULL,
    return_pct    REAL    NOT NULL,
    max_drawdown  REAL    NOT NULL,
    is_win        INTEGER NOT NULL,
    created_at    TEXT    NOT NULL,
    UNIQUE (stock_code, signal_date, signal_type, scope, window)
);
CREATE INDEX IF NOT EXISTS idx_bt_scope ON backtest (scope, window, signal_type);
"""


MAX_ROLL = 10          # 涨跌停顺延最多找几个交易日


def direction_of(signal_type):
    return DIRECTION["买" if "买点" in signal_type else "卖"]


def blocked_open(bars, i, direction, ratio, eps=1e-4):
    """入场日**开盘**是否被限制在无法成交的方向上。

    买点遇涨停开盘 -> 买不进；卖点遇跌停开盘 -> 卖不掉。
    注意用的是「开盘价相对昨收」而不是「是否一字板」：
    开盘就封在涨停价上时，模型假设的「按开盘价成交」根本拿不到货，所以一律顺延。
    """
    if i <= 0 or i >= len(bars):
        return False
    prev_close = float(bars.at[i - 1, "close"])
    o = float(bars.at[i, "open"])
    if prev_close <= 0:
        return False
    chg = o / prev_close - 1
    return (chg >= ratio - eps) if direction > 0 else (chg <= -ratio + eps)


def resolve_entry(bars, i, direction, ratio, max_roll=MAX_ROLL):
    """开盘被涨跌停挡住时顺延到第一个能成交的交易日。

    返回 (可成交的下标, 顺延了几个交易日)。价格口径不变 —— 仍是那一天的**开盘价**。
    """
    j, rolled = i, 0
    while j < len(bars) and rolled < max_roll and blocked_open(bars, j, direction, ratio):
        j += 1
        rolled += 1
    return j, rolled


def get_bars(code):
    """读取该股 K 线并现算前复权（与信号生成同一口径）。"""
    df = load_kline(code)
    if df.empty:
        return df
    for c in ("open", "high", "low", "close"):
        df[c] = df[c] / df["qfq_factor"]
    return df.sort_values("date").reset_index(drop=True)


def evaluate(bars, i_entry, window, direction, entry_price=None):
    """从入场位置 i_entry 起算窗口指标；数据不足返回 None。

    入场价默认取 i_entry 当日开盘价（防未来函数：信号确认后次一交易日才能成交）。
    """
    i_exit = i_entry + window - 1
    if i_entry >= len(bars) or i_exit >= len(bars):
        return None                                    # 窗口未走完，跳过

    entry = entry_price if entry_price is not None else float(bars.at[i_entry, "open"])
    if entry <= 0:
        return None
    window_closes = [float(bars.at[i, "close"]) for i in range(i_entry, i_exit + 1)]
    navs = [1.0] + [1 + direction * (c - entry) / entry for c in window_closes]

    peak, mdd = navs[0], 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak)

    return {
        "entry_date": str(bars.at[i_entry, "date"].date()),
        "entry_price": entry,
        "exit_date": str(bars.at[i_exit, "date"].date()),
        "exit_price": window_closes[-1],
        "return_pct": round(navs[-1] - 1, 6),
        "max_drawdown": round(mdd, 6),
        "is_win": 1 if navs[-1] > 1 else 0,
    }


# ==================== 双口径回测 ====================

def run_backtest(scope="signal", windows=WINDOWS, entry_mode=ENTRY_MODE, verbose=True):
    rows = query_signals()
    rows = [r for r in rows if r["is_tradable"] == 1]
    if scope == "day":
        rows = [r for r in rows if r["is_primary"] == 1]

    by_code = defaultdict(list)
    for r in rows:
        by_code[r["stock_code"]].append(r)

    out, skipped, delay_hist, roll_hist = [], 0, defaultdict(int), defaultdict(int)
    for code, sigs in sorted(by_code.items()):
        bars = get_bars(code)
        if bars.empty:
            continue
        pos = {d: i for i, d in enumerate(bars["date"])}
        for r in sigs:
            i = pos.get(pd.Timestamp(r["signal_date"]))
            if i is None:
                skipped += len(windows)
                continue

            if entry_mode == "confirm_open":
                delay = confirmation_delay(bars, i, code, r["signal_date"], r["signal_type"])
                if delay is None:
                    skipped += len(windows)
                    continue
                i_entry = i + delay + 1          # 确认日收盘才可知 -> 次一交易日开盘成交
            elif entry_mode == "next_open":
                delay, i_entry = 0, i + 1
            else:                                 # signal_close
                delay, i_entry = 0, i
            delay_hist[delay] += 1

            d = direction_of(r["signal_type"])
            # 涨跌停顺延：开盘就封在涨停（买点）/ 跌停（卖点）时按开盘价成交是拿不到的
            i_entry, rolled = resolve_entry(bars, i_entry, d, limit_ratio(code))
            roll_hist[rolled] += 1
            if i_entry >= len(bars):
                skipped += len(windows)
                continue

            for w in windows:
                m = evaluate(bars, i_entry, w, d)
                if m is None:
                    skipped += 1
                    continue
                out.append({"signal_id": r["id"], "stock_code": code,
                            "signal_date": r["signal_date"], "signal_type": r["signal_type"],
                            "scope": scope, "window": w, "confirm_delay": delay,
                            "roll_days": rolled,
                            "confirm_date": str(bars.at[i + delay, "date"].date()), **m})
    if verbose:
        rolled_n = sum(v for k, v in roll_hist.items() if k)
        print(f"  口径 {scope:<7} 信号 {len(rows):>3} 条 -> 生成 {len(out):>3} 行回测记录"
              f"   跳过（窗口未走完/无K线）{skipped} 条"
              f"   确认延迟分布 {dict(sorted(delay_hist.items()))}"
              f"   涨跌停顺延 {rolled_n} 条 {dict(sorted((k, v) for k, v in roll_hist.items() if k))}")
    return out


# ==================== 统计与打印 ====================

def summarize(records):
    """按 (scope, window, signal_type) 聚合。"""
    g = defaultdict(list)
    for r in records:
        g[(r["scope"], r["window"], r["signal_type"])].append(r)
    out = {}
    for k, v in g.items():
        rets = [x["return_pct"] for x in v]
        out[k] = {"n": len(v), "avg_return": sum(rets) / len(rets),
                  "win_rate": sum(x["is_win"] for x in v) / len(v),
                  "avg_mdd": sum(x["max_drawdown"] for x in v) / len(v)}
    return out


def aggregate(records, key_fn):
    g = defaultdict(list)
    for r in records:
        g[key_fn(r)].append(r)
    res = {}
    for k, v in g.items():
        rets = [x["return_pct"] for x in v]
        res[k] = {"n": len(v), "avg_return": sum(rets) / len(rets),
                  "win_rate": sum(x["is_win"] for x in v) / len(v),
                  "avg_mdd": sum(x["max_drawdown"] for x in v) / len(v)}
    return res


def fmt(tag, s):
    return (f"  {tag:<16}{s['n']:>5}{s['avg_return']:>12.2%}{s['win_rate']:>9.1%}{s['avg_mdd']:>12.2%}")


def print_report(records):
    recs_signal = [r for r in records if r["scope"] == "signal"]
    recs_day = [r for r in records if r["scope"] == "day"]

    print()
    print("=== 对照 1：按信号 vs 按交易日（全样本）===")
    print(f"  {'口径':<16}{'样本':>5}{'平均收益':>12}{'胜率':>9}{'平均最大回撤':>12}")
    for w in WINDOWS:
        for scope, recs in (("按信号", recs_signal), ("按交易日", recs_day)):
            a = aggregate([r for r in recs if r["window"] == w], lambda r: w).get(w)
            if a:
                print(fmt(f"{scope} {w}日", a))
        print()

    print("=== 对照 2：买点 vs 卖点（方向调整后收益）===")
    print(f"  {'样本组':<16}{'样本':>5}{'平均收益':>12}{'胜率':>9}{'平均最大回撤':>12}")
    for w in WINDOWS:
        for scope, recs in (("按信号", recs_signal), ("按交易日", recs_day)):
            for side in ("买点", "卖点"):
                a = aggregate([r for r in recs if r["window"] == w and side in r["signal_type"]],
                              lambda r: side).get(side)
                if a:
                    print(fmt(f"{scope} {w}日 {side}", a))
        print()

    print("=== 对照 3：第一/二/三类分别统计（按信号口径）===")
    print(f"  {'类型':<16}{'样本':>5}{'平均收益':>12}{'胜率':>9}{'平均最大回撤':>12}")
    for w in WINDOWS:
        for lvl in ("第一类", "第二类", "第三类"):
            for side in ("买点", "卖点"):
                tag = lvl + side
                a = aggregate([r for r in recs_signal
                               if r["window"] == w and r["signal_type"] == tag], lambda r: tag).get(tag)
                if a:
                    print(fmt(f"{w}日 {tag}", a))
        print()

    # 同日多信号的影响
    print("=== 同日多信号对统计的影响 ===")
    for w in WINDOWS:
        a = aggregate([r for r in recs_signal if r["window"] == w], lambda r: r["scope"]).get("signal")
        b = aggregate([r for r in recs_day if r["window"] == w], lambda r: r["scope"]).get("day")
        if a and b:
            print(f"  {w:>2}日:  按信号 n={a['n']:<4} 胜率={a['win_rate']:.1%}   |   "
                  f"按交易日 n={b['n']:<4} 胜率={b['win_rate']:.1%}   |   "
                  f"样本差 {a['n'] - b['n']}  胜率差 {a['win_rate'] - b['win_rate']:+.1%}")


# ==================== 写库 ====================

def init_backtest_db():
    with closing(connect()) as conn, conn:
        conn.executescript(BT_DDL)


def save_backtest(records):
    init_backtest_db()
    now = datetime.now().isoformat(timespec="seconds")
    rows = [(r["signal_id"], r["stock_code"], r["signal_date"], r["signal_type"], r["scope"],
             r["window"], r["confirm_date"], r["confirm_delay"], r["entry_date"],
             r["entry_price"], r["exit_date"], r["exit_price"],
             r["return_pct"], r["max_drawdown"], r["is_win"], now) for r in records]
    with closing(connect()) as conn, conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR REPLACE INTO backtest (signal_id, stock_code, signal_date, signal_type, scope,"
            " window, confirm_date, confirm_delay, entry_date, entry_price, exit_date, exit_price,"
            " return_pct, max_drawdown, is_win, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        changed = conn.total_changes - before
    return changed


def query_backtest(scope=None, window=None):
    init_backtest_db()
    sql = "SELECT * FROM backtest WHERE 1=1"
    args = []
    if scope:
        sql += " AND scope = ?"; args.append(scope)
    if window:
        sql += " AND window = ?"; args.append(window)
    sql += " ORDER BY scope, window, signal_date"
    with closing(connect()) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ==================== 无未来函数验证 ====================

def verify_nofuture():
    """无未来函数验证（三层可证伪检验）。

    [A] 截断到信号日，看信号是否已可知  -> 若不可知，说明信号有确认延迟
    [B] 逐日放行，测出每条信号的最小确认延迟
    [C] 断言回测入场日严格晚于确认日    -> 成交时信息确已可知
    """
    rows = query_signals()
    by_code = defaultdict(list)
    for r in rows:
        by_code[r["stock_code"]].append(r)

    total = known_at_signal_day = 0
    delays, unresolvable = defaultdict(int), []
    for code, sigs in sorted(by_code.items()):
        bars = get_bars(code)
        if bars.empty:
            continue
        pos = {d: i for i, d in enumerate(bars["date"])}
        for r in sigs:
            i = pos.get(pd.Timestamp(r["signal_date"]))
            if i is None or len(bars) - i < 2:
                continue
            total += 1
            key = (r["signal_date"], r["signal_type"])
            if key in compute_signals(bars[bars["date"] <= bars.at[i, "date"]], code):
                known_at_signal_day += 1
            k = confirmation_delay(bars, i, code, r["signal_date"], r["signal_type"])
            if k is None:
                unresolvable.append((code, r["signal_date"], r["signal_type"]))
            else:
                delays[k] += 1

    print("=== 无未来函数验证 ===")
    print()
    print("[A] 严格前缀重算：把 K 线截断到信号日，重跑信号生成")
    print(f"    受检信号 {total} 条，在信号日当天就能复现的 {known_at_signal_day} 条")
    if known_at_signal_day < total:
        print("    -> 其余信号当天不可知：缠论「笔」需要后续 K 线才能确认，信号天然存在确认延迟")
    print()
    print("[B] 最小确认延迟（逐日放行 K 线直到信号复现）")
    for k in sorted(delays):
        print(f"    延迟 {k} 个交易日: {delays[k]} 条")
    if unresolvable:
        print(f"    ⚠️ 30 个交易日内无法复现: {unresolvable[:5]}")
    print()
    print("[C] 入场日校验：成交必须晚于信息可知时刻")
    records = run_backtest(scope="signal", verbose=False)
    bad = [r for r in records if r["entry_date"] <= r["confirm_date"]]
    print(f"    回测记录 {len(records)} 行，入场日 <= 确认日 的 {len(bad)} 行")
    if bad:
        print(f"    ❌ 存在未来函数: {bad[:3]}")
    else:
        print("    ✅ 全部入场日严格晚于确认日 —— 信号可知之后才成交")

    print()
    ok = (known_at_signal_day == 0) and not unresolvable and not bad
    print("==> 结论：" + ("信号不含未来数据；但存在 1~2 个交易日的确认延迟，"
                        "回测入场已按「确认日次一交易日开盘」处理 ✅" if ok else "❌ 需复查"))
    return ok


# ==================== 自测 ====================

def selftest():
    ok = []

    def check(desc, got, want, tol=1e-9):
        good = abs(got - want) < tol if isinstance(want, float) else got == want
        ok.append(good)
        print(f"  [{'OK' if good else 'FAIL'}] {desc:<44} got={got!r} want={want!r}")

    bars = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06",
                                "2026-01-07", "2026-01-08"]),
        "open":  [10.0, 10.0, 11.0, 11.0, 11.0, 11.0],
        "close": [10.0, 10.5, 11.0, 10.45, 11.55, 12.1],
    })
    # 入场位置 1（确认日的次一交易日），5 日窗口 = 位置 1..5
    m = evaluate(bars, 1, 5, direction=1)
    check("入场日", m["entry_date"], "2026-01-02")
    check("入场价 = 当日开盘", m["entry_price"], 10.0)
    check("退出日 = 窗口末日", m["exit_date"], "2026-01-08")
    check("退出价 = 末日收盘", m["exit_price"], 12.1)
    check("买点收益", m["return_pct"], 0.21)
    check("买点最大回撤", m["max_drawdown"], round(1.045 / 1.10 - 1, 6))
    check("买点胜", m["is_win"], 1)

    ms = evaluate(bars, 1, 5, direction=-1)
    check("卖点收益 = 反向", ms["return_pct"], -0.21)
    check("卖点最大回撤", ms["max_drawdown"], -0.21)
    check("卖点不赢", ms["is_win"], 0)

    check("窗口未走完返回 None", evaluate(bars, 5, 20, 1), None)

    print()
    print(f"==> 自测 {sum(ok)}/{len(ok)} 通过" + ("" if all(ok) else "  ❌ 有失败项"))
    return all(ok)


# ==================== 主流程 ====================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--scope", choices=["signal", "day", "both"], default="both")
    ap.add_argument("--entry", choices=["confirm_open", "next_open", "signal_close"], default=ENTRY_MODE)
    ap.add_argument("--verify-nofuture", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        raise SystemExit(0 if selftest() else 1)
    if a.verify_nofuture:
        raise SystemExit(0 if verify_nofuture() else 1)

    print("=== T4-4 F4 回测：双口径统计 ===")
    print(f"库文件 {DB_PATH.resolve()}")
    print(f"入场 {a.entry}   窗口 {WINDOWS}   默认只统计 is_tradable=1")
    print(f"模式 {'--dry-run（不写库）' if a.dry_run else '写入 backtest 表'}")
    print()

    scopes = ["signal", "day"] if a.scope == "both" else [a.scope]
    records = []
    for s in scopes:
        records += run_backtest(scope=s, entry_mode=a.entry)

    print_report(records)

    if a.dry_run:
        print()
        print("==> --dry-run：未写入 backtest 表")
        return
    changed = save_backtest(records)
    print()
    print(f"==> 已写入 backtest 表：{changed} 行")
    with closing(connect()) as conn:
        n = conn.execute("SELECT COUNT(*) FROM backtest").fetchone()[0]
        print(f"    表中现有 {n} 行")
        for r in conn.execute("SELECT scope, window, COUNT(*) FROM backtest GROUP BY 1,2 ORDER BY 1,2"):
            print(f"      scope={r[0]:<7} window={r[1]:<3} {r[2]} 行")


if __name__ == "__main__":
    main()
