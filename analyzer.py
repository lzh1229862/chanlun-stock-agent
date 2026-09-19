"""公共分析函数：单只股票的结构化分析（CLI 与 UI 共用）。

设计约束
    - 【不改动】任何既有后端模块，只是把现有原语按与 agent_graph 节点
      【完全相同】的顺序串起来，保证 UI 与 CLI 的分析逻辑不分叉。
    - 默认【只读】：不写 signals / backtest / reports 表 —— UI 预览不应有副作用。
      （批量落库仍由 main.py -> agent_graph 负责）

复用关系（与 agent_graph 节点同源）
    数据    storage_kline.load_kline  /  confirm_dates.load_frames
    缠论    czsc.CZSC(min_bi_len=MIN_BI_LEN, max_bi_num=MAX_BI_NUM)
            + min_loop.build_zs / build_signals
    过滤    signal_filter.build_context / filter_signals
    三日期  confirm_dates.confirm_entry
    主次    mark_primary.decide_primary
    回测    backtest.query_backtest（读库，不重算）
    LLM     llm_client.summarize

对外接口
    analyze_stock(code, date=None, use_llm=True) -> dict   实时分析
    generate_llm_summary(result) -> dict                  按需生成 AI 总结（UI 按钮用）
    load_from_db(code, date=None) -> dict | None          先查历史（UI 第三步用）
"""
import time
from datetime import date as _date
from datetime import datetime
from datetime import timedelta

import pandas as pd

from backtest import query_backtest
from confirm_dates import confirm_entry, load_frames
from mark_primary import decide_primary
from min_loop import MAX_BI_NUM, MIN_BI_LEN, build_signals, build_zs
from signal_filter import board_of, build_context, fetch_name, filter_signals, limit_ratio
from storage_kline import load_kline, parquet_path, update_kline
from storage_report import query_reports
from storage_signal import query_signals


def _norm_code(code):
    return str(code).strip().zfill(6)


def _structure(code, q):
    """缠论结构（与 report_builder.stock_structure 同口径：前复权）。"""
    import czsc

    qq = q.rename(columns={"volume": "vol"}).copy()
    qq["dt"] = pd.to_datetime(qq["date"])
    qq["symbol"] = code
    bars = czsc.format_standard_kline(qq, freq=czsc.Freq.D)
    cz = czsc.CZSC(bars, min_bi_len=MIN_BI_LEN, max_bi_num=MAX_BI_NUM)
    bis = list(cz.bi_list)
    fxs = list(cz.fx_list)
    zss = build_zs(bis)
    close = float(q["close"].iloc[-1])

    pos, z = "无中枢", (zss[-1] if zss else None)
    if z:
        pos = "中枢上方" if close > z["zg"] else ("中枢下方" if close < z["zd"] else "中枢内部")
    return {
        "n_fx": len(fxs), "n_bi": len(bis), "n_zs": len(zss),
        "position": pos,
        "zs_list": [{"sdt": str(x["sdt"].date()), "edt": str(x["edt"].date()),
                     "zd": round(x["zd"], 4), "zg": round(x["zg"], 4),
                     "zz": round((x["zd"] + x["zg"]) / 2, 4), "n": x["n"]} for x in zss],
        "bi_list": [{"sdt": str(b.sdt.date()), "edt": str(b.edt.date()),
                     "direction": str(b.direction), "low": round(b.low, 4),
                     "high": round(b.high, 4), "length": b.length,
                     "change": round(float(b.change), 6)} for b in bis],
        "fx_list": [{"dt": str(f.dt.date()), "mark": str(f.mark),
                     "fx": round(float(f.fx), 4)} for f in fxs],
    }, zss, bis


