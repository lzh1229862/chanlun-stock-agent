"""缠论分析 Web UI（Streamlit）。

后端逻辑一行未改 —— 全部通过 analyzer.py 调用。

启动
    python -m streamlit run app.py
    或双击 run_ui.bat

对应设计的步骤
    第 1 步 公共分析函数  analyzer.analyze_stock / load_from_db / generate_llm_summary / load_ohlc
    第 2 步 最小界面      侧边栏搜索框 + 分析按钮 + 结果展示
    第 3 步 历史查询      先查 SQLite，有则展示，无则实时分析；含「强制刷新」
    第 4 步 可视化        K 线 + 分型 / 笔 / 中枢 / 买卖点标注
    第 5 步 LLM 按需      先出规则结果，「生成 AI 总结」按钮
    第 6 步 自选股与日报  编辑股票池（写 config/watchlist.local.yaml）+ 查看历史日报
    第 7 步 双皮肤        左下角 🌙 夜晚 / ☀️ 白天 切换（skin.py，纯 CSS 联动）
"""
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import skin
from analyzer import (analyze_stock, ensure_kline, generate_llm_summary, load_from_db,
                      load_ohlc)
from fundamentals import fmt_num, fmt_pct, fmt_yi, fmt_yi_plain
from storage_kline import load_kline
from signal_score import label_of as score_label
from structure_gap import GAP_THRESHOLD, label_of as gap_label
from watchlist_store import (MAX_NAME_LEN, MAX_STOCKS, is_custom, load_default,
                             load_pool_name, load_watchlist, normalize_name, parse_codes,
                             reset_watchlist, save_pool_name, save_watchlist,
                             validate_codes)

LEVEL_TAG = {"第一类买点": "1买", "第二类买点": "2买", "第三类买点": "3买",
             "第一类卖点": "1卖", "第二类卖点": "2卖", "第三类卖点": "3卖"}

REPORT_DIR = Path("reports")


# ==================== 视图层小工具（无业务逻辑）====================

def esc(v):
    if v is None:
        return "—"
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def html(s):
    """注入样式 / 片段。st.html 是官方接口，无需 unsafe_allow_html，且不占版面。"""
    st.html(s)


def is_night():
    return st.session_state.get("skin", "day") == "night"


def section(no, title, hint=""):
    return (f'<div class="chx-sec"><span class="no">{no}</span><h2>{title}</h2>'
            f'<span class="rule"></span><span class="hint">{esc(hint)}</span></div>')


def metric_cards(items):
    """items: [(标签, 值, 值后缀, 副行, 值配色class)]，值用等宽字体。"""
    cells = []
    for label, value, suffix, sub, cls in items:
        unit = f"<u>{esc(suffix)}</u>" if suffix else ""
        cells.append(f'<div class="chx-metric"><div class="k">{esc(label)}</div>'
                     f'<div class="v {cls}">{esc(value)}{unit}</div>'
                     f'<div class="s">{sub}</div></div>')
    return '<div class="chx-metrics">' + "".join(cells) + "</div>"


def table(headers, rows, note=""):
    """rows: [[单元格HTML, ...]]，单元格自行负责转义 / 上色。"""
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    note_html = f'<div class="chx-footline">{note}</div>' if note else ""
    return ('<div class="chx-card tight"><div class="chx-scroll">'
            f'<table class="chx-table"><thead><tr>{head}</tr></thead>'
            f'<tbody>{body}</tbody></table></div>{note_html}</div>')


REPORT_CARD_CSS = (
    '<style>'
    '.chx-reports{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}'
    '@media (max-width:1100px){.chx-reports{grid-template-columns:repeat(2,minmax(0,1fr))}}'
    '.chx-reports .report{border:1px solid var(--line);border-radius:13px;background:var(--card);'
    'padding:14px 16px;position:relative;overflow:hidden;box-shadow:var(--shadow)}'
    '.chx-reports .report.on{border-color:color-mix(in srgb,var(--accent) 45%,transparent)}'
    '.chx-reports .report.on:after{content:"";position:absolute;left:0;top:0;bottom:0;width:2px;'
    'background:var(--accent);box-shadow:0 0 12px var(--accent)}'
    '.chx-reports .report .d{font-family:var(--mono);font-size:15px;color:var(--txt);letter-spacing:.04em}'
    '.chx-reports .report .m{font-family:var(--mono);font-size:11px;color:var(--txt-3);'
    'margin-top:9px;line-height:1.9}'
    '.chx-reports .report .m b{color:var(--txt-2);font-weight:500}'
    '.chx-reports .report .go{margin-top:12px;font-size:11.5px;color:var(--accent);'
    'font-family:var(--mono);letter-spacing:.06em}'
    '.chx-badge{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:5px;'
    'border:1px solid var(--line);color:var(--txt-3)}'
    '</style>'
)


def list_daily_reports():
    """reports/ 下有 report.md 的日期目录，倒序。"""
    if not REPORT_DIR.exists():
        return []
    return sorted([p.name for p in REPORT_DIR.iterdir()
                   if p.is_dir() and (p / "report.md").exists()], reverse=True)


def report_counts(day):
    """从 signals.csv 估一下这份日报的信号量；读不到就返回 0。"""
    p = REPORT_DIR / day / "signals.csv"
    if not p.exists():
        return 0, 0
    try:
        csv = pd.read_csv(p)
        n = len(csv)
        n_main = 0
        if "is_primary" in csv.columns:
            n_main = int(pd.to_numeric(csv["is_primary"], errors="coerce").fillna(0).sum())
        elif "主信号" in csv.columns:
            n_main = int((csv["主信号"].astype(str).str.strip() == "★").sum())
        return n, n_main
    except Exception:
        return 0, 0


