"""verify_hypotheses.py —— LLM 提假设 + 数据裁决（T16 / ADR-021）。

⚠️ 为什么强制分成两个子命令
    如果允许「边看数据边改假设」，那就是在一堆组合里挑最显著的那个 —— 必然过拟合。
    所以：
        gen   调 LLM 生成假设 → **原样落盘** config/hypotheses.json（预注册）
        test  只读那个文件，用数据裁决
    gen 之后**不允许再改** hypotheses.json。要改就重开一轮，并记录这是第几轮。

⚠️ 多重比较
    测 H 条假设、每条看 95% 置信区间，期望会有 0.05H 条**纯靠运气**显著。
    所以本工具同时报：
      · Bonferroni 阈值（α/H）
      · 分半验证（前 60% 时间 vs 后 40%），两段都要成立才算数
    只看单条 CI 就宣布「发现规律」，是这一类工作最常见的自欺方式。

用法
    python verify_hypotheses.py gen              # 调 LLM 生成并预注册
    python verify_hypotheses.py test             # 用数据裁决（可重复跑）
    python verify_hypotheses.py test --window 10
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

import backtest as bt
import verify_edge as ve
from verify_edge import mean_ci, pct, welch_diff_ci

FEAT_PATH = Path("data/signal_features.parquet")
HYP_PATH = Path("config/hypotheses.json")          # 第 1 轮（兼容旧路径）
PROMPT_PATH = Path("config/prompts/hypotheses.txt")


def hyp_path(rnd):
    return HYP_PATH if rnd == 1 else Path("config/hypotheses_r%d.json" % rnd)


def hyp_result_path(rnd):
    return Path("config/hypotheses_results.json") if rnd == 1 else \
        Path("config/hypotheses_r%d_results.json" % rnd)


def prompt_path(rnd):
    return PROMPT_PATH if rnd == 1 else Path("config/prompts/hypotheses_r%d.txt" % rnd)


def carry_over(rnd, ids):
    """把上一轮某几条假设原样带进本轮（用于「重测」）。

    重测必须**原样**：改一个阈值就不是重测了，是新的假设。
    """
    prev = json.loads(hyp_path(rnd - 1).read_text(encoding="utf-8"))
    want = set(ids or [])
    out = []
    for h in prev["hypotheses"]:
        if h["id"] in want:
            h2 = dict(h)
            h2["id"] = h["id"] + "R"
            h2["retest_of"] = h["id"]
            out.append(h2)
    return out
WINDOW = 5
COST = 0.00202
MIN_GROUP = 100


# ==================== 假设生成 ====================

def read_prompt(path=PROMPT_PATH):
    """按 === SYSTEM === / === USER === 分段；# 开头是注释。"""
    body = "\n".join(l for l in Path(path).read_text(encoding="utf-8").splitlines()
                     if not l.lstrip().startswith("#"))
    _, rest = body.split("=== SYSTEM ===", 1)
    sys_part, user_part = rest.split("=== USER ===", 1)
    return sys_part.strip(), user_part.strip()


def api_key():
    """优先环境变量，其次 Windows 用户环境注册表（不落盘、不打印）。"""
    import os
    if os.getenv("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    out = subprocess.run(["reg", "query", "HKCU\\Environment", "/v", "DEEPSEEK_API_KEY"],
                         capture_output=True, text=True, encoding="gbk", errors="replace")
    for line in out.stdout.splitlines():
        if "DEEPSEEK_API_KEY" in line and "REG_SZ" in line:
            return line.split("REG_SZ", 1)[1].strip()
    return ""


def extract_json(text):
    t = text.strip()
    t = re.sub(r"^\s*```(?:json)?", "", t).strip()
    t = re.sub(r"```\s*$", "", t).strip()
    i, j = t.find("["), t.rfind("]")
    if i < 0 or j < 0:
        raise ValueError("回复里找不到 JSON 数组：%s" % t[:200])
    return json.loads(t[i:j + 1])


