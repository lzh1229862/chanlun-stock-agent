"""缠论分析 Web UI（Streamlit）。

后端逻辑一行未改 —— 全部通过 analyzer.py 调用。

启动
    python -m streamlit run app.py
    或双击 run_ui.bat

对应设计的 5 步
    第 1 步 公共分析函数  analyzer.analyze_stock / load_from_db / generate_llm_summary / load_ohlc
    第 2 步 最小界面      侧边栏搜索框 + 分析按钮 + 结果展示
    第 3 步 历史查询      先查 SQLite，有则展示，无则实时分析；含「强制刷新」
    第 4 步 可视化        K 线 + 分型 / 笔 / 中枢 / 买卖点标注
    第 5 步 LLM 按需      先出规则结果，「生成 AI 总结」按钮
    第 6 步 自选股与日报  编辑股票池（写 config/watchlist.local.yaml）+ 查看历史日报
"""
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from analyzer import (analyze_stock, ensure_kline, generate_llm_summary, load_from_db,
                      load_ohlc)
from storage_kline import load_kline
from watchlist_store import (MAX_STOCKS, is_custom, load_default, load_watchlist,
                             parse_codes, reset_watchlist, save_watchlist, validate_codes)

LEVEL_TAG = {"第一类买点": "1买", "第二类买点": "2买", "第三类买点": "3买",
             "第一类卖点": "1卖", "第二类卖点": "2卖", "第三类卖点": "3卖"}

REPORT_DIR = Path("reports")


def list_daily_reports():
    """reports/ 下有 report.md 的日期目录，倒序。"""
    if not REPORT_DIR.exists():
        return []
    return sorted([p.name for p in REPORT_DIR.iterdir()
                   if p.is_dir() and (p / "report.md").exists()], reverse=True)


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


def render_pool_page():
    """自选股编辑 + 历史日报查看（页面下半部分就是日报，点开即读）。"""
    st.title("自选股与日报")
    st.caption("股票池决定每日批处理扫哪些股票；改完第二天（周一至周五 18:05 的计划任务）自动生效。")

    pool = load_watchlist()
    custom = is_custom()
    src_txt = ("自定义（config/watchlist.local.yaml）" if custom
               else "仓库默认（config/settings.yaml）")
    st.caption(f"当前池 **{len(pool)}** 只 · 来源：{src_txt}")

    st.subheader("编辑股票池")
    ver = st.session_state.get("pool_ver", 0)
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
        else:
            st.success(f"股票池已更新为 {len(info['saved'])} 只：" + "、".join(info["saved"]))
        if info["failed"]:
            st.error("以下代码拉不到数据，**建议删掉后重新保存**"
                     "（可能是无效代码、已退市，或当时网络不通）："
                     + chr(10) + chr(10)
                     + chr(10).join(f"- `{c}`：{why}" for c, why in info["failed"]))
        if info["rows"]:
            st.caption(f"已预热历史数据共 {info['rows']} 行；第二天批处理不用再临时拉取。")

    st.divider()
    st.subheader("历史日报")
    dates = list_daily_reports()
    if not dates:
        st.info("还没有日报。跑一次 `python main.py`，或双击 `run.bat`，就会有第一份。")
        return
    st.caption(f"共 {len(dates)} 份（reports/ 目录，每天一份全池报告）")
    pick = st.selectbox("选择日期", dates, key="report_pick")
    if st.button("查看这份日报", width="stretch"):
        st.session_state["view_report"] = pick

    view = st.session_state.get("view_report")
    if not view:
        return
    path = REPORT_DIR / view / "report.md"
    if not path.exists():
        st.error(f"文件不存在：{path}")
        return
    st.divider()
    head = st.columns([3, 1])
    head[0].subheader(f"日报 · {view}")
    csv_path = path.parent / "signals.csv"
    if csv_path.exists():
        head[1].download_button("下载 signals.csv", csv_path.read_bytes(),
                                file_name=f"signals-{view}.csv", mime="text/csv",
                                width="stretch")
    st.markdown(path.read_text(encoding="utf-8"))


st.set_page_config(page_title="缠论分析 Agent", layout="wide")