def warm_up(codes):
    """保存股票池前逐只试拉一次数据，顺便把历史补齐。

    返回 (预热成功的代码, [(代码, 失败原因), ...], 总行数)。
    """
    warmed, failed, rows = [], [], 0
    bar = st.progress(0.0, text="准备中…")
    for i, c in enumerate(codes, 1):
        bar.progress((i - 1) / len(codes), text=f"[{i}/{len(codes)}] 校验并拉取 {c} …")
        try:
            src, err = ensure_kline(c, str(date.today()))
        except Exception as e:
            src, err = None, f"{type(e).__name__}: {e}"
        if src is None:
            failed.append((c, err or "拿不到数据"))
        else:
            warmed.append(c)
            try:
                rows += len(load_kline(c))
            except Exception:
                pass
    bar.progress(1.0, text="完成")
    return warmed, failed, rows


@st.cache_data(ttl=300, show_spinner=False)
def invalidated_signals(limit=5):
    """B7：对比历史日报里的信号清单与当前库，找出「曾经播报、现在失效」的信号。

    两种失效：
      · 消失      —— 日报里有，当前库里查不到（笔被重划 / 历史数据被修正）
      · 不可交易  —— 日报里 is_tradable=1，现在变成 0

    实测**确认后**的信号改写率是 0（ADR-011 / verify_robustness 的 555 条 0 改写），
    所以正常情况下这里应该是空的 —— 它是一个**安全网**，不是常规提醒。
    """
    from contextlib import closing

    from storage_signal import connect
    dates = list_daily_reports()[:limit]
    seen = []
    for d in dates:
        p = REPORT_DIR / d / "signals.csv"
        if not p.exists():
            continue
        try:
            df = pd.read_csv(p, encoding="utf-8-sig", dtype=str)
        except Exception:
            continue
        for r in df.to_dict("records"):
            seen.append({"report": d,
                         "code": str(r.get("stock_code", "")).strip().zfill(6),
                         "date": str(r.get("signal_date", "")).strip(),
                         "type": str(r.get("signal_type", "")).strip(),
                         "was_tradable": str(r.get("is_tradable", "1")).strip() == "1"})
    if not seen:
        return []
    codes = sorted({s["code"] for s in seen})
    have = {}
    try:
        with closing(connect()) as conn:
            q = ("SELECT stock_code, signal_date, signal_type, is_tradable FROM signals"
                 " WHERE stock_code IN (%s)" % ",".join("?" * len(codes)))
            for r in conn.execute(q, codes):
                have[(r[0], r[1], r[2])] = r[3]
    except Exception:
        return []
    out = []
    for s in seen:
        k = (s["code"], s["date"], s["type"])
        if k not in have:
            out.append({**s, "reason": "已消失（信号被重算掉）"})
        elif s["was_tradable"] and not have[k]:
            out.append({**s, "reason": "变为不可交易"})
    return out


def stock_score_history(code):
    """B8：本股历史上按打分分组的表现。返回 [[打分, 窗口, 样本, 平均收益, 胜率], ...]"""
    from contextlib import closing

    from signal_score import label_of, score_of
    from storage_signal import connect
    sql = ("SELECT b.window AS w, b.return_pct AS r,"
           " s.zs_gap_days AS g, s.zs_width_pct AS wd"
           " FROM backtest b JOIN signals s"
           "   ON s.stock_code=b.stock_code AND s.signal_date=b.signal_date"
           "  AND s.signal_type=b.signal_type"
           " WHERE b.stock_code=? AND b.scope='signal'")
    try:
        with closing(connect()) as conn:
            rows = [dict(x) for x in conn.execute(sql, (code,))]
    except Exception:
        return []
    if not rows:
        return []
    agg = {}
    for r in rows:
        s = score_of(r["wd"], r["g"])
        agg.setdefault((s, r["w"]), []).append(r["r"])
    out = []
    for s in (2, 1, 0):
        for w in (5, 10, 20):
            v = agg.get((s, w))
            if not v or len(v) < 3:
                continue
            ret = sum(v) / len(v)
            win = sum(1 for x in v if x > 0) / len(v)
            out.append([f'<span class="star">{label_of(s)}</span>', f"{w}日", len(v),
                        f'<span class="{"chx-up" if ret >= 0 else "chx-down"}">{ret:+.2%}</span>',
                        f"{win:.1%}"])
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def pool_names(codes):
    """代码 -> 公司简称。取不到就退化成空串，**绝不因为网络问题把页面搞崩**。"""
    from signal_filter import fetch_name
    out = {}
    for c in codes:
        try:
            out[c] = fetch_name(c) or ""
        except Exception:
            out[c] = ""
    return out


def pool_alerts(day=None):
    """当日池内「已确认 + 可交易」的信号。

    返回 (日期, 总条数, {代码: 条数})。
    「当日」是**日历上的今天** —— 批处理 18:05 跑完后，当天确认的信号就会让红点亮起；
    第二天自然消失（那就是一次通知，不是一个常驻状态）。
    """
    from contextlib import closing

    from storage_signal import connect
    d = str(day or date.today())
    codes = load_watchlist()
    if not codes:
        return d, 0, {}
    sql = ("SELECT stock_code, COUNT(*) FROM signals "
           "WHERE confirm_date = ? AND is_tradable = 1 AND stock_code IN (%s) "
           "GROUP BY stock_code" % ",".join("?" * len(codes)))
    try:
        with closing(connect()) as conn:
            rows = list(conn.execute(sql, [d] + list(codes)))
    except Exception:
        return d, 0, {}
    by = {r[0]: r[1] for r in rows}
    return d, sum(by.values()), by


