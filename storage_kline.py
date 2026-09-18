"""T3-1 + T3-2：A 股日线 Parquet 存取 + 增量更新。

存储格式（每只股票一个文件）：
    路径  data/raw/{code}.parquet
    列    date, open, high, low, close, volume, amount, qfq_factor
    约定  价格一律存【不复权】；qfq_factor 为当日的前复权因子（ADR-002）
          前复权价 = 不复权价 / qfq_factor
          这样做是因为前复权价会在每次除权除息后【回溯变化】，直接存 qfq 会让增量拼接错位。

运行：python storage_kline.py     # 覆盖三种边界：无数据全量 / 已最新跳过 / 缺中间补拉
"""
import time
from datetime import date, timedelta
from pathlib import Path

import akshare as ak
import pandas as pd

DATA_DIR = Path("data/raw")
COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount", "qfq_factor"]
RAW_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]


def ts_code(code):
    return ("sh" if code[0] == "6" else "sz") + code


def parquet_path(code):
    return DATA_DIR / f"{code}.parquet"


# ============ T3-1：Parquet 存取 ============

def load_kline(code):
    """读本地 Parquet；无文件时返回列齐全的空表。"""
    p = parquet_path(code)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_parquet(p)
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]")
    return df.sort_values("date").reset_index(drop=True)


def save_kline(code, df):
    """写回 Parquet（去重 + 按日期排序）。返回落盘行数。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = df[COLUMNS].drop_duplicates(subset="date", keep="last").sort_values("date").reset_index(drop=True)
    out["qfq_factor"] = out["qfq_factor"].astype(float)
    out.to_parquet(parquet_path(code), index=False)
    return len(out)


# ============ 数据获取 ============

def fetch_raw(code, start, end):
    """腾讯源，【不复权】日线。"""
    df = ak.stock_zh_a_hist_tx(symbol=ts_code(code), start_date=start.strftime("%Y%m%d"),
                               end_date=end.strftime("%Y%m%d"), adjust="")
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=RAW_COLUMNS)
    df = df[["date", "open", "high", "low", "close", "volume", "amount"]].copy()
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]")
    return df


def fetch_factor(code, start, end):
    """前复权因子（阶梯函数，仅除权除息日有记录）。多取一段历史，保证窗口首日也能向前找到因子。"""
    df = ak.stock_zh_a_daily(symbol=ts_code(code), start_date="1990-01-01",
                             end_date=end.strftime("%Y-%m-%d"), adjust="qfq-factor")
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]")
    df["qfq_factor"] = df["qfq_factor"].astype(float)   # akshare 返回的是字符串
    return df.sort_values("date").reset_index(drop=True)


def trade_dates(start, end):
    """交易日历（AKShare/新浪）。"""
    cal = ak.tool_trade_date_hist_sina()
    cal["trade_date"] = pd.to_datetime(cal["trade_date"]).astype("datetime64[ns]")
    s, e = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    m = cal[(cal.trade_date >= s) & (cal.trade_date <= e)]
    return list(m.trade_date)


def attach_factor(df, code, end):
    """按日期前向填充 qfq_factor。"""
    if len(df) == 0:
        return df
    f = fetch_factor(code, None, end)
    if len(f) == 0:
        df = df.copy()
        df["qfq_factor"] = 1.0
        return df
    left = df.copy()
    left["date"] = pd.to_datetime(left["date"]).astype("datetime64[ns]")
    right = f.copy()
    right["date"] = pd.to_datetime(right["date"]).astype("datetime64[ns]")
    merged = pd.merge_asof(left.sort_values("date"), right, on="date", direction="backward")
    if merged["qfq_factor"].isna().any():
        merged["qfq_factor"] = merged["qfq_factor"].fillna(f["qfq_factor"].iloc[0])
    return merged


# ============ T3-2：增量更新 ============

def update_kline(code, start, end, verbose=True):
    """增量更新：只拉缺失交易日，合并后写回。返回 (df, stats)。"""
    t0 = time.perf_counter()
    local = load_kline(code)
    n_local = len(local)

    missing = [d for d in trade_dates(start, end) if d not in set(local["date"])]

    if not missing:
        if verbose:
            print(f"  [跳过] 本次拉取 0 行，本地已有 {n_local} 行；本地已最新，无需请求接口"
                  f"   耗时 {time.perf_counter() - t0:.2f}s")
        return local, {"fetched": 0, "local": n_local, "merged": n_local, "skipped": True}

    if n_local == 0:
        ranges = [(missing[0], missing[-1])]      # 本地无数据：整段全量拉，避免被假期切碎
    else:
        ranges, seg = [], missing[0]              # 只拉缺口；间隔 <=10 天的缺口合并
        for prev, cur in zip(missing, missing[1:]):
            if (cur - prev).days > 10:
                ranges.append((seg, prev))
                seg = cur
        ranges.append((seg, missing[-1]))

    parts, n_fetched = [], 0
    for a, b in ranges:
        chunk = fetch_raw(code, a, b)
        n_fetched += len(chunk)
        parts.append(chunk)

    if n_local:
        parts.insert(0, local[RAW_COLUMNS])
    merged = pd.concat(parts, ignore_index=True)
    merged = merged.drop_duplicates(subset="date", keep="last").sort_values("date").reset_index(drop=True)
    merged = attach_factor(merged, code, end)
    save_kline(code, merged)

    dt = time.perf_counter() - t0
    if verbose:
        print(f"  本次拉取 {n_fetched} 行，本地已有 {n_local} 行 -> 合并后 {len(merged)} 行"
              f"   缺失交易日 {len(missing)} 个（分 {len(ranges)} 段拉取）   耗时 {dt:.2f}s")
    return merged, {"fetched": n_fetched, "local": n_local, "merged": len(merged),
                    "ranges": len(ranges), "skipped": False, "seconds": dt}


# ============ 演示：三种边界 ============
if __name__ == "__main__":
    CODE = "600519"
    END = date.today()
    START = END - timedelta(days=365 * 2)

    print(f"=== storage_kline  股票 {CODE}  窗口 {START} ~ {END} ===")
    print(f"存储路径 {parquet_path(CODE)}   列 {COLUMNS}")
    print()

    print("--- 场景1：本地无数据 -> 全量拉取 ---")
    parquet_path(CODE).unlink(missing_ok=True)
    update_kline(CODE, START, END)
    print()

    print("--- 场景2：紧接着再跑一次 -> 应跳过 ---")
    update_kline(CODE, START, END)
    print()

    print("--- 场景3：人为挖掉中间 3 个交易日 -> 应只补拉缺口 ---")
    df = load_kline(CODE)
    mid = len(df) // 2
    cut = df["date"].iloc[mid:mid + 3]
    print(f"  删除 {[str(d.date()) for d in cut]}")
    df.drop(index=df.index[mid:mid + 3]).to_parquet(parquet_path(CODE), index=False)
    update_kline(CODE, START, END)
    print()

    out = load_kline(CODE)
    print(f"=== 最终本地数据：{len(out)} 行  {out['date'].min().date()} ~ {out['date'].max().date()} ===")
    show = out.tail(3).copy()
    show["前复权价"] = (show["close"] / show["qfq_factor"]).round(2)
    print(show[["date", "close", "qfq_factor", "前复权价"]].to_string(index=False))
