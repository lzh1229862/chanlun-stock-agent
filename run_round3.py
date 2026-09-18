"""T3-4 串起数据 + 缠论 + 存储：一键跑通单只股票全流程。

流程：
    增量更新 K 线 -> 从 Parquet 读 -> 现算前复权 -> czsc 缠论 -> 统一格式信号 -> 写入 SQLite

运行：python run_round3.py
"""
import time
from datetime import date, timedelta

import czsc
import pandas as pd

from min_loop import MAX_BI_NUM, MIN_BI_LEN, build_signals, build_zs
from storage_kline import load_kline, parquet_path, update_kline
from storage_signal import query_signals, save_signals

CODE = "600519"
YEARS = 2


def to_qfq(df):
    """不复权 + 复权因子 -> 前复权（ADR-002）。前复权价 = 不复权价 / qfq_factor。"""
    out = df.copy()
    for c in ("open", "high", "low", "close"):
        out[c] = out[c] / out["qfq_factor"]
    return out


def main():
    t0 = time.perf_counter()
    end = date.today()
    start = end - timedelta(days=365 * YEARS)

    print(f"=== run_round3  股票 {CODE}  窗口 {start} ~ {end} ===")
    print()

    print("[1/5] 增量更新 K 线（Parquet）")
    _, st = update_kline(CODE, start, end)
    print(f"      文件 {parquet_path(CODE)}")
    print()

    print("[2/5] 从 Parquet 读取")
    df = load_kline(CODE)
    print(f"      本地 {len(df)} 行    {df['date'].min().date()} ~ {df['date'].max().date()}"
          f"    最新不复权收盘 {df['close'].iloc[-1]:.2f}   qfq_factor {df['qfq_factor'].iloc[-1]:.6f}")
    print()

    print("[3/5] 现算前复权 -> czsc 缠论计算")
    q = to_qfq(df)
    q["dt"] = pd.to_datetime(q["date"])
    q["symbol"] = CODE
    q = q.rename(columns={"volume": "vol"})
    bars = czsc.format_standard_kline(q, freq=czsc.Freq.D)
    c = czsc.CZSC(bars, min_bi_len=MIN_BI_LEN, max_bi_num=MAX_BI_NUM)
    bis = list(c.bi_list)
    zss = build_zs(bis)
    print(f"      K线 {len(bars)} 根   分型 {len(c.fx_list)}   笔 {len(bis)}   中枢 {len(zss)}"
          f"   最新前复权收盘 {q['close'].iloc[-1]:.2f}")
    print()

    print("[4/5] 生成统一格式信号")
    records = build_signals(bis, zss)
    signals = [{"date": r["date"], "type": r["type"], "reason": r["reason"]} for r in records]
    print(f"      本次算出 {len(signals)} 条")
    for s in signals[-3:]:
        print(f"        {s['date']}  {s['type']}  {s['reason'][:40]}")
    print()

    print("[5/5] 写入 SQLite")
    added, skipped = save_signals(CODE, signals)
    total = len(query_signals(code=CODE))
    print(f"      新增 {added} 条   重复跳过 {skipped} 条   库中 {CODE} 合计 {total} 条")
    print()

    print(f"=== 完成，总耗时 {time.perf_counter() - t0:.2f}s ===")
    print(f"    Parquet 拉取 {st['fetched']} 行   信号 {len(signals)} 条   入库新增 {added} 条")
    return {"fetched": st["fetched"], "signals": len(signals), "added": added, "total": total}


if __name__ == "__main__":
    main()
