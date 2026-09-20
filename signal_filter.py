"""T4-1 F3 信号过滤：区分「结构信号」与「可交易信号」。

过滤规则（PRD F3.1 / F3.2 / F3.3）
    F3.1  ST / *ST                -> 不可交易
    F3.2  上市不足 60 个交易日     -> 不可交易（次新股）
    F3.3  买点当日涨停            -> 不可交易（买不进）
          卖点当日跌停            -> 不可交易（卖不出）

数据来源（东财接口本机不可用，见 ADR-001）
    ST 状态  : 腾讯行情 https://qt.gtimg.cn/q=sh600519（GBK，字段 1 = 证券简称）
               备选：ak.stock_info_a_code_name() 全市场 5565 只代码/名称（约 51s）
    上市日期 : 新浪全历史 ak.stock_zh_a_daily(symbol, adjust="") 的最早日期
               备选（批量更快）：ak.stock_info_sh_name_code() / ak.stock_info_sz_name_code()
    涨跌停价 : 自算 round_half_up(除权调整后前收盘 x (1 ± 比例), 2)
               当前交易日可用腾讯行情字段 [47] 涨停 / [48] 跌停 交叉验证（实测完全一致）
    交易日历 : ak.tool_trade_date_hist_sina()

运行
    python signal_filter.py             # 600519 演示：过滤前后信号数量
    python signal_filter.py --explain   # 逐条打印判定过程
    python signal_filter.py --selftest  # 纯函数自测（不联网）
    python signal_filter.py --scan      # 扫描本地全部 K 线，统计真实涨跌停日
"""
import argparse
import math
from datetime import date, timedelta
from pathlib import Path

import akshare as ak
import pandas as pd
import requests

from storage_kline import load_kline, parquet_path, ts_code
from storage_signal import query_signals

MIN_LISTED_TRADING_DAYS = 60

SETTINGS_PATH = Path('config/settings.yaml')


def load_filter_settings():
    """读取 config/settings.yaml 的 filter 段（缺省返回空 dict，即全部走默认）。"""
    try:
        import yaml
        cfg = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
        return cfg.get("filter") or {}
    except Exception:
        return {}
LIMIT_TOL = 0.005          # 判定「收盘价 = 涨跌停价」的容差（元）
QUOTE_URL = "https://qt.gtimg.cn/q={}"

# 名称中含这些关键词 -> 不可交易
EXCLUDE_NAME_KEYWORDS = ("ST",)      # PRD F3.1
EXTRA_EXCLUDE_KEYWORDS = ("退",)     # 超出 PRD 的补充：退市股同样不可交易，如需严格照 PRD 可清空

BOARDS = [
    (("600", "601", "603", "605"), "沪市主板"),
    (("000", "001", "002", "003"), "深市主板"),
    (("300", "301"), "创业板"),
    (("688", "689"), "科创板"),
    (("43", "83", "87", "88", "92"), "北交所"),
]


# ==================== 纯函数（可独立测试，不联网） ====================

def round_half_up(x, nd=2):
    """四舍五入到 nd 位小数（交易所口径，不用 Python 的银行家舍入）。"""
    m = 10 ** nd
    return math.floor(x * m + 0.5) / m


def board_of(code):
    """按代码前缀判断所属板块。"""
    for prefixes, name in BOARDS:
        if code.startswith(prefixes):
            return name
    return "未知"


def limit_ratio(code, is_st=False):
    """涨跌幅限制比例。

    北交所 ±30%；创业板/科创板 ±20%（ST 也是 20%）；主板 ±10%，主板 ST ±5%。
    注：新股上市初期（主板前 5 个交易日、创科板前 5 个交易日）不设限或另有规则，
        但这类标的必被 F3.2 次新股规则排除，故此处不单独处理。
    """
    board = board_of(code)
    if board == "北交所":
        return 0.30
    if board in ("创业板", "科创板"):
        return 0.20
    if is_st:
        return 0.05
    return 0.10


def limit_prices(prev_close, ratio, factor_prev=1.0, factor_today=1.0):
    """返回 (涨停价, 跌停价)。

    前收盘价需做除权调整：除权除息日交易所用的是「除权参考价」而不是未调整的前收盘。
    qfq = raw / factor，故 调整后前收盘 = prev_close * factor_today / factor_prev。
    """
    base = prev_close * (factor_today / factor_prev) if factor_prev else prev_close
    return round_half_up(base * (1 + ratio)), round_half_up(base * (1 - ratio))


