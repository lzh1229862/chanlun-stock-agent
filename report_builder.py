"""T5-1 / T5-4 Markdown 报告构建（规则生成 + 可选 LLM 总结）。

输出
    reports/YYYY-MM-DD/report.md

LLM 调用策略（T5-4）
    有信号 且 存在 is_tradable=1 的信号 -> 调用 LLM 生成「AI 总结」
    无信号 或 无可交易信号              -> 只进股票池表格，不调 LLM
    LLM 调用失败                        -> 降级为纯规则报告，报告照常生成
    --no-llm                            -> 完全不调用

报告结构区分
    规则结论   由代码按固定规则生成（结构位置 / 最近信号 / 距今交易日）
    AI 总结    由 DeepSeek 按 config/prompts/report_summary.txt 生成，可给出方向性判断

运行
    python report_builder.py                # 生成今日报告（含 LLM）
    python report_builder.py --no-llm       # 只出规则报告
    python report_builder.py --date 2026-09-18
    python report_builder.py --code 600519  # 只报指定股票（覆盖 watchlist）
    python report_builder.py --stdout
"""
import argparse
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import czsc
import pandas as pd
import yaml

from confirm_dates import load_frames
from llm_client import MODEL as LLM_MODEL
from llm_client import summarize as llm_summarize
from min_loop import MAX_BI_NUM, MIN_BI_LEN, build_zs
from fundamentals import format_lines as fundamentals_lines
from structure_gap import GAP_THRESHOLD, label_of
from fundamentals import get_fundamentals
from fundamentals import summarize as summarize_fundamentals
from signal_filter import fetch_name, trading_calendar
from storage_signal import query_signals

# ==================== 报告模板配置（调整报告改这里） ====================

REPORT_TITLE = "缠论分析日报"
OUTPUT_DIR = Path("reports")
SETTINGS_PATH = Path("config/settings.yaml")

DISCLAIMER = """
> **非投资建议**：本报告由程序按缠论规则自动生成，仅是对价格结构的算法化描述，
> 不构成任何投资建议，不承诺收益，不含自动下单。据此操作风险自负。
""".strip()

MAX_SIGNALS_PER_STOCK = 5       # 每只股票最多展示几条信号明细
STRUCTURE_BI_SHOW = 3           # 结构段展示几笔
RECENT_TRADING_DAYS = 20        # 多少交易日内算「近期信号」
BACKTEST_SCOPE = "signal"       # 回测参考用哪个口径：signal（按信号）/ day（按交易日）

FILTER_LEGEND = {
    "st": "ST/*ST", "delisted": "退市整理", "new": "次新股（上市不足 60 交易日）",
    "limitup": "买点当日涨停，买不进", "limitdown": "卖点当日跌停，卖不出",
    "susp": "信号日停牌（成交量为 0）",
    "illiquid": "成交额低于下限，流动性不足",
    "volspike": "成交量异常放大",
}


# ==================== 工具 ====================

def pct(x, nd=2):
    return f"{x:+.{nd}%}" if x is not None else "—"


def num(x, nd=2):
    return f"{x:.{nd}f}" if x is not None else "—"


def trading_days_between(cal, a, b):
    """(a, b] 之间的交易日数。"""
    a, b = pd.Timestamp(a), pd.Timestamp(b)
    return sum(1 for d in cal if a < d <= b)


def load_watchlist():
    """自选股池。优先 config/watchlist.local.yaml（UI 保存的），否则 config/settings.yaml。

    实际读写在 watchlist_store 里（ADR-016），这里保留同名函数只是为了让
    agent_graph / main.py 这些老调用方不用改。
    """
    from watchlist_store import load_watchlist as _load
    return _load()


def filter_note(filter_version, is_tradable):
    if is_tradable:
        return "可交易"
    fv = filter_version or ""
    codes = fv.split(":", 1)[1] if ":" in fv else ""
    if not codes:
        return "不可交易（原因未记录）"
    return "不可交易：" + "；".join(FILTER_LEGEND.get(c, c) for c in codes.split("+"))


