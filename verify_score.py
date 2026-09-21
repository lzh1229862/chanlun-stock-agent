"""verify_score.py —— 排序打分的样本外验证（T20 / ADR-024）。

### 为什么还要再做一次

ADR-023 的打分在三个窗口上都过了（CI 不跨 0、分半一致、单调），但它是**探索性**的：
两个因子各自预注册验证过，**「把它们加起来」以及阈值 0.12 / 10 都是在全样本上定的**。
分半验证（前 60% vs 后 40%）**不能替代时间样本外** —— 它仍然用了同一批数据选阈值。

### 这次怎么做

    prereg   把假设、阈值、期间、通过标准**先写死落盘**（config/score_prereg.json）
    test     只读那个文件，做**真正的时间切分**验证

**训练期** 2015-01-01 ~ 2020-12-31
**检验期** 2021-01-01 ~ 2026-12-31

基准在**各期自己的跨度内重算**（与 verify_edge 的约定一致）。

### 诚实声明（必须写进结论）

阈值的**数值**来自 ADR-023，而 ADR-023 用的是全样本（含检验期）。
所以这次检验仍然带一层**阈值选择的污染** —— 它比 ADR-023 强，但**不是完全干净的样本外**。
真正干净的版本需要一个从头到尾都没被看过的数据段，本项目没有（历史只有一份）。

本工具额外做一件事：**只用训练期重新选一遍阈值**，
看选出来的阈值是不是还在 0.12 / 10 附近 —— 如果差很远，说明阈值本身就不可靠。

### 用法

    python verify_score.py prereg     # 先落盘预注册（之后不得修改）
    python verify_score.py test       # 再做时间切分验证
"""
import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

import verify_hypotheses as vh
from signal_score import G_THR, W_THR

PREREG_PATH = Path("config/score_prereg.json")
RESULT_PATH = Path("config/score_prereg_results.json")

TRAIN = ("2015-01-01", "2020-12-31")
TEST = ("2021-01-01", "2026-12-31")
WINDOWS = (5, 10, 20)

# 只在训练期上重新选阈值时的候选（次级分析用）
W_CANDS = (0.05, 0.062, 0.08, 0.10, 0.12, 0.15)
G_CANDS = (5, 8, 10, 12, 15)


def prereg():
    rec = {
        "preregistered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "hypothesis": "打分高的信号，后续超额显著高于打分低的信号（score2 超额 > score0 超额）",
        "score": "score = [zs_width_pct >= %.2f] + [0 <= zs_gap_days < %d]" % (W_THR, G_THR),
        "thresholds": {"zs_width_pct": W_THR, "zs_gap_days": G_THR},
        "direction": "positive",
        "train_period": list(TRAIN),
        "test_period": list(TEST),
        "windows": list(WINDOWS),
        "pass_criteria": {
            "ci_excludes_zero_in_test": True,
            "split_half_consistent_in_test": True,
            "monotonic_2_gt_1_gt_0_in_test": True,
        },
        "known_contamination": (
            "阈值数值来自 ADR-023，而 ADR-023 用的是全样本（含检验期）。"
            "本验证仍带一层阈值选择的污染；比 ADR-023 强，但不是完全干净的样本外。"),
        "primary_metric": "检验期内 score2 超额 − score0 超额（各期基准在自己跨度内重算）",
    }
    PREREG_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREREG_PATH.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print("✓ 已预注册到 %s（此后不得修改）" % PREREG_PATH)
    for k in ("hypothesis", "score", "train_period", "test_period", "windows"):
        print("    %-14s %s" % (k, rec[k]))
    print("    通过标准      %s" % rec["pass_criteria"])
    print()
    print("  ⚠️  %s" % rec["known_contamination"])
    return 0


def period_slice(m, period, window, w_thr, g_thr):
    """切出某一期、算好打分，并**在本期自己的跨度内**重算基准。"""
    lo, hi = period
    sub = m[(m["entry_date"] >= lo) & (m["entry_date"] <= hi)].copy()
    if sub.empty:
        return None
    base = vh.baseline_per_code(sub, window)
    sub["_w"] = (sub["zs_width_pct"] >= w_thr).astype(int)
    sub["_g"] = ((sub["zs_gap_days"] >= 0) & (sub["zs_gap_days"] < g_thr)).astype(int)
    sub["_s"] = sub["_w"] + sub["_g"]
    return sub, base


