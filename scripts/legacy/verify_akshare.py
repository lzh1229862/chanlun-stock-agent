"""T0-2 可行性验证：AKShare 拉取 A 股日线（600519，最近 2 年，前复权 qfq）。

运行：python verify_akshare.py
"""
from datetime import date, timedelta
from time import perf_counter

import akshare as ak

SYMBOL = "600519"
YEARS = 2

end = date.today()
start = end - timedelta(days=365 * YEARS)
beg, fin = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

sources = [
    ("eastmoney / stock_zh_a_hist", lambda: ak.stock_zh_a_hist(
        symbol=SYMBOL, period="daily", start_date=beg, end_date=fin, adjust="qfq")),
    ("sina / stock_zh_a_daily", lambda: ak.stock_zh_a_daily(
        symbol=f"sh{SYMBOL}", start_date=start.isoformat(), end_date=end.isoformat(), adjust="qfq")),
    ("tencent / stock_zh_a_hist_tx", lambda: ak.stock_zh_a_hist_tx(
        symbol=f"sh{SYMBOL}", start_date=beg, end_date=fin, adjust="qfq")),
]

df = source = None
elapsed = 0.0
for name, fetch in sources:
    t0 = perf_counter()
    try:
        result = fetch()
    except Exception as e:
        print(f"[跳过] {name} -> {type(e).__name__}: {str(e)[:90]}")
        continue
    if result is None or len(result) == 0:
        print(f"[跳过] {name} -> 返回 0 行")
        continue
    df, source, elapsed = result, name, perf_counter() - t0
    break
else:
    raise SystemExit("所有数据源均失败，原因见上方 [跳过] 行")

cols = list(df.columns)
date_col = "date" if "date" in cols else "日期"

print(f"akshare 版本 : {ak.__version__}")
print(f"数据源      : {source}  ({elapsed:.1f}s)")
print(f"股票代码    : {SYMBOL}   复权: qfq(前复权)   请求区间: {start} ~ {end}")
print()
print("=== 字段列表 ===")
for i, c in enumerate(cols, 1):
    print(f"  {i:2d}. {c:<18} {df[c].dtype}")
print()
print("=== 前 5 行 ===")
print(df.head(5).to_string(index=False))
print()
print("=== 后 5 行 ===")
print(df.tail(5).to_string(index=False))
print()
print("=== 汇总 ===")
print(f"总行数   : {len(df)}")
print(f"日期范围 : {df[date_col].min()} ~ {df[date_col].max()}")
print(f"字段数量 : {len(cols)}")
