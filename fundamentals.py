"""基本面数据：行业分类 + 财务摘要 + 估值快照（ADR-017）。

数据源（**全部绕开东财** —— 东财在本机被服务端重置，见 ADR-001）：
    公司概况 / 所属行业 / 上市日期   akshare stock_profile_cninfo          （巨潮）
    中证行业分类层级                 akshare stock_industry_change_cninfo  （巨潮）
    财务摘要（多期）                 akshare stock_financial_abstract_ths  （同花顺）
    估值快照 PE/PB/市值/换手率       腾讯 qt.gtimg.cn（复用 signal_filter 已经发过的那个请求）

**不参与回测**：财报有披露滞后（半年报 8 月才出），拿最新财报去评估历史信号会引入
前视偏差，把回测胜率悄悄抬高。所以基本面只用于展示与 LLM 上下文，不进任何筛选或回测。
"""
import pandas as pd

FIN_TTL_DAYS = 7        # 财务摘要缓存有效期（季报级数据）
PROFILE_TTL_DAYS = 30   # 公司概况 / 行业缓存有效期（基本不变）

# 同花顺财务摘要的列名 -> 我们的字段名
FIN_FIELDS = {
    "净利润": "net_profit",
    "净利润同比增长率": "net_profit_yoy",
    "营业总收入": "revenue",
    "营业总收入同比增长率": "revenue_yoy",
    "基本每股收益": "eps",
    "每股净资产": "bps",
    "净资产收益率": "roe",
    "销售毛利率": "gross_margin",
    "销售净利率": "net_margin",
    "资产负债率": "debt_ratio",
}

# 腾讯行情字段下标。已交叉验证（用财务数据自己算一遍对上的）：
#   茅台 [39]=19.30  ==  TTM净利 814.34亿 / 12.5亿股 = EPS 65.15 -> 1257.12/65.15
#   茅台 [46]=6.25   ==  1257.12 / 每股净资产 200.99
#   茅台 [45]=15715.03亿 == 1257.12 * 12.5008亿股
#   茅台 [38]=0.20%  ==  24891手 / 12.5亿股
TX_IDX = {
    "price": 3, "change_pct": 32, "turnover_pct": 38, "pe_ttm": 39, "amplitude_pct": 43,
    "float_cap_yi": 44, "total_cap_yi": 45, "pb": 46, "volume_ratio": 49,
    "pe_dyn": 52, "pe_static": 53,
}

_UNITS = {"万": 1e4, "亿": 1e8, "万亿": 1e12}
_BLANK = {"", "nan", "none", "null", "false", "true", "--", "-", "—"}


def parse_num(v):
    """把同花顺那种 "272.43亿" / "1.47%" / False 统一转成 float 或 None。

    注意：百分数原样存（"1.47%" -> 1.47，表示 1.47%），不做 /100。
    """
    if v is None or v is True or v is False:
        return None
    if isinstance(v, (int, float)):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if pd.isna(f) else f
    s = str(v).strip()
    if s.lower() in _BLANK:
        return None
    if s.endswith("%"):
        s = s[:-1].strip()
    mult = 1.0
    for u in ("万亿", "亿", "万"):        # 万亿 必须排在 亿 前面
        if s.endswith(u):
            mult = _UNITS[u]
            s = s[:-len(u)].strip()
            break
    try:
        return float(s.replace(",", "")) * mult
    except ValueError:
        return None


def _txt(v):
    """pandas 的空值、以及字符串 "None" 都当作没有。"""
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    s = str(v).strip()
    return "" if s.lower() in _BLANK else s


# ==================== 抓取 ====================

def fetch_profile(code):
    """巨潮公司概况：所属行业 / 上市日期 / 主营业务。"""
    import akshare as ak
    df = ak.stock_profile_cninfo(symbol=code)
    if df is None or df.empty:
        return None
    r = df.iloc[0]
    return {
        "code": code,
        "name": _txt(r.get("A股简称")),
        "market": _txt(r.get("所属市场")),
        "industry": _txt(r.get("所属行业")),
        "listing_date": _txt(r.get("上市日期")),
        "main_business": _txt(r.get("主营业务")),
    }


