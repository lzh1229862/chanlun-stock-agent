"""delisted_pool.py —— 退市股池：解决幸存者偏差（T24 / ADR-027）。

### 为什么

现有 45 只样本**全部是活下来的股票**。规则如果对「会死的股票」表现差很多，
前面的所有数字都是乐观的。要检验这一点，必须把退市股拉进来。

### 可行性（已实测）

- 退市名单可得：上交所 " + "stock_info_sh_delist" + " / 深交所 " + "stock_info_sz_delist" + "，合计 **367 只**（2015 年后约 276 只）
- **历史行情能拿到，且恰好止于退市日**（实测 600087 止于 2014-06-04、600806 止于 2018-07-11、600005 止于 2017-01-23）
- 更老的（2002~2009 退市）数据已被数据源清理，取不到

### 选项 (c)：按日期判定 ST 状态

主流水线的 F3.1 过滤读的是**当前名称**（含 "ST" 即不可交易）。但退市股的当前名称大多含 "退"，
会被整体排除 —— 等于白做。而且**它们在退市前确实大多是 ST**，直接放进来也不对。

选项 (c)：**退市前 " + "ST_WINDOW_MONTHS" + " 个月视为 ST**（A 股从实施退市风险警示到终止上市通常 1~2 年）。
落在窗口内的信号**丢弃**，窗口外的正常参与统计。

### 存哪

" + "data/delisted/{code}.parquet" + " —— **与 data/raw/ 分开**。
绝不能混进现有 45 只的统计样本，否则两个池子互相污染、结论说不清。
"""
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

DELISTED_DIR = Path("data/delisted")
POOL_PATH = Path("config/delisted.json")
ST_WINDOW_MONTHS = 12
MIN_BARS = 250


def pool_path():
    return POOL_PATH


def build_pool(verbose=True):
    """从交易所退市名单建池（含退市日）。"""
    import akshare as ak
    sh = ak.stock_info_sh_delist()
    sz = ak.stock_info_sz_delist(symbol="终止上市公司")
    sh["code"] = sh["公司代码"].astype(str).str.zfill(6)
    sh["name"] = sh["公司简称"].astype(str)
    sh["dl"] = pd.to_datetime(sh["暂停上市日期"], errors="coerce")
    sz["code"] = sz["证券代码"].astype(str).str.zfill(6)
    sz["name"] = sz["证券简称"].astype(str)
    sz["dl"] = pd.to_datetime(sz["终止上市日期"], errors="coerce")
    a = pd.concat([sh[["code", "name", "dl"]].assign(src="sh"),
                   sz[["code", "name", "dl"]].assign(src="sz")])
    a = a.dropna(subset=["dl"]).drop_duplicates("code")
    # 只留沪深主板/创业板/科创板（与主池口径一致），排除北交所
    a = a[a["code"].str.startswith(("60", "68", "00", "30"))]
    a = a[a["dl"].dt.year >= 2015].sort_values("dl")
    rec = [{"code": r["code"], "name": r["name"], "delist_date": str(r["dl"].date()),
            "src": r["src"]} for _, r in a.iterrows()]
    POOL_PATH.parent.mkdir(parents=True, exist_ok=True)
    POOL_PATH.write_text(json.dumps({"built_at": str(date.today()),
                                     "st_window_months": ST_WINDOW_MONTHS,
                                     "n": len(rec), "stocks": rec},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
    if verbose:
        print("  退市股池 %d 只（2015 年后、沪深主板/创业板/科创板）-> %s" % (len(rec), POOL_PATH))
        print("  退市年份:", a["dl"].dt.year.value_counts().sort_index().to_dict())
    return rec


def load_pool():
    if not POOL_PATH.exists():
        return build_pool()
    return json.loads(POOL_PATH.read_text(encoding="utf-8"))["stocks"]


def is_in_st_window(signal_date, delist_date):
    """选项 (c)：信号日是否落在「退市前 ST 窗口」内（落在里面就丢弃）。"""
    sd, dd = pd.Timestamp(signal_date), pd.Timestamp(delist_date)
    return sd >= dd - pd.DateOffset(months=ST_WINDOW_MONTHS)


def fetch_one(code, verbose=False):
    """拉一只退市股的历史（到退市日为止）。返回 (status, detail)。"""
    from storage_kline import fetch_raw, fetch_factor, attach_factor
    try:
        raw = fetch_raw(code, date(2005, 1, 1), date(2026, 9, 18))
    except Exception as e:
        return "fail", "%s: %s" % (type(e).__name__, str(e)[:50])
    if raw is None or len(raw) < MIN_BARS:
        return "empty", "%d 行" % (0 if raw is None else len(raw))
    # ⚠️ 这里原来写的是 fetch_factor(code) —— 少传 start/end，TypeError 被 except 静默吞掉，
    # 结果 252 只退市股全部存成了 qfq_factor=1.0（不复权）。除权缺口会造出假的下跌笔。
    # 教训与 ADR-028 同一条：**宽泛的异常捕获会把「代码写错」伪装成「没有数据」**。
    end = raw["date"].max().date() if not raw.empty else date(2026, 9, 18)
    df = attach_factor(raw, code, end)
    if "qfq_factor" not in df.columns or df["qfq_factor"].nunique() <= 1:
        return "nofactor", "拿不到复权因子（qfq_factor 只有 1 个值）"
    DELISTED_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(DELISTED_DIR / ("%s.parquet" % code), index=False)
    return "ok", "%d 行 %s ~ %s" % (len(df), str(df["date"].min())[:10], str(df["date"].max())[:10])


def load_delisted(code):
    p = DELISTED_DIR / ("%s.parquet" % code)
    if not p.exists():
        return pd.DataFrame()
    d = pd.read_parquet(p)
    if not d.empty:
        d["date"] = pd.to_datetime(d["date"])
    return d


def fetch_batch(limit=40, skip_done=True, verbose=True):
    """分批拉取，可断点续跑（已有文件的跳过）。"""
    pool = load_pool()
    todo = [s for s in pool
            if not (skip_done and (DELISTED_DIR / ("%s.parquet" % s["code"])).exists())]
    todo = todo[:limit]
    print("=== 退市股取数：池 %d 只，待拉 %d 只，本次 %d 只 ===" % (len(pool), len(todo), len(todo)))
    stat = {"ok": 0, "empty": 0, "fail": 0}
    for s in todo:
        st, detail = fetch_one(s["code"])
        stat[st] = stat.get(st, 0) + 1
        if verbose:
            print("  %s %-8s %-8s %s" % (s["code"], s["name"][:8], st, detail), flush=True)
    print("  完成 %s" % stat)
    return stat


def main():
    import argparse
    import sys
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="重建退市股池")
    ap.add_argument("--limit", type=int, default=40)
    a = ap.parse_args()
    if a.build:
        build_pool()
        return 0
    if not POOL_PATH.exists():
        build_pool()
    fetch_batch(limit=a.limit)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
