"""T4-3 同日多信号优先级：为每个 (stock_code, signal_date) 组选出唯一主信号。

优先级规则（可配置，见下方常量）
    1. 类型分优先：第三类 30 > 第二类 20 > 第一类 10
       依据「确认度」：三类有中枢边界作客观数值参照；二类依赖前高/前低；
       一类依赖背驰力度比较，最脆弱（ADR-006 实测 0.4% 价格差即可翻转）。
    2. 同类型分时按方向打破：卖点优先（风控优先）
    3. 仍同分时按 signal_type 字符串排序 —— 保证重跑结果确定性一致

is_primary 的语义
    它只是「按交易日」回测口径的**去重标记**。
    is_primary=0 的行不删除、不代表无效，仍参与「按信号」口径回测（按 signal_group_id 归组）。

运行
    python mark_primary.py --dry-run   # 只看分组与判定，不写库
    python mark_primary.py             # 正式标记
    python mark_primary.py --selftest  # 规则自测（不联网）
    python mark_primary.py --db data/x.db
"""
import argparse
from collections import Counter
from pathlib import Path

import storage_signal
from storage_signal import query_signals, set_primary

# ==================== 优先级配置（可改，不写死） ====================

# 类型分：越大越优先。F4 回测出各类型胜率后可直接覆盖这张表。
TYPE_SCORE = {
    "第三类买点": 30, "第三类卖点": 30,
    "第二类买点": 20, "第二类卖点": 20,
    "第一类买点": 10, "第一类卖点": 10,
}

# 平局策略：同类型分的买卖点同日相撞时生效
TIEBREAK = "sell"        # "sell" 风控优先（默认） / "buy" 机会优先

DEFAULT_SCORE = 0        # 未登记类型的兜底分，命中时打印告警

DIRECTION_RANK = ({"卖": 0, "买": 1} if TIEBREAK == "sell" else {"买": 0, "卖": 1})


def direction_of(signal_type):
    if "买点" in signal_type:
        return "买"
    if "卖点" in signal_type:
        return "卖"
    return "?"


def score_of(signal_type):
    return TYPE_SCORE.get(signal_type, DEFAULT_SCORE)


def sort_key(signal_type):
    """升序排序用：分数取负 -> 方向权重 -> 类型名（保证确定性）。"""
    return (-score_of(signal_type), DIRECTION_RANK.get(direction_of(signal_type), 9), signal_type)


def decide_primary(signals):
    """按 (stock_code, signal_date) 分组并选出唯一主信号。

    signals: [{"stock_code", "signal_date", "signal_type"}, ...]
    返回 (decisions, groups)
        decisions: [{"stock_code","date","type","is_primary"}, ...]
        groups   : [{"stock_code","date","members":[...],"primary":...,"multi":bool}, ...]
    """
    buckets = {}
    for s in signals:
        buckets.setdefault((s["stock_code"], s["signal_date"]), []).append(s["signal_type"])

    decisions, groups = [], []
    for (code, d) in sorted(buckets):
        types = sorted(buckets[(code, d)], key=sort_key)
        members = [{"type": t, "score": score_of(t), "direction": direction_of(t),
                    "is_primary": 1 if i == 0 else 0} for i, t in enumerate(types)]
        for m in members:
            decisions.append({"stock_code": code, "date": d,
                              "type": m["type"], "is_primary": m["is_primary"]})
        groups.append({"stock_code": code, "date": d, "members": members,
                       "primary": types[0], "multi": len(types) > 1})
    return decisions, groups


# ==================== 自测（不联网） ====================