NUM_OPS = {">=", ">", "<=", "<"}
CAT_FEATS = {"position", "regime", "board", "signal_type"}
NUM_FEATS = {"zs_gap_days", "zs_width_pct", "zs_n", "bi_bars", "bi_pct", "vol_ratio_20",
             "amount_yi", "mom_20", "vola_20", "dist_ma60", "confirm_delay"}


def validate(hs):
    """结构校验 —— 挡住 LLM 编造特征名 / 写错操作符。"""
    errs = []
    for h in hs:
        hid = h.get("id", "?")
        for k in ("id", "hypothesis", "theory", "when", "predict"):
            if k not in h:
                errs.append("%s 缺字段 %s" % (hid, k))
        if h.get("predict") not in ("high", "low"):
            errs.append("%s predict 必须是 high/low" % hid)
        if not isinstance(h.get("when"), list) or not h["when"]:
            errs.append("%s when 必须是非空列表" % hid)
            continue
        for c in h["when"]:
            f = c.get("feature")
            if f in NUM_FEATS:
                if c.get("op") not in NUM_OPS:
                    errs.append("%s 数值条件 op 非法：%r" % (hid, c.get("op")))
                if not isinstance(c.get("threshold"), (int, float)):
                    errs.append("%s 数值条件缺 threshold" % hid)
            elif f in CAT_FEATS:
                if not isinstance(c.get("in"), list):
                    errs.append("%s 分类条件缺 in 列表" % hid)
            else:
                errs.append("%s 用了未知特征 %r" % (hid, f))
    return errs


def cmd_gen(args):
    key = api_key()
    if not key:
        print("找不到 API key（环境变量 DEEPSEEK_API_KEY 或用户注册表）")
        return 1
    import os
    os.environ["DEEPSEEK_API_KEY"] = key
    import llm_client as L

    system, user = read_prompt(prompt_path(args.round))
    print("=== 第 %d 轮  调 %s 生成假设（prompt %d 字）===" % (args.round, L.MODEL, len(user)))
    t0 = time.time()
    txt, usage, attempts = L.call_deepseek(user, system=system, timeout=args.timeout)
    print("  用时 %.1fs  尝试 %d 次  token %s" % (time.time() - t0, attempts, usage))

    hs = extract_json(txt)
    errs = validate(hs)
    if errs:
        print("  ✗ 结构校验不通过：")
        for e in errs:
            print("     " + e)
        Path("config/hypotheses.raw.txt").write_text(txt, encoding="utf-8")
        print("  原始回复已存 config/hypotheses.raw.txt，请人工检查")
        return 1

    if args.carry_ids:
        carried = carry_over(args.round, args.carry_ids.split(","))
        errs2 = validate(carried)
        if errs2:
            print("  ✗ 结转假设校验不通过：%s" % errs2)
            return 1
        hs = hs + carried
        print("  结转上一轮的 %s -> %s（原样重测）"
              % (args.carry_ids, ",".join(h["id"] for h in carried)))

    rec = {"round": args.round,
           "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "model": L.MODEL,
           "prompt_file": str(prompt_path(args.round)), "n": len(hs), "usage": usage,
           "carried": args.carry_ids or "", "hypotheses": hs}
    p = hyp_path(args.round)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  ✓ 已预注册 %d 条到 %s（本文件之后不得修改）" % (len(hs), p))
    print()
    for h in hs:
        cond = " AND ".join(
            ("%s %s %s" % (c["feature"], c.get("op"), c.get("threshold"))) if "op" in c
            else ("%s ∈ %s" % (c["feature"], c["in"])) for c in h["when"])
        print("  [%s] %s" % (h["id"], h["hypothesis"]))
        print("        切分: %s       预测: 满足组更%s" % (cond, "好" if h["predict"] == "high" else "差"))
    return 0


# ==================== 数据裁决 ====================

def match(row, when):
    for c in when:
        v = row.get(c["feature"])
        if "in" in c:
            if v not in c["in"]:
                return False
        else:
            if v is None or pd.isna(v):
                return False
            t = c["threshold"]
            op = c["op"]
            ok = ((op == ">=" and v >= t) or (op == ">" and v > t)
                  or (op == "<=" and v <= t) or (op == "<" and v < t))
            if not ok:
                return False
    return True


