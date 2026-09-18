"""T2-1 最小闭环：单只股票缠论结构计算（分型 / 笔 / 中枢 / 三类买卖点）。

运行：python min_loop.py

环境：Python 3.12.10  +  czsc 1.0.1  +  akshare 1.18.96
说明：czsc 1.0.1 不提供内置买卖点信号函数，三类买卖点规则在本脚本内实现（PRD F2.4 方案 B）。
"""
import platform
from datetime import date, timedelta

import akshare as ak
import pandas as pd

import czsc

SYMBOL = "600519"
TS_SYMBOL = "sh600519"
YEARS = 2
UP, DOWN = "向上", "向下"

# ---------- 1. 拉数据（腾讯源，前复权） ----------
end = date.today()
start = end - timedelta(days=365 * YEARS)
raw = ak.stock_zh_a_hist_tx(symbol=TS_SYMBOL, start_date=start.strftime("%Y%m%d"),
                            end_date=end.strftime("%Y%m%d"), adjust="qfq")
raw = raw.rename(columns={"volume": "vol"})
raw["dt"] = pd.to_datetime(raw["date"])
raw["symbol"] = SYMBOL
bars = czsc.format_standard_kline(raw, freq=czsc.Freq.D)

print(f"Python {platform.python_version()}   czsc {czsc.__version__}   akshare {ak.__version__}")
print(f"标的 {SYMBOL}   前复权(qfq)   K线 {len(bars)} 根   {bars[0].dt.date()} ~ {bars[-1].dt.date()}")
print(f"最新收盘 {bars[-1].close:.2f}")
print()

# ---------- 2. czsc 结构计算 ----------
c = czsc.CZSC(bars)
fxs = list(c.fx_list)
bis = list(c.bi_list)
zss = list(c.zs_list)

print("=== 最近 5 个分型 ===")
for f in fxs[-5:]:
    print(f"  {f.dt.date()}  {f.mark}  分型价={f.fx:>9.2f}   区间[{f.low:.2f}, {f.high:.2f}]")
print()

print("=== 最近 5 笔 ===")
for b in bis[-5:]:
    print(f"  {b.sdt.date()} -> {b.edt.date()}  {b.direction}  {b.low:>9.2f} ~ {b.high:>9.2f}"
          f"   幅度={b.change:+7.2%}  长度={b.length:>2d}  力度={b.power_price:.3f}")
print()

print("=== 最近 3 个中枢 ===")
if not zss:
    print("  无")
for z in zss[-3:]:
    print(f"  {z.sdt.date()} -> {z.edt.date()}   中枢区间[zd={z.zd:>9.2f}, zg={z.zg:>9.2f}]"
          f"   中轴={z.zz:>9.2f}   极值[dd={z.dd:.2f}, gg={z.gg:.2f}]   含笔={len(z.bis)}")
print()

# ---------- 3. 三类买卖点（方案 B：基于 bi_list / zs_list 自研规则） ----------
signals = []

# 3.1 第一类买卖点：下跌/上涨段末端背驰（价格创新低 + 力度/量能/长度衰减）
for i in range(2, len(bis)):
    cur, prev, mid = bis[i], bis[i - 2], bis[i - 1]
    if str(prev.direction) != str(cur.direction) or str(mid.direction) == str(cur.direction):
        continue
    weaker = cur.power_price < prev.power_price and (
        cur.power_volume < prev.power_volume or cur.length < prev.length)
    if str(cur.direction) == DOWN and cur.low < prev.low and weaker:
        signals.append((cur.edt, "第一类买点", cur.low,
                        f"下跌笔创新低 {cur.low:.2f} < 前低 {prev.low:.2f}，力度 {cur.power_price:.3f} < {prev.power_price:.3f}"))
    if str(cur.direction) == UP and cur.high > prev.high and weaker:
        signals.append((cur.edt, "第一类卖点", cur.high,
                        f"上涨笔创新高 {cur.high:.2f} > 前高 {prev.high:.2f}，力度 {cur.power_price:.3f} < {prev.power_price:.3f}"))

# 3.2 第二类买卖点：一类点之后回抽不破前低/前高
for j in range(2, len(bis) - 2):
    cur = bis[j]
    for sig in list(signals):
        if sig[0] != cur.edt:
            continue
        b1, b2 = bis[j + 1], bis[j + 2]
        if sig[1] == "第一类买点" and str(b1.direction) == UP and str(b2.direction) == DOWN:
            if b2.low > cur.low:
                signals.append((b2.edt, "第二类买点", b2.low,
                                f"一买 {cur.low:.2f} 后回抽不破前低，回踩低点 {b2.low:.2f}"))
        if sig[1] == "第一类卖点" and str(b1.direction) == DOWN and str(b2.direction) == UP:
            if b2.high < cur.high:
                signals.append((b2.edt, "第二类卖点", b2.high,
                                f"一卖 {cur.high:.2f} 后反抽不破前高，反抽高点 {b2.high:.2f}"))

# 3.3 第三类买卖点：突破中枢后回踩不回中枢
for i in range(len(bis) - 1):
    b, nb = bis[i], bis[i + 1]
    z = next((x for x in reversed(zss) if x.sdt <= b.sdt), None)
    if z is None:
        continue
    if str(b.direction) == UP and b.high > z.zg and str(nb.direction) == DOWN and nb.low > z.zg:
        signals.append((nb.edt, "第三类买点", nb.low,
                        f"向上突破中枢 zg={z.zg:.2f}，回踩低点 {nb.low:.2f} 未回中枢"))
    if str(b.direction) == DOWN and b.low < z.zd and str(nb.direction) == UP and nb.high < z.zd:
        signals.append((nb.edt, "第三类卖点", nb.high,
                        f"向下跌破中枢 zd={z.zd:.2f}，反抽高点 {nb.high:.2f} 未回中枢"))

signals.sort(key=lambda s: s[0])

print(f"=== 买卖点信号（共 {len(signals)} 条）===")
if not signals:
    print("  无")
for dt, typ, price, reason in signals:
    print(f"  {dt.date()}  {typ}  价格={price:>9.2f}   {reason}")
print()

counts = {}
for _, typ, _, _ in signals:
    counts[typ] = counts.get(typ, 0) + 1
print("统计：" + ("  ".join(f"{k} {v} 条" for k, v in sorted(counts.items())) if counts else "无信号"))
