"""backtest_chunk.py —— 分批跑回测（T15-2）。

为什么需要
    backtest.run_backtest 一次要处理库里所有信号，每条信号都要跑一次
    confirm_dates.confirmation_delay（内部是前缀重算 czsc），股票池一大就超过
    命令行工具的墙钟上限。这个脚本把股票集切成若干批，**每批复用 run_backtest
    的完整逻辑**（只是把 bt.query_signals 临时换成子集），逐批落库。

    分批是安全的：backtest 表有 UNIQUE(stock_code, signal_date, signal_type, scope, window)，
    同一批重跑不会产生重复行。

用法
    python backtest_chunk.py --scope signal --n 18 --chunk 0
    python backtest_chunk.py --scope signal --n 18 --chunk 1
    python backtest_chunk.py --scope day    --n 18 --chunk 0 ...
"""
import argparse
import sys
import time

import backtest as bt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["signal", "day"], default="signal")
    ap.add_argument("--n", type=int, default=18, help="每批股票数")
    ap.add_argument("--chunk", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    all_rows = bt.query_signals()
    codes = sorted({r["stock_code"] for r in all_rows})
    part = codes[a.chunk * a.n:(a.chunk + 1) * a.n]
    if not part:
        print("chunk %d 为空（共 %d 只，n=%d）" % (a.chunk, len(codes), a.n))
        return 0
    sub = [r for r in all_rows if r["stock_code"] in part]

    print("=== 回测分批：scope=%s  chunk=%d  n=%d ===" % (a.scope, a.chunk, a.n))
    print("  全库 %d 只股票 / %d 条信号；本批 %d 只 / %d 条"
          % (len(codes), len(all_rows), len(part), len(sub)))
    print("  本批股票:", " ".join(part))
    print()

    orig = bt.query_signals
    bt.query_signals = lambda: sub
    t0 = time.time()
    try:
        recs = bt.run_backtest(scope=a.scope, verbose=True)
    finally:
        bt.query_signals = orig

    print("  用时 %.1fs" % (time.time() - t0))
    if a.dry_run:
        print("  --dry-run：不写库")
        return 0
    bt.init_backtest_db()
    saved = bt.save_backtest(recs)
    print("  已写入 %d 行" % saved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