def fetch_industry_cninfo(code):
    """中证行业分类层级：行业门类 / 次类 / 大类 / 中类。"""
    import akshare as ak
    from datetime import datetime
    df = ak.stock_industry_change_cninfo(symbol=code, start_date="20200101",
                                         end_date=datetime.now().strftime("%Y%m%d"))
    if df is None or df.empty:
        return None
    # 同一只股票会返回多套分类标准（中证 / 申万 / 中上协，还带一份「旧」）。
    # 优先取「中证行业分类标准」—— 只有它给出 门类/次类/大类/中类 四级完整层级；
    # 没有就退到最新的非旧标准。
    if "分类标准" in df.columns:
        std = df["分类标准"].astype(str).str.strip()
        exact = df[std == "中证行业分类标准"]
        if len(exact):
            df = exact
        else:
            keep = df[~std.str.contains("旧")]
            if len(keep):
                df = keep
    if "变更日期" in df.columns:
        df = df.sort_values("变更日期")
    r = df.iloc[-1]
    parts = [_txt(r.get(k)) for k in ("行业门类", "行业次类", "行业大类", "行业中类")]
    parts = [p for p in parts if p]
    return " / ".join(parts) if parts else None


def fetch_financials(code, periods=6):
    """同花顺财务摘要最近 periods 期，返回**报告期倒序**（最近的在前）的 list。"""
    import akshare as ak
    df = ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期")
    if df is None or df.empty or "报告期" not in df.columns:
        return []
    df = df.copy()
    df["_p"] = pd.to_datetime(df["报告期"], errors="coerce")
    df = df.dropna(subset=["_p"]).sort_values("_p")
    out = []
    for _, r in df.tail(periods).iterrows():
        item = {"report_period": r["_p"].strftime("%Y-%m-%d")}
        for src, dst in FIN_FIELDS.items():
            item[dst] = parse_num(r.get(src))
        out.append(item)
    out.reverse()
    return out


def fetch_valuation(code):
    """腾讯行情快照里的估值字段 —— 复用 signal_filter 已发过的请求，不额外联网。"""
    from signal_filter import fetch_quote
    parts = fetch_quote(code)
    if len(parts) <= max(TX_IDX.values()):
        return None
    out = {}
    for k, i in TX_IDX.items():
        try:
            f = float(parts[i])
            out[k] = None if f != f else f
        except (ValueError, TypeError, IndexError):
            out[k] = None
    return out


# ==================== 带缓存的汇总入口 ====================

def get_fundamentals(code, refresh=False):
    """汇总「公司概况 + 财务摘要 + 估值快照」，各步独立容错，**绝不抛异常**。

    任一步失败只写进 errors，其余部分照常返回（有缓存就用缓存）。
    """
    import storage_fundamental as sf

    out = {"code": code, "profile": {}, "financials": [], "valuation": None, "errors": []}

    # --- 公司概况 / 行业 ---
    prof = None if refresh else sf.load_profile(code)
    if prof is None or sf.is_stale(prof.get("updated_at"), PROFILE_TTL_DAYS):
        try:
            fresh = fetch_profile(code) or {}
            fresh["code"] = code
            try:
                fresh["industry_cninfo"] = fetch_industry_cninfo(code)
            except Exception as e:
                out["errors"].append("中证行业分类: %s" % type(e).__name__)
            if fresh.get("industry") or fresh.get("name"):
                sf.save_profile(fresh)
                prof = fresh
        except Exception as e:
            out["errors"].append("公司概况: %s: %s" % (type(e).__name__, str(e)[:80]))
            if prof is None:
                prof = sf.load_profile(code)
    out["profile"] = prof or {}

    # --- 财务摘要 ---
    rows = [] if refresh else sf.load_financials(code)
    if not rows or sf.is_stale(rows[0].get("updated_at"), FIN_TTL_DAYS):
        try:
            got = fetch_financials(code)
            if got:
                sf.save_financials(code, got)
                rows = sf.load_financials(code)
        except Exception as e:
            out["errors"].append("财务摘要: %s: %s" % (type(e).__name__, str(e)[:80]))
            if not rows:
                rows = sf.load_financials(code)
    out["financials"] = rows

    # --- 估值快照 ---
    try:
        out["valuation"] = fetch_valuation(code)
    except Exception as e:
        out["errors"].append("估值快照: %s: %s" % (type(e).__name__, str(e)[:80]))
    return out


