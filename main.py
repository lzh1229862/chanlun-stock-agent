"""缠论股票分析 Agent 主入口（PRD F7.3 CLI / F7.4 日志）。

一键流程
    拉数据 -> 校验 -> 缠论计算 -> 信号过滤 -> 回测统计 -> 报告+LLM -> 归档

用法
    python main.py                          # 今日全流程（股票池取 config/settings.yaml）
    python main.py --date 2026-09-19
    python main.py --stocks 600519,000001
    python main.py --no-llm                 # 只出规则报告，不调 LLM
    python main.py --nodes fetch,chan       # 只跑部分节点（调试）
    python main.py --engine native          # 不用 LangGraph，走手写顺序执行
    python main.py --log-file logs/x.json   # 指定运行日志路径

退出码
    0  完成（可能带告警，见运行末尾汇总）
    1  致命错误（异常中断）
"""
import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

from agent_graph import PIPELINE, run_pipeline

LOG_DIR = Path("logs")


def parse_stocks(s):
    """解析 --stocks，支持中英文逗号与空格。"""
    if not s:
        return None
    for ch in ("，", ";", "；", " "):
        s = s.replace(ch, ",")
    return [x.strip() for x in s.split(",") if x.strip()]


def print_summary(state):
    print()
    print("=== 分支决策汇总 ===")
    if state["branch_log"]:
        for b in state["branch_log"]:
            print(f"  [{b['node']}] {b['condition']}  {b['subject']}  ->  "
                  f"{b['decision']}（{b['reason']}）")
    else:
        print("  （本次没有触发任何分支，全部走主干）")

    print()
    print("=== 产物 ===")
    print(f"  通过校验的股票 : {state['active_codes']}")
    print("  信号数         : " + "，".join(f"{c}={len(v)}" for c, v in state["signals"].items()))
    print("  可交易信号     : " + "，".join(
        f"{c}={sum(1 for s in v if s['is_tradable'])}"
        for c, v in state["tradable_signals"].items()))
    print(f"  回测           : {state['backtest_result']}")
    print(f"  归档目录       : {state['archive_dir']}")
    if state["errors"]:
        print(f"  ⚠️ 告警 {len(state['errors'])} 条：")
        for e in state["errors"]:
            print(f"     [{e['node']}] {e['code']}：{e['error']}")
    else:
        print("  告警           : 无")


def write_log(path, state, started, seconds, exit_code):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "started_at": started, "finished_at": datetime.now().isoformat(timespec="seconds"),
        "seconds": round(seconds, 2), "exit_code": exit_code,
        "trade_date": state["trade_date"], "stock_pool": state["stock_pool"],
        "active_codes": state["active_codes"],
        "nodes": state["node_log"], "branches": state["branch_log"], "errors": state["errors"],
        "signal_counts": {c: len(v) for c, v in state["signals"].items()},
        "tradable_counts": {c: sum(1 for s in v if s["is_tradable"])
                            for c, v in state["tradable_signals"].items()},
        "backtest": state["backtest_result"], "archive_dir": state["archive_dir"],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser(description="缠论股票分析 Agent")
    ap.add_argument("--date", default=None, help="交易日/报告日，默认今天")
    ap.add_argument("--stocks", default=None, help="股票池，逗号分隔；默认取 config/settings.yaml")
    ap.add_argument("--no-llm", action="store_true", help="不调用 LLM，只出规则报告")
    ap.add_argument("--nodes", default=None, help="只跑指定节点，逗号分隔（调试用）")
    ap.add_argument("--engine", choices=["auto", "native", "langgraph"], default="auto")
    ap.add_argument("--log-file", default=None, help="运行日志路径，默认 logs/<交易日>.json")
    ap.add_argument("--trading-day-only", action="store_true",
                    help="非交易日直接跳过（给 Windows 计划任务用）")
    a = ap.parse_args()

    if a.no_llm:
        import os
        os.environ.pop("DEEPSEEK_API_KEY", None)

    if a.trading_day_only:
        import pandas as pd

        from signal_filter import trading_calendar
        d = pd.Timestamp(a.date) if a.date else pd.Timestamp(date.today())
        if d not in set(trading_calendar()):
            print(f"{d.date()} 不是交易日（周末或节假日），跳过本次运行。")
            return 0

    codes = parse_stocks(a.stocks)
    nodes = [n.strip() for n in a.nodes.split(",")] if a.nodes else None
    if nodes:
        bad = [n for n in nodes if n not in PIPELINE]
        if bad:
            print(f"未知节点 {bad}，可用：{PIPELINE}", file=sys.stderr)
            return 1

    started = datetime.now().isoformat(timespec="seconds")
    t0 = time.perf_counter()
    try:
        state = run_pipeline(trade_date=a.date, stock_pool=codes, nodes=nodes, engine=a.engine)
    except Exception as e:
        print(f"\n致命错误：{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    seconds = time.perf_counter() - t0
    print_summary(state)

    log_path = a.log_file or (LOG_DIR / f"{state['trade_date']}.json")
    write_log(log_path, state, started, seconds, 0)
    print()
    print(f"运行日志：{Path(log_path).resolve()}")
    print(f"总耗时  ：{seconds:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