def limit_status(close, prev_close, ratio, factor_prev=1.0, factor_today=1.0, tol=LIMIT_TOL):
    """判断收盘是否封在涨/跌停。返回 ("涨停"|"跌停"|None, 该价格)。"""
    up, down = limit_prices(prev_close, ratio, factor_prev, factor_today)
    if abs(close - up) < tol:
        return "涨停", up
    if abs(close - down) < tol:
        return "跌停", down
    return None, None


def is_buy_point(signal_type):
    return "买点" in signal_type


def is_sell_point(signal_type):
    return "卖点" in signal_type


def name_hit(name, keywords):
    return any(k in name for k in keywords if k)


# ==================== 数据获取 ====================

_QUOTE_CACHE = {}
_LISTING_CACHE = {}
_CALENDAR = None


def fetch_quote(code):
    """腾讯行情原始字段（一串用 ~ 分隔的值）。

    这一个请求里除了证券简称，还带着 PE / PB / 总市值 / 换手率 等估值字段 ——
    以前解析完简称就把整包丢了。现在整包缓存下来给 fundamentals 复用，不额外联网。
    字段下标见 fundamentals.TX_IDX（已用「TTM 净利 / 每股净资产 自己算一遍」交叉验证）。
    """
    if code in _QUOTE_CACHE:
        return _QUOTE_CACHE[code]
    r = requests.get(QUOTE_URL.format(ts_code(code)), timeout=10)
    txt = r.content.decode("gbk", errors="replace")
    parts = txt.split('"')[1].split("~") if '"' in txt else []
    _QUOTE_CACHE[code] = parts
    return parts


def fetch_name(code):
    """腾讯行情取证券简称（东财接口不可用）。"""
    parts = fetch_quote(code)
    return parts[1] if len(parts) > 1 else ""


def fetch_listing_date(code):
    """上市日期 = 新浪全历史日线的最早交易日。

    备选（批量场景更快，一次拿全市场）：沪市 ak.stock_info_sh_name_code()、
    深市 ak.stock_info_sz_name_code()，均含上市日期列。
    """
    if code in _LISTING_CACHE:
        return _LISTING_CACHE[code]
    df = ak.stock_zh_a_daily(symbol=ts_code(code), adjust="")
    d = pd.to_datetime(df["date"]).min()
    _LISTING_CACHE[code] = d
    return d


def trading_calendar():
    global _CALENDAR
    if _CALENDAR is None:
        cal = ak.tool_trade_date_hist_sina()
        _CALENDAR = sorted(pd.to_datetime(cal["trade_date"]).tolist())
    return _CALENDAR


def build_context(code):
    """一次性取齐过滤所需的外部信息。"""
    name = fetch_name(code)
    return {
        "name": name,
        "is_st": name_hit(name, EXCLUDE_NAME_KEYWORDS),
        "is_delisted": name_hit(name, EXTRA_EXCLUDE_KEYWORDS),
        "listing_date": fetch_listing_date(code),
        "calendar": trading_calendar(),
        "volume": load_filter_settings().get("volume") or {},
    }


def listed_trading_days(calendar, listing_date, asof):
    return sum(1 for d in calendar if listing_date <= d <= asof)


# ==================== 过滤主逻辑 ====================

