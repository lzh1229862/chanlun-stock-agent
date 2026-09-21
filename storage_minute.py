"""storage_minute.py —— 60 分钟级别数据（T21 / ADR-025）。

### 数据源与硬限制

新浪 stock_zh_a_minute(symbol="sh600519", period="60", adjust="qfq")
**固定返回最近 1970 根**，不给你更多。60 分钟一天 4 根 → 1970 根 ≈ **492 个交易日 ≈ 2 年**。

换成 period="30" 时行数还是 1970，但起点推到 2025-09 —— 也就是说这个接口就是「最近 1970 根」，
**历史深度不可协商**。这决定了 60 分钟级别的验证样本天生只有 2 年，结论只能当线索。

### 两个必须处理的数据问题

1. **最后一根是盘中未完成的 bar，OHLC 全为 NaN**（只有 volume/amount）。必须丢掉，
   否则 czsc 会吃到 NaN。
2. 时间戳形如 "2024-09-06 15:00:00"，60 分钟一天 4 根（10:30 / 11:30 / 14:00 / 15:00）。
   「日线信号日 D 时的 60 分钟状态」应该取 time <= D 15:00 的最后一根。

### 存哪

data/minute/{code}.parquet —— 与 data/raw/（日线）、data/index/（指数）分开，
同样因为代码会撞车，也不该混进统计样本。
"""
import time
from pathlib import Path

import pandas as pd

MINUTE_DIR = Path("data/minute")
STALE_HOURS = 6
FREQ = "60"


def minute_path(code):
    return MINUTE_DIR / ("%s.parquet" % code)


def exchange_symbol(code):
    """新浪要 sh/sz 前缀。"""
    if code.startswith(("60", "68", "9")):
        return "sh" + code
    return "sz" + code


def clean(df):
    """规整新浪返回：去 NaN 行、统一列名、按时间排序去重。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=["dt", "open", "high", "low", "close", "volume", "amount"])
    d = df.copy()
    d["dt"] = pd.to_datetime(d["day"])
    for c in ("open", "high", "low", "close", "volume", "amount"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["open", "high", "low", "close"])   # 丢掉未完成的 bar
    d = d.drop(columns=["day"]).sort_values("dt").drop_duplicates("dt").reset_index(drop=True)
    return d


def fetch_minute(code, freq=FREQ):
    """拉 60 分钟前复权数据。"""
    import akshare as ak
    df = ak.stock_zh_a_minute(symbol=exchange_symbol(code), period=freq, adjust="qfq")
    return clean(df)


def is_stale(code, verbose=False):
    p = minute_path(code)
    if not p.exists():
        return True
    age = (time.time() - p.stat().st_mtime) / 3600.0
    if age > STALE_HOURS:
        return True
    if verbose:
        print("    [minute] %s 本地 %.1f 小时前" % (code, age))
    return False


def save_minute(code, df):
    MINUTE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(minute_path(code), index=False)
    return len(df)


def load_minute(code):
    p = minute_path(code)
    if not p.exists():
        return pd.DataFrame()
    d = pd.read_parquet(p)
    if not d.empty:
        d["dt"] = pd.to_datetime(d["dt"])
    return d


def ensure_minute(code, refresh=False, verbose=False):
    """没有或过期就拉。失败返回 ("error", 原因)，成功返回 (来源, None)。"""
    if not refresh and not is_stale(code, verbose):
        d = load_minute(code)
        if not d.empty:
            return "cache", None
    try:
        df = fetch_minute(code)
    except Exception as e:
        d = load_minute(code)
        if not d.empty:
            return "cache", "拉取失败，用本地既有数据（%s）" % type(e).__name__
        return None, "拉取失败且本地无数据（%s）" % type(e).__name__
    if df.empty:
        return None, "接口返回空"
    save_minute(code, df)
    return "fetched", None