# ==================== 第 2 步：侧边栏（模式切换 + 查询 / 股票池）====================
with st.sidebar:
    # ?page=pool 可直接打开「自选股与日报」，方便收藏 / 分享链接
    _modes = ["单股分析", "自选股与日报"]
    _idx = 1 if st.query_params.get("page") == "pool" else 0
    mode = st.radio("模式", _modes, index=_idx, label_visibility="collapsed")
    st.divider()

    if mode == "单股分析":
        st.header("查询")
        code_in = st.text_input("股票代码", value="600519", max_chars=6, help="6 位 A 股代码")
        date_in = st.date_input("报告日期", value=date.today())
        st.divider()
        force = st.checkbox("强制刷新", value=False, help="跳过本地历史，重新实时计算")
        with_llm = st.checkbox("分析时立即调用 LLM", value=False, help="默认关闭：先看规则结果，再按需生成")
        run_btn = st.button("开始分析", type="primary", width="stretch")
        st.divider()
        st.caption("数据：本地 Parquet（腾讯日线 + 前复权因子）")
        st.caption("信号 / 回测 / LLM：本地 SQLite")
    else:
        _pool = load_watchlist()
        st.caption(f"当前股票池 **{len(_pool)}** 只"
                   + ("（自定义）" if is_custom() else "（仓库默认）"))
        st.caption("在右侧编辑股票池、查看历史日报。")

if mode == "自选股与日报":
    render_pool_page()
    st.stop()

# ==================== 第 3 步：先查历史，无则分析 ====================
if run_btn:
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
    st.title("缠论分析 Agent")
    st.info("在左侧输入股票代码，点「开始分析」。")
    st.caption("提示：本地有历史的股票会直接读库（快、不花钱）；没有历史的会实时计算。")
    st.stop()

if not res.get("ok"):
    st.error(res.get("error") or "分析失败")
    st.stop()

st.title(f"{res['code']}　{res.get('name', '')}")
st.caption(f"{res.get('board', '')} · 涨跌幅限制 ±{res.get('limit_ratio', 0):.0%}"
           f" · 来源：{res.get('source')}（{st.session_state.get('note', '')}）"
           f" · 耗时 {res.get('elapsed')}s")
st.warning("**非投资建议**：本工具由程序按缠论规则自动生成结构化描述，"
           "不构成任何投资建议，不承诺收益，不含自动下单。据此操作风险自负。")

k = res.get("kline") or {}
stt = res.get("structure") or {}
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("最新收盘（不复权）", k.get("close_raw"))
c2.metric("价格位置", stt.get("position", "—"))
c3.metric("信号数", len(res.get("signals") or []))
c4.metric("可交易", res.get("tradable_count", 0))
c5.metric("其中主信号", res.get("primary_count", 0))

st.divider()

# ==================== 第 4 步：K 线可视化 ====================
st.subheader("K 线与缠论标注")
df = load_ohlc(res["code"])
if df.empty:
    st.info("无 K 线数据")
