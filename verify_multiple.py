"""verify_multiple.py —— 全项目多重比较台账 + FDR（B5 / ADR-033）。

### 为什么

项目里每个 ADR 只对自己那一小批做 Bonferroni。但从没有**全局台账**。
累计跑过的检验可能上百次 —— 那么「显著」有多少本来就是运气？

### 三条纪律

1. **不手抄数字**：能从数据重算的就重算，能从存档 JSON 抽取的就抽取
2. **按族（family）做 BH-FDR**：同一个问题下的多个切分是一族，族内共享错误率预算
3. **同时报全局**：族内控制 + 全局控制，两个都看

### p 值从哪来

我们从来没存过 p 值，只存了置信区间。由 (估计, CI) 反推：

    se = (hi - lo) / (2 * 1.96)
    z  = est / se
    p  = 2 * (1 - Phi(|z|))

### 用法

    python verify_multiple.py ledger      # 建台账 + FDR
    python verify_multiple.py accounting  # 只做「项目总共跑了多少次检验」的记账
"""
import argparse
import json
import math
import sys
from pathlib import Path

import backtest as bt
import verify_edge as ve
import verify_hypotheses as vh

LEDGER = Path("config/tests_ledger.json")
TYPES = ["第一类买点", "第一类卖点", "第二类买点", "第二类卖点", "第三类买点", "第三类卖点"]
WINDOWS = (5, 10, 20)
REGIMES = ("上行", "震荡", "下行")
Q = 0.05


def phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def p_from_ci(est, lo, hi):
    if est is None or lo is None or hi is None or hi <= lo:
        return None
    se = (hi - lo) / (2 * 1.96)
    if se <= 0:
        return None
    return max(min(2 * (1 - phi(abs(est / se))), 1.0), 0.0)


def excess_vals(sub, base):
    """逐条信号的方向调整后超额。"""
    if sub.empty:
        return []
    d = bt.direction_of(sub["signal_type"].iloc[0])
    return [r.return_pct - d * base[r.stock_code]
            for r in sub.itertuples() if r.stock_code in base]


def alpha_vals(sub, idx_map):
    out = []
    for r in sub.itertuples():
        ir = ve.index_return(idx_map, r.entry_date, r.exit_date)
        if ir is None:
            continue
        out.append(r.return_pct - bt.direction_of(r.signal_type) * ir)
    return out


# ---------------- 从数据重算的两族 ----------------

def family_table_a(m):
    """族 F1：六类信号 x 三窗口的超额（表A）。18 次检验。"""
    out = []
    for w in WINDOWS:
        sub = m[m["window"] == w]
        base = vh.baseline_per_code(sub, w)
        for t in TYPES:
            s = sub[sub["signal_type"] == t]
            v = excess_vals(s, base)
            if len(v) < 2:
                continue
            est, lo, hi = ve.mean_ci(v)
            out.append({"id": "表A|%s|%d日" % (t, w), "family": "F1 表A 六类x三窗口",
                        "n": len(v), "est": est, "lo": lo, "hi": hi,
                        "p": p_from_ci(est, lo, hi)})
    return out


def family_regime_alpha(m, idx_map, reg_df):
    """族 F2：六类信号 x 三窗口 x 三状态的真 alpha。54 次检验。"""
    import market_regime as mr
    out = []
    for w in WINDOWS:
        sub = m[m["window"] == w].copy()
        sub["_rg"] = [mr.regime_on(reg_df, d) for d in sub["signal_date"]]
        for t in TYPES:
            for rg in REGIMES:
                s = sub[(sub["signal_type"] == t) & (sub["_rg"] == rg)]
                v = alpha_vals(s, idx_map)
                if len(v) < 2:
                    continue
                est, lo, hi = ve.mean_ci(v)
                out.append({"id": "regime|%s|%d日|%s" % (t, w, rg),
                            "family": "F2 regime 分层 alpha 六类x三窗口x三状态",
                            "n": len(v), "est": est, "lo": lo, "hi": hi,
                            "p": p_from_ci(est, lo, hi)})
    return out


# ---------------- 从存档抽取 ----------------

def from_json(path, family, extract):
    p = Path(path)
    if not p.exists():
        return []
    d = json.loads(p.read_text(encoding="utf-8"))
    out = []
    for x in extract(d):
        if x:
            x["family"] = family
            x["p"] = p_from_ci(x.get("est"), x.get("lo"), x.get("hi"))
            out.append(x)
    return out


