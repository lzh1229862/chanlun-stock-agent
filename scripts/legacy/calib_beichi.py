"""ADR-006 校准：一类买卖点背驰的力度容差 POWER_TOL。

问题
    2026-01-06 第一类卖点，当前笔/对比笔的 power_price 比值在两种价格口径下分别为
    0.9924 与 1.0059 —— 仅因价格口径差 0.36% 就跨过 1.0，导致信号时有时无。
    而同一信号的 power_volume 比值 0.5192、length 比值 0.4167 都很安全，
    说明脆弱点只在「价格力度」这一项。

方法
    口径 A = 腾讯直接前复权；口径 B = Parquet 不复权 / 新浪因子（当前主流程口径）。
    对 5 只股票分别按两种口径计算信号，扫描 POWER_TOL，寻找
    「两种口径信号集合完全一致」的最小容差。

运行：python calib_beichi.py
"""
from datetime import date, timedelta

import akshare as ak
import pandas as pd

import czsc
from min_loop import MAX_BI_NUM, MIN_BI_LEN, build_signals, build_zs
from storage_kline import load_kline, update_kline

STOCKS = ["600519", "000001", "300750", "601318", "000858"]
TOLS = [0.0, 0.005, 0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15]

END = date.today()
START = END - timedelta(days=365 * 2)


def ts_code(code):
    return ("sh" if code[0] == "6" else "sz") + code


def bis_of(px):
    px = px.rename(columns={"volume": "vol"}).copy()
    px["dt"] = pd.to_datetime(px["date"])
    px["symbol"] = "S"
    bars = czsc.format_standard_kline(px, freq=czsc.Freq.D)
    return list(czsc.CZSC(bars, min_bi_len=MIN_BI_LEN, max_bi_num=MAX_BI_NUM).bi_list)


def load_both(code):
    """返回 (bis_A, bis_B)。"""
    a = ak.stock_zh_a_hist_tx(symbol=ts_code(code), start_date=START.strftime("%Y%m%d"),
                              end_date=END.strftime("%Y%m%d"), adjust="qfq")
    update_kline(code, START, END, verbose=False)
    b = load_kline(code)
    for c in ("open", "high", "low", "close"):
        b[c] = b[c] / b["qfq_factor"]
    return bis_of(a), bis_of(b)


print(f"取数中：{len(STOCKS)} 只股票 × {len(TOLS)} 个容差，窗口 {START} ~ {END}")
data = {}
for code in STOCKS:
    ba, bb = load_both(code)
    data[code] = (ba, build_zs(ba), bb, build_zs(bb))
print("取数完成")
print()

print("=== 容差扫描 ===")
print(f"  {'tol':>6}  {'A信号数':>8}{'B信号数':>9}{'A/B差异':>9}   稳定性")
best = None
for tol in TOLS:
    na = nb = n_diff = 0
    diffs = []
    for code, (ba, za, bb, zb) in data.items():
        sa = {(r["date"], r["type"]) for r in build_signals(ba, za, tol)}
        sb = {(r["date"], r["type"]) for r in build_signals(bb, zb, tol)}
        na += len(sa)
        nb += len(sb)
        n_diff += len(sa ^ sb)
        for d in sorted(sa ^ sb):
            diffs.append((code, d))
    ok = "一致" if n_diff == 0 else "不一致"
    print(f"  {tol:>6.3f}  {na:>8}{nb:>9}{n_diff:>9}   {ok}")
    if n_diff == 0 and best is None:
        best = (tol, na)
    if tol == 0.0 and diffs:
        for code, d in diffs:
            print(f"          差异信号: {code} {d[0]} {d[1]}")
print()

print("=== 各股票信号数（容差 0 vs 选定值）===")
for tol in [0.0, best[0] if best else TOLS[-1]]:
    row = []
    for code, (ba, za, bb, zb) in data.items():
        row.append(f"{code}:{len(build_signals(bb, zb, tol))}")
    print(f"  tol={tol:.3f}  " + "  ".join(row))
print()

if best:
    print(f"==> 最小稳定容差 = {best[0]:.3f}（5 只股票合计 {best[1]} 条信号，两种口径完全一致）")
else:
    print("==> 扫描范围内未找到完全一致的最小容差，需扩大范围或改判定方式")
