"""verify_claims.py —— 用时间样本外体检既有结论（B1 / ADR-029）。

### 为什么

ADR-024 用打分证明了「**样本内比样本外乐观 30%**」。但那个 30% 只验证过打分一个对象。
而 ADR-019 / ADR-020 里那些**头条结论全都是全样本的**，从没做过样本外：

- 六类信号超额 +1.15%~+1.59% 且 CI 不跨 0
- 买点在上行/震荡/下行三状态都有真 alpha
- 卖点只在上涨状态显著
- 随机 35 只 vs 知名 10 只无差异

**本工具就是给它们逐个做体检。** 结论可能被推翻 —— 越早越好。

### 做法（沿用 ADR-021 的纪律）

    prereg   把每条结论 + 通过标准**先落盘**（config/claims_prereg.json）
    test     只读那个文件，在**检验期 2021-2026**上逐条判定

- 训练期不参与判定，只是用来对照「样本内有多乐观」
- 基准在**检验期自己的跨度内**重算（与 verify_edge 一致）
- 通过标准：**CI 不跨 0 且方向正确**，并附分半一致性
"""
import argparse
import json
import sys
import time
from pathlib import Path

import backtest as bt
import market_regime as mr
import storage_index as si
import verify_edge as ve
import verify_hypotheses as vh
from verify_score import TEST, TRAIN

PREREG = Path("config/claims_prereg.json")
RESULT = Path("config/claims_prereg_results.json")
WINDOW = 5

TYPES = ["第一类买点", "第一类卖点", "第二类买点", "第二类卖点", "第三类买点", "第三类卖点"]
REGIMES = ("上行", "震荡", "下行")


def prereg():
    claims = [
        {"id": "C%d" % (i + 1), "desc": "%s 在检验期 5 日超额 > 0 且 CI 不跨 0" % t,
         "kind": "type_excess", "signal_type": t}
        for i, t in enumerate(TYPES)
    ]
    claims += [
        {"id": "C7", "desc": "买点真 alpha（vs 沪深300）在上行/震荡/下行三状态均 > 0 且 CI 不跨 0",
         "kind": "buy_alpha_all_regimes"},
        {"id": "C8", "desc": "卖点真 alpha 只在「上行」显著，震荡/下行 CI 跨 0",
         "kind": "sell_alpha_only_up"},
        {"id": "C9", "desc": "随机 35 只 vs 知名 10 只，差值 CI 跨 0（无显著差异）",
         "kind": "random_vs_famous"},
    ]
    rec = {
        "preregistered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "question": "ADR-019 / ADR-020 的那些全样本结论，在检验期还站得住吗？",
        "train_period": list(TRAIN), "test_period": list(TEST), "window": WINDOW,
        "pass_criteria": "只看检验期：CI 不跨 0（C9 反过来要求跨 0）+ 方向正确 + 分半一致",
        "claims": claims,
    }
    PREREG.parent.mkdir(parents=True, exist_ok=True)
    PREREG.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已预注册 %d 条结论 -> %s" % (len(claims), PREREG))
    for c in claims:
        print("   [%s] %s" % (c["id"], c["desc"]))
    return 0


def alpha_vals(sub, idx_map):
    """真 alpha = 方向调整后的信号收益 − 方向调整后的指数收益。

    ⚠️ 注意：backtest 表里的 return_pct **已经是方向调整后的**
    （evaluate 里 return_pct = direction * (close/entry - 1)，卖点为正 = 股价在跌）。
    所以这里只能写 return_pct - d*ir，**不能再乘一次 d** —— 那对卖点等于把方向乘了两次。
    早期版本就犯了这个错，把「卖点 alpha 全负」这个假结论造了出来。
    """
    out = []
    for r in sub.itertuples():
        ir = ve.index_return(idx_map, r.entry_date, r.exit_date)
        if ir is None:
            continue
        d = bt.direction_of(r.signal_type)
        out.append(r.return_pct - d * ir)
    return out


def excess_vals(sub, base):
    """逐条信号的超额 = 方向调整后的信号收益 − 方向调整后的同股票基准。"""
    d = bt.direction_of(sub["signal_type"].iloc[0])
    return [r.return_pct - d * base[r.stock_code]
            for r in sub.itertuples() if r.stock_code in base]


def half_consistency(sub, idx_map, kind, key=None):
    """分半：把样本按入场日中位切两段，看两段的指标符号是否一致。"""
    if sub.empty:
        return None
    cut = vh.ve.split_point([{"entry_date": x} for x in sub["entry_date"]], 0.5)
    vals = []
    for part in (sub[sub["entry_date"] <= cut], sub[sub["entry_date"] > cut]):
        if len(part) < 25:
            vals.append(None)
            continue
        if kind == "alpha":
            v = alpha_vals(part, idx_map)
            vals.append(ve.mean_ci(v)[0] if len(v) > 1 else None)
        else:
            vals.append(float(part["return_pct"].mean()))
    return vals


def period_slice(period, window=WINDOW):
    m = vh.load_merged()
    m = m[m["window"] == window]
    lo, hi = period
    return m[(m["entry_date"] >= lo) & (m["entry_date"] <= hi)].copy()