def filter_signals(code, signals, bars, context, min_days=MIN_LISTED_TRADING_DAYS, explain=False):
    """对信号逐条判定，返回带 is_tradable / filter_reason 的新列表。

    code     : 股票代码，如 "600519"
    signals  : [{"date": "2025-01-20", "type": "第三类卖点", "reason": "..."}, ...]
    bars     : 该股 K 线（Parquet 口径：不复权价格 + qfq_factor）
    context  : build_context(code) 的返回值；也可手工构造以便离线测试
    """
    bars = bars.sort_values("date").reset_index(drop=True)
    pos = {d: i for i, d in enumerate(bars["date"])}
    ratio = limit_ratio(code, context["is_st"])

    out = []
    for s in signals:
        d = pd.Timestamp(s["date"])
        reasons, codes, detail = [], [], {}

        if context["is_st"]:
            reasons.append(f"ST 股（{context['name']}）")
            codes.append("st")
        if context.get("is_delisted"):
            reasons.append(f"退市整理（{context['name']}）")
            codes.append("delisted")

        n_days = listed_trading_days(context["calendar"], context["listing_date"], d)
        detail["上市交易日"] = n_days
        if n_days < min_days:
            reasons.append(f"次新股（上市 {n_days} 个交易日 < {min_days}）")
            codes.append("new")

        i = pos.get(d)
        if i is None:
            detail["涨跌停"] = "无当日K线，跳过判定"
        elif i == 0:
            detail["涨跌停"] = "无前收盘，跳过判定"
        else:
            close = float(bars.at[i, "close"])
            prev_close = float(bars.at[i - 1, "close"])
            f_prev = float(bars.at[i - 1, "qfq_factor"])
            f_today = float(bars.at[i, "qfq_factor"])
            up, dn = limit_prices(prev_close, ratio, f_prev, f_today)
            detail.update({"收盘": round(close, 2), "前收": round(prev_close, 2),
                           "涨停价": up, "跌停价": dn})
            status, price = limit_status(close, prev_close, ratio, f_prev, f_today)
            if status == "涨停" and is_buy_point(s["type"]):
                reasons.append(f"当日涨停 {price:.2f}，买不进")
                codes.append("limitup")
            elif status == "跌停" and is_sell_point(s["type"]):
                reasons.append(f"当日跌停 {price:.2f}，卖不出")
                codes.append("limitdown")
            detail["涨跌停"] = status or "无"
        # --- F3.4 量能过滤（可选，config/settings.yaml 的 filter.volume.enable 控制）---
        vcfg = context.get("volume") or {}
        if vcfg.get("enable") and i is not None:
            vol = float(bars.at[i, "volume"])
            amt = float(bars.at[i, "amount"])
            detail["成交量"] = vol
            detail["成交额"] = amt
            if vcfg.get("exclude_suspended", True) and vol <= 0:
                reasons.append("信号日成交量为 0（停牌，无法成交）")
                codes.append("susp")
            lo = vcfg.get("min_amount")
            if lo and amt < lo:
                reasons.append(f"信号日成交额 {amt / 1e4:.0f} 万 < 下限 {lo / 1e4:.0f} 万")
                codes.append("illiquid")
            mr = vcfg.get("max_volume_ratio")
            if mr:
                prev = bars["volume"].iloc[max(0, i - 20):i]
                avg = float(prev.mean()) if len(prev) else 0.0
                if avg > 0:
                    ratio = vol / avg
                    detail["量比"] = round(ratio, 2)
                    if ratio > mr:
                        reasons.append(f"信号日成交量为前 20 日均量的 {ratio:.1f} 倍 > {mr}")
                        codes.append("volspike")

        out.append({**s, "is_tradable": 0 if reasons else 1,
                    "filter_reason": "；".join(reasons),
                    "filter_codes": "+".join(codes), "detail": detail})
    return out


# ==================== 自测（不联网） ====================