def _payload(result):
    """构造 LLM 输入（与 llm_client.build_payload_from_db 同形状）。"""
    st = result.get("structure") or {}
    z = (st.get("zs_list") or [None])[-1]
    return {
        "stock_code": result["code"], "stock_name": result.get("name", ""),
        "report_date": result["trade_date"],
        "structure": {
            "last_date": (result.get("kline") or {}).get("last_date"),
            "close": (result.get("kline") or {}).get("close_raw"),
            "position": st.get("position"), "n_bi": st.get("n_bi"), "n_zs": st.get("n_zs"),
            "zs": ({"sdt": z["sdt"], "edt": z["edt"], "zd": z["zd"], "zg": z["zg"]} if z else None),
            "bis": [{"sdt": b["sdt"], "edt": b["edt"], "direction": b["direction"],
                     "low": b["low"], "high": b["high"]}
                    for b in (st.get("bi_list") or [])[-3:]],
        },
        "signals": [{"signal_date": r["date"], "confirm_date": r.get("confirm_date"),
                     "entry_ref_price": r.get("entry_ref_price"), "signal_type": r["type"],
                     "is_tradable": r["is_tradable"], "is_primary": r.get("is_primary", 0),
                     "filter_version": r.get("filter_version", ""),
                     "signal_reason": r.get("reason", "")}
                    for r in (result.get("signals") or [])[-5:]],
        "backtest": result.get("backtest") or [],
    }


def _expected_last_trading_day(trade_date):
    """返回 <= trade_date 的最后一个交易日（判断本地数据是否最新用）。

    交易日历要联网取；取不到时返回 None 表示「无法判断」，由调用方决定怎么处理，
    这里绝不向上抛异常（否则完全断网时 analyze_stock 会崩，违背 ADR-015）。
    """
    import pandas as pd

    try:
        from signal_filter import trading_calendar
        prev = [d for d in trading_calendar() if d <= pd.Timestamp(trade_date)]
    except Exception:
        return None
    return prev[-1] if prev else None


def ensure_kline(code, trade_date, allow_fetch=True, years=2, verbose=False):
    """确保本地有该股 K 线且已更新到最新交易日（T7-1b 数据兜底）。

    返回 (data_source, data_error)：
        data_source = "cache"   本地已有且最新 -> 直接用，不拉取
                    = "fetched" 本次执行了拉取 / 增量更新
                    = None      本地无数据且拉取失败
        data_error  = None，或失败原因字符串
    """
    import pandas as pd

    df = load_kline(code)
    last_td = _expected_last_trading_day(trade_date)
    # last_td 为 None = 交易日历取不到。此时本地有数据就直接用：
    # 日历都取不到，网络多半也不通，再硬拉只会更慢，最后照样降级回缓存。
    if not df.empty and (last_td is None or df["date"].max() >= last_td):
        return "cache", None

    if not allow_fetch:
        if df.empty:
            return None, "本地无数据且未开启自动拉取"
        return "cache", None

    end = pd.Timestamp(trade_date)
    if df.empty:
        start = (end - timedelta(days=365 * years)).date()
    else:
        start = df["date"].min().date()
    try:
        _, st = update_kline(code, start, end, verbose=False)
    except Exception as e:
        reason = f"{type(e).__name__}: {str(e)[:150]}"
        if df.empty:
            return None, f"自动拉取失败（{reason}）"
        return "cache", f"增量更新失败，已用本地既有数据（{reason}）"

    n_fetched = int(st.get("fetched", 0))
    if verbose:
        print(f"    [ensure_kline] {code} 拉取 {n_fetched} 行")
    if load_kline(code).empty:
        return None, "拉取后本地仍无数据（可能是无效代码或数据源不可用）"
    return ("fetched" if n_fetched else "cache"), None


def analyze_stock(code, date=None, use_llm=True, verbose=False, allow_fetch=True):
    """实时分析单只股票，返回结构化结果 dict。

    code      6 位股票代码
    date      交易日/报告日，默认今天
    use_llm   是否调用 DeepSeek（False 则 llm 字段为 None，可稍后用 generate_llm_summary 补）

    本函数**绝不抛异常**：数据源不通、交易日历取不到、缠论库报错等任何内部失败，
    都会转成 ok=False + error 的结构化结果返回（ADR-015）。
    """
    t0 = time.perf_counter()
    code = _norm_code(code)
    trade_date = str(date or _date.today())
    try:
        return _analyze_impl(code, trade_date, use_llm=use_llm, verbose=verbose,
                             allow_fetch=allow_fetch)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        return {"code": code, "trade_date": trade_date, "source": "live", "ok": False,
                "error": f"分析过程异常（{msg}）", "elapsed": round(time.perf_counter() - t0, 2),
                "data_source": None, "data_error": msg}


