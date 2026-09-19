"""T6-1 / T6-2 / T6-3 主流程编排：线性 6 节点 + 4 条条件分支 + 双执行引擎。

设计要点
    - 每个节点是普通函数 node_xxx(state) -> dict（返回增量字典），**不依赖图运行时**，
      所以可以脱离编排单独调用调试。
    - 双引擎：native（手写顺序执行）/ langgraph（StateGraph）。节点函数完全共用。
      state 里列表型键（errors / node_log / branch_log）在两边都是追加语义：
      native 用 merge()，langgraph 用 Annotated[list, operator.add]。

条件分支（T6-2）
    B1 数据校验失败      -> 该股票退出 active_codes，记 errors，不阻塞整体
    B2 无可交易信号      -> 跳过 LLM（节点级：全池都无 -> use_llm=False，真省钱）
    B3 LLM 不可用/失败   -> 降级为纯规则报告
    B4 库中无历史信号    -> 跳过回测，并在报告里注明

运行
    python agent_graph.py                  # 默认按 config/settings.yaml 的 watchlist 跑
    python agent_graph.py --date 2026-09-19
    python agent_graph.py --code 600519 --code 000001
    python agent_graph.py --no-llm
    python agent_graph.py --nodes fetch,chan          # 只跑部分节点（调试）
    python agent_graph.py --engine native|langgraph

单节点调试（不跑全图）
    from agent_graph import new_state, merge, node_fetch, node_chan
    st = new_state("2026-09-19", ["600519"])
    st = merge(st, node_fetch(st))
    st = merge(st, node_chan(st))
    print(st["signals"]["600519"][:2])
"""
import argparse
import operator
import time
from datetime import date, datetime, timedelta
from typing import Annotated, TypedDict

import pandas as pd

from backtest import run_backtest, save_backtest
from confirm_dates import backfill as backfill_confirm
from confirm_dates import load_frames
from mark_primary import decide_primary
from min_loop import MAX_BI_NUM, MIN_BI_LEN, build_signals, build_zs
from signal_filter import build_context, filter_signals, trading_calendar
from storage_kline import load_kline, parquet_path, update_kline
from storage_signal import apply_filter, query_signals, save_signals, set_primary

# ==================== 参数 ====================

YEARS = 2
MIN_ROWS = 60                 # 行数下限（够不够算笔）
MAX_STALE_TRADING_DAYS = 5    # 最新交易日距今超过这么多交易日 -> 数据陈旧
MAX_MISSING_RATE = 0.05       # 缺失交易日比例上限
MAX_ABS_CHANGE = 0.21         # 单日涨跌幅绝对值上限（超过所有板块限制）

APPEND_KEYS = {"errors", "node_log", "branch_log"}
PIPELINE = ["fetch", "validate", "chan", "filter", "backtest", "report"]


# ==================== 状态 ====================

class AgentState(TypedDict, total=False):
    """LangGraph 状态模式。列表型键用 operator.add 归约（追加语义）。"""
    trade_date: str
    stock_pool: list
    active_codes: list
    kline_data: dict
    signals: dict
    tradable_signals: dict
    backtest_result: dict
    llm_plan: dict
    report_content: str
    llm_summary: dict
    archive_dir: str
    errors: Annotated[list, operator.add]
    node_log: Annotated[list, operator.add]
    branch_log: Annotated[list, operator.add]


def new_state(trade_date=None, stock_pool=None):
    """构造初始 state。stock_pool 为空时读 config/settings.yaml 的 watchlist。"""
    if not stock_pool:
        from report_builder import load_watchlist
        stock_pool = load_watchlist()
    return {
        "trade_date": str(trade_date or date.today()),
        "stock_pool": list(stock_pool),
        "active_codes": list(stock_pool),
        "kline_data": {}, "signals": {}, "tradable_signals": {},
        "backtest_result": {}, "llm_plan": {},
        "report_content": None, "llm_summary": {}, "archive_dir": None,
        "errors": [], "node_log": [], "branch_log": [],
    }


def merge(state, delta):
    """把节点返回的增量字典合并进 state；列表键按追加语义（等价 langgraph 的 reducer）。"""
    out = dict(state)
    for k, v in (delta or {}).items():
        if k in APPEND_KEYS and isinstance(v, list):
            out[k] = list(state.get(k) or []) + v
        else:
            out[k] = v
    return out