def selftest():
    ok = []

    def check(desc, got, want):
        good = got == want
        ok.append(good)
        print(f"  [{'OK' if good else 'FAIL'}] {desc:<46} got={got!r} want={want!r}")

    print("--- 1) 板块识别 ---")
    check("600519", board_of("600519"), "沪市主板")
    check("000001", board_of("000001"), "深市主板")
    check("300750", board_of("300750"), "创业板")
    check("688981", board_of("688981"), "科创板")
    check("430047", board_of("430047"), "北交所")

    print("--- 2) 涨跌幅限制比例 ---")
    check("主板", limit_ratio("600519"), 0.10)
    check("主板 ST", limit_ratio("600519", True), 0.05)
    check("创业板", limit_ratio("300750"), 0.20)
    check("创业板 ST", limit_ratio("300750", True), 0.20)
    check("科创板", limit_ratio("688981"), 0.20)
    check("北交所", limit_ratio("430047"), 0.30)

    print("--- 3) 涨跌停价（与腾讯官方值对齐） ---")
    check("600519 昨收 1266.98", limit_prices(1266.98, 0.10), (1393.68, 1140.28))
    check("000001 昨收 11.61", limit_prices(11.61, 0.10), (12.77, 10.45))
    check("创业板 20% 昨收 100.00", limit_prices(100.0, 0.20), (120.0, 80.0))
    check("ST 5% 昨收 10.00", limit_prices(10.0, 0.05), (10.5, 9.5))
    check("除权调整 100*(1.0/1.0411)", limit_prices(100.0, 0.10, 1.0411, 1.0), (105.66, 86.45))

    print("--- 4) 封板判定 ---")
    check("涨停", limit_status(1393.68, 1266.98, 0.10), ("涨停", 1393.68))
    check("跌停", limit_status(1140.28, 1266.98, 0.10), ("跌停", 1140.28))
    check("普通", limit_status(1300.00, 1266.98, 0.10), (None, None))

    print("--- 5) 过滤主逻辑（构造数据，不联网） ---")
    bars = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]),
        "close": [100.0, 110.0, 99.0, 100.5],
        "qfq_factor": [1.0, 1.0, 1.0, 1.0],
    })
    # 2026-01-06 涨停(100->110)，2026-01-07 跌停(110->99)，2026-01-08 普通
    cal = [pd.Timestamp("2025-01-01") + timedelta(days=i) for i in range(400)]
    base_ctx = {"name": "测试股", "is_st": False, "is_delisted": False,
                "listing_date": pd.Timestamp("2001-01-01"), "calendar": cal}
    sigs = [{"date": "2026-01-06", "type": "第一类买点"},
            {"date": "2026-01-07", "type": "第一类卖点"},
            {"date": "2026-01-08", "type": "第三类买点"}]

    r = filter_signals("600519", sigs, bars, base_ctx)
    check("涨停日买点 -> 不可交易", r[0]["is_tradable"], 0)
    check("涨停日买点原因含涨停", "涨停" in r[0]["filter_reason"], True)
    check("跌停日卖点 -> 不可交易", r[1]["is_tradable"], 0)
    check("跌停日卖点原因含跌停", "跌停" in r[1]["filter_reason"], True)
    check("普通日买点 -> 可交易", r[2]["is_tradable"], 1)
    check("普通日无过滤原因", r[2]["filter_reason"], "")
    check("普通日 filter_codes 为空", r[2]["filter_codes"], "")
    check("涨停买点 filter_codes", r[0]["filter_codes"], "limitup")
    check("跌停卖点 filter_codes", r[1]["filter_codes"], "limitdown")
    check("涨停日的卖点不被涨停过滤",
          filter_signals("600519", [{"date": "2026-01-06", "type": "第一类卖点"}], bars, base_ctx)[0]["is_tradable"], 1)
    check("跌停日的买点不被跌停过滤",
          filter_signals("600519", [{"date": "2026-01-07", "type": "第一类买点"}], bars, base_ctx)[0]["is_tradable"], 1)

    r_st = filter_signals("600519", sigs, bars, {**base_ctx, "name": "*ST测试", "is_st": True})
    check("ST -> 全部不可交易", [x["is_tradable"] for x in r_st], [0, 0, 0])
    check("ST 原因可追溯", "ST" in r_st[2]["filter_reason"], True)

    r_new = filter_signals("600519", [sigs[2]], bars, {**base_ctx, "listing_date": pd.Timestamp("2025-12-01")})
    check("次新股 -> 不可交易", r_new[0]["is_tradable"], 0)
    check("次新股原因含次新股", "次新股" in r_new[0]["filter_reason"], True)

    print("--- 6) 量能过滤（F3.4）---")
    def mk(vols, amts):
        import pandas as _pd
        return _pd.DataFrame({"date": _pd.date_range("2026-01-01", periods=len(vols), freq="D"),
                              "open": [100.0]*len(vols), "high": [100.0]*len(vols),
                              "low": [100.0]*len(vols), "close": [100.0]*len(vols),
                              "qfq_factor": [1.0]*len(vols),
                              "volume": vols, "amount": amts})
    one_buy = [{"date": "2026-01-01", "type": "第一类买点"}]
    v_on = {**base_ctx, "volume": {"enable": True, "exclude_suspended": True,
                                    "min_amount": 1.0e7, "max_volume_ratio": None}}
    v_off = {**base_ctx, "volume": {"enable": False, "min_amount": 1.0e7}}
    vb = mk([1000.0, 20000.0], [1.0e6, 5.0e6])
    rv = filter_signals("600519", one_buy, vb, v_on)
    check("成交额不足 -> 不可交易", rv[0]["is_tradable"], 0)
    check("成交额不足 code=illiquid", rv[0]["filter_codes"], "illiquid")
    rv2 = filter_signals("600519", one_buy, vb, v_off)
    check("量能过滤关闭 -> 可交易", rv2[0]["is_tradable"], 1)
    check("关闭时不写 volume detail", "成交量" in rv2[0]["detail"], False)
    vb2 = mk([0.0, 100.0], [0.0, 1.0e8])
    v_susp = {**base_ctx, "volume": {"enable": True, "exclude_suspended": True}}
    check("停牌 -> code=susp", filter_signals("600519", one_buy, vb2, v_susp)[0]["filter_codes"], "susp")
    check("停牌+成交额为0 -> 同时命中 susp 与 illiquid", filter_signals("600519", one_buy, vb2, v_on)[0]["filter_codes"], "susp+illiquid")
    vb3 = mk([100.0]*20 + [5000.0], [1.0e8]*21)
    v3 = {**base_ctx, "volume": {"enable": True, "max_volume_ratio": 10.0}}
    late_buy = [{"date": "2026-01-21", "type": "第一类买点"}]
    check("放量异常 -> code=volspike", filter_signals("600519", late_buy, vb3, v3)[0]["filter_codes"], "volspike")
    print(f"==> 自测 {sum(ok)}/{len(ok)} 通过" + ("" if all(ok) else "  ❌ 有失败项"))
    return all(ok)