def _analyze_impl(code, trade_date, use_llm=True, verbose=False, allow_fetch=True):
    t0 = time.perf_counter()
    out = {"code": code, "trade_date": trade_date, "source": "live", "ok": False,
           "error": None, "elapsed": None,
           "data_source": None, "data_error": None}

    try:
        name = fetch_name(code)
    except Exception:
        name = ""
    out.update({"name": name, "board": board_of(code), "is_st": "ST" in name,
                "limit_ratio": limit_ratio(code, "ST" in name)})

    # --- T7-1b 数据兜底：本地无数据 / 不是最新 -> 自动拉取或增量更新 ---
    data_source, data_error = ensure_kline(code, trade_date, allow_fetch=allow_fetch,
                                           verbose=verbose)
    out["data_source"], out["data_error"] = data_source, data_error
    if data_source is None:
        out["error"] = f"无法获取 K 线数据：{data_error}"
        out["elapsed"] = round(time.perf_counter() - t0, 2)
        return out

    raw, q = load_frames(code)
    if q.empty:
        out["data_source"] = None
        out["error"] = f"K 线数据为空（data_error={data_error}）"
        out["elapsed"] = round(time.perf_counter() - t0, 2)
        return out

    structure, zss, bis = _structure(code, q)
    out["structure"] = structure
    out["kline"] = {
        "rows": len(raw), "start": str(raw["date"].iloc[0].date()),
        "end": str(raw["date"].iloc[-1].date()),
        "last_date": str(raw["date"].iloc[-1].date()),
        "close_raw": round(float(raw["close"].iloc[-1]), 4),
        "close_qfq": round(float(q["close"].iloc[-1]), 4),
        "path": str(parquet_path(code)),
    }

    # 结构信号 -> 过滤（与 node_filter 完全一致）
    recs = build_signals(bis, zss)
    ctx = build_context(code)
    results = filter_signals(code, [{"date": r["date"], "type": r["type"], "reason": r["reason"]}
                                    for r in recs], raw, ctx)

    # 三日期 + 主次标记
    ce = confirm_entry(code, [{"signal_date": r["date"], "signal_type": r["type"]} for r in results])
    decisions, _ = decide_primary([{"stock_code": code, "signal_date": r["date"],
                                    "signal_type": r["type"]} for r in results])
    pmap = {(d["date"], d["type"]): d["is_primary"] for d in decisions}

    sigs = []
    for r in results:
        cd, price, note = ce.get((r["date"], r["type"]), (None, None, ""))
        sigs.append({
            "date": r["date"], "type": r["type"], "price": r.get("price"),
            "reason": r.get("reason", ""), "direction": "买" if "买点" in r["type"] else "卖",
            "is_tradable": r["is_tradable"], "filter_reason": r.get("filter_reason", ""),
            "filter_codes": r.get("filter_codes", ""),
            "is_primary": pmap.get((r["date"], r["type"]), 0),
            "confirm_date": cd, "entry_ref_price": price, "backfill_note": note,
        })
    out["signals"] = sigs
    out["tradable_count"] = sum(1 for s in sigs if s["is_tradable"])
    out["primary_count"] = sum(1 for s in sigs if s["is_primary"])

    # 回测统计（读库，只取本次出现的类型）
    types = {s["type"] for s in sigs}
    agg = {}
    for r in query_backtest(scope="signal"):
        if r["signal_type"] in types:
            agg.setdefault((r["window"], r["signal_type"]), []).append(r)
    bt = []
    for (w, t), v in sorted(agg.items()):
        rets = [x["return_pct"] for x in v]
        bt.append({"window": w, "signal_type": t, "n": len(v),
                   "avg_return": sum(rets) / len(rets),
                   "win_rate": sum(x["is_win"] for x in v) / len(v),
                   "avg_mdd": sum(x["max_drawdown"] for x in v) / len(v)})
    out["backtest"] = bt

    out["ok"] = True
    out["llm"] = None
    if use_llm:
        out["llm"] = generate_llm_summary(out, verbose=verbose)

    out["elapsed"] = round(time.perf_counter() - t0, 2)
    return out


def load_ohlc(code):
    """给图表用的前复权 OHLCV。UI 第 4 步的 K 线图数据源。"""
    raw, q = load_frames(code)
    if q.empty:
        return q
    return q[["date", "open", "high", "low", "close", "volume"]].copy()