def _log(node, msg, seconds):
    line = f"[{PIPELINE.index(node) + 1}/{len(PIPELINE)} {node:<8}] {msg}  {seconds:.2f}s"
    print(line)
    return {"node": node, "msg": msg, "seconds": round(seconds, 2),
            "ts": datetime.now().isoformat(timespec="seconds")}


def _branch(node, subject, condition, decision, reason):
    line = f"   ↳ 分支 {node}/{condition}: {subject} -> {decision}（{reason}）"
    print(line)
    return {"node": node, "subject": subject, "condition": condition,
            "decision": decision, "reason": reason,
            "ts": datetime.now().isoformat(timespec="seconds")}


# ==================== 条件函数（T6-2 核心） ====================

def cond_validate(state, code):
    """B1：该股票数据是否通过校验。返回 (通过?, 原因)。"""
    info = (state["kline_data"] or {}).get(code) or {}
    if not info:
        return False, "无数据记录"
    if not info.get("valid"):
        return False, "；".join(info.get("issues") or ["校验未通过"])
    return True, "校验通过"


def cond_has_tradable(state, code):
    """B2：该股票是否有可交易信号。返回 (有?, 原因)。"""
    tr = [s for s in (state["tradable_signals"].get(code) or []) if s.get("is_tradable")]
    if not tr:
        return False, "无可交易信号（无信号或全部被过滤）"
    return True, f"{len(tr)} 条可交易信号"


def cond_llm_available(state):
    """B3 前置：LLM 是否可用（key 是否就绪）。返回 (可用?, 原因)。"""
    import os

    from llm_client import API_KEY_ENV
    if not os.getenv(API_KEY_ENV):
        return False, f"环境变量 {API_KEY_ENV} 未设置"
    return True, "API key 就绪"


def cond_llm_failed(stats):
    """B3 后置：本次是否有股票 LLM 调用失败。返回 (有失败?, 原因)。"""
    n = (stats or {}).get("failed", 0)
    if n:
        return True, f"{n} 只股票的 LLM 调用失败"
    return False, "全部成功"


def cond_backtest_available(state):
    """B4：库里是否有历史信号可供回测。返回 (可回测?, 原因)。"""
    rows = [s for s in query_signals() if s["is_tradable"] == 1]
    if not rows:
        return False, "库中无可交易历史信号"
    return True, f"{len(rows)} 条可回测信号"


def cond_has_signals(state):
    """B2 节点级：全池是否至少有一只股票存在结构信号。"""
    if state.get("signals"):
        return True, "存在结构信号"
    return False, "全部股票均无结构信号"


# ==================== 校验实现（F1.5） ====================