def render_scan_page():
    """全市场买点（T18 / ADR-022）。只展示，不算回测/基本面/LLM；点一行跳单股分析。"""
    import market_scan as mscan

    st.title("全市场买点")
    st.caption("扫全市场（沪深主板 / 创业板 / 科创板，排除北交所与 ST），**只挑买点**。"
               "窗口按**确认日**算 —— 只放已经可确认的信号，未确认的不入选"
               "（否则会出现「今天有、明天没了」）。")

    rows, sd = mscan.query_scan()
    if not rows:
        st.info("还没有扫描结果。先跑一次 `python market_scan.py`"
                "（可先 `--limit 50` 试跑；全市场首次约 4~5 小时，之后数据已缓存约 1 小时）。")
        return

    n_scan = len(mscan.done_codes(sd))
    st.caption(f"扫描日 {sd} · 已扫 {n_scan} 只 · 原始命中 {len(rows)} 条")

    all_types = sorted({r["signal_type"] for r in rows})
    pref = [t for t in all_types if "第三类" in t] or all_types
    c1, c2, c3 = st.columns([2, 2, 2])
    types = c1.multiselect("买点类型", all_types, default=pref)
    amts = [r["amount_yi"] or 0 for r in rows]
    hi = float(round(max(amts) if amts else 1.0, 1))
    min_amt = c2.slider("近 20 日日均成交额下限（亿）", 0.0, max(hi, 0.5),
                        min(2.0, hi), 0.5)
    drop_far = c3.checkbox(f"排除距中枢 ≥ {GAP_THRESHOLD} 日（H3）", value=True)

    sel = [r for r in rows if r["signal_type"] in types
           and (r["amount_yi"] or 0) >= min_amt
           and (not drop_far
                or (r["zs_gap_days"] if r["zs_gap_days"] is not None else -1) < GAP_THRESHOLD)]
    st.caption(f"筛出 **{len(sel)}** 条（原始 {len(rows)} 条）")
    if not sel:
        st.info("当前筛选下没有结果，放宽一点试试。")
        return

    df = pd.DataFrame([{
        "打分": score_label(r.get("score")),
        "代码": r["stock_code"], "名称": r["stock_name"], "类型": r["signal_type"],
        "信号日": r["signal_date"], "确认日": r["confirm_date"],
        "距中枢": gap_label(r["zs_gap_days"]), "成交额(亿)": r["amount_yi"],
    } for r in sel])  # rows 已按 score 从高到低排
    ev = st.dataframe(df, width="stretch", hide_index=True, on_select="rerun",
                      selection_mode="single-row",
                      height=min(600, 80 + 35 * min(len(df), 15)))
    picked = []
    try:
        picked = list(ev.selection["rows"])
    except Exception:
        picked = []
    if picked:
        # 只写中间键；真正的模式切换在脚本顶部做（控件创建之前）
        st.session_state["_jump_code"] = str(df.iloc[picked[0]]["代码"])
        st.rerun()

    st.caption("打分行 = 「中枢宽度 ≥ 0.12」+「距中枢结束 < 10 日」。"
               "**已通过时间样本外验证**（训练 2015-2020 / 检验 2021-2026，ADR-024）："
               "检验期两个条件都满足的信号，5/10/20 日超额 +2.98% / +3.34% / +4.07%，"
               "比都不满足的高 +1.67% / +2.28% / +3.48%（三窗口 CI 均不跨 0、分半一致、单调）。"
               "⚠️ 样本内普遍比样本外乐观约 **30%**；这里的「超额」是相对同股票随机入场，"
               "不是收益率，不含成本与容量约束。")
    st.caption("点一行 → 跳转单股分析。本页只放代码 / 名称 / 类型 / 日期 / 距中枢 / 成交额；"
               "**不算回测、不抓基本面、不调 LLM** —— 那些点进去之后按需触发。")
    st.caption("提示：全市场原始命中通常上千条，务必用上面三个条件收窄 —— "
               "实测「成交额 ≥2 亿 + 排除 H3 远 + 只看三类买点」可压到约 70 只。")