def run_claim(c, test_df, idx_map, base, reg_df):
    kind = c["kind"]
    if kind == "type_excess":
        sub = test_df[test_df["signal_type"] == c["signal_type"]]
        if len(sub) < 30:
            return {"verdict": "样本不足", "n": len(sub)}
        excs = excess_vals(sub, base)
        m, lo, hi = ve.mean_ci(excs)
        ok = (lo is not None and lo > 0)
        hh = half_consistency(sub, idx_map, "raw")
        hh_ok = bool([x for x in hh if x is not None]) and all(x > 0 for x in hh if x is not None)
        return {"n": len(sub), "mean": m, "lo": lo, "hi": hi,
                "sig": bool(lo is not None and (lo > 0 or hi < 0)),
                "halves": hh, "half_ok": hh_ok,
                "verdict": "成立" if (ok and hh_ok) else ("样本外不成立" if not ok else "分半不一致")}

    if kind == "buy_alpha_all_regimes":
        out, allok = {}, True
        # 按 regime 分组要用**信号日**的市场状态（不是入场日）
        buys = test_df[test_df["signal_type"].str.contains("买点")].copy()
        buys["_rg"] = [mr.regime_on(reg_df, d) for d in buys["signal_date"]]
        for rg in REGIMES:
            v = alpha_vals(buys[buys["_rg"] == rg], idx_map)
            if len(v) < 30:
                out[rg] = {"n": len(v), "alpha": None, "ok": False}
                allok = False
                continue
            m, lo, hi = ve.mean_ci(v)
            ok = lo is not None and lo > 0
            allok = allok and ok
            out[rg] = {"n": len(v), "alpha": m, "lo": lo, "hi": hi, "ok": ok}
        return {"detail": out, "verdict": "成立" if allok else "样本外不成立"}

    if kind == "sell_alpha_only_up":
        sells = test_df[test_df["signal_type"].str.contains("卖点")].copy()
        sells["_rg"] = [mr.regime_on(reg_df, d) for d in sells["signal_date"]]
        out, ok_all = {}, True
        for rg in REGIMES:
            v = alpha_vals(sells[sells["_rg"] == rg], idx_map)
            if len(v) < 30:
                out[rg] = {"n": len(v), "ok": False}
                ok_all = False
                continue
            m, lo, hi = ve.mean_ci(v)
            sig = not (lo <= 0 <= hi)
            out[rg] = {"n": len(v), "alpha": m, "lo": lo, "hi": hi, "sig": sig}
            if rg == "上行":
                ok_all = ok_all and sig and m > 0
            else:
                ok_all = ok_all and (not sig)
        return {"detail": out, "verdict": "成立" if ok_all else "样本外不成立"}

    if kind == "random_vs_famous":
        import watchlist_store as ws
        pool = set(ws.load_watchlist())
        allc = sorted(test_df["stock_code"].unique())
        rnd = [x for x in allc if x not in pool]
        a = test_df[test_df["stock_code"].isin(pool)]
        b = test_df[test_df["stock_code"].isin(rnd)]
        if len(a) < 30 or len(b) < 30:
            return {"verdict": "样本不足"}
        ra, _ = vh.excess_of(a, base, WINDOW)
        rb, _ = vh.excess_of(b, base, WINDOW)
        d, lo, hi = ve.welch_diff_ci(rb, ra)
        ok = lo <= 0 <= hi
        return {"n_famous": len(a), "n_random": len(b), "diff": d, "lo": lo, "hi": hi,
                "verdict": "成立（无显著差异）" if ok else "样本外出现显著差异"}
    return {"verdict": "未知类型"}


def test():
    if not PREREG.exists():
        print("先跑 prereg")
        return 1
    rec = json.loads(PREREG.read_text(encoding="utf-8"))
    idx_df, _, _ = si.ensure_index("000300")
    idx_map = ve.make_idx_map(idx_df)
    reg_df = mr.with_indicators(idx_df)

    print("=== 既有结论的时间样本外体检（B1）===")
    print("预注册：%s（%s）" % (PREREG, rec["preregistered_at"]))
    print("判定只看检验期 %s ~ %s，窗口 %d 日" % (TEST[0], TEST[1], WINDOW))
    print()
    results = {}
    te = period_slice(TEST)
    base_te = vh.baseline_per_code(te, WINDOW)
    print("  检验期样本 %d 条 / %d 只" % (len(te), te["stock_code"].nunique()))
    print()
    for c in rec["claims"]:
        r = run_claim(c, te, idx_map, base_te, reg_df)
        results[c["id"]] = r
        print("  [%s] %s" % (c["id"], c["desc"]))
        print("        -> %s" % r.get("verdict"))
        if "n" in r:
            print("           n=%s 超额=%s CI [%s, %s]  分半 %s"
                  % (r.get("n"), ve.pct(r.get("mean")), ve.pct(r.get("lo")),
                     ve.pct(r.get("hi")),
                     "/".join(ve.pct(x) if x is not None else "—" for x in (r.get("halves") or []))))
        if "detail" in r:
            for k, v in r["detail"].items():
                print("           %-4s n=%-4s alpha=%s CI [%s, %s] %s"
                      % (k, v.get("n"), ve.pct(v.get("alpha")), ve.pct(v.get("lo")),
                         ve.pct(v.get("hi")), v.get("ok", v.get("sig"))))
        if "diff" in r:
            print("           知名 n=%d 随机 n=%d 差值 %s CI [%s, %s]"
                  % (r["n_famous"], r["n_random"], ve.pct(r["diff"]),
                     ve.pct(r["lo"]), ve.pct(r["hi"])))
        print()
    ok = [k for k, v in results.items() if str(v.get("verdict", "")).startswith(("成立", "成立（"))]
    print("=== 汇总 ===")
    print("  %d 条结论：样本外成立 %d 条，不成立 %d 条，样本不足 %d 条"
          % (len(results), len(ok),
             sum(1 for v in results.values() if v.get("verdict") == "样本外不成立"),
             sum(1 for v in results.values() if v.get("verdict") == "样本不足")))
    print("  成立的：%s" % ("、".join(ok) if ok else "（无）"))
    RESULT.write_text(json.dumps({"tested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                  "window": WINDOW, "test_period": list(TEST),
                                  "results": results}, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print("  结果已存 %s" % RESULT)
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