def load_archived():
    out = []
    # 第 1 轮假设（6 条，5 日）
    out += from_json("config/hypotheses_results.json", "F4 第1轮假设", lambda d: [
        {"id": "H|" + r["id"], "n": r.get("n_hi"), "est": r.get("diff"),
         "lo": r["ci"][0], "hi": r["ci"][1]} for r in d.get("results", [])
        if r.get("ci") and r.get("diff") is not None])
    # 第 2 轮假设（6 条，存档只留最后一次窗口）
    out += from_json("config/hypotheses_r2_results.json", "F5 第2轮假设（仅最后跑的窗口）", lambda d: [
        {"id": "R|" + r["id"], "n": r.get("n_hi"), "est": r.get("diff"),
         "lo": r["ci"][0], "hi": r["ci"][1]} for r in d.get("results", [])
        if r.get("ci") and r.get("diff") is not None])
    # 打分样本外（3 窗口 x 2 期）
    def ex_score(d):
        r = []
        for w, arr in (d.get("by_window") or {}).items():
            for x in arr:
                if x.get("ci") and x.get("diff") is not None:
                    r.append({"id": "score|%s日|%s" % (w, x.get("period")),
                              "n": x.get("n"), "est": x["diff"],
                              "lo": x["ci"][0], "hi": x["ci"][1]})
        return r
    out += from_json("config/score_prereg_results.json", "F7 打分样本外", ex_score)
    # 单因子 vs 组合
    def ex_cmp(d):
        r = []
        for k, arr in (d.get("result") or {}).items():
            if not isinstance(arr, list):
                continue
            for x in arr:
                if x.get("ci") and x.get("diff") is not None:
                    r.append({"id": "cmp|%s|%s日" % (k, x.get("window")),
                              "n": x.get("n"), "est": x["diff"],
                              "lo": x["ci"][0], "hi": x["ci"][1]})
        return r
    out += from_json("config/score_compare_results.json", "F8 单因子 vs 组合", ex_cmp)
    # ADR-029 的 9 条体检结论
    out += from_json("config/claims_prereg_results.json", "F9 ADR-029 结论体检", lambda d: [
        {"id": "claim|" + k, "n": v.get("n"), "est": v.get("mean"),
         "lo": v.get("lo"), "hi": v.get("hi")}
        for k, v in (d.get("results") or {}).items() if v.get("lo") is not None])
    # 第 3 轮假设：背驰强度（MACD 面积比），人工预注册
    def ex_r3(d):
        return [{"id": "R3|" + r["id"], "n": r.get("n_hi"), "est": r.get("diff"),
                 "lo": r["ci"][0], "hi": r["ci"][1]} for r in d.get("results", [])
                if r.get("ci") and r.get("diff") is not None]
    out += from_json("config/hypotheses_r3_results.json",
                     "F14 第3轮假设：背驰强度（MACD 面积比）", ex_r3)
    out += from_json("config/hypotheses_r3_exploratory_results.json",
                     "F15 第3轮探索性：二三类点（一类点已被硬过滤）", ex_r3)
    return out


def bh(items, q=Q):
    """Benjamini-Hochberg：返回 q 值与被判为发现的下标。"""
    ok = [x for x in items if x.get("p") is not None]
    ok.sort(key=lambda x: x["p"])
    n = len(ok)
    if not n:
        return []
    kmax, qs = 0, []
    for i, x in enumerate(ok, 1):
        thr = i / n * q
        qs.append(thr)
        if x["p"] <= thr:
            kmax = i
    for i, x in enumerate(ok):
        x["bh_q"] = qs[i]
        x["discovery"] = i < kmax
    return ok