def render_pool_page():
    """自选股编辑 + 历史日报查看（页面下半部分就是日报，点开即读）。"""
    pool = load_watchlist()
    custom = is_custom()
    src_txt = "自定义（config/watchlist.local.yaml）" if custom else "仓库默认（config/settings.yaml）"
    dates = list_daily_reports()

    html('<div class="chx-hero"><div class="chx-hero-top"><div>'
         '<div class="chx-title">自选股与日报</div>'
         f'<div class="chx-meta">股票池决定每日批处理扫哪些股票 · 上限 {MAX_STOCKS} 只 · 6 位 A 股代码</div>'
         '</div>'
         f'<div class="chx-price"><b class="chx-cy">{len(pool)}<small>只</small></b>'
         f'<span>{esc(src_txt)}</span></div></div>'
         '<div class="chx-strip">'
         f'<div>当前池<b>{len(pool)} 只</b></div>'
         f'<div>日报份数<b>{len(dates)} 份</b></div>'
         f'<div>最近日报<b>{esc(dates[0]) if dates else "—"}</b></div>'
         '<div>批处理<b>工作日 18:05</b></div>'
         '</div></div>')

    ver = st.session_state.get("pool_ver", 0)
    name_now = load_pool_name()
    html(section("01", "股票池", f"当前名称：{esc(name_now)} · 上限 {MAX_STOCKS} 只"))

    # —— 池名称（只改显示名，不影响批处理扫哪些股票）——
    c1, c2 = st.columns([3, 1])
    name_in = c1.text_input(
        "池名称", value=name_now, max_chars=MAX_NAME_LEN, key=f"pool_name_{ver}",
        help="只改这个池的显示名。保存后池会变成「自定义」（写进 "
             "config/watchlist.local.yaml，不进版本库）")
    nm_new = normalize_name(name_in)
    if c2.button("保存名称", width="stretch", disabled=(nm_new == name_now)):
        save_pool_name(nm_new)
        st.session_state["pool_ver"] = ver + 1
        st.session_state["pool_result"] = {"saved": load_watchlist(), "failed": [],
                                           "rows": 0, "renamed": nm_new}
        st.rerun()

    # —— 池内清单：代码 + 公司名称 + 当日确认信号 ——
    _names = pool_names(tuple(pool))
    _ad, _an, _aby = pool_alerts()
    html(table(["代码", "名称", "当日确认信号", "来源"],
               [[esc(c), esc(_names.get(c) or "—"),
                 (f'<span class="tag warn">{_aby[c]} 条</span>' if _aby.get(c) else "—"),
                 "自定义" if custom else "仓库默认"] for c in pool],
               f"共 {len(pool)} 只 · 「当日确认信号」= {_ad} 已确认且可交易的信号数"
               + (f"（池内合计 {_an} 条）" if _an else "")))

    # —— 信号失效自检（B7）——
    _inv = invalidated_signals()
    if _inv:
        st.warning(f"⚠️ 有 **{len(_inv)}** 条曾播报过的信号现在失效了")
        html(table(["日报", "代码", "信号日", "类型", "失效原因"],
                   [[esc(x["report"]), esc(x["code"]), esc(x["date"]), esc(x["type"]),
                     f'<span class="tag warn">{esc(x["reason"])}</span>'] for x in _inv],
                   "这些信号曾出现在上面的日报里，但当前库里查不到，或已从可交易变成不可交易。"))
    else:
        st.caption("信号失效自检：最近几份日报里的信号与当前库**完全一致（0 条失效）**。"
                   "这是预期状态 —— 确认后的信号实测改写率为 0（ADR-011 的 555 条 0 改写）。")

    st.caption("编辑池内容（每行一个，或逗号 / 空格分隔）")
    text = st.text_area("股票代码", value=chr(10).join(pool), height=200,
                        key=f"pool_text_{ver}",
                        help=f"每行一个，或用逗号/空格分隔；最多 {MAX_STOCKS} 只，6 位数字")
    codes = parse_codes(text)
    ok_codes, bad = validate_codes(codes)
    # 上限只约束用户自己设的池：仓库默认池若被改成超过 MAX_STOCKS（历史样本），
    # 原样不动就不拦，否则一打开页面就报「最多 10 只」、两个按钮全灰，什么也干不了。
    too_many = len(ok_codes) > MAX_STOCKS and ok_codes != pool

    if bad:
        st.error("这些不是有效代码：" + "、".join(f"`{c}`（{why}）" for c, why in bad))
    if too_many:
        st.error(f"最多 {MAX_STOCKS} 只，当前填了 {len(ok_codes)} 只")
    if ok_codes and not bad and not too_many:
        if ok_codes == pool:
            st.caption(f"与当前池一致（{len(ok_codes)} 只），无需替换。")
            if len(pool) > MAX_STOCKS and not custom:
                st.caption(f"（仓库默认池是 {len(pool)} 只技术样本，超过 {MAX_STOCKS} 只上限；"
                           f"原样不动就不拦。你自己设的池最多 {MAX_STOCKS} 只。）")
        else:
            st.caption(f"将替换为 {len(ok_codes)} 只：" + "、".join(ok_codes))

    can_save = bool(ok_codes) and not bad and not too_many and ok_codes != pool
    c1, c2 = st.columns([1, 1])
    if c1.button("替换股票池", type="primary", width="stretch", disabled=not can_save):
        st.session_state["pool_pending"] = ok_codes
    if c2.button(f"恢复默认（{len(load_default())} 只）", width="stretch",
                 disabled=not custom):
        reset_watchlist()
        st.session_state["pool_ver"] = ver + 1
        st.session_state["pool_result"] = {"saved": load_watchlist(), "failed": [],
                                           "rows": 0, "reset": True}
        st.rerun()

    pending = st.session_state.get("pool_pending")
    if pending:
        st.warning(
            f"**确认替换？** 股票池将从 {len(pool)} 只变成 {len(pending)} 只："
            + "、".join(pending) + chr(10) + chr(10)
            + "⚠️ **换池后前几天，新股票报告里的「回测统计」一栏会是空的 —— 这是正常的。**"
            + chr(10) + chr(10)
            + "回测统计用的是数据库里**已有的历史信号**，新股票还没积累；而且缠论信号本身"
            + "有 1~2 个交易日的确认延迟（ADR-011），历史信号只能一天天攒起来。"
            + chr(10)
            + "结构、买卖点、过滤结果、AI 总结这些内容当天就有。"
        )
        c1, c2 = st.columns([1, 1])
        if c1.button("确认替换", type="primary", width="stretch"):
            with st.spinner("逐只校验并预热数据（首次会慢一些）…"):
                warmed, failed, rows = warm_up(pending)
            saved = save_watchlist(pending)
            st.session_state["pool_result"] = {"saved": saved, "failed": failed,
                                               "rows": rows}
            st.session_state.pop("pool_pending", None)
            st.session_state["pool_ver"] = ver + 1
            st.rerun()
        if c2.button("取消", width="stretch"):
            st.session_state.pop("pool_pending", None)
            st.rerun()

    info = st.session_state.pop("pool_result", None)
    if info:
        if info.get("reset"):
            st.success(f"已恢复仓库默认池（{len(info['saved'])} 只）。")
        elif info.get("renamed") and not info.get("failed"):
            st.success(f"池名称已改为「{info['renamed']}」。")
        else:
            st.success(f"股票池已更新为 {len(info['saved'])} 只：" + "、".join(info["saved"]))
        if info["failed"]:
            st.error("以下代码拉不到数据，**建议删掉后重新保存**"
                     "（可能是无效代码、已退市，或当时网络不通）："
                     + chr(10) + chr(10)
                     + chr(10).join(f"- `{c}`：{why}" for c, why in info["failed"]))
        if info["rows"]:
            st.caption(f"已预热历史数据共 {info['rows']} 行；第二天批处理不用再临时拉取。")

    html(section("02", "历史日报", f"reports/ 目录 · 共 {len(dates)} 份"))
    if not dates:
        st.info("还没有日报。跑一次 `python main.py`，或双击 `run.bat`，就会有第一份。")
        return

    view = st.session_state.get("view_report")
    cards = []
    for d in dates[:12]:
        n_sig, n_main = report_counts(d)
        detail = (f'可交易信号 <b>{n_sig}</b> 条 · 主信号 <b>{n_main}</b> 条'
                  if n_sig else '<span class="chx-badge">无信号</span>')
        cards.append(f'<div class="report {"on" if view == d else ""}">'
                     f'<div class="d">{d}</div><div class="m">{detail}</div>'
                     f'<div class="go">{"正在查看 ↓" if view == d else "查看这份日报 →"}</div></div>')
    html(REPORT_CARD_CSS + '<div class="chx-reports">' + "".join(cards) + "</div>")

    c1, c2 = st.columns([3, 1])
    pick = c1.selectbox("选择日期", dates, key="report_pick",
                        label_visibility="collapsed")
    if c2.button("打开日报", width="stretch"):
        st.session_state["view_report"] = pick
        st.rerun()

    if not view:
        return
    path = REPORT_DIR / view / "report.md"
    if not path.exists():
        st.error(f"文件不存在：{path}")
        return
    html(section("03", f"日报 · {view}", f"reports/{view}/report.md"))
    csv_path = path.parent / "signals.csv"
    if csv_path.exists():
        st.download_button("下载 signals.csv", csv_path.read_bytes(),
                           file_name=f"signals-{view}.csv", mime="text/csv")
    html('<div class="chx-card">')
    st.markdown(path.read_text(encoding="utf-8"))
    html("</div>")