def generate_llm_summary(result, verbose=False):
    """为已分析好的 result 生成 AI 总结（UI 的「生成 AI 总结」按钮调它）。"""
    try:
        from llm_client import summarize
        r = summarize(_payload(result), verbose=verbose)
        return {"text": r["text"], "tokens": (r["usage"] or {}).get("total_tokens", 0),
                "cost": r["cost"], "cached": r["cached"], "model": r["model"], "error": None}
    except Exception as e:
        return {"text": None, "tokens": 0, "cost": 0.0, "cached": False, "model": "",
                "error": f"{type(e).__name__}: {str(e)[:200]}"}


def load_from_db(code, date=None):
    """从 SQLite 读取已落库的分析结果（UI 第三步：先查历史）。

    返回与 analyze_stock 同形状的 dict；库里完全没有该股记录时返回 None。
    结构与 K 线仍实时计算（便宜且总是最新），信号/三日期/主次/LLM 来自库。
    """
    t0 = time.perf_counter()
    code = _norm_code(code)
    rows = query_signals(code=code)
    if not rows:
        return None

    trade_date = str(date or _date.today())
    try:
        name = fetch_name(code)
    except Exception:
        name = ""
    out = {"code": code, "trade_date": trade_date, "source": "history", "ok": True,
           "error": None, "name": name, "board": board_of(code), "is_st": "ST" in name,
           "limit_ratio": limit_ratio(code, "ST" in name),
           "data_source": None, "data_error": None}

    # --- T7-1b 数据兜底（历史模式也保证有 K 线可看）---
    data_source, data_error = ensure_kline(code, trade_date)
    out["data_source"], out["data_error"] = data_source, data_error

    raw, q = load_frames(code)
    if not q.empty:
        structure, _, _ = _structure(code, q)
        out["structure"] = structure
        out["kline"] = {"rows": len(raw), "start": str(raw["date"].iloc[0].date()),
                        "end": str(raw["date"].iloc[-1].date()),
                        "last_date": str(raw["date"].iloc[-1].date()),
                        "close_raw": round(float(raw["close"].iloc[-1]), 4),
                        "close_qfq": round(float(q["close"].iloc[-1]), 4),
                        "path": str(parquet_path(code))}

    rows.sort(key=lambda x: x["signal_date"])
    sigs = [{
        "date": r["signal_date"], "type": r["signal_type"], "price": None,
        "reason": r["signal_reason"], "direction": "买" if "买点" in r["signal_type"] else "卖",
        "is_tradable": r["is_tradable"], "filter_reason": "", "filter_codes": "",
        "is_primary": r["is_primary"], "confirm_date": r["confirm_date"],
        "entry_ref_price": r["entry_ref_price"], "backfill_note": r.get("backfill_note") or "",
        "filter_version": r.get("filter_version"),
    } for r in rows]
    out["signals"] = sigs
    out["tradable_count"] = sum(1 for s in sigs if s["is_tradable"])
    out["primary_count"] = sum(1 for s in sigs if s["is_primary"])

    types = {s["type"] for s in sigs}
    agg = {}
    for r in query_backtest(scope="signal"):
        if r["signal_type"] in types:
            agg.setdefault((r["window"], r["signal_type"]), []).append(r)
    bt = []
    for (w, t), v in sorted(agg.items()):
        rets = [x["return_pct"] for x in v]
        bt.append({"window": w, "signal_type": t, "n": len(v),
                   "avg_return": sum(rets) / len(rets),
                   "win_rate": sum(x["is_win"] for x in v) / len(v),
                   "avg_mdd": sum(x["max_drawdown"] for x in v) / len(v)})
    out["backtest"] = bt

    # LLM 总结取自 reports 表：取最近一条【含 LLM 文本】的归档
    # （不能直接取 rep[0]：某次用 --no-llm 跑出的归档会覆盖更晚的日期但内容是空的）
    out["llm"] = None
    for row in query_reports(stock_code=code):
        if row.get("llm_summary"):
            out["llm"] = {"text": row["llm_summary"], "tokens": 0, "cost": 0.0,
                              "cached": True, "model": "（来自归档）", "error": None,
                              "report_date": row["report_date"]}
            break
    out["elapsed"] = round(time.perf_counter() - t0, 2)
    return out