def selftest():
    ok = []

    def check(desc, got, want):
        good = got == want
        ok.append(good)
        print(f"  [{'OK' if good else 'FAIL'}] {desc:<48} got={got!r} want={want!r}")

    print("--- 1) 方向识别 ---")
    check("第三类买点", direction_of("第三类买点"), "买")
    check("第一类卖点", direction_of("第一类卖点"), "卖")
    check("未知类型", direction_of("泛买点"), "买")

    print("--- 2) 排序键（分数高的排前面） ---")
    ordered = sorted(["第一类买点", "第三类卖点", "第二类买点", "第三类买点"], key=sort_key)
    check("三类 > 二类 > 一类", ordered, ["第三类卖点", "第三类买点", "第二类买点", "第一类买点"])
    check("同分时卖点优先(三卖在三买前)", ordered[0], "第三类卖点")

    print("--- 3) 分组与主信号唯一性 ---")
    sigs = [
        {"stock_code": "600519", "signal_date": "2025-06-18", "signal_type": "第二类卖点"},
        {"stock_code": "600519", "signal_date": "2025-06-18", "signal_type": "第三类卖点"},
        {"stock_code": "600519", "signal_date": "2025-02-06", "signal_type": "第一类买点"},
        {"stock_code": "000001", "signal_date": "2026-08-28", "signal_type": "第二类买点"},
        {"stock_code": "000001", "signal_date": "2026-08-28", "signal_type": "第三类买点"},
    ]
    dec, grp = decide_primary(sigs)
    check("组数", len(grp), 3)
    check("多信号组数", sum(g["multi"] for g in grp), 2)
    check("每组主信号恰好 1 个",
          all(sum(m["is_primary"] for m in g["members"]) == 1 for g in grp), True)
    g = [x for x in grp if x["date"] == "2025-06-18"][0]
    check("三卖+二卖 -> 主信号是三卖", g["primary"], "第三类卖点")
    g2 = [x for x in grp if x["date"] == "2026-08-28"][0]
    check("三买+二买 -> 主信号是三买", g2["primary"], "第三类买点")

    print("--- 4) 买卖冲突（构造：同类型分 30 的买点与卖点同日） ---")
    dec2, grp2 = decide_primary([{"stock_code": "X", "signal_date": "2026-01-01", "signal_type": "第三类买点"},
                                 {"stock_code": "X", "signal_date": "2026-01-01", "signal_type": "第三类卖点"}])
    check("同分买卖冲突 -> 卖点优先", grp2[0]["primary"], "第三类卖点")

    print("--- 5) 跨类型冲突：三买(30) vs 一卖(10) ---")
    dec3, grp3 = decide_primary([{"stock_code": "X", "signal_date": "2026-01-01", "signal_type": "第一类卖点"},
                                 {"stock_code": "X", "signal_date": "2026-01-01", "signal_type": "第三类买点"}])
    check("类型分优先，不被方向一刀切", grp3[0]["primary"], "第三类买点")

    print("--- 6) 未登记类型兜底 ---")
    dec4, grp4 = decide_primary([{"stock_code": "X", "signal_date": "2026-01-01", "signal_type": "泛买点"},
                                 {"stock_code": "X", "signal_date": "2026-01-01", "signal_type": "第一类买点"}])
    check("未登记类型分=0，让位给已登记类型", grp4[0]["primary"], "第一类买点")
    check("未知类型正确计入成员", len(grp4[0]["members"]), 2)

    print()
    print(f"==> 自测 {sum(ok)}/{len(ok)} 通过" + ("" if all(ok) else "  ❌ 有失败项"))
    return all(ok)


# ==================== 主流程 ====================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        raise SystemExit(0 if selftest() else 1)

    if a.db:
        storage_signal.DB_PATH = Path(a.db)
    print("=== T4-3 同日多信号优先级 ===")
    print(f"库文件 {storage_signal.DB_PATH.resolve()}")
    print(f"模式   {'--dry-run（不写库）' if a.dry_run else '正式标记'}")
    print(f"规则   类型分 {TYPE_SCORE}")
    print(f"       平局 {TIEBREAK} 优先；未登记类型分 {DEFAULT_SCORE}")
    print()

    rows = query_signals()
    if not rows:
        print("库中无信号。")
        return

    decisions, groups = decide_primary(rows)
    before = Counter(r["is_primary"] for r in rows)
    after = Counter(d["is_primary"] for d in decisions)

    unknown = sorted({d["type"] for d in decisions if d["type"] not in TYPE_SCORE})
    if unknown:
        print(f"⚠️ 未登记类型（按 {DEFAULT_SCORE} 分处理）: {unknown}")
        print()

    multi = [g for g in groups if g["multi"]]
    print(f"信号 {len(rows)} 条  分 {len(groups)} 组  其中多信号组 {len(multi)} 组")
    print()

    if multi:
        print("--- 多信号组判定 ---")
        for g in multi:
            print(f"  {g['stock_code']} {g['date']}  {len(g['members'])} 个信号，主信号是 {g['primary']}")
            for m in g["members"]:
                tag = "主信号" if m["is_primary"] else "次信号"
                print(f"      {tag}  {m['type']:<12} 分={m['score']:<3} 方向={m['direction']}")
        print()
    else:
        print("（无多信号组）")
        print()

    print("--- 主信号唯一性校验 ---")
    bad = [g for g in groups if sum(m["is_primary"] for m in g["members"]) != 1]
    print(f"  分组总数 {len(groups)}，主信号数 != 1 的组: {len(bad)}"
          f"  {'✅ 每组恰好一个主信号' if not bad else '❌ ' + str(bad[:3])}")
    print()

    print("--- is_primary 变更 ---")
    print(f"  标记前  is_primary=1: {before.get(1, 0):>3} 条   is_primary=0: {before.get(0, 0):>3} 条")
    print(f"  标记后  is_primary=1: {after.get(1, 0):>3} 条   is_primary=0: {after.get(0, 0):>3} 条")
    print()

    if a.dry_run:
        print("==> --dry-run：未写入任何数据")
        return

    matched, changed = set_primary(decisions)
    print(f"==> 已标记：匹配 {matched} 行，实际变更 {changed} 行（未新增/未删除任何行）")
    final = Counter(r["is_primary"] for r in query_signals())
    print(f"    复核 is_primary 分布: {dict(final)}")


if __name__ == "__main__":
    main()
