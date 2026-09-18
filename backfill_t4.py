"""T4-2 回填历史信号的 is_tradable（F3 过滤）。

逻辑
    读 signals 表全部历史信号 -> 按股票分组 -> 用 T4-1 的过滤逻辑重算
    -> UPDATE is_tradable 与 filter_version（不新增行、不改去重键、不重算缠论）

filter_version 编码（复用该列记录命中的规则组合，不改表结构）
    v1_f3               通过全部规则，可交易
    v1_f3:st            ST / *ST
    v1_f3:delisted      退市整理
    v1_f3:new           次新股（上市不足 60 个交易日）
    v1_f3:limitup       买点当日涨停，买不进
    v1_f3:limitdown     卖点当日跌停，卖不出
    多规则命中按上述顺序用 + 连接，如 v1_f3:st+new

运行
    python backfill_t4.py --dry-run         # 只看影响，不写库
    python backfill_t4.py                   # 正式回填
    python backfill_t4.py --db data/x.db    # 指定库
"""
import argparse
from collections import Counter
from pathlib import Path

import storage_signal
from signal_filter import (MIN_LISTED_TRADING_DAYS, board_of, build_context,
                           filter_signals, limit_ratio)
from storage_kline import load_kline
from storage_signal import apply_filter, query_signals

BASE_VERSION = "v1_f3"

RULE_LEGEND = [
    ("st", "ST / *ST"),
    ("delisted", "退市整理"),
    ("new", f"次新股（上市不足 {MIN_LISTED_TRADING_DAYS} 个交易日）"),
    ("limitup", "买点当日涨停，买不进"),
    ("limitdown", "卖点当日跌停，卖不出"),
]
LEGEND = dict(RULE_LEGEND)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=None)
    a = ap.parse_args()

    if a.db:
        storage_signal.DB_PATH = Path(a.db)
    db = storage_signal.DB_PATH

    print("=== T4-2 回填历史信号 is_tradable ===")
    print(f"库文件 {db.resolve()}")
    print(f"模式   {'--dry-run（不写库）' if a.dry_run else '正式回填'}")
    print()

    rows = query_signals()
    if not rows:
        print("库中无信号，无需回填。")
        return

    before = Counter(r["is_tradable"] for r in rows)
    before_ver = Counter(r["filter_version"] for r in rows)

    by_code = {}
    for r in rows:
        by_code.setdefault(r["stock_code"], []).append(r)

    print(f"读取历史信号 {len(rows)} 条，涉及 {len(by_code)} 只股票")
    print()

    decisions, after_ok, skipped = {}, 0, []
    reason_hits, version_after = Counter(), Counter()

    for code, rs in sorted(by_code.items()):
        bars = load_kline(code)
        if bars.empty:
            skipped.append(code)
            after_ok += sum(r["is_tradable"] for r in rs)
            continue
        ctx = build_context(code)
        results = filter_signals(code, [{"date": r["signal_date"], "type": r["signal_type"]}
                                        for r in rs], bars, ctx)
        ds = []
        for r in results:
            v = BASE_VERSION if r["is_tradable"] else f"{BASE_VERSION}:{r['filter_codes']}"
            ds.append({"date": r["date"], "type": r["type"],
                       "is_tradable": r["is_tradable"], "filter_version": v})
            version_after[v] += 1
            if not r["is_tradable"]:
                for c in r["filter_codes"].split("+"):
                    reason_hits[c] += 1
        decisions[code] = ds
        n_ok = sum(d["is_tradable"] for d in ds)
        after_ok += n_ok
        print(f"  [{code}] K线 {len(bars):>4} 行   {ctx['name']:<8} {board_of(code):<8}"
              f"±{limit_ratio(code, ctx['is_st']):.0%}   ST={ctx['is_st']}   "
              f"上市 {ctx['listing_date'].date()}   信号 {len(ds)} 条 -> 可交易 {n_ok} 条")

    if skipped:
        print(f"  跳过（无本地 K 线，无法判定涨跌停）: {skipped}")
    print()

    n = len(rows)
    print("--- 汇总 ---")
    print(f"  总信号数        {n}")
    print(f"  过滤前可交易数  {before.get(1, 0)}")
    print(f"  过滤后可交易数  {after_ok}")
    print(f"  被过滤          {n - after_ok}")
    print()

    print("--- 被过滤原因分布 ---")
    if reason_hits:
        for code_key, desc in RULE_LEGEND:
            if reason_hits.get(code_key):
                print(f"  {code_key:<10}{reason_hits[code_key]:>4} 条   {desc}")
        multi = sum(1 for v, c in version_after.items() if v.count("+") > 0)
        if multi:
            print(f"  （其中多规则同时命中 {multi} 条）")
    else:
        print("  （无）")
    print()

    print("--- filter_version 变更 ---")
    for v, c in sorted(before_ver.items()):
        print(f"  过滤前  {v:<16} {c:>4} 条")
    for v, c in sorted(version_after.items()):
        print(f"  过滤后  {v:<16} {c:>4} 条")
    print()

    if a.dry_run:
        print("==> --dry-run：未写入任何数据")
        return

    changed = 0
    for code, ds in decisions.items():
        _, c = apply_filter(code, ds, BASE_VERSION)
        changed += c
    print(f"==> 已回填：{changed} 行被更新（{n} 条信号，未新增行）")
    ver = Counter(r["filter_version"] for r in query_signals())
    print(f"    复核 filter_version 分布: {dict(ver)}")


if __name__ == "__main__":
    main()
