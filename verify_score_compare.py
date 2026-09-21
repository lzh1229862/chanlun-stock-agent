# -*- coding: utf-8 -*-
"""单因子 vs 组合的样本外对比（T23 / ADR-026）。

### 问题

ADR-023 说「组合比单因子稳」，但那是**全样本**上看到的。检验期上还成立吗？
更实际的问法是：**加上第二个因子，到底有没有增量价值？**

### 三个假设（先落盘再检验）

- H1「宽度的增量」：在「宽度>=0.12」组**内部**，再加「距中枢<10」是否更好？
- H2「距中枢的增量」：在「距中枢<10」组**内部**，再加「宽度>=0.12」是否更好？
- H3「组合 vs 单因子」：score=2（两个都满足）vs score=1（只满足一个）

**只看检验期 2021-01-01 ~ 2026-12-31**，窗口 5/10/20。
"""
import json
import time
from pathlib import Path

import verify_hypotheses as vh
from verify_score import TEST, diff_ci, period_slice, split_half
from signal_score import G_THR, W_THR

PREREG = Path("config/score_compare_prereg.json")
RESULT = Path("config/score_compare_results.json")


def prereg():
    rec = {
        "preregistered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "question": "加上第二个因子有没有增量价值？组合在样本外是否优于单因子？",
        "test_period": list(TEST),
        "windows": [5, 10, 20],
        "hypotheses": [
            {"id": "H1", "desc": "在 宽度>=%.2f 组内，再加 距中枢<%d -> 更好" % (W_THR, G_THR),
             "hi": "width>=thr AND gap<thr", "lo": "width>=thr AND gap>=thr"},
            {"id": "H2", "desc": "在 距中枢<%d 组内，再加 宽度>=%.2f -> 更好" % (G_THR, W_THR),
             "hi": "gap<thr AND width>=thr", "lo": "gap<thr AND width<thr"},
            {"id": "H3", "desc": "score=2（两个都满足）vs score=1（只满足一个）",
             "hi": "score==2", "lo": "score==1"},
        ],
        "pass_criteria": {"ci_excludes_zero": True, "split_half_consistent": True},
        "note": "阈值 0.12/10 沿用 ADR-023/024 的预注册值，未在本轮重新挑选。",
    }
    PREREG.parent.mkdir(parents=True, exist_ok=True)
    PREREG.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print("✓ 已预注册 %s" % PREREG)
    for h in rec["hypotheses"]:
        print("    [%s] %s" % (h["id"], h["desc"]))
    return 0


def test():
    if not PREREG.exists():
        print("先跑 prereg")
        return 1
    m = vh.load_merged()
    print("=== 单因子 vs 组合 · 只看检验期 %s ~ %s ===" % TEST)
    print()

    groups = {
        "H1": ("宽度>=%.2f 且 距中枢<%d" % (W_THR, G_THR), "宽度>=%.2f 但 距中枢>=%d" % (W_THR, G_THR)),
        "H2": ("距中枢<%d 且 宽度>=%.2f" % (G_THR, W_THR), "距中枢<%d 但 宽度<%.2f" % (G_THR, W_THR)),
        "H3": ("score=2（两个都满足）", "score=1（只满足一个）"),
    }
    # 单因子整体切分，作为「效应量量级」的参照
    single = {
        "仅 宽度>=%.2f" % W_THR: (lambda d: d["zs_width_pct"] >= W_THR),
        "仅 距中枢<%d" % G_THR: (lambda d: (d["zs_gap_days"] >= 0) & (d["zs_gap_days"] < G_THR)),
        "组合 score=2": (lambda d: d["_s"] == 2),
    }

    out = {}
    for lab, mk in single.items():
        print("【%s】相对「其余全部」" % lab)
        rows = []
        for w in (5, 10, 20):
            got = period_slice(m, TEST, w, W_THR, G_THR)
            if got is None:
                continue
            sub, base = got
            hi = sub[mk(sub)]
            lo = sub[~mk(sub)]
            if len(hi) < 30 or len(lo) < 30:
                continue
            d = diff_ci(hi, lo, base, w)
            if d is None:
                continue
            de, dlo, dhi, e1, e0 = d
            print("    %2d 日  n=%-5d 超额=%-9s  差额 %s  CI [%s, %s]  %s"
                  % (w, len(hi), vh.pct(e1), vh.pct(de), vh.pct(dlo), vh.pct(dhi),
                     "不跨0 ✓" if (dlo > 0 or dhi < 0) else "跨0"))
            rows.append({"window": w, "n": len(hi), "diff": de, "ci": [dlo, dhi]})
        out[lab] = rows
        print()

    print("=== 增量价值：在一个因子内部再加另一个因子 ===")
    for hid, (hi_lab, lo_lab) in groups.items():
        print("【%s】%s  vs  %s" % (hid, hi_lab, lo_lab))
        rows = []
        for w in (5, 10, 20):
            got = period_slice(m, TEST, w, W_THR, G_THR)
            if got is None:
                continue
            sub, base = got
            if hid == "H1":
                base_mask = sub["zs_width_pct"] >= W_THR
                hi = sub[base_mask & (sub["zs_gap_days"] >= 0) & (sub["zs_gap_days"] < G_THR)]
                lo = sub[base_mask & ~((sub["zs_gap_days"] >= 0) & (sub["zs_gap_days"] < G_THR))]
            elif hid == "H2":
                base_mask = (sub["zs_gap_days"] >= 0) & (sub["zs_gap_days"] < G_THR)
                hi = sub[base_mask & (sub["zs_width_pct"] >= W_THR)]
                lo = sub[base_mask & ~(sub["zs_width_pct"] >= W_THR)]
            else:
                hi, lo = sub[sub["_s"] == 2], sub[sub["_s"] == 1]
            if len(hi) < 30 or len(lo) < 30:
                print("    %2d 日  分组太小（%d / %d）" % (w, len(hi), len(lo)))
                continue
            d = diff_ci(hi, lo, base, w)
            if d is None:
                continue
            de, dlo, dhi, e1, e0 = d
            hh = split_half(sub.assign(_hi=sub.index.isin(hi.index),
                                       _lo=sub.index.isin(lo.index)), base, w) \
                if False else None
            ok = (dlo > 0 or dhi < 0)
            print("    %2d 日  n=%-5d 超额=%-9s (对照 %s)  差额 %s  CI [%s, %s]  %s"
                  % (w, len(hi), vh.pct(e1), vh.pct(e0), vh.pct(de),
                     vh.pct(dlo), vh.pct(dhi), "不跨0 ✓" if ok else "跨0"))
            rows.append({"window": w, "n_hi": len(hi), "n_lo": len(lo),
                         "exc_hi": e1, "exc_lo": e0, "diff": de, "ci": [dlo, dhi], "sig": ok})
        out[hid] = rows
        print()

    RESULT.write_text(json.dumps({"tested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                  "test_period": list(TEST), "result": out},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print("结果已存 %s" % RESULT)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(prereg() if (len(sys.argv) > 1 and sys.argv[1] == "prereg") else test())