def load_merged():
    feats = pd.read_parquet(FEAT_PATH)
    conn = __import__("storage_signal").connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT stock_code, signal_date, signal_type, entry_date, exit_date, window, "
        "return_pct, is_win, confirm_date FROM backtest WHERE scope='signal'")]
    conn.close()
    bt_df = pd.DataFrame(rows)
    m = feats.merge(bt_df, on=["stock_code", "signal_date", "signal_type"], how="inner")
    return m


def baseline_per_code(m, window):
    """每只股票一个「全区间无条件基准」。**只算一次**，所有分组共用。

    这里刻意不做「按分组各自的时间跨度重算基准」—— 那需要为每个分组跑一遍
    逐日 evaluate，6 条假设 × 2 组会把耗时推到分钟级，而分组在时间上是交错的，
    用全区间基准不会引入方向性偏差。
    """
    lo, hi = m["entry_date"].min(), m["entry_date"].max()
    out = {}
    for c in sorted(m["stock_code"].unique()):
        b = ve.unconditional(c, window, lo, hi)
        if b[0] is not None:
            out[c] = b[0]
    return out


def excess_of(sub, base, window):
    rets = sub["return_pct"].tolist()
    d = bt.direction_of(sub["signal_type"].iloc[0])
    codes = sub["stock_code"]
    num = sum(d * base[c] for c in codes if c in base)
    den = sum(1 for c in codes if c in base)
    if den == 0:
        return None
    return rets, sum(rets) / len(rets) - num / den


