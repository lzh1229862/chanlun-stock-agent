"""T0-1 可行性验证：用 czsc 计算分型 / 笔 / 中枢 / 买卖点信号。

运行：python verify_czsc.py
"""
from time import perf_counter

import akshare as ak
import pandas as pd

import czsc

SYMBOL = "600519"
TS_SYMBOL = "sh600519"
START, END = "20240918", "20260918"
SHOW = 3

t0 = perf_counter()
raw = ak.stock_zh_a_hist_tx(symbol=TS_SYMBOL, start_date=START, end_date=END, adjust="qfq")
raw = raw.rename(columns={"volume": "vol"})
raw["dt"] = pd.to_datetime(raw["date"])
raw["symbol"] = SYMBOL
bars = czsc.format_standard_kline(raw, freq=czsc.Freq.D)

print(f"czsc 版本  : {czsc.__version__}")
print(f"数据源     : akshare.stock_zh_a_hist_tx (腾讯, qfq 前复权)")
print(f"标的       : {SYMBOL}    K线根数: {len(bars)}    区间: {bars[0].dt.date()} ~ {bars[-1].dt.date()}")
print(f"拉取+转换  : {perf_counter() - t0:.1f}s")
print()

t0 = perf_counter()
c = czsc.CZSC(bars)
print(f"CZSC 计算  : {perf_counter() - t0:.2f}s    freq={c.freq}    symbol={c.symbol}")
print()

print(f"=== 分型 FX ===  数量: {len(c.fx_list)}   展示前 {SHOW} 条")
for f in c.fx_list[:SHOW]:
    print(f"  dt={f.dt.date()}  mark={f.mark}  fx={f.fx}  high={f.high}  low={f.low}  构成K线={len(f.elements)}")
print()

print(f"=== 笔 BI ===  数量: {len(c.bi_list)}   展示前 {SHOW} 条")
for b in c.bi_list[:SHOW]:
    print(f"  {b.direction}  {b.sdt.date()} -> {b.edt.date()}  high={b.high:.2f}  low={b.low:.2f}  长度={b.length}  幅度={b.change:+.2%}")
print()

print(f"=== 中枢 ZS ===  数量: {len(c.zs_list)}   展示前 {SHOW} 条")
for z in c.zs_list[:SHOW]:
    print(f"  {z.sdt.date()} -> {z.edt.date()}  zg={z.zg:.2f}  zd={z.zd:.2f}  zz={z.zz:.2f}  gg={z.gg:.2f}  dd={z.dd:.2f}  含笔={len(z.bis)}")
print()

print("=== 买卖点信号 ===")
print(f"czsc 内置 signals 模块      : {hasattr(czsc, 'signals')}")
print(f"CZSC.signals 内容           : {dict(c.signals)}")
print(f"批量信号接口                : czsc.generate_czsc_signals(bars, signals_config, sdt, init_n)")
print("说明: czsc 1.0.1 未随包提供任何买卖点信号函数，signals_config 必须由使用方自行提供")
print("      旧版 czsc 0.10.x 提供 czsc.signals.cxt_* 买卖点函数，例如")
print("      cxt_first_buy_V221126 / cxt_third_buy_V230228 / cxt_second_bs_V230320")
print("      其信号字段形如: 600519_日线_D1B_BUY1_一买_9笔_任意_0  ->  '一买_9笔_任意_0'")
