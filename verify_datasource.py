"""数据源一致性检查（ADR-039）—— 接了别的源之后，**自己判断接得对不对**。

用法：

    python verify_datasource.py --provider local_csv
    python verify_datasource.py --provider local_csv --codes 600519,000001
    python verify_datasource.py --provider local_csv --start 2024-01-01 --end 2026-09-19

三件事，前两件查「接得对不对」，第三件查「**结论会不会变**」：

    1 契约检查    你的返回符不符合 datasource.py 里的约定
    2 交叉对比    与内置源在重叠区间的价格 / 因子偏差，并指出差在哪些日子
    3 信号一致性  同一套缠论规则，两个源给出的买卖点差多少  <- 最有说服力的一条

第 3 条才是重点：价格差 0.3% 看着无所谓，但它可能让某一笔多走一根 K 线，
整段笔 / 中枢重划，买卖点全变。**只有信号一致率才能说明换源后结论还站不站得住。**
"""
import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

import datasource as ds
import structure_gap as sg
from min_loop import build_signals
from storage_kline import merge_factor, to_qfq

OUT_PATH = Path("config/datasource_check.json")
DEFAULT_CODES = ["600519", "000001", "002594", "300750",
                 "601899", "688981", "600036", "000858"]

# 判据：价格差多少还算「同一个源」，信号一致率多少还算「结论不变」
PRICE_OK_MEDIAN = 0.001          # 中位相对偏差 0.1%
PRICE_OK_P95 = 0.01              # p95 相对偏差 1%
SIGNAL_OK = 0.90                 # 信号 Jaccard 一致率 90%


def contract_check(df):
    """检查返回是否符合 datasource.py 的约定。返回问题列表（空 = 通过）。"""
    if df is None:
        return ["返回了 None —— 空数据的约定是返回列齐全的空表"]
    if not isinstance(df, pd.DataFrame):
        return ["返回的不是 DataFrame，是 %s" % type(df).__name__]
    miss = [c for c in ds.RAW_COLUMNS if c not in df.columns]
    if miss:
        return ["缺列 %s（需要 %s）" % (miss, ds.RAW_COLUMNS)]
    if len(df) == 0:
        return []
    bad = []
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        bad.append("date 不是 datetime64（实际 %s）" % df["date"].dtype)
    d = pd.to_datetime(df["date"], errors="coerce")
    if d.isna().any():
        bad.append("date 有 NaT（%d 个）" % int(d.isna().sum()))
    if not d.is_monotonic_increasing:
        bad.append("date 不是升序")
    if d.duplicated().any():
        bad.append("date 有重复（%d 个）" % int(d.duplicated().sum()))
    for c in ("open", "high", "low", "close", "volume", "amount"):
        v = pd.to_numeric(df[c], errors="coerce")
        if v.isna().any():
            bad.append("%s 有 NaN / 非数值（%d 个）" % (c, int(v.isna().sum())))
    o = pd.to_numeric(df["open"], errors="coerce")
    h = pd.to_numeric(df["high"], errors="coerce")
    l = pd.to_numeric(df["low"], errors="coerce")
    c = pd.to_numeric(df["close"], errors="coerce")
    if (c <= 0).any():
        bad.append("close 有 <= 0 的值（%d 个）" % int((c <= 0).sum()))
    if (pd.to_numeric(df["volume"], errors="coerce") < 0).any():
        bad.append("volume 有负值")
    hb = h < pd.concat([o, c, l], axis=1).max(axis=1) - 1e-6
    if hb.any():
        bad.append("high < max(open,close,low)（%d 行）" % int(hb.sum()))
    lb = l > pd.concat([o, c, h], axis=1).min(axis=1) + 1e-6
    if lb.any():
        bad.append("low > min(open,close,high)（%d 行）" % int(lb.sum()))
    return bad


def frame_of(src, code, start, end):
    """取一只股票的 (不复权+因子, 前复权)。任何一步失败返回 (None, None, 原因)。"""
    start, end = pd.Timestamp(start), pd.Timestamp(end)   # 协议里 start/end 是可被 Timestamp 解析的值
    try:
        raw = src.kline(code, start, end)
    except ds.NotSupported as e:
        return None, None, str(e)
    except Exception as e:
        return None, None, "kline 抛异常：%s: %s" % (type(e).__name__, e)
    raw = ds.normalize_kline(raw)
    if len(raw) == 0:
        return raw, raw, "这个区间没有数据"
    fac = None
    if src.supports("factor"):
        try:
            fac = src.factor(code, start, end)
        except Exception:
            fac = None
    merged = merge_factor(raw, ds.normalize_factor(fac) if fac is not None else None)
    return merged, to_qfq(merged), None


def signals_of(q, code):
    """在给定前复权帧上跑同一套规则。与 market_scan 走的是同一条路径。"""
    try:
        bis, zss = sg.structure_at(code, q, len(q) - 1)
        return {(str(r["date"]), r["type"]) for r in build_signals(bis, zss)}
    except Exception:
        return set()