def cmd_test(args):
    hp = hyp_path(args.round)
    if not hp.exists():
        print("还没有预注册假设，先跑：python verify_hypotheses.py gen --round %d" % args.round)
        return 1
    rec = json.loads(hp.read_text(encoding="utf-8"))
    hs = rec["hypotheses"]
    m = load_merged()
    w = args.window
    m = m[m["window"] == w]
    base = baseline_per_code(m, w)
    print("=== 数据裁决 ===")
    print("假设来源：%s（第 %s 轮，%s，%s）"
          % (hp, rec.get("round", 1), rec.get("generated_at"), rec.get("model")))
    print("样本：%d 条回测记录 / %d 只股票 / 窗口 %d 日 / 基准股 %d 只"
          % (len(m), m["stock_code"].nunique(), w, len(base)))
    print("多重比较：测 %d 条，95%% 置信水平下**期望有 %.1f 条纯靠运气显著**；"
          "Bonferroni 阈值约 α=%.4f" % (len(hs), 0.05 * len(hs), 0.05 / len(hs)))
    print()

    cut = ve.split_point([{"entry_date": d} for d in m["entry_date"]], 0.6)
    print("  %-4s %-7s %-7s %-10s %-7s %-10s %-10s %-20s %-9s %s"
          % ("id", "满足n", "不满足n", "满足超额", "不满足", "超额差", "超额差 95%CI",
             "分半一致性", "扣费后", "判断"))
    print("       （超额差 = 满足超额 − 不满足超额；CI 由两组均值差的 Welch 区间等宽平移而来）")
    results = []
    for h in hs:
        hi = m[m.apply(lambda r: match(r, h["when"]), axis=1)]
        lo = m[~m.apply(lambda r: match(r, h["when"]), axis=1)]
        if len(hi) < MIN_GROUP or len(lo) < MIN_GROUP:
            print("  %-4s 分组太小（%d / %d），跳过" % (h["id"], len(hi), len(lo)))
            results.append({"id": h["id"], "verdict": "分组太小"})
            continue
        ea = excess_of(hi, base, w)
        eb = excess_of(lo, base, w)
        if ea is None or eb is None:
            continue
        rets_a, exc_a = ea
        rets_b, exc_b = eb
        d, dlo, dhi = welch_diff_ci(rets_a, rets_b)
        # ⚠️ welch_diff_ci 给的是「**均值**差」，而旁边两列是「**超额**」（各自减了自己的基准）。
        # 两者相差 = 两组基准之差。基准对给定分组是**常数**，所以区间宽度不变、只需平移中心。
        # 报「超额差」才和旁边的列一致 —— 否则读者会看到 3.19% vs 1.00% 却写着差 0.55%。
        d_exc = exc_a - exc_b
        shift = d_exc - d
        dlo, dhi = dlo + shift, dhi + shift
        d = d_exc
        # 分半验证
        a1 = hi[hi["entry_date"] <= cut]
        a2 = hi[hi["entry_date"] > cut]
        b1 = lo[lo["entry_date"] <= cut]
        b2 = lo[lo["entry_date"] > cut]
        halves = []
        for x1, y1 in ((a1, b1), (a2, b2)):
            if len(x1) < 30 or len(y1) < 30:
                halves.append(None)
                continue
            r1, e1 = excess_of(x1, base, w)
            r2, e2 = excess_of(y1, base, w)
            halves.append(e1 - e2)
        hh = [q for q in halves if q is not None]
        same = bool(hh) and all((q > 0) == (d > 0) for q in hh)
        expected = (h["predict"] == "high" and d > 0) or (h["predict"] == "low" and d < 0)
        sig = not (dlo <= 0 <= dhi)
        if not sig:
            verdict = "不显著"
        elif not same:
            verdict = "显著但分半不一致"
        elif expected:
            verdict = "✓ 成立（方向符合预测）"
        else:
            verdict = "✗ 方向与预测相反"
        print("  %-4s %-7d %-7d %-10s %-7s %-10s %-10s %-20s %-9s %s"
              % (h["id"], len(hi), len(lo), pct(exc_a), pct(exc_b), pct(d),
                 "[%s, %s]" % (pct(dlo), pct(dhi)),
                 "%s/%s" % (pct(hh[0]) if len(hh) > 0 else "—",
                            pct(hh[1]) if len(hh) > 1 else "—"),
                 pct(exc_a - COST), verdict))
        results.append({"id": h["id"], "n_hi": len(hi), "n_lo": len(lo),
                        "exc_hi": exc_a, "exc_lo": exc_b, "diff": d,
                        "ci": [dlo, dhi], "halves": hh, "verdict": verdict,
                        "predict": h["predict"], "hypothesis": h["hypothesis"]})

    print()
    ok = [r for r in results if r.get("verdict", "").startswith("✓")]
    print("=== 汇总 ===")
    print("  %d 条假设：成立 %d 条，显著但分半不一致 %d 条，不显著 %d 条，方向相反 %d 条，分组太小 %d 条"
          % (len(results), len(ok),
             sum(1 for r in results if r.get("verdict") == "显著但分半不一致"),
             sum(1 for r in results if r.get("verdict") == "不显著"),
             sum(1 for r in results if r.get("verdict", "").startswith("✗")),
             sum(1 for r in results if r.get("verdict") == "分组太小")))
    for r in ok:
        print("  ✓ [%s] %s   差值 %s CI [%s, %s]" % (
            r["id"], r["hypothesis"], pct(r["diff"]), pct(r["ci"][0]), pct(r["ci"][1])))
    print()
    print("  提醒：即使上面前 5 条全部成立，也要对照「期望 %.1f 条靠运气显著」这个基准再下结论。"
          % (0.05 * len(hs)))
    hyp_result_path(args.round).write_text(
        json.dumps({"tested_at": time.strftime("%Y-%m-%d %H:%M:%S"), "window": w,
                    "n_rows": len(m), "bonferroni_alpha": 0.05 / len(hs),
                    "expected_false_positives": 0.05 * len(hs),
                    "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  结果已存 %s" % hyp_result_path(args.round))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--round", type=int, default=1)
    g.add_argument("--timeout", type=float, default=240)
    g.add_argument("--carry-ids", default=None,
                   help="把上一轮这几条假设原样带进本轮重测，如 H4,H6")
    t = sub.add_parser("test")
    t.add_argument("--round", type=int, default=1)
    t.add_argument("--window", type=int, default=WINDOW)
    a = ap.parse_args()
    return cmd_gen(a) if a.cmd == "gen" else cmd_test(a)


if __name__ == "__main__":
    sys.exit(main())