def ledger():
    m = vh.load_merged()
    idx_df, _, _ = __import__("storage_index").ensure_index("000300")
    idx_map = ve.make_idx_map(idx_df)
    import market_regime as mr
    reg_df = mr.with_indicators(idx_df)

    print("=== 建台账（重算两族 + 抽取存档）===")
    f1 = family_table_a(m)
    print("  F1 表A 六类x三窗口      %d 次检验" % len(f1))
    f2 = family_regime_alpha(m, idx_map, reg_df)
    print("  F2 regime 分层 alpha     %d 次检验" % len(f2))
    arch = load_archived()
    print("  存档抽取                 %d 次检验" % len(arch))
    allt = f1 + f2 + arch
    print("  合计                     %d 次检验" % len(allt))

    fams = {}
    for x in allt:
        fams.setdefault(x["family"], []).append(x)

    print()
    print("=== 按族做 BH-FDR（q=%.2f）===" % Q)
    print("  %-38s %-6s %-10s %-10s %s" % ("族", "检验数", "族内发现", "期望假阳性", "说明"))
    for name, items in sorted(fams.items()):
        ok = bh(items)
        disc = sum(1 for x in ok if x.get("discovery"))
        exp_fp = 0.05 * len(ok)
        print("  %-38s %-6d %-10d %-10.1f %s"
              % (name[:36], len(ok), disc, exp_fp,
                 "族内 %.0f%% 的显著可能是运气" % (100 * exp_fp / max(disc, 1)) if disc else ""))

    print()
    print("=== 全局一次 BH-FDR（把所有检验放一起，最保守）===")
    ok = bh(allt)
    disc = [x for x in ok if x.get("discovery")]
    print("  总检验 %d 次；按 q=%.2f 全局判为发现的 %d 次" % (len(ok), Q, len(disc)))
    print("  单次检验 alpha=0.05 下，期望纯运气显著 %.1f 次" % (0.05 * len(ok)))
    p5 = sum(1 for x in ok if x["p"] < 0.05)
    print("  实际 p<0.05 的 %d 次（比期望多 %.1f 次）" % (p5, p5 - 0.05 * len(ok)))
    print()
    print("  全局仍判为发现的：")
    for x in disc[:30]:
        print("    %-42s n=%-5s est=%-9s p=%.4f  q=%.4f"
              % (x["id"][:42], x.get("n"), ve.pct(x["est"]), x["p"], x["bh_q"]))

    LEDGER.write_text(json.dumps(
        {"built_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
         "q": Q, "n_tests": len(ok), "n_global_discoveries": len(disc),
         "expected_false_positives_at_005": 0.05 * len(ok),
         "families": {k: len(v) for k, v in fams.items()},
         "tests": [{k: v for k, v in x.items() if k != "rets"} for x in allt]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print("  台账已存 %s" % LEDGER)
    return 0


ACCOUNTING = [
    ("F1 表A 六类信号 x 三窗口 超额", 18, "重算"),
    ("F2 regime 分层 alpha 六类x三窗口x三状态", 54, "重算"),
    ("F3 周线共振 三条件 x 六类", 18, "未存 CI"),
    ("F4 第1轮假设 6 条", 6, "存档"),
    ("F5 第2轮假设 6 条 x 三窗口", 18, "存档仅最后窗口"),
    ("F6 阈值扫描 H3 七档 + H4 八档", 15, "未存 CI"),
    ("F7 打分样本外 三窗口 x 两期", 6, "存档"),
    ("F8 单因子 vs 组合", 9, "存档"),
    ("F9 ADR-029 结论体检 9 条", 9, "存档"),
    ("F10 去偏对比 六类", 6, "未存 CI"),
    ("F11 参数敏感性 min_bi_len/max_zs_bis/power_tol", 10, "未存 CI"),
    ("F12 60分钟级别共振 三窗口", 3, "未存 CI"),
    ("F13 B3 固定股票池 三窗口", 3, "未存 CI"),
    ("F14 第3轮假设：背驰强度 主检验", 3, "存档"),
    ("F15 第3轮探索性：一类点已过滤", 2, "存档"),
]


def accounting():
    print("=== 全项目检验次数记账 ===")
    print("  %-46s %-8s %s" % ("族", "检验数", "数据来源"))
    tot = 0
    for name, n, src in ACCOUNTING:
        tot += n
        print("  %-46s %-8d %s" % (name, n, src))
    print("  %-46s %-8d" % ("合计", tot))
    print()
    print("  单次 alpha=0.05 下，期望**纯靠运气**显著 %.1f 次" % (0.05 * tot))
    print("  按 Bonferroni 校正，单次阈值应收到 %.5f" % (0.05 / tot))
    print()
    print("  注：这些检验**高度相关**（同一批信号、同一份数据、只是换了切分维度），")
    print("      所以「期望假阳性」是**上界**。有效的独立检验数远小于 %d。" % tot)
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ledger")
    sub.add_parser("accounting")
    a = ap.parse_args()
    return ledger() if a.cmd == "ledger" else accounting()


if __name__ == "__main__":
    sys.exit(main())