def staleness(sig, report_date, cal):
    """返回 (时效标签, 提示文本)。"""
    sd, cd = sig["signal_date"], sig["confirm_date"]
    if not cd:
        return "待确认", (f"> ⚠️ **该信号尚未确认**（信号日 {sd}）。缠论「笔」需要后续 K 线才能确认，"
                          f"当前数据不足以判定确认日，**暂不可作为操作依据**。")
    k = trading_days_between(cal, sd, cd)
    since = trading_days_between(cal, cd, report_date)
    label = "近期" if since <= RECENT_TRADING_DAYS else "历史"
    if since == 0:
        txt = (f"> ⚠️ **本信号为 {k} 个交易日前发出（{sd}），今日（{cd}）确认。**"
               f"信号日与确认日不同，**它不是今日盘中就能得知的信号**。")
    else:
        txt = (f"> ⚠️ **本信号 {sd} 发出、{cd} 确认**（发出到确认相隔 {k} 个交易日；"
               f"确认至今已 {since} 个交易日）。**不是今日新产生的信号**，请勿当作即时入场提示。")
    return label, txt


# ==================== 结构计算 ====================

def stock_structure(code):
    raw, q = load_frames(code)
    if q.empty:
        return None
    qq = q.rename(columns={"volume": "vol"}).copy()
    qq["dt"] = pd.to_datetime(qq["date"])
    qq["symbol"] = code
    bars = czsc.format_standard_kline(qq, freq=czsc.Freq.D)
    cz = czsc.CZSC(bars, min_bi_len=MIN_BI_LEN, max_bi_num=MAX_BI_NUM)
    bis = list(cz.bi_list)
    zss = build_zs(bis)
    close = float(q["close"].iloc[-1])

    pos, z = "无中枢", (zss[-1] if zss else None)
    if z:
        pos = "中枢上方" if close > z["zg"] else ("中枢下方" if close < z["zd"] else "中枢内部")
    return {"close": close, "close_raw": float(raw["close"].iloc[-1]),
            "last_date": str(raw["date"].iloc[-1].date()), "zs": z, "position": pos,
            "bis": bis[-STRUCTURE_BI_SHOW:], "n_bi": len(bis), "n_zs": len(zss),
            "fx": len(list(cz.fx_list))}


# ==================== 各段落渲染 ====================

def render_overview(report_date, codes, all_, tradable, primary, pending):
    return "\n".join([
        "## 报告概览", "",
        "| 项 | 值 |", "| --- | --- |",
        f"| 报告日期 | {report_date} |",
        f"| 股票池 | {'、'.join(codes)}（{len(codes)} 只）|",
        f"| 信号总数 | {len(all_)} |",
        f"| 可交易信号 | {len(tradable)} |",
        f"| 其中主信号 | {len(primary)} |",
        f"| 待确认信号 | {len(pending)} |",
        "",
    ])


def render_watchlist_table(codes, by_code, names, llm_state):
    L = ["## 股票池概览", "",
         "| 代码 | 名称 | 行业 | 信号数 | 可交易 | 主信号 | 本次 LLM |",
         "| --- | --- | --- | --- | --- | --- | --- |"]
    for c in codes:
        sigs = by_code.get(c, [])
        tr = sum(1 for s in sigs if s["is_tradable"])
        pr = sum(1 for s in sigs if s["is_primary"])
        ind = (fundamentals_summary(c, names.get(c, "")).get("industry") or "-")
        L.append(f"| {c} | {names.get(c, '')} | {ind} | {len(sigs)} | {tr} | {pr} "
                 f"| {llm_state.get(c, '-')} |")
    L.append("")
    return "\n".join(L)