def validate_one(code, bars, trade_date, cal):
    """返回 (valid, issues, info)。"""
    issues = []
    if bars is None or bars.empty:
        return False, ["无 K 线数据"], {}
    n = len(bars)
    start, end = str(bars["date"].iloc[0].date()), str(bars["date"].iloc[-1].date())
    if n < MIN_ROWS:
        issues.append(f"行数 {n} < {MIN_ROWS}")

    stale = sum(1 for d in cal if pd.Timestamp(end) < d <= pd.Timestamp(trade_date))
    if stale > MAX_STALE_TRADING_DAYS:
        issues.append(f"最新交易日 {end} 距今 {stale} 个交易日 > {MAX_STALE_TRADING_DAYS}")

    expected = [d for d in cal if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
    missing = len(set(expected) - set(bars["date"]))
    rate = missing / len(expected) if expected else 0
    if rate > MAX_MISSING_RATE:
        issues.append(f"缺失交易日 {missing}/{len(expected)} = {rate:.1%} > {MAX_MISSING_RATE:.0%}")

    abn = int((bars["high"] < bars["low"]).sum()) + int((bars["close"] <= 0).sum())
    chg = bars["close"].pct_change().abs()
    abn += int((chg > MAX_ABS_CHANGE).sum())
    if abn:
        issues.append(f"异常价格行 {abn} 条")

    info = {"rows": n, "start": start, "end": end, "missing": missing,
            "expected": len(expected), "abnormal": abn, "stale_days": stale,
            "path": str(parquet_path(code))}
    return (len(issues) == 0), issues, info


# ==================== 节点 ====================

def node_fetch(state):
    """1 拉数据：增量更新每只股票的日线。"""
    t0 = time.perf_counter()
    end = pd.Timestamp(state["trade_date"])
    start = end - timedelta(days=365 * YEARS)
    kd, logs = {}, []
    for code in state["stock_pool"]:
        try:
            _, st = update_kline(code, start, end, verbose=False)
            kd[code] = {"fetched": st.get("fetched", 0), "local": st.get("local", 0),
                        "skipped": st.get("skipped", False)}
            logs.append(_log("fetch", f"{code}  拉取 {st.get('fetched', 0)} 行 / "
                                       f"本地已有 {st.get('local', 0)} 行",
                             time.perf_counter() - t0))
        except Exception as e:
            kd[code] = {"fetched": 0, "local": 0, "error": f"{type(e).__name__}: {e}"}
            logs.append(_log("fetch", f"{code}  失败 {type(e).__name__}: {str(e)[:60]}",
                             time.perf_counter() - t0))
    if not logs:
        logs.append(_log("fetch", "股票池为空，无事可做", time.perf_counter() - t0))
    return {"kline_data": kd, "node_log": logs}


def node_validate(state):
    """2 校验：检查完整性 / 缺失交易日 / 异常价格。B1 在此生效。"""
    t0 = time.perf_counter()
    cal = trading_calendar()
    kd, errors, branches, logs = dict(state["kline_data"]), [], [], []
    active = []
    for code in state["stock_pool"]:
        bars = load_kline(code)
        ok, issues, info = validate_one(code, bars, state["trade_date"], cal)
        kd[code] = {**kd.get(code, {}), **info, "valid": ok, "issues": issues}
        logs.append(_log("validate", f"{code}  rows={info.get('rows', 0)} "
                                      f"缺失={info.get('missing', 0)} 异常={info.get('abnormal', 0)} "
                                      f"valid={ok}", time.perf_counter() - t0))
        pass_, reason = cond_validate({"kline_data": kd}, code)
        if pass_:
            active.append(code)
        else:
            errors.append({"node": "validate", "code": code, "error": reason,
                           "ts": datetime.now().isoformat(timespec="seconds")})
            branches.append(_branch("validate", code, "B1 数据校验", "跳过该股票，不阻塞整体", reason))
    return {"kline_data": kd, "active_codes": active, "errors": errors,
            "branch_log": branches, "node_log": logs}


def node_chan(state):
    """3 缠论计算：分型 / 笔 / 中枢 / 三类买卖点（统一格式信号）。"""
    t0 = time.perf_counter()
    import czsc

    sigs, logs = {}, []
    for code in state["active_codes"]:
        raw, q = load_frames(code)
        if q.empty:
            sigs[code] = []
            logs.append(_log("chan", f"{code}  无 K 线，跳过", time.perf_counter() - t0))
            continue
        qq = q.rename(columns={"volume": "vol"}).copy()
        qq["dt"] = pd.to_datetime(qq["date"])
        qq["symbol"] = code
        bars = czsc.format_standard_kline(qq, freq=czsc.Freq.D)
        cz = czsc.CZSC(bars, min_bi_len=MIN_BI_LEN, max_bi_num=MAX_BI_NUM)
        bis = list(cz.bi_list)
        zss = build_zs(bis)
        recs = build_signals(bis, zss)
        sigs[code] = recs
        logs.append(_log("chan", f"{code}  分型 {len(list(cz.fx_list))} / 笔 {len(bis)} / "
                                  f"中枢 {len(zss)}  信号 {len(recs)} 条",
                         time.perf_counter() - t0))
    return {"signals": sigs, "node_log": logs}


def node_filter(state):
    """4 信号过滤 + 三日期回填 + 同日主次标记。B2 的股票级判定素材在此产出。"""
    t0 = time.perf_counter()
    tradable, logs, branches, errors = {}, [], [], []
    for code in state["active_codes"]:
        sigs = state["signals"].get(code) or []
        if not sigs:
            tradable[code] = []
            logs.append(_log("filter", f"{code}  无结构信号", time.perf_counter() - t0))
            continue
        try:
            bars = load_kline(code)
            ctx = build_context(code)
            results = filter_signals(code, [{"date": s["date"], "type": s["type"],
                                             "reason": s["reason"]} for s in sigs], bars, ctx)
            save_signals(code, [{"date": r["date"], "type": r["type"], "reason": r["reason"]}
                                for r in results])
            apply_filter(code, [{"date": r["date"], "type": r["type"],
                                 "is_tradable": r["is_tradable"]} for r in results], "v1_f3")
            backfill_confirm(verbose=False)
            decisions, _ = decide_primary(query_signals(code=code))
            set_primary(decisions)
            tradable[code] = [{**r, "is_primary": next(
                (d["is_primary"] for d in decisions
                 if d["date"] == r["date"] and d["type"] == r["type"]), 0)} for r in results]
            n_ok = sum(1 for r in tradable[code] if r["is_tradable"])
            logs.append(_log("filter", f"{code}  可交易 {n_ok} / 不可交易 {len(results) - n_ok}"
                                       f"  主信号 {sum(d['is_primary'] for d in decisions)}",
                             time.perf_counter() - t0))
            ok, reason = cond_has_tradable({"tradable_signals": tradable}, code)
            if not ok:
                branches.append(_branch("filter", code, "B2 无可交易信号",
                                        "跳过 LLM（仅进表格）", reason))
        except Exception as e:
            tradable[code] = []
            errors.append({"node": "filter", "code": code, "error": f"{type(e).__name__}: {e}",
                           "ts": datetime.now().isoformat(timespec="seconds")})
            logs.append(_log("filter", f"{code}  失败 {type(e).__name__}: {str(e)[:60]}",
                             time.perf_counter() - t0))
    return {"tradable_signals": tradable, "branch_log": branches,
            "errors": errors, "node_log": logs}


def node_backtest(state):
    """5 回测统计。B4 在此生效。"""
    t0 = time.perf_counter()
    ok, reason = cond_backtest_available(state)
    if not ok:
        b = _branch("backtest", "全局", "B4 回测数据", "跳过回测，报告注明", reason)
        logs = [_log("backtest", f"跳过：{reason}", time.perf_counter() - t0)]
        return {"backtest_result": {"skipped": True, "reason": reason, "records": 0},
                "branch_log": [b], "node_log": logs}

    records = []
    for scope in ("signal", "day"):
        records += run_backtest(scope=scope, verbose=False)
    n = save_backtest(records)
    logs = [_log("backtest", f"生成 {len(records)} 行回测记录 / 落库 {n} 行（signal + day 双口径）",
                 time.perf_counter() - t0)]
    return {"backtest_result": {"skipped": False, "records": len(records), "saved": n,
                                "scopes": ["signal", "day"]},
            "node_log": logs}


def node_report(state):
    """6 报告构建 + LLM 总结 + 归档。B2（节点级）/ B3 在此生效。"""
    t0 = time.perf_counter()
    from report_builder import build_report
    from storage_report import archive_report

    codes = state["active_codes"]
    branches, logs = [], []

    need = {c: cond_has_tradable(state, c) for c in codes}
    any_need = any(v[0] for v in need.values())
    llm_plan = {c: v[0] for c, v in need.items()}

    llm_ok, llm_reason = cond_llm_available(state)

    use_llm = bool(codes) and any_need and llm_ok
    if not any_need:
        off_reason = "全池无可交易信号"
        branches.append(_branch("report", "全局", "B2 无可交易信号", "跳过 LLM，只出规则报告",
                                f"全池 {len(codes)} 只股票均无可交易信号"))
    elif not llm_ok:
        off_reason = f"LLM 不可用：{llm_reason}"
        branches.append(_branch("report", "全局", "B3 LLM 可用性", "降级为纯规则报告", llm_reason))
    else:
        off_reason = "已用 --no-llm 关闭"

    md, out, stats = build_report(state["trade_date"], codes or None,
                                  use_llm=use_llm, llm_off_reason=off_reason, verbose=False)

    failed, fail_reason = cond_llm_failed(stats)
    if failed:
        branches.append(_branch("report", "全局", "B3 LLM 调用结果", "降级为纯规则报告", fail_reason))

    notes = [b for b in (state.get("branch_log") or []) if b["condition"].startswith(("B1", "B4"))]
    if notes or branches:
        md = _append_branch_section(md, state, branches, notes)
    # 无论有没有分支都要归档：否则干净运行不会覆盖上一期的报告文件
    archive_report(state["trade_date"], md, stats)

    logs.append(_log("report", f"LLM 调用 {stats.get('called', 0)} / 跳过 {stats.get('skipped', 0)} / "
                               f"失败 {stats.get('failed', 0)} / token {stats.get('tokens', 0)} / "
                               f"¥{stats.get('cost', 0):.6f}  归档 {out.parent}",
                     time.perf_counter() - t0))
    return {"report_content": md,
            "llm_summary": {c: (v or {}).get("text", "")
                            for c, v in (stats.get("llm_by_code") or {}).items()},
            "archive_dir": str(out.parent), "llm_plan": llm_plan,
            "branch_log": branches, "node_log": logs}


def _append_branch_section(md, state, later_branches, earlier_branches):
    """把本次运行的条件分支决策写进报告末尾（图自己产出的内容，不改 T5 模块）。"""
    rows = earlier_branches + later_branches
    lines = ["", "---", "", "## 本次运行分支说明", "",
             "| 环节 | 对象 | 条件 | 决策 | 原因 |", "| --- | --- | --- | --- | --- |"]
    for b in rows:
        lines.append(f"| {b['node']} | {b['subject']} | {b['condition']} | "
                     f"{b['decision']} | {b['reason']} |")
    bt = state.get("backtest_result") or {}
    if bt.get("skipped"):
        lines += ["", f"> 回测已跳过：{bt.get('reason')}。上方「回测统计参考」为空表属预期。"]
    errs = state.get("errors") or []
    if errs:
        lines += ["", "**告警**（不阻塞整体运行）：", ""]
        for e in errs:
            lines.append(f"- [{e['node']}] {e['code']}：{e['error']}")
    lines.append("")
    return md + chr(10).join(lines)


# ==================== 编排 ====================

NODE_FUNCS = {"fetch": node_fetch, "validate": node_validate, "chan": node_chan,
              "filter": node_filter, "backtest": node_backtest, "report": node_report}


def build_langgraph():
    """把同样的 6 个节点接到 LangGraph StateGraph 上（需 pip install langgraph）。"""
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(AgentState)
    for name in PIPELINE:
        g.add_node(name, NODE_FUNCS[name])
    g.add_edge(START, PIPELINE[0])
    for a, b in zip(PIPELINE, PIPELINE[1:]):
        g.add_edge(a, b)
    g.add_edge(PIPELINE[-1], END)
    return g.compile()


def _summary(state, verbose):
    if verbose:
        print()
        print(f"=== 完成：节点日志 {len(state['node_log'])} 条 / 分支决策 "
              f"{len(state['branch_log'])} 条 / 告警 {len(state['errors'])} 条 ===")


def run_pipeline(state=None, nodes=None, trade_date=None, stock_pool=None, verbose=True,
                 engine="auto"):
    """线性跑完（或只跑 nodes 指定的子集）。engine: auto / native / langgraph。"""
    state = state or new_state(trade_date, stock_pool)
    todo = [n for n in PIPELINE if not nodes or n in nodes]
    if verbose:
        print(f"=== 主流程编排  交易日 {state['trade_date']}  股票池 {state['stock_pool']} ===")
        print(f"    执行节点：{' -> '.join(todo)}")
        print()

    if engine in ("auto", "langgraph") and not nodes:
        try:
            app = build_langgraph()
            if verbose:
                print("    执行引擎：LangGraph StateGraph")
            state = dict(app.invoke(state))
            _summary(state, verbose)
            return state
        except Exception as e:
            if engine == "langgraph":
                raise
            if verbose:
                print(f"    (langgraph 不可用，回退原生执行：{type(e).__name__}: {str(e)[:80]})")

    for name in todo:
        state = merge(state, NODE_FUNCS[name](state))
    _summary(state, verbose)
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--code", action="append", default=None)
    ap.add_argument("--nodes", default=None, help="只跑指定节点，逗号分隔，如 fetch,chan")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--engine", choices=["auto", "native", "langgraph"], default="auto")
    a = ap.parse_args()

    if a.no_llm:
        import os
        os.environ.pop("DEEPSEEK_API_KEY", None)   # 走 B3「LLM 不可用」分支

    st = run_pipeline(trade_date=a.date, stock_pool=a.code,
                      nodes=a.nodes.split(",") if a.nodes else None, engine=a.engine)

    print()
    print("=== 分支决策汇总 ===")
    if st["branch_log"]:
        for b in st["branch_log"]:
            print(f"  [{b['node']}] {b['condition']}  {b['subject']}  ->  "
                  f"{b['decision']}（{b['reason']}）")
    else:
        print("  （本次没有触发任何分支，全部走主干）")
    print()
    print("=== 产物 ===")
    print(f"  active_codes : {st['active_codes']}")
    print(f"  信号数       : " + "，".join(f"{c}={len(v)}" for c, v in st["signals"].items()))
    print(f"  可交易信号   : " + "，".join(
        f"{c}={sum(1 for s in v if s['is_tradable'])}" for c, v in st["tradable_signals"].items()))
    print(f"  回测         : {st['backtest_result']}")
    print(f"  归档目录     : {st['archive_dir']}")
    print(f"  告警         : {st['errors'] or '无'}")


if __name__ == "__main__":
    main()