st.set_page_config(page_title="缠论分析 Agent", layout="wide")

# ==================== 皮肤：白天 / 夜晚 ====================
html(skin.build_css())

# 「从扫描页点进来」的跳转处理。
# 必须在**控件创建之前**改它们的 state —— Streamlit 不允许在控件实例化后再改。
# 所以扫描页只写中间键 _jump_code，这里在脚本顶部消费掉。
_jump = st.session_state.pop("_jump_code", None)
if _jump:
    st.session_state["mode_radio"] = "单股分析"
    st.session_state["code_input"] = str(_jump)
    st.session_state["auto_run"] = True

with st.sidebar:
    html('<div class="chx-brand"><div class="logo">缠</div>'
         '<div><b>缠论分析 Agent</b><span>CHAN · TERMINAL</span></div></div>')
    st.divider()
    # ?page=pool / ?page=scan 可直接打开对应页，方便收藏 / 分享链接。
    # 用 key= 让「从扫描页点进来」能设置控件状态（Streamlit 只能在控件创建**之前**改它的 state）。
    _modes = ["单股分析", "自选股与日报", "全市场买点"]
    if "mode_radio" not in st.session_state:
        st.session_state["mode_radio"] = {"pool": "自选股与日报",
                                           "scan": "全市场买点"}.get(
            st.query_params.get("page"), "单股分析")
    st.session_state.setdefault("code_input", "600519")

    # 当日池内有确认信号 -> 在「自选股与日报」这一项挂红点。
    # 用 format_func 而不是改选项字符串：值保持干净，其它地方不用跟着解析后缀。
    _alert_day, _alert_n, _alert_by = pool_alerts()

    _inv_n = len(invalidated_signals())

    def _mode_label(m):
        if m != "自选股与日报":
            return m
        if _inv_n:
            return m + " ⚠️"          # 曾播报的信号失效了 —— 这个比新信号更需要看
        return (m + " 🔴") if _alert_n else m

    mode = st.radio("模式", _modes, key="mode_radio", format_func=_mode_label,
                    label_visibility="collapsed")
    if _inv_n:
        st.caption(f"⚠️ 有 **{_inv_n}** 条曾播报的信号已失效，去「自选股与日报」看")
    if _alert_n:
        st.caption(f"🔴 {_alert_day} 池内 **{_alert_n}** 条新确认信号"
                   f"（{'、'.join(sorted(_alert_by))}）")
    st.divider()

    if mode == "单股分析":
        code_in = st.text_input("股票代码", max_chars=6, help="6 位 A 股代码", key="code_input")
        date_in = st.date_input("报告日期", value=date.today())
        html('<div class="chx-sec">选项</div>')
        force = st.checkbox("强制刷新", value=False, help="跳过本地历史，重新实时计算")
        with_llm = st.checkbox("分析时立即调用 LLM", value=False,
                               help="默认关闭：先看规则结果，再按需生成")
        with st.container(key="cta_main"):
            run_btn = st.button("开始分析", type="primary", width="stretch")
        st.divider()
        st.caption("数据：本地 Parquet（腾讯日线 + 前复权因子）· 信号 / 回测 / LLM：本地 SQLite")
    elif mode == "自选股与日报":
        _pool = load_watchlist()
        html(f'<div class="chx-note">当前股票池 <b>{len(_pool)}</b> 只'
             + ("（自定义）" if is_custom() else "（仓库默认）") + '</div>')
        st.caption("在右侧编辑股票池、查看历史日报。")
        run_btn = False
    else:
        st.caption("全市场扫描结果。点一行即可跳到单股分析。")
        run_btn = False

    # 皮肤开关放最下面：🌙 夜晚 / ☀️ 白天
    st.divider()
    # 白天开关：勾选 = ☀️ 白天（默认），未勾选 = 🌙 夜晚
    with st.container(key="chx_skin"):
        day_on = st.checkbox("skin_day", label_visibility="collapsed",
                             value=st.session_state.get("skin", "day") == "day",
                             key="skin_day_toggle")
    st.session_state["skin"] = "day" if day_on else "night"