def group_excess(sub, base, window, score):
    g = sub[sub["_s"] == score]
    if len(g) < 30:
        return None
    r, e = vh.excess_of(g, base, window)
    return e, len(g), r


def diff_ci(hi, lo, base, window):
    if hi is None or lo is None:
        return None
    r1, e1 = vh.excess_of(hi, base, window)
    r0, e0 = vh.excess_of(lo, base, window)
    d, dlo, dhi = vh.welch_diff_ci(r1, r0)
    de = e1 - e0
    return de, dlo + (de - d), dhi + (de - d), e1, e0


def split_half(sub, base, window):
    cut = vh.ve.split_point([{"entry_date": x} for x in sub["entry_date"]], 0.5)
    out = []
    for hi_part, lo_part in ((sub[(sub["_s"] == 2) & (sub["entry_date"] <= cut)],
                              sub[(sub["_s"] == 0) & (sub["entry_date"] <= cut)]),
                             (sub[(sub["_s"] == 2) & (sub["entry_date"] > cut)],
                              sub[(sub["_s"] == 0) & (sub["entry_date"] > cut)])):
        if len(hi_part) < 25 or len(lo_part) < 25:
            out.append(None)
            continue
        _, e1 = vh.excess_of(hi_part, base, window)
        _, e0 = vh.excess_of(lo_part, base, window)
        out.append(e1 - e0)
    return out


def report_period(name, m, period, window, w_thr, g_thr, rows):
    got = period_slice(m, period, window, w_thr, g_thr)
    if got is None:
        print("  %-6s 无数据" % name)
        return None
    sub, base = got
    cells = {s: group_excess(sub, base, window, s) for s in (2, 1, 0)}
    d = diff_ci(sub[sub["_s"] == 2], sub[sub["_s"] == 0], base, window)
    if d is None:
        print("  %-6s 分组太小（score2=%s score0=%s）"
              % (name, len(sub[sub["_s"] == 2]), len(sub[sub["_s"] == 0])))
        return None
    de, dlo, dhi, e2, e0 = d
    hh = split_half(sub, base, window)
    sig = not (dlo <= 0 <= dhi)
    mono = all(cells[s] is not None and (cells[2][0] > cells[1][0] > cells[0][0])
               for s in (2, 1, 0)) if all(cells.values()) else False
    print("  %-6s n=%-5d  score2=%-9s(n=%d)  score1=%-9s  score0=%-9s(n=%d)"
          % (name, len(sub), vh.pct(e2) if e2 is not None else "—",
             len(sub[sub["_s"] == 2]),
             vh.pct(cells[1][0]) if cells[1] else "—",
             vh.pct(e0) if e0 is not None else "—", len(sub[sub["_s"] == 0])))
    print("         score2−score0 = %s   CI [%s, %s]  %s   单调 %s   分半 %s"
          % (vh.pct(de), vh.pct(dlo), vh.pct(dhi), "不跨0 ✓" if sig else "跨0",
             "✓" if mono else "✗",
             "/".join(vh.pct(x) if x is not None else "—" for x in hh)))
    half_ok = bool([x for x in hh if x is not None]) and all(x > 0 for x in hh if x is not None)
    rows.append({"period": name, "n": len(sub), "exc2": e2, "exc0": e0, "diff": de,
                 "ci": [dlo, dhi], "sig": sig, "mono": mono, "halves": hh,
                 "half_ok": half_ok})
    return rows[-1]


def best_threshold_on_train(m, window):
    """**只用训练期**重新选一遍阈值，看选出来的是不是还在 0.12 / 10 附近。"""
    got = period_slice(m, TRAIN, window, W_THR, G_THR)
    if got is None:
        return None, None
    sub, base = got
    best = {}
    for lab, cands, other, key in (("zs_width_pct", W_CANDS, None, "_w"),
                                   ("zs_gap_days", G_CANDS, None, "_g")):
        scores = []
        for c in cands:
            s2 = sub.copy()
            if lab == "zs_width_pct":
                s2["_w"] = (s2["zs_width_pct"] >= c).astype(int)
            else:
                s2["_g"] = ((s2["zs_gap_days"] >= 0) & (s2["zs_gap_days"] < c)).astype(int)
            s2["_s"] = s2["_w"] + s2["_g"]
            d = diff_ci(s2[s2["_s"] == 2], s2[s2["_s"] == 0], base, window)
            if d is None:
                scores.append((c, None))
                continue
            scores.append((c, d[0]))
        ok = [(c, v) for c, v in scores if v is not None]
        best[lab] = max(ok, key=lambda x: x[1]) if ok else (None, None)
        print("    %-14s 训练期各档 score2−score0: %s"
              % (lab, "  ".join("%s:%s" % (c, vh.pct(v) if v is not None else "—")
                                for c, v in scores)))
    return best["zs_width_pct"][0], best["zs_gap_days"][0]