else:
    fig = go.Figure()
    fig.add_trace(go.Candlestick(x=df["date"], open=df["open"], high=df["high"],
                                 low=df["low"], close=df["close"], name="K线",
                                 increasing_line_color="#e2534b",
                                 decreasing_line_color="#3ba272"))

    for z in (stt.get("zs_list") or []):
        fig.add_shape(type="rect", x0=z["sdt"], x1=z["edt"], y0=z["zd"], y1=z["zg"],
                      fillcolor="rgba(110,110,240,0.16)",
                      line=dict(color="rgba(110,110,240,0.55)", width=1), layer="below")

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
                                 line=dict(color="#8a8a8a", width=1.2)))

    fxs = stt.get("fx_list") or []
    for tag, sym, color, name in (("顶", "triangle-down", "#d62728", "顶分型"),
                                  ("底", "triangle-up", "#2ca02c", "底分型")):
        pts = [f for f in fxs if tag in f["mark"]]
        if pts:
            fig.add_trace(go.Scatter(x=[f["dt"] for f in pts], y=[f["fx"] for f in pts],
                                     mode="markers", name=name,
                                     marker=dict(symbol=sym, size=7, color=color, opacity=0.7)))

    # 用位置索引：df 有名为 date 的列，r.date 会遮蔽 Timestamp.date() 方法
    pos = {str(d.date()): i for i, d in enumerate(df["date"])}
    for sg in (res.get("signals") or []):
        i = pos.get(str(sg["date"]))
        if i is None:
            continue
        row = df.iloc[i]
        is_buy = sg["direction"] == "买"
        y = float(row["low"]) * 0.985 if is_buy else float(row["high"]) * 1.015
        fig.add_trace(go.Scatter(
            x=[row["date"]], y=[y], mode="markers+text",
            text=[LEVEL_TAG.get(sg["type"], sg["type"][:3])],
            textposition="bottom center" if is_buy else "top center",
            name=sg["type"], showlegend=False,
            marker=dict(symbol="star", size=14,
                        color="#1a9850" if is_buy else "#d62728",
                        line=dict(color="white", width=0.8)),
            hovertemplate=(f"{sg['date']} {sg['type']}<br>"
                           f"确认日 {sg.get('confirm_date') or '待确认'}<br>"
                           f"入场参考 {sg.get('entry_ref_price') or '-'}"
                           "<extra></extra>")))

    fig.update_layout(height=580, xaxis_rangeslider_visible=False,
                      margin=dict(l=8, r=8, t=28, b=8),
                      legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0),
                      hovermode="x unified")
    st.plotly_chart(fig, width="stretch")
    st.caption("灰色折线 = 笔；蓝框 = 中枢；三角 = 分型；星标 = 买卖点（★绿=买 / ★红=卖）。"
               "价格为前复权口径，与「最新收盘（不复权）」在历史日期上会有差异。")

# ==================== 信号明细 ====================
st.subheader("信号明细")
sigs = res.get("signals") or []
if sigs:
    tb = pd.DataFrame([{
        "信号日": x["date"], "确认日": x.get("confirm_date") or "待确认",
        "类型": x["type"], "入场参考价": x.get("entry_ref_price"),
        "可交易": "是" if x["is_tradable"] else "否",
        "主信号": "★" if x.get("is_primary") else "",
        "过滤": x.get("filter_codes") or "通过",
        "理由": x.get("reason", ""),
    } for x in sigs[::-1]])
    st.dataframe(tb, width="stretch", hide_index=True)
    st.caption("信号日 = 触发笔结束日；确认日 = 信号日 + 实测确认延迟"
               "（缠论「笔」需后续 K 线确认，见 ADR-011，通常 1~2 个交易日）")
else:
    st.info("该股票在当前窗口内没有缠论买卖点信号。")

# ==================== 回测统计 ====================
st.subheader("回测统计参考")
bt = res.get("backtest") or []
if bt:
    tb2 = pd.DataFrame([{
        "类型": b["signal_type"], "窗口": f"{b['window']}日", "样本": b["n"],
        "平均收益": f"{b['avg_return']:+.2%}", "胜率": f"{b['win_rate']:.1%}",
        "平均最大回撤": f"{b['avg_mdd']:.2%}",
    } for b in bt])
    st.dataframe(tb2, width="stretch", hide_index=True)
    st.caption("口径：入场 = 确认日次一交易日开盘；卖点收益已做方向调整"
               "（价格下跌记为正）。样本少时不具解释力。")
else:
    st.info("无回测数据。")

# ==================== 第 5 步：LLM 按需生成 ====================
st.subheader("AI 总结")
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
    st.markdown(llm["text"])
    st.caption(f"模型 {llm.get('model', '')} · token {llm.get('tokens', 0)}"
               f" · 费用 ¥{llm.get('cost', 0):.6f} · 缓存 {'是' if llm.get('cached') else '否'}")
elif llm.get("error"):
    st.warning(f"生成失败，已降级为仅规则结果：{llm['error']}")
else:
    st.caption("尚未生成。")

with st.expander("查看原始结构化数据（analyze_stock 的返回）"):
    st.json(res, expanded=False)

st.divider()
st.caption("本页面由程序自动生成，非投资建议。")