if mode == "自选股与日报":
    render_pool_page()
    st.stop()

if mode == "全市场买点":
    render_scan_page()
    st.stop()

# ==================== 第 3 步：先查历史，无则分析 ====================
if run_btn or st.session_state.pop("auto_run", False):
    code = (code_in or "").strip()
    if len(code) != 6 or not code.isdigit():
        st.error("请输入 6 位数字股票代码")
        st.stop()

    res, note = None, ""
    with st.spinner("分析中…"):
        if not force:
            res = load_from_db(code, str(date_in))
            if res is not None:
                note = "命中本地历史（未重新计算）"
        if res is None:
            res = analyze_stock(code, str(date_in), use_llm=with_llm)
            note = "强制刷新，已重新计算" if force else "本地无记录，已转为实时计算"
    st.session_state["res"] = res
    st.session_state["note"] = note

res = st.session_state.get("res")
if res is None:
    html('<div class="chx-hero"><div class="chx-hero-top"><div>'
         '<div class="chx-title">缠论分析 Agent</div>'
         '<div class="chx-meta">在左侧输入 6 位 A 股代码，点「开始分析」</div></div></div>'
         '<div class="chx-strip">'
         '<div>第一步<b>输入代码</b></div>'
         '<div>第二步<b>选择日期</b></div>'
         '<div>第三步<b>开始分析</b></div>'
         '</div></div>')
    st.info("本地有历史的股票会直接读库（快、不花钱）；没有历史的会实时计算。"
            "左下角可切换 🌙 夜晚 / ☀️ 白天 两套界面。")
    st.caption("非投资建议 · 不含自动下单 · 数据来源：本地 Parquet + SQLite")
    st.stop()

if not res.get("ok"):
    st.error(res.get("error") or "分析失败")
    st.stop()

# ==================== 结果：头部带 + 读数卡 ====================
k = res.get("kline") or {}
stt = res.get("structure") or {}
sigs = res.get("signals") or []
note = st.session_state.get("note", "")

close = k.get("close_raw")
try:
    close_txt = f"{float(close):,.2f}"
except (TypeError, ValueError):
    close_txt = "—"

chg = None
try:
    _df = load_ohlc(res["code"])
    if len(_df) >= 2:
        chg = float(_df["close"].iloc[-1]) / float(_df["close"].iloc[-2]) - 1
except Exception:
    chg = None
if chg is None:
    chg_html, close_cls = "", "chx-dim"
else:
    chg_html = f'{"▲" if chg >= 0 else "▼"} {chg:+.2%}'
    close_cls = "chx-up" if chg >= 0 else "chx-down"

cached = res.get("source") == "cache"
html('<div class="chx-hero"><div class="chx-hero-top"><div>'
     f'<div class="chx-title">{esc(res.get("code"))}<small>{esc(res.get("name", ""))}</small></div>'
     f'<div class="chx-meta">{esc(res.get("board", ""))} · 涨跌幅限制 '
     f'±{res.get("limit_ratio", 0):.0%} · 来源 {esc(res.get("source"))} · {esc(note)}'
     f' · 耗时 {esc(res.get("elapsed"))}s</div></div>'
     f'<div class="chx-chip{" dim" if not cached else ""}">'
     f'{"已入库 · 无需重算" if cached else "实时计算"}</div>'
     f'<div class="chx-price"><b class="{close_cls}">{close_txt}<small>{chg_html}</small></b>'
     f'<span>最新收盘（不复权）· {esc(str(k.get("date", "")))}</span></div></div>'
     '<div class="chx-strip">'
     f'<div>信号数<b>{len(sigs)} 条</b></div>'
     f'<div>可交易<b>{res.get("tradable_count", 0)} 条</b></div>'
     f'<div>其中主信号<b>{res.get("primary_count", 0)} 条</b></div>'
     '</div></div>')

st.warning("**非投资建议**：本工具由程序按缠论规则自动生成结构化描述，"
           "不构成任何投资建议，不承诺收益，不含自动下单。据此操作风险自负。")

pos = stt.get("position", "—")
html(metric_cards([
    ("最新收盘（不复权）", close_txt, "", "前复权口径见下方图表说明", close_cls),
    ("价格位置", pos, "", "中枢 / 分型综合判定",
     "chx-cy" if pos and pos != "—" else "chx-dim"),
    ("信号数", len(sigs), "条", "当前窗口内缠论买卖点", ""),
    ("可交易", res.get("tradable_count", 0), "条", "F3 过滤后", ""),
    ("其中主信号", res.get("primary_count", 0), "条", "同日多信号已归集", "chx-cy"),
]))

# ==================== 第 4 步：K 线可视化 ====================
html(section("01", "K 线与缠论标注", f"{len(sigs)} 条信号 · 前复权"))
df = load_ohlc(res["code"])
if df.empty:
    st.info("无 K 线数据")