def compare_code(provider, builtin, code, start, end):
    """比一只股票。返回一条结构化结果。"""
    out = {"code": code}
    praw, pq, perr = frame_of(provider, code, start, end)
    braw, bq, berr = frame_of(builtin, code, start, end)
    out["provider_error"], out["builtin_error"] = perr, berr
    out["n_provider"] = int(len(praw)) if praw is not None else 0
    out["n_builtin"] = int(len(braw)) if braw is not None else 0
    out["contract"] = contract_check(praw)
    if praw is None or braw is None or len(praw) == 0 or len(braw) == 0:
        return out

    ps, bs = set(praw["date"]), set(braw["date"])
    out["dates_only_provider"] = len(ps - bs)
    out["dates_only_builtin"] = len(bs - ps)
    m = praw[["date", "close"]].merge(braw[["date", "close"]], on="date",
                                      suffixes=("_p", "_b"))
    if len(m):
        r = (m["close_p"] / m["close_b"] - 1).abs()
        out["raw_close"] = {"n": int(len(m)), "median": float(r.median()),
                            "p95": float(r.quantile(0.95)), "max": float(r.max())}
        w = m.assign(rel=r).nlargest(3, "rel")
        out["worst"] = [{"date": str(x["date"])[:10], "provider": float(x["close_p"]),
                         "builtin": float(x["close_b"]), "rel": float(x["rel"])}
                        for _, x in w.iterrows()]

    mq = pq[["date", "close"]].merge(bq[["date", "close"]], on="date",
                                     suffixes=("_p", "_b"))
    if len(mq):
        rq = (mq["close_p"] / mq["close_b"] - 1).abs()
        out["qfq_close"] = {"n": int(len(mq)), "median": float(rq.median()),
                            "p95": float(rq.quantile(0.95)), "max": float(rq.max())}

    # 信号一致性：**先把两边对齐到共同日期**，把「覆盖差」与「价格差」分开
    common = sorted(ps & bs)
    a = pq[pq["date"].isin(common)].reset_index(drop=True)
    b = bq[bq["date"].isin(common)].reset_index(drop=True)
    if len(common) >= 60:
        ca, cb = signals_of(a, code), signals_of(b, code)
        union = ca | cb
        out["signals_provider"], out["signals_builtin"] = len(ca), len(cb)
        out["jaccard"] = (len(ca & cb) / len(union)) if union else 1.0
        out["only_provider"] = sorted("%s %s" % x for x in (ca - cb))[:5]
        out["only_builtin"] = sorted("%s %s" % x for x in (cb - ca))[:5]
    return out


def run(provider_name, codes, start, end):
    ds.discover()
    if ds.load_errors():
        print("[!] provider 加载出错：")
        for name, err in ds.load_errors():
            print("     %s: %s" % (name, err))
    provider = ds.get(provider_name)
    builtin = ds.get(ds.DEFAULT_NAME)
    print("=== 数据源一致性检查 ===")
    print("  被测源：%s  (%s)" % (provider.name, provider.description))
    print("  参照源：%s  (%s)" % (builtin.name, builtin.description))
    print("  区间：%s ~ %s   股票：%d 只" % (start, end, len(codes)))
    if not provider.supports("factor"):
        print("  注：该源不提供复权因子，按 1.0 处理（等于不复权）")
    print()

    rows = []
    for c in codes:
        t = time.time()
        r = compare_code(provider, builtin, c, start, end)
        r["seconds"] = round(time.time() - t, 2)
        rows.append(r)
        pm = (r.get("qfq_close") or {}).get("median")
        j = r.get("jaccard")
        print("  %-8s 契约 %-22s 前复权偏差中位 %-9s 信号一致 %s"
              % (c, "OK" if not r["contract"] else r["contract"][0][:22],
                 ("%.4f%%" % (100 * pm)) if pm is not None else "—",
                 ("%.1f%%" % (100 * j)) if j is not None else "—"))
        if r.get("provider_error"):
            print("           -> %s" % r["provider_error"][:110])

    ok = [r for r in rows if not r["contract"]]
    med = [r["qfq_close"]["median"] for r in rows if r.get("qfq_close")]
    p95 = [r["qfq_close"]["p95"] for r in rows if r.get("qfq_close")]
    js = [r["jaccard"] for r in rows if r.get("jaccard") is not None]
    verdict = {}
    print()
    print("=== 结论 ===")
    print("  契约通过 %d/%d" % (len(ok), len(rows)))
    if med:
        mm, pp = max(med), max(p95)
        print("  前复权收盘：最大中位偏差 %.4f%%   最大 p95 偏差 %.4f%%" % (100 * mm, 100 * pp))
        verdict["price_ok"] = bool(mm <= PRICE_OK_MEDIAN and pp <= PRICE_OK_P95)
    if js:
        wj = min(js)
        print("  信号一致率：最低 %.1f%%（平均 %.1f%%）" % (100 * wj, 100 * sum(js) / len(js)))
        verdict["signal_ok"] = bool(wj >= SIGNAL_OK)
    print()
    if verdict.get("signal_ok"):
        print("  [OK] 换成这个源之后，**缠论买卖点基本不变** -> 既有结论可以照用。")
    elif js:
        print("  [!] 买卖点会变。**换个源结论就不一样**，不能直接沿用本项目的统计结论；")
        print("      要么把你的源当独立样本重新验证，要么先查清分歧原因。")
    else:
        print("  [i] 这几种股票上算不出信号一致率（数据太短或拿不到），只能看契约与价格。")

    res = {"checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "provider": provider.name, "builtin": builtin.name,
           "start": str(start), "end": str(end), "codes": codes,
           "thresholds": {"price_median": PRICE_OK_MEDIAN, "price_p95": PRICE_OK_P95,
                          "signal_jaccard": SIGNAL_OK},
           "verdict": verdict, "rows": rows}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  明细已存 %s" % OUT_PATH)
    return 0 if not any(r["contract"] for r in rows) else 1


