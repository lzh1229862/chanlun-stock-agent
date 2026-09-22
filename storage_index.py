"""指数日线存取（基准收益与市场状态判断用，见 ADR-018）。

为什么单独存一份
    指数代码和股票代码会撞车：000001 既是上证指数、也是平安银行。
    所以指数落在 data/index/{code}.parquet，与行情数据的 data/raw/ 完全分开，永不混淆。
    指数不需要复权，列里也不带 qfq_factor。

数据源
    新浪 stock_zh_index_daily —— 实测 0.5 秒拿到全历史（沪深300 自 2002 年起 5996 行，到最新交易日）。
    东财的 stock_zh_index_daily_em / index_zh_a_hist 在本机同样被服务端重置（同 ADR-001）。
    腾讯 stock_zh_index_daily_tx 也能用，但要 15 秒，不取。

刷新策略
    指数历史很短（全量 0.5 秒），所以不做增量：本地最后日期距今超过 STALE_DAYS 就整段重拉。
"""
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

INDEX_DIR = Path("data/index")
STALE_DAYS = 4          # 本地最后日期距今超过这么多天就重拉（跨周末 + 节假日余量）

COLUMNS = ["date", "open", "high", "low", "close", "volume"]

# 常用指数（新浪接口需要 sh/sz 前缀）
INDEX_NAMES = {
    "000300": "沪深300", "000905": "中证500", "000001": "上证指数",
    "399001": "深证成指", "399006": "创业板指", "000688": "科创50",
}
DEFAULT_INDEX = "000300"


def index_name(code):
    return INDEX_NAMES.get(code, code)


def symbol_of(code):
    """指数代码 -> 新浪符号。000xxx 在上交所，399xxx 在深交所。"""
    return ("sh" if str(code).startswith("000") else "sz") + str(code)


def index_path(code):
    return INDEX_DIR / ("%s.parquet" % code)


def load_index(code):
    """读本地指数 Parquet；无文件返回列齐全的空表。"""
    p = index_path(code)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_parquet(p)
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]")
    return df.sort_values("date").reset_index(drop=True)


def _sina_index(code):
    """新浪全历史指数日线。内置数据源的实现，一般不要直接调。"""
    import akshare as ak
    df = ak.stock_zh_index_daily(symbol=symbol_of(code))
    if df is None or df.empty:
        return pd.DataFrame(columns=COLUMNS)
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"]).astype("datetime64[ns]")
    for c in ("open", "high", "low", "close", "volume"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    for c in COLUMNS:
        if c not in out.columns:
            out[c] = pd.NA
    return out[COLUMNS].dropna(subset=["close"]).sort_values("date").reset_index(drop=True)


def fetch_index(code):
    """活动数据源的指数日线（ADR-039）。默认 = 新浪。"""
    import datasource as ds
    df = ds.active().index_kline(code)
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=COLUMNS)
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"]).astype("datetime64[ns]")
    for c in ("open", "high", "low", "close", "volume"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    for c in COLUMNS:
        if c not in out.columns:
            out[c] = pd.NA
    return out[COLUMNS].dropna(subset=["close"]).sort_values("date").reset_index(drop=True)


def save_index(code, df):
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    out = df[COLUMNS].drop_duplicates(subset="date", keep="last")
    out = out.sort_values("date").reset_index(drop=True)
    out.to_parquet(index_path(code), index=False)
    return len(out)


def is_stale(df, today=None, stale_days=STALE_DAYS):
    if df is None or df.empty:
        return True
    last = pd.Timestamp(df["date"].max()).date()
    return (today or date.today()) - last > timedelta(days=stale_days)


def ensure_index(code=DEFAULT_INDEX, allow_fetch=True, verbose=False):
    """确保本地有该指数日线且不太旧。返回 (df, source, error)。

    source = "cache"（本地够新，没联网） / "fetched"（本次拉过） / None（拿不到）
    与 analyzer.ensure_kline 同样的契约：**失败返回结构化错误，不抛异常**。
    """
    df = load_index(code)
    if not is_stale(df):
        return df, "cache", None

    if not allow_fetch:
        return (df, "cache", None) if not df.empty else (None, None, "本地无数据且未开启自动拉取")

    try:
        fresh = fetch_index(code)
    except Exception as e:
        reason = "%s: %s" % (type(e).__name__, str(e)[:120])
        if df.empty:
            return None, None, "指数拉取失败（%s）" % reason
        return df, "cache", "指数刷新失败，已用本地既有数据（%s）" % reason

    if fresh.empty:
        if df.empty:
            return None, None, "指数拉取返回空（代码 %s 可能不存在）" % code
        return df, "cache", "指数拉取返回空，已用本地既有数据"

    save_index(code, fresh)
    if verbose:
        print("    [ensure_index] %s %s -> %d 行" % (code, index_name(code), len(fresh)))
    return fresh, "fetched", None