# ==================== 格式化 ====================

def fmt_yi(x):
    """元 -> 亿元字符串。"""
    return "—" if x is None else "%.2f亿" % (x / 1e8)


def fmt_pct(x):
    return "—" if x is None else "%.2f%%" % x


def fmt_num(x):
    return "—" if x is None else "%.2f" % x


def fmt_yi_plain(x):
    """已经是亿元的数（腾讯总市值）。"""
    return "—" if x is None else "%.2f亿" % x


_yi, _pct, _f2 = fmt_yi, fmt_pct, fmt_num


def summarize(f, name=""):
    """把 get_fundamentals 的结果压成一层扁平 dict，报告 / UI / LLM 共用。"""
    f = f or {}
    prof = f.get("profile") or {}
    fins = f.get("financials") or []
    val = f.get("valuation") or {}
    cur = fins[0] if fins else {}
    return {
        "code": f.get("code", ""),
        "name": name or prof.get("name") or "",
        "market": prof.get("market") or "",
        "industry": prof.get("industry") or "",
        "industry_cninfo": prof.get("industry_cninfo") or "",
        "listing_date": prof.get("listing_date") or "",
        "main_business": prof.get("main_business") or "",
        "report_period": cur.get("report_period") or "",
        "net_profit": cur.get("net_profit"),
        "net_profit_yoy": cur.get("net_profit_yoy"),
        "revenue": cur.get("revenue"),
        "revenue_yoy": cur.get("revenue_yoy"),
        "eps": cur.get("eps"),
        "bps": cur.get("bps"),
        "roe": cur.get("roe"),
        "gross_margin": cur.get("gross_margin"),
        "net_margin": cur.get("net_margin"),
        "debt_ratio": cur.get("debt_ratio"),
        "pe_ttm": val.get("pe_ttm"),
        "pe_static": val.get("pe_static"),
        "pb": val.get("pb"),
        "total_cap_yi": val.get("total_cap_yi"),
        "float_cap_yi": val.get("float_cap_yi"),
        "turnover_pct": val.get("turnover_pct"),
        "errors": f.get("errors") or [],
        "periods": len(fins),
        # 多期趋势（报告期倒序），给 UI 画个小小的趋势表
        "history": [{"report_period": r.get("report_period"), "revenue": r.get("revenue"),
                     "revenue_yoy": r.get("revenue_yoy"), "net_profit": r.get("net_profit"),
                     "net_profit_yoy": r.get("net_profit_yoy"), "roe": r.get("roe")}
                    for r in fins],
    }


def format_lines(s):
    """给报告正文 / LLM 上下文用的纯文本行。"""
    L = []
    ind = s.get("industry") or ""
    if ind:
        market = ("（%s）" % s["market"]) if s.get("market") else ""
        L.append("- 行业：%s%s" % (ind, market))
    cn = s.get("industry_cninfo") or ""
    if cn and cn != ind:
        L.append("- 中证行业分类：%s" % cn)
    if s.get("listing_date"):
        L.append("- 上市日期：%s" % s["listing_date"])
    if s.get("report_period"):
        L.append("- 最新报告期 %s：营收 %s（同比 %s）；净利润 %s（同比 %s）；"
                 "ROE %s；毛利率 %s；资产负债率 %s"
                 % (s["report_period"], _yi(s.get("revenue")), _pct(s.get("revenue_yoy")),
                    _yi(s.get("net_profit")), _pct(s.get("net_profit_yoy")),
                    _pct(s.get("roe")), _pct(s.get("gross_margin")), _pct(s.get("debt_ratio"))))
    if s.get("pe_ttm") is not None or s.get("pb") is not None:
        L.append("- 估值（腾讯快照）：PE(TTM) %s；PB %s；总市值 %s；换手率 %s"
                 % (_f2(s.get("pe_ttm")), _f2(s.get("pb")),
                    "—" if s.get("total_cap_yi") is None else "%.2f亿" % s["total_cap_yi"],
                    _pct(s.get("turnover_pct"))))
    mb = s.get("main_business") or ""
    if mb:
        L.append("- 主营业务：%s" % (mb[:120] + ("…" if len(mb) > 120 else "")))
    return L