# ============ 规则对数据精度的敏感度 ============

NOISE_SIGMAS = (0.0, 0.0002, 0.0005, 0.001, 0.003)


class NoisySource(ds.DataSource):
    """把任意数据源包一层，逐日加正态噪声。

    用途不是「模拟一个坏源」，而是量出一个**关于规则本身**的事实：
    数据精度差多少，买卖点就会变多少。这与用哪个源无关。
    """

    def __init__(self, inner, sigma, seed=7):
        self.inner, self.sigma, self.seed = inner, sigma, seed
        self.name, self.description = inner.name, inner.description
        self.provides = inner.provides

    def kline(self, code, start, end):
        import zlib

        import numpy as np
        df = ds.normalize_kline(self.inner.kline(code, start, end))
        if len(df) == 0 or not self.sigma:
            return df
        # 用代码派生种子：同一只股票每次跑都一样，结果可复现
        rs = np.random.default_rng(zlib.crc32(str(code).encode()) + self.seed)
        for c in ("open", "high", "low", "close"):
            df[c] = df[c] * (1 + rs.normal(0, self.sigma, len(df)))
        df["high"] = np.maximum(df["high"], df[["open", "close", "low"]].max(axis=1))
        df["low"] = np.minimum(df["low"], df[["open", "close", "high"]].min(axis=1))
        return df

    def factor(self, code, start, end):
        return self.inner.factor(code, start, end)


def noise_curve(codes, start, end, sigmas=NOISE_SIGMAS):
    """逐日噪声 -> 信号一致率。回答「数据要准到什么程度，买卖点才不会变」。"""
    builtin = ds.get(ds.DEFAULT_NAME)
    print("=== 规则对数据精度的敏感度 ===")
    print("  对内置源逐日加正态噪声，再看缠论买卖点变了多少")
    print("  %-12s %-16s %-12s %s" % ("逐日噪声 sigma", "前复权偏差中位", "信号一致率", "每只"))
    rows = []
    for sigma in sigmas:
        prov = NoisySource(builtin, sigma)
        js, ms = [], []
        for c in codes:
            r = compare_code(prov, builtin, c, start, end)
            if r.get("jaccard") is not None:
                js.append(r["jaccard"])
            if r.get("qfq_close"):
                ms.append(r["qfq_close"]["median"])
        row = {"sigma": sigma,
               "deviation_median": max(ms) if ms else None,
               "jaccard": (sum(js) / len(js)) if js else None,
               "per_code": js}
        rows.append(row)
        print("  %-14s %-16s %-12s %s" % (
            "%.2f%%" % (100 * sigma),
            "%.4f%%" % (100 * max(ms)) if ms else "—",
            "%.1f%%" % (100 * row["jaccard"]) if js else "—",
            " ".join("%.0f%%" % (100 * x) for x in js)))
    print()
    print("  这一栏与用哪个数据源**无关** —— 它是规则本身的脆性。")
    print("  读法：如果两套源之间只差 0.05%，买卖点就已经变了 24%，")
    print("        那么「今天这只股票有第 X 类买点」就是一句**很脆**的话。")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None, help="providers/ 里的数据源名字")
    ap.add_argument("--noise", action="store_true",
                    help="额外测「规则对数据精度的敏感度」曲线")
    ap.add_argument("--codes", default=None, help="逗号分隔；默认用一组大盘股")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end", default=None, help="默认 = 今天")
    a = ap.parse_args()
    codes = [c.strip() for c in a.codes.split(",")] if a.codes else DEFAULT_CODES
    end = a.end or str(pd.Timestamp.today().date())
    ds.discover()
    rc = 0
    if a.provider:
        rc = run(a.provider, codes, a.start, end)
    elif not a.noise:
        print("要么给 --provider，要么给 --noise")
        return 2
    if a.noise:
        print()
        rows = noise_curve(codes, a.start, end)
        p = Path("config/datasource_noise.json")
        p.write_text(json.dumps({"checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "codes": codes, "rows": rows},
                                ensure_ascii=False, indent=2), encoding="utf-8")
        print("  明细已存 %s" % p)
    return rc


if __name__ == "__main__":
    sys.exit(main())