else:
    C = skin.chart_colors(is_night())
    fig = go.Figure()
    fig.add_trace(go.Candlestick(x=df["date"], open=df["open"], high=df["high"],
                                 low=df["low"], close=df["close"], name="K线",
                                 increasing_line_color=C["up"], increasing_fillcolor=C["up"],
                                 decreasing_line_color=C["down"], decreasing_fillcolor=C["down"]))

    for z in (stt.get("zs_list") or []):
        fig.add_shape(type="rect", x0=z["sdt"], x1=z["edt"], y0=z["zd"], y1=z["zg"],
                      fillcolor="rgba(124,92,255,0.13)" if is_night() else "rgba(91,75,224,0.10)",
                      line=dict(color=C["zs"], width=1), layer="below")

    bis = stt.get("bi_list") or []
    xs, ys = [], []
    for b in bis:
        p0 = b["low"] if b["direction"] == "向上" else b["high"]
        p1 = b["high"] if b["direction"] == "向上" else b["low"]
        if not xs:
            xs.append(b["sdt"])
            ys.append(p0)
        xs.append(b["edt"])
        ys.append(p1)
    if xs:
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", name="笔",
                                 line=dict(color=C["bi"], width=1.3)))

    fxs = stt.get("fx_list") or []
    for tag, sym, color, name in (("顶", "triangle-down", C["up"], "顶分型"),
                                  ("底", "triangle-up", C["down"], "底分型")):
        pts = [f for f in fxs if tag in f["mark"]]
        if pts:
            fig.add_trace(go.Scatter(x=[f["dt"] for f in pts], y=[f["fx"] for f in pts],
                                     mode="markers", name=name,
                                     marker=dict(symbol=sym, size=8, color=color, opacity=0.85)))

    # 用位置索引：df 有名为 date 的列，r.date 会遮蔽 Timestamp.date() 方法
    pos_i = {str(d.date()): i for i, d in enumerate(df["date"])}
    for sg in sigs:
        i = pos_i.get(str(sg["date"]))
        if i is None:
            continue
        row = df.iloc[i]
        is_buy = sg["direction"] == "买"
        y = float(row["low"]) * 0.985 if is_buy else float(row["high"]) * 1.015
        color = C["buy"] if is_buy else C["sell"]
        fig.add_trace(go.Scatter(
            x=[row["date"]], y=[y], mode="markers+text",
            text=[LEVEL_TAG.get(sg["type"], sg["type"][:3])],
            textposition="bottom center" if is_buy else "top center",
            textfont=dict(color=color, size=11),
            name=sg["type"], showlegend=False,
            marker=dict(symbol="star", size=16, color=color,
                        line=dict(color=C["ring"], width=0.8)),
            hovertemplate=(f"{sg['date']} {sg['type']}<br>"
                           f"确认日 {sg.get('confirm_date') or '待确认'}<br>"
                           f"入场参考 {sg.get('entry_ref_price') or '-'}"
                           "<extra></extra>")))

    fig.update_layout(height=560, xaxis_rangeslider_visible=False,
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(family=skin.MONO, size=11, color=C["axis"]),
                      margin=dict(l=8, r=8, t=30, b=8),
                      legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0,
                                  font=dict(color=C["axis"], size=11),
                                  bgcolor="rgba(0,0,0,0)"),
                      hovermode="x unified",
                      xaxis=dict(gridcolor=C["grid"], linecolor=C["line"], zeroline=False,
                                 showspikes=True, spikecolor=C["axis"], spikethickness=1),
                      yaxis=dict(gridcolor=C["grid"], linecolor=C["line"], zeroline=False,
                                 side="right"))
    st.plotly_chart(fig, width="stretch", theme=None)
    st.caption("灰色折线 = 笔；紫框 = 中枢；三角 = 分型；星标 = 买卖点（★青=买 / ★琥珀=卖）。"
               "价格为前复权口径，与「最新收盘（不复权）」在历史日期上会有差异。")

# ==================== 公司基本面 ====================
fin = res.get("fundamentals") or {}
html(section("02", "公司基本面", fin.get("industry") or "暂无数据"))
if not fin:
    st.info("暂无基本面数据（接口不可用，或该股无记录）。")
else:
    _ry, _ny = fin.get("revenue_yoy"), fin.get("net_profit_yoy")
    html(metric_cards([
        ("PE (TTM)", fmt_num(fin.get("pe_ttm")), "", "静态 " + fmt_num(fin.get("pe_static")), "chx-cy"),
        ("PB", fmt_num(fin.get("pb")), "", "市净率 · 腾讯快照", ""),
        ("总市值", fmt_yi_plain(fin.get("total_cap_yi")), "", "亿元 · 腾讯快照", ""),
        ("ROE", fmt_pct(fin.get("roe")), "", esc(fin.get("report_period") or "—"), ""),
        ("营收同比", fmt_pct(_ry), "", "净利润同比 " + fmt_pct(_ny),
         "chx-up" if (_ry or 0) >= 0 else "chx-down"),
    ]))

    prof_rows = [
        ["所属行业", esc(fin.get("industry") or "—")
         + (f'（{esc(fin.get("market"))}）' if fin.get("market") else "")],
        ["中证行业分类", esc(fin.get("industry_cninfo") or "—")],
        ["上市日期", esc(fin.get("listing_date") or "—")],
    ]
    if fin.get("main_business"):
        prof_rows.append(["主营业务", f'<span class="txt">{esc(fin["main_business"])}</span>'])
    html(table(["项目", "内容"], prof_rows))

    hist = fin.get("history") or []
    if hist:
        hrows = []
        for h in hist:
            ry, ny = h.get("revenue_yoy"), h.get("net_profit_yoy")
            hrows.append([
                esc(h.get("report_period")), fmt_yi(h.get("revenue")),
                f'<span class="{"chx-up" if (ry or 0) >= 0 else "chx-down"}">{fmt_pct(ry)}</span>',
                fmt_yi(h.get("net_profit")),
                f'<span class="{"chx-up" if (ny or 0) >= 0 else "chx-down"}">{fmt_pct(ny)}</span>',
                fmt_pct(h.get("roe")),
            ])
        html(table(["报告期", "营业总收入", "同比", "净利润", "同比", "ROE"], hrows,
                   "数据源：巨潮（公司概况 / 行业）+ 同花顺（财务摘要）+ 腾讯（估值快照）。"
                   "基本面只用于交代背景，不参与信号过滤与回测 —— 财报有披露滞后，"
                   "用最新财报评估历史信号会造成前视偏差（ADR-017）。"))
    if fin.get("errors"):
        st.caption("部分基本面数据获取失败：" + "；".join(fin["errors"]))