def render_staleness_alert(report_date, all_, cal):
    dated = [s for s in all_ if s["confirm_date"]]
    L = ["## ⏱ 信号时效提醒", ""]
    today_sig = [s for s in dated if s["confirm_date"] == report_date]
    if today_sig:
        L.append(f"本次有 **{len(today_sig)}** 条信号在 {report_date} 确认（处于可操作窗口）。")
    else:
        L.append(f"**本次没有在 {report_date} 确认的新信号。**")
    if dated:
        lags = [trading_days_between(cal, s["signal_date"], s["confirm_date"]) for s in dated]
        L += ["",
              f"报告中信号的「信号日 → 确认日」延迟：最小 {min(lags)}、最大 {max(lags)} 个交易日"
              f"（缠论「笔」需后续 K 线确认，见 ADR-011）。",
              "**任何一条信号都不是当日盘中即可得知的**，请务必对照每条的信号日与确认日。"]
    if all_ and not dated:
        L += ["", "⚠️ 全部信号目前都处于**待确认**状态，暂不可作为操作依据。"]
    L.append("")
    return "\n".join(L)


_FIN_CACHE = {}


def fundamentals_summary(code, name=""):
    """基本面摘要。带进程内缓存 —— 同一轮里 render_stock 和 build_payload 都要用，
    不缓存会重复查库/重复联网（冷启动每只约 2 秒，之后命中 SQLite 缓存为 0）。"""
    if code not in _FIN_CACHE:
        try:
            _FIN_CACHE[code] = summarize_fundamentals(get_fundamentals(code), name=name)
        except Exception:
            _FIN_CACHE[code] = {}
    return _FIN_CACHE[code]


def render_fundamentals(code, name=""):
    """公司基本面段落。数据只用于交代背景，**不作为涨跌方向依据**（见 ADR-017）。"""
    lines = fundamentals_lines(fundamentals_summary(code, name))
    if not lines:
        return "**公司基本面**\n\n- 暂无数据（接口不可用或该股无记录）\n"
    return "**公司基本面**\n\n" + "\n".join(lines) + "\n"


def render_structure(st):
    L = ["**当前缠论结构**（前复权口径，前复权价 = 不复权价 / qfq_factor）", ""]
    if not st:
        return "\n".join(L + ["- 本地无 K 线数据，无法计算结构", ""])
    L += [f"- 最新交易日：{st['last_date']}    最新收盘：{num(st['close_raw'])}（不复权）",
          f"- 结构规模：{st['fx']} 个分型 / {st['n_bi']} 笔 / {st['n_zs']} 个中枢"]
    z = st["zs"]
    if z:
        L += [f"- 最近中枢：{z['sdt'].date()} ~ {z['edt'].date()}，"
              f"区间 [{num(z['zd'])} , {num(z['zg'])}]，含 {z['n']} 笔",
              f"- **价格位置：{st['position']}**"]
    else:
        L.append("- 未识别到中枢")
    L += ["", f"- 最新 {STRUCTURE_BI_SHOW} 笔：", "",
          "  | 起 | 止 | 方向 | 价格区间 |", "  | --- | --- | --- | --- |"]
    for b in st["bis"]:
        L.append(f"  | {b.sdt.date()} | {b.edt.date()} | {b.direction} | {num(b.low)} ~ {num(b.high)} |")
    L.append("")
    return "\n".join(L)


def render_llm(llm):
    """AI 总结段落。llm = {"state":..., "text":..., "tokens":..., "cost":..., "cached":...}"""
    if llm and llm.get("text"):
        u = llm.get("tokens", 0)
        tag = "缓存命中" if llm.get("cached") else "本次调用"
        return ("\n".join([f"**AI 总结**（{llm.get('model', LLM_MODEL)}，{tag}，"
                            f"token {u}，费用 ¥{llm.get('cost', 0):.6f}）", "",
                            llm["text"], ""]))
    reason = (llm or {}).get("reason", "未调用")
    if reason.startswith("调用失败"):
        return f"**AI 总结**：{reason}\n"
    return f"**AI 总结**：本次未调用（{reason}）\n"