# ==================== 真实数据扫描 ====================

def scan():
    """扫描本地全部 K 线，统计真实涨跌停日（验证封板判定在真实数据上确实触发）。"""
    files = sorted(Path("data/raw").glob("*.parquet"))
    if not files:
        print("data/raw 下没有 Parquet，先跑 run_round3.py")
        return
    print(f"扫描 {len(files)} 只股票的本地 K 线：")
    print(f"  {'code':<8}{'板块':<10}{'限制':<7}{'行数':>6}{'涨停日':>8}{'跌停日':>8}   最大单日涨幅   最大单日跌幅")
    for f in files:
        code = f.stem
        bars = load_kline(code).sort_values("date").reset_index(drop=True)
        ratio = limit_ratio(code)
        n_up = n_dn = 0
        chg, up_dates, dn_dates = [], [], []
        for i in range(1, len(bars)):
            close = float(bars.at[i, "close"])
            prev = float(bars.at[i - 1, "close"])
            st, _ = limit_status(close, prev, ratio,
                                 float(bars.at[i - 1, "qfq_factor"]), float(bars.at[i, "qfq_factor"]))
            if st:
                (up_dates if st == "涨停" else dn_dates).append(str(bars.at[i, "date"].date()))
            n_up += st == "涨停"
            n_dn += st == "跌停"
            if prev:
                chg.append(close / prev - 1)
        print(f"  {code:<8}{board_of(code):<10}{ratio:<7.0%}{len(bars):>6}{n_up:>8}{n_dn:>8}"
              f"{max(chg):>13.2%}{min(chg):>15.2%}")
        if up_dates:
            print(f"          涨停日: {up_dates}")
        if dn_dates:
            print(f"          跌停日: {dn_dates}")


# ==================== 演示 ====================

def demo(code, explain=False):
    signals = [{"date": r["signal_date"], "type": r["signal_type"], "reason": r["signal_reason"]}
               for r in query_signals(code=code)]
    bars = load_kline(code)
    print(f"=== T4-1 F3 信号过滤   {code} ===")
    if not bars.empty:
        print(f"本地 K 线 {len(bars)} 行   {bars['date'].min().date()} ~ {bars['date'].max().date()}")

    ctx = build_context(code)
    ratio = limit_ratio(code, ctx["is_st"])
    today = pd.Timestamp(date.today())
    print(f"证券简称 {ctx['name']}   板块 {board_of(code)}   涨跌幅限制 ±{ratio:.0%}   "
          f"ST {ctx['is_st']}   上市 {ctx['listing_date'].date()}"
          f"（至 {today.date()} 共 {listed_trading_days(ctx['calendar'], ctx['listing_date'], today)} 个交易日）")
    print(f"信号来源 signals 表（filter_version 均为 v0_no_filter）")
    print()

    if not signals:
        print("库中无该股票信号，先跑 run_round3.py")
        return

    results = filter_signals(code, signals, bars, ctx)
    if explain:
        print("--- 逐条判定 ---")
        for r in results:
            d = r["detail"]
            print(f"  {r['date']}  {r['type']:<12} 收盘={d.get('收盘','-'):>9}  前收={d.get('前收','-'):>9}"
                  f"  涨停价={d.get('涨停价','-'):>9}  跌停价={d.get('跌停价','-'):>9}"
                  f"  上市{d.get('上市交易日','-')}日  封板={d.get('涨跌停','-'):<6}"
                  f" -> {'可交易' if r['is_tradable'] else '不可交易'}")
        print()

    n_all = len(results)
    n_ok = sum(r["is_tradable"] for r in results)
    print(f"信号 {n_all} 个，可交易 {n_ok} 个，不可交易 {n_all - n_ok} 个")
    if n_all - n_ok:
        print("不可交易明细：")
        for r in results:
            if not r["is_tradable"]:
                print(f"  {r['date']}  {r['type']:<12} 原因: {r['filter_reason']}")
    else:
        print(f"（{code} 非 ST、上市已久、信号日均未封板，故全部可交易）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="600519")
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--scan", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(0 if selftest() else 1)
    if a.scan:
        scan()
    else:
        demo(a.code, a.explain)