# ==================== 信号明细 ====================
html(section("03", "信号明细", f"{len(sigs)} 条 · 按信号日倒序"))
if sigs:
    rows = []
    for x in sigs[::-1]:
        is_buy = x["direction"] == "买"
        tag = f'<span class="tag {"buy" if is_buy else "sell"}">{esc(x["type"])}</span>'
        tradable = ('<span class="tag ok">是</span>' if x["is_tradable"]
                    else '<span class="tag no">否</span>')
        star = '<span class="star">★</span>' if x.get("is_primary") else ""
        filt = esc(x.get("filter_codes") or "通过")
        if filt != "通过":
            filt = f'<span class="tag warn">{filt}</span>'
        if x.get("confirm_date"):
            confirm = esc(x["confirm_date"])
        else:
            confirm = '<span class="tag warn">待确认</span>'
        price = x.get("entry_ref_price")
        price = f"{float(price):.2f}" if price else "—"
        # H3（ADR-021）：距最近中枢结束的交易日数，>=10 日的历史超额明显偏低
        gap = x.get("zs_gap_days")
        if gap is None:
            gap_cell = "—"
        elif gap < 0:
            gap_cell = '<span class="tag">无中枢</span>'
        elif gap >= GAP_THRESHOLD:
            gap_cell = f'<span class="tag warn">⚠️ {gap} 日</span>'
        else:
            gap_cell = f"{gap} 日"
        # 排序打分（ADR-023/024）：[中枢宽度>=0.12] + [距中枢<10日]，已通过时间样本外验证
        score_cell = f'<span class="star">{score_label(x.get("zs_score"))}</span>'
        rows.append([esc(x["date"]), confirm, tag, score_cell, gap_cell, price, tradable, star,
                     filt, f'<span class="txt">{esc(x.get("reason", ""))}</span>'])
    html(table(["信号日", "确认日", "类型", "打分", "距中枢", "入场参考价", "可交易", "主信号",
                "过滤", "理由"],
               rows,
               "信号日 = 触发笔结束日；确认日 = 信号日 + 实测确认延迟（ADR-011，通常 1~2 个交易日）；"
               "打分 = [中枢宽度 ≥ 0.12] + [距中枢结束 < 10 日]，★★ / ★ / —，"
               "已通过时间样本外验证（ADR-024：检验期 5/10/20 日超额差 +1.67% / +2.28% / +3.48%，"
               "CI 均不跨 0）；距中枢 = 结构离得远的信号可靠性下降（ADR-021）"))
else:
    st.info("该股票在当前窗口内没有缠论买卖点信号。")

# ==================== 回测统计 ====================
html(section("04", "回测统计参考", "双口径 · 5 / 10 / 20 日"))
bt = res.get("backtest") or []
if bt:
    rows = []
    for b in bt:
        r = b["avg_return"]
        rows.append([esc(b["signal_type"]), f'{b["window"]}日', b["n"],
                     f'<span class="{"chx-up" if r >= 0 else "chx-down"}">{r:+.2%}</span>',
                     f'{b["win_rate"]:.1%}', f'{abs(b["avg_mdd"]):.2%}'])
    html(table(["类型", "窗口", "样本", "平均收益", "胜率", "平均最大回撤"], rows,
               "口径：入场 = 确认日次一交易日开盘；卖点收益已做方向调整"
               "（价格下跌记为正）。样本少时不具解释力。"))
else:
    st.info("无回测数据。")

# —— 本股历史打分表现（B8）——
_sc_hist = stock_score_history(code)
if _sc_hist:
    st.caption("**本股历史上按打分分组的表现**（只用这一只股票自己的历史信号）")
    html(table(["打分", "窗口", "样本", "平均收益", "胜率"], _sc_hist,
               "打分 = [中枢宽度 >= 0.12] + [距中枢结束 < 10 日]。"
               "这是**本股**的数字，可能与全样本（ADR-024 检验期：★★ 约 +3.0%）差很多；"
               "样本 <= 3 的格子已省略。"))

# ==================== 第 5 步：LLM 按需生成 ====================
html(section("05", "AI 总结", "按需生成 · 失败自动降级为规则结果"))
llm = res.get("llm") or {}
has_text = bool(llm.get("text"))
col_btn, col_info = st.columns([1, 4])
if col_btn.button("生成 AI 总结", disabled=has_text, width="stretch"):
    with st.spinner("调用 DeepSeek…"):
        res["llm"] = generate_llm_summary(res)
        st.session_state["res"] = res
    llm = res["llm"]
    has_text = bool(llm.get("text"))
col_info.caption("规则结果无需 LLM 即可使用；AI 总结只做解释与归纳，失败会自动降级。")

if has_text:
    html('<div class="chx-card chx-ai">')
    st.markdown(llm["text"])
    st.caption(f"模型 {llm.get('model', '')} · token {llm.get('tokens', 0)}"
               f" · 费用 ¥{llm.get('cost', 0):.6f} · 缓存 {'是' if llm.get('cached') else '否'}")
    html("</div>")
elif llm.get("error"):
    st.warning(f"生成失败，已降级为仅规则结果：{llm['error']}")
else:
    st.caption("尚未生成。")

# ==================== 原始数据 ====================
html(section("06", "原始结构化数据", "analyze_stock 返回"))
with st.expander("展开查看 JSON", expanded=False):
    st.json(res, expanded=False)

html('<div class="chx-footline">本页面由程序自动生成 · 非投资建议 · 不含自动下单 · '
     '数据来源：本地 Parquet + SQLite</div>')