def test():
    if not PREREG_PATH.exists():
        print("还没有预注册，先跑：python verify_score.py prereg")
        return 1
    rec = json.loads(PREREG_PATH.read_text(encoding="utf-8"))
    w_thr = rec["thresholds"]["zs_width_pct"]
    g_thr = rec["thresholds"]["zs_gap_days"]
    m = vh.load_merged()
    print("=== 打分的样本外验证 ===")
    print("预注册：%s（%s）" % (PREREG_PATH, rec["preregistered_at"]))
    print("假设  ：%s" % rec["hypothesis"])
    print("打分  ：%s" % rec["score"])
    print("全库  ：%d 条 / %d 只股票 / %s ~ %s"
          % (len(m), m["stock_code"].nunique(),
             str(m["entry_date"].min())[:10], str(m["entry_date"].max())[:10]))
    print()

    all_rows = {}
    for w in WINDOWS:
        print("【窗口 %d 日】" % w)
        rows = []
        report_period("训练期", m, TRAIN, w, w_thr, g_thr, rows)
        report_period("检验期", m, TEST, w, w_thr, g_thr, rows)
        all_rows[w] = rows
        print()

    print("=== 判定（只看检验期）===")
    passed = []
    for w in WINDOWS:
        r = next((x for x in all_rows[w] if x["period"] == "检验期"), None)
        if r is None:
            print("  %2d 日：样本不足" % w)
            continue
        ok = r["sig"] and r["half_ok"] and r["mono"]
        passed.append((w, ok))
        print("  %2d 日：CI %s | 分半 %s | 单调 %s  ->  %s"
              % (w, "不跨0 ✓" if r["sig"] else "跨0 ✗",
                 "一致 ✓" if r["half_ok"] else "不一致 ✗",
                 "✓" if r["mono"] else "✗",
                 "通过" if ok else "未通过"))
    n_ok = sum(1 for _, ok in passed if ok)
    print()
    print("  通过 %d/%d 个窗口" % (n_ok, len(passed)))
    if n_ok == len(passed) and passed:
        print("  => 打分在**时间样本外**成立（在已知的阈值选择污染下）")
    elif n_ok:
        print("  => 部分窗口成立，不能算通过")
    else:
        print("  => **没有通过** —— 打分只在全样本上好看")

    print()
    print("=== 次级分析：只用训练期重新选阈值（看阈值稳不稳）===")
    bw, bg = best_threshold_on_train(m, 5)
    print("    训练期最优: zs_width_pct=%.3f  zs_gap_days=%s" % (bw, bg)
          if bw is not None else "    训练期无法选阈值")
    print("    预注册用值: zs_width_pct=%.3f  zs_gap_days=%s" % (w_thr, g_thr))
    if bw is not None:
        ok_w = abs(bw - w_thr) <= 0.025
        ok_g = bg is not None and abs(bg - g_thr) <= 2
        print("    阈值是否稳定: width %s / gap %s"
              % ("✓ 接近" if ok_w else "✗ 差得远", "✓ 接近" if ok_g else "✗ 差得远"))

    RESULT_PATH.write_text(json.dumps(
        {"tested_at": time.strftime("%Y-%m-%d %H:%M:%S"), "thresholds": rec["thresholds"],
         "train": list(TRAIN), "test": list(TEST), "n_rows": len(m),
         "by_window": {str(k): v for k, v in all_rows.items()},
         "train_best_threshold": {"zs_width_pct": bw, "zs_gap_days": bg},
         "passed_windows": [w for w, ok in passed if ok]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print("  结果已存 %s" % RESULT_PATH)
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prereg")
    sub.add_parser("test")
    a = ap.parse_args()
    return prereg() if a.cmd == "prereg" else test()


if __name__ == "__main__":
    sys.exit(main())