def render_signals(sigs, report_date, cal):
    show = sigs[-MAX_SIGNALS_PER_STOCK:][::-1]
    L = [f"**信号明细**（最近 {len(show)} 条，按信号日倒序）", "",
         "| 信号日 | 确认日 | 类型 | 入场参考价 | 时效 | 可交易 | 主信号 | 距中枢 |",
         "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in show:
        label, _ = staleness(s, report_date, cal)
        L.append(f"| {s['signal_date']} | {s['confirm_date'] or '**待确认**'} | {s['signal_type']} | "
                 f"{num(s['entry_ref_price'])} | {label} | {'✅' if s['is_tradable'] else '❌'} | "
                 f"{'★' if s['is_primary'] else ''} | {label_of(s.get('zs_gap_days'))} |")
    L.append("")

    far = [s for s in show if s.get("zs_gap_days") is not None
           and s["zs_gap_days"] >= GAP_THRESHOLD]
    if far:
        L += [f"> ⚠️ 上表有 **{len(far)} 条信号距最近中枢结束已超过 {GAP_THRESHOLD} 个交易日**"
              f"（{'、'.join(s['signal_date'] for s in far)}）。实测这类信号的 5 日超额"
              f"从 +1.30% 降到 +0.73%（差值 −0.56%，95%CI 不跨 0，分半验证同向，见 ADR-021），"
              f"**结构已经离得比较远，可靠性下降**。", ""]

    pend = [s for s in show if not s["confirm_date"]]
    if pend:
        L += [f"> ⚠️ 上表中有 **{len(pend)} 条信号尚未确认**"
              f"（{'、'.join(s['signal_date'] for s in pend)}）。"
              f"缠论「笔」需要后续 K 线才能确认，**这些信号暂不可作为操作依据**。", ""]

    s = next((x for x in show if x["confirm_date"]), show[0])
    title = "最近一条已确认信号的三日期" if s["confirm_date"] else "最近一条信号的三日期"
    L += [f"**{title}**", "",
          "| 项 | 值 | 含义 |", "| --- | --- | --- |",
          f"| 信号日 signal_date | {s['signal_date']} | 缠论信号实际发生日（触发笔结束日）|",
          f"| 确认日 confirm_date | {s['confirm_date'] or '待确认'} | 信号可被确认的日期（笔需后续 K 线确认）|",
          f"| 入场参考价 entry_ref_price | {num(s['entry_ref_price'])} | 确认日次一交易日**不复权**开盘价 |",
          ""]
    _, note = staleness(s, report_date, cal)
    L += [note, ""]

    L += ["**信号理由**", ""]
    for s in show:
        L.append(f"- {s['signal_date']} {s['signal_type']}：{s['signal_reason']}")
    L.append("")

    L += ["**过滤说明**", ""]
    for s in show:
        L.append(f"- {s['signal_date']} {s['signal_type']}："
                 f"{filter_note(s['filter_version'], s['is_tradable'])}"
                 f"（filter_version={s['filter_version']}）")
    if show[0]["backfill_note"]:
        L += ["", f"> 回填说明：{show[0]['backfill_note']}", ""]
    return "\n".join(L)


def render_backtest_ref(sigs, bt_stats):
    types = sorted({s["signal_type"] for s in sigs})
    scope_cn = "按信号" if BACKTEST_SCOPE == "signal" else "按交易日"
    L = [f"**回测统计参考**（口径：{scope_cn}；入场=确认日次一交易日开盘，见 ADR-011）", "",
         "| 类型 | 窗口 | 样本 | 平均收益 | 胜率 | 平均最大回撤 |",
         "| --- | --- | --- | --- | --- | --- |"]
    n = 0
    for t in types:
        for w in (5, 10, 20):
            st = bt_stats.get((BACKTEST_SCOPE, w, t))
            if st:
                L.append(f"| {t} | {w}日 | {st['n']} | {pct(st['avg_return'])} | "
                         f"{st['win_rate']:.1%} | {pct(st['avg_mdd'])} |")
                n += 1
    if n == 0:
        L.append("| — | — | — | — | — | — |")
    L += ["", "> ⚠️ 样本量很小（当前仅 1 只股票），统计值**不具解释力**，仅作管线演示。", ""]
    return "\n".join(L)


def one_line_conclusion(st, sigs, report_date, cal):
    parts = []
    if st:
        tail = st["position"] if st["zs"] else "未识别到中枢"
        parts.append(f"最新收盘 {num(st['close_raw'])}，价格位于最近{tail}")
    if sigs:
        s = sigs[-1]
        label, _ = staleness(s, report_date, cal)
        state = "可交易" if s["is_tradable"] else "不可交易"
        parts.append(f"最近一条信号为 {s['signal_date']} 的**{s['signal_type']}**"
                     f"（{s['confirm_date'] or '待确认'} 确认，入场参考价 {num(s['entry_ref_price'])}，"
                     f"{state}，{label}信号）")
        parts.append(f"该信号距今 {trading_days_between(cal, s['signal_date'], report_date)} 个交易日")
    else:
        parts.append("库中暂无该股信号")
    return "；".join(parts) + "。"


def render_stock(code, name, sigs, report_date, cal, bt_stats, llm):
    st = stock_structure(code)
    L = [f"### {code} {name}".rstrip(), "",
         f"**规则结论**：{one_line_conclusion(st, sigs, report_date, cal)}", "",
         render_llm(llm),
         render_structure(st),
         render_fundamentals(code, name)]
    if sigs:
        L.append(render_signals(sigs, report_date, cal))
        L.append(render_backtest_ref(sigs, bt_stats))
    else:
        L += ["**信号明细**：库中暂无该股信号。", ""]
    return "\n".join(L)


def render_footer(llm_stats):
    L = ["## 附录：口径说明", "",
         "| 项 | 说明 |", "| --- | --- |",
         "| 价格口径 | 报告中的价格均为**不复权**（盘面实际价）；结构段为前复权口径 |",
         "| 信号日 | 触发笔结束日 = BI.edt = BI.fx_b.dt |",
         "| 确认日 | 信号日 + 实测确认延迟（逐日放行 K 线重跑得出，ADR-011）|",
         "| 入场参考价 | 确认日次一交易日不复权开盘价；与回测表 entry_price（前复权）口径不同 |",
         "| 待确认 | confirm_date 为 NULL，表示当前数据不足以确认该信号，不可操作 |",
         "| 过滤 | is_tradable，原因见 filter_version（如 v1_f3:limitup = 买点当日涨停）|",
         "| 规则结论 | 由代码按固定规则生成，不含模型判断 |",
         "| AI 总结 | 由 DeepSeek 生成，可给方向性判断；失败时自动降级为纯规则报告 |",
         ""]
    if llm_stats:
        L += ["### 本次 LLM 调用情况", "",
              "| 项 | 值 |", "| --- | --- |",
              f"| 调用 LLM 的股票数 | {llm_stats['called']} |",
              f"| 跳过 LLM 的股票数 | {llm_stats['skipped']} |",
              f"| 调用失败（已降级） | {llm_stats['failed']} |",
              f"| 其中命中缓存 | {llm_stats['cached']} |",
              f"| token 消耗 | {llm_stats['tokens']} |",
              f"| 费用 | ¥{llm_stats['cost']:.6f} |",
              f"| 模型 | {llm_stats['model']} |",
              ""]
        if llm_stats["skip_detail"]:
            L.append("跳过原因：" + "；".join(f"{k} {v} 只" for k, v in llm_stats["skip_detail"].items()))
            L.append("")
    L += ["---", "", "*本报告由程序自动生成，非投资建议。*", ""]
    return "\n".join(L)


# ==================== LLM 输入组装 ====================

def build_payload(code, name, sigs, st, bt_stats, report_date):
    structure = None
    if st:
        structure = {
            "last_date": st["last_date"], "close": round(st["close_raw"], 2),
            "position": st["position"], "n_bi": st["n_bi"], "n_zs": st["n_zs"],
            "zs": ({"sdt": str(st["zs"]["sdt"].date()), "edt": str(st["zs"]["edt"].date()),
                    "zd": round(st["zs"]["zd"], 2), "zg": round(st["zs"]["zg"], 2)}
                   if st["zs"] else None),
            "bis": [{"sdt": str(b.sdt.date()), "edt": str(b.edt.date()),
                     "direction": str(b.direction), "low": round(b.low, 2),
                     "high": round(b.high, 2)} for b in st["bis"]],
        }
    types = {s["signal_type"] for s in sigs}
    bt = [{"signal_type": t, "window": w, **v}
          for (scope, w, t), v in sorted(bt_stats.items())
          if scope == BACKTEST_SCOPE and t in types]
    fin = fundamentals_lines(fundamentals_summary(code, name))
    return {"stock_code": code, "stock_name": name, "report_date": report_date,
            "fundamentals": "\n".join(fin) if fin else "（无数据）",
            "structure": structure,
            "signals": [{"signal_date": s["signal_date"], "confirm_date": s["confirm_date"],
                         "entry_ref_price": s["entry_ref_price"], "signal_type": s["signal_type"],
                         "is_tradable": s["is_tradable"], "is_primary": s["is_primary"],
                         "filter_version": s["filter_version"],
                         "signal_reason": s["signal_reason"]} for s in sigs],
            "backtest": bt}


# ==================== 主流程 ====================

def build_report(report_date=None, codes=None, use_llm=True, verbose=True,
                 llm_off_reason='已用 --no-llm 关闭'):
    from backtest import query_backtest

    t0 = time.perf_counter()
    report_date = str(report_date or date.today())
    cal = trading_calendar()

    watch = codes or load_watchlist()
    sigs_all = query_signals()
    if codes:
        sigs_all = [s for s in sigs_all if s["stock_code"] in set(codes)]

    by_code = defaultdict(list)
    for s in sigs_all:
        by_code[s["stock_code"]].append(s)
    for v in by_code.values():
        v.sort(key=lambda x: x["signal_date"])

    all_codes = sorted(set(watch) | set(by_code))
    if not all_codes:
        return None, None, None

    names = {}
    for c in all_codes:
        try:
            names[c] = fetch_name(c)
        except Exception:
            names[c] = ""

    agg = defaultdict(list)
    for r in query_backtest():
        agg[(r["scope"], r["window"], r["signal_type"])].append(r)
    bt_stats = {}
    for k, v in agg.items():
        rets = [x["return_pct"] for x in v]
        bt_stats[k] = {"n": len(v), "avg_return": sum(rets) / len(rets),
                       "win_rate": sum(x["is_win"] for x in v) / len(v),
                       "avg_mdd": sum(x["max_drawdown"] for x in v) / len(v)}

    # ---- 决定每只股票是否调 LLM，并执行 ----
    stats = {"called": 0, "skipped": 0, "failed": 0, "cached": 0, "tokens": 0, "cost": 0.0,
             "model": LLM_MODEL, "skip_detail": defaultdict(int)}
    llm_by_code, state_by_code = {}, {}

    for c in all_codes:
        sigs = by_code.get(c, [])
        tradable = [s for s in sigs if s["is_tradable"]]
        if not use_llm:
            llm_by_code[c] = {"reason": llm_off_reason}
            state_by_code[c] = "跳过（--no-llm）"
            stats["skipped"] += 1
            stats["skip_detail"]["--no-llm"] += 1
            continue
        if not sigs:
            llm_by_code[c] = {"reason": "无信号"}
            state_by_code[c] = "跳过（无信号）"
            stats["skipped"] += 1
            stats["skip_detail"]["无信号"] += 1
            continue
        if not tradable:
            llm_by_code[c] = {"reason": "有信号但无可交易信号"}
            state_by_code[c] = "跳过（无可交易信号）"
            stats["skipped"] += 1
            stats["skip_detail"]["无可交易信号"] += 1
            continue

        st = stock_structure(c)
        payload = build_payload(c, names[c], sigs, st, bt_stats, report_date)
        try:
            r = llm_summarize(payload, verbose=False)
            llm_by_code[c] = {"text": r["text"], "tokens": (r["usage"] or {}).get("total_tokens", 0),
                              "cost": r["cost"], "cached": r["cached"], "model": r["model"]}
            state_by_code[c] = "✅ 命中缓存" if r["cached"] else "✅ 已调用"
            stats["called"] += 1
            stats["cached"] += 1 if r["cached"] else 0
            stats["tokens"] += (r["usage"] or {}).get("total_tokens", 0)
            stats["cost"] += r["cost"] or 0
        except Exception as e:
            llm_by_code[c] = {"reason": f"调用失败（{type(e).__name__}），已降级为纯规则报告"}
            state_by_code[c] = "❌ 失败（已降级）"
            stats["failed"] += 1
            stats["skipped"] += 0
            if verbose:
                print(f"  ⚠️ {c} LLM 调用失败: {type(e).__name__}: {str(e)[:100]}")

    # ---- 渲染 ----
    tradable_all = [s for s in sigs_all if s["is_tradable"]]
    primary_all = [s for s in sigs_all if s["is_primary"]]
    pending_all = [s for s in sigs_all if not s["confirm_date"]]

    parts = [f"# {REPORT_TITLE}  {report_date}", "", DISCLAIMER, "", "---", "",
             render_overview(report_date, all_codes, sigs_all, tradable_all, primary_all, pending_all),
             "---", "",
             render_watchlist_table(all_codes, by_code, names, state_by_code),
             "---", "",
             render_staleness_alert(report_date, sigs_all, cal),
             "---", "", "## 个股分析", ""]
    sections = {}
    for c in all_codes:
        if not by_code.get(c):
            sections[c] = "\n".join([f"### {c} {names.get(c, '')}".rstrip(), "",
                                     "**规则结论**：库中暂无该股信号，本次不做结构解读。", "",
                                     render_llm(llm_by_code.get(c))])
        else:
            sections[c] = render_stock(c, names.get(c, ""), by_code[c], report_date, cal,
                                       bt_stats, llm_by_code.get(c))
    for c in all_codes:
        parts += [sections[c], "---", ""]
    parts.append(render_footer(stats))

    md = "\n".join(parts)
    stats.update({"seconds": round(time.perf_counter() - t0, 2), "report_date": report_date, "codes": all_codes, "sections": sections, "by_code": dict(by_code), "llm_by_code": llm_by_code})
    return md, OUTPUT_DIR / report_date / "report.md", stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--code", action="append", default=None)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--stdout", action="store_true")
    a = ap.parse_args()

    md, out, stats = build_report(a.date, a.code, use_llm=not a.no_llm)
    if md is None:
        print("股票池为空且库中无信号。请检查 config/settings.yaml 的 watchlist，或先跑 run_round3.py")
        return
    if a.stdout:
        print(md)
        return

    from storage_report import archive_report
    archive_dir = archive_report(stats["report_date"], md, stats)
    print(f"归档目录: {archive_dir.resolve()}")
    print("  包含 report.md / signals.csv / meta.json")
    print(f"  字符数 {len(md)}   行数 {md.count(chr(10)) + 1}   耗时 {stats['seconds']}s")
    print()
    print("=== LLM 调用统计 ===")
    print(f"  调用 LLM 的股票数 : {stats['called']}")
    print(f"  跳过 LLM 的股票数 : {stats['skipped']}"
          + (f"   （{'；'.join(f'{k} {v} 只' for k, v in stats['skip_detail'].items())}）"
             if stats["skip_detail"] else ""))
    print(f"  调用失败（已降级）: {stats['failed']}")
    print(f"  其中命中缓存      : {stats['cached']}")
    print(f"  token 消耗        : {stats['tokens']}")
    print(f"  费用              : ¥{stats['cost']:.6f}")
    print(f"  模型              : {stats['model']}")


if __name__ == "__main__":
    main()
