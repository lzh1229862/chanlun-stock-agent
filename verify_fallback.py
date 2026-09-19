"""verify_fallback.py —— T7-1b「数据兜底」验收脚本。

覆盖 5 个用例：
  T1  本地数据最新          -> 命中缓存，一次网络请求都不发
  T2  删掉本地 Parquet      -> analyze_stock 自动拉取并成功出结果
  T3  拉取时抛异常（断网）  -> 返回结构化错误，不抛出、不崩溃
  T4  本地为空 + 关闭拉取   -> 明确提示「未开启自动拉取」
  T5  本地数据过期          -> 增量更新补齐到最新交易日

用法：
  python verify_fallback.py             全跑（T2 / T5 需要联网）
  python verify_fallback.py --offline   跳过需要联网的用例
  python verify_fallback.py --code 000001 --date 2026-09-18
"""
import argparse
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd

import analyzer
import storage_kline as sk

RESULTS = []
_ORIG_FETCH = None


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  [" + ("PASS" if ok else "FAIL") + "] " + name + (("  -> " + str(detail)) if detail else ""))
    return bool(ok)


def backup(code):
    p = sk.parquet_path(code)
    if not p.exists():
        return None
    tmp = Path(tempfile.mkdtemp()) / p.name
    shutil.copy2(p, tmp)
    return tmp


def restore(code, tmp):
    p = sk.parquet_path(code)
    if p.exists():
        p.unlink()
    if tmp is not None:
        shutil.copy2(tmp, p)


def block_network():
    def boom(*a, **k):
        raise ConnectionError("模拟断网：无法连接到数据源")
    sk.fetch_raw = boom
    sk.fetch_factor = boom


def unblock_network():
    sk.fetch_raw, sk.fetch_factor = _ORIG_FETCH


def main():
    global _ORIG_FETCH
    _ORIG_FETCH = (sk.fetch_raw, sk.fetch_factor)

    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="跳过需要联网的用例")
    ap.add_argument("--code", default="600519")
    ap.add_argument("--date", default=str(date.today()))
    args = ap.parse_args()
    code, day = args.code, args.date
    print("标的 " + code + "    报告日期 " + day)
    print()

    # ---------------- T1 缓存命中：连网络都掐掉，仍应成功 ----------------
    print("T1 本地数据最新 -> 命中缓存，不发网络请求")
    block_network()
    try:
        src, err = analyzer.ensure_kline(code, day)
        check("ensure_kline 返回 cache", src == "cache", "data_source=" + str(src) + " data_error=" + str(err))
        res = analyzer.analyze_stock(code, day, use_llm=False)
        check("analyze_stock 成功", bool(res.get("ok")), str(res.get("error")))
        check("data_source == cache", res.get("data_source") == "cache", "data_source=" + str(res.get("data_source")))
        check("无 data_error", not res.get("data_error"), str(res.get("data_error")))
    except Exception as e:
        check("T1 不应抛异常", False, type(e).__name__ + ": " + str(e))
    finally:
        unblock_network()

    if args.offline:
        print()
        print("（--offline：跳过 T2 / T5）")
        return summary()

    # ---------------- T2 删本地数据 -> 自动拉取 ----------------
    print()
    print("T2 删除本地 Parquet -> analyze_stock 自动拉取")
    tmp = backup(code)
    p = sk.parquet_path(code)
    if p.exists():
        p.unlink()
    try:
        check("删除成功，本地为空", analyzer.load_kline(code).empty, str(p))
        res = analyzer.analyze_stock(code, day, use_llm=False)
        check("analyze_stock 未崩溃且成功", bool(res.get("ok")), str(res.get("error")))
        check("data_source == fetched", res.get("data_source") == "fetched", "data_source=" + str(res.get("data_source")))
        check("Parquet 已重建", p.exists(), str(p))
        post = analyzer.load_kline(code)
        check("重建后确实有 K 线", not post.empty, "rows=" + str(len(post)))
        want = analyzer._expected_last_trading_day(day)
        got = None if post.empty else post["date"].max()
        check("已更新到最新交易日", got is not None and want is not None and got >= want,
              "本地末日=" + str(None if got is None else got.date()) + " 期望>=" + str(None if want is None else want.date()))
    except Exception as e:
        check("T2 不应抛异常", False, type(e).__name__ + ": " + str(e))
    finally:
        restore(code, tmp)

    # ---------------- T3 断网 ----------------
    print()
    print("T3 断网（拉取抛异常）-> 返回结构化错误，不崩溃")
    tmp = backup(code)
    if p.exists():
        p.unlink()
    block_network()
    try:
        src, err = analyzer.ensure_kline(code, day)
        check("ensure_kline 返回 None", src is None, "data_source=" + str(src))
        check("data_error 带失败原因", bool(err), str(err))
        res = analyzer.analyze_stock(code, day, use_llm=False)
        check("analyze_stock 未抛异常（返回了 dict）", isinstance(res, dict))
        check("ok == False", res.get("ok") is False, "ok=" + str(res.get("ok")))
        check("error 为结构化文案", "无法获取 K 线数据" in str(res.get("error")), str(res.get("error")))
        check("data_error 同时透出到结果", bool(res.get("data_error")), str(res.get("data_error")))
        check("data_source 为 None", res.get("data_source") is None, str(res.get("data_source")))
    except Exception as e:
        check("T3 不应抛异常", False, type(e).__name__ + ": " + str(e))
    finally:
        restore(code, tmp)
        unblock_network()

    # ---------------- T4 关闭兜底 ----------------
    print()
    print("T4 本地为空 + allow_fetch=False -> 明确提示未开启自动拉取")
    tmp = backup(code)
    if p.exists():
        p.unlink()
    try:
        res = analyzer.analyze_stock(code, day, use_llm=False, allow_fetch=False)
        check("ok == False", res.get("ok") is False)
        check("提示未开启自动拉取", "未开启自动拉取" in str(res.get("data_error")), str(res.get("data_error")))
    except Exception as e:
        check("T4 不应抛异常", False, type(e).__name__ + ": " + str(e))
    finally:
        restore(code, tmp)

    # ---------------- T5 增量更新 ----------------
    print()
    print("T5 本地数据过期 -> 增量补齐（只拉缺口）")
    tmp = backup(code)
    try:
        full = sk.load_kline(code)
        if len(full) < 60:
            check("本地样本足够", False, "rows=" + str(len(full)))
        else:
            cut = full.iloc[:-20].copy()
            sk.save_kline(code, cut)
            stale_last = sk.load_kline(code)["date"].max()
            src, err = analyzer.ensure_kline(code, day)
            merged = sk.load_kline(code)
            check("data_source == fetched", src == "fetched", "data_source=" + str(src) + " data_error=" + str(err))
            check("行数增加", len(merged) > len(cut), str(len(cut)) + " -> " + str(len(merged)))
            check("最后交易日推进", merged["date"].max() > stale_last,
                  str(stale_last.date()) + " -> " + str(merged["date"].max().date()))
    except Exception as e:
        check("T5 不应抛异常", False, type(e).__name__ + ": " + str(e))
    finally:
        restore(code, tmp)

    return summary()


def summary():
    print()
    bad = [r for r in RESULTS if not r[1]]
    print("共 " + str(len(RESULTS)) + " 项，通过 " + str(len(RESULTS) - len(bad)) + "，失败 " + str(len(bad)))
    for n, _ok, d in bad:
        print("   FAIL  " + n + "   " + str(d))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
