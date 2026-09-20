"""migrate_hist.py —— 把股票池的历史一次性拉长到指定起点（T14-1 / ADR-019）。

为什么需要
    默认只拉 2 年（analyzer.ensure_kline 的 years=2），覆盖不了熊市。
    拉长到 2015-01-01 后，下行样本占比从 5.5% 涨到 29.3%，四个结论因此被推翻。

安全措施
    1. **先备份** data/raw/*.parquet + chan_agent.db 到 data/_backup_before_hist/（只备一次）
    2. **只扩不缩** —— 不删任何已有数据；事后想回退，把备份覆盖回去即可
    3. 跑完做 **max_bi_num 余量检查**：czsc 超限是静默截断（丢早期笔、首笔日期漂移），
       不会报错，所以必须主动查。余量低于 20% 就该把 MAX_BI_NUM 调大（见 min_loop.py）

跑完之后**还不是结束**：要清空 signals/backtest/reports 再用新历史整体重建，
否则 2 年和 11 年的样本会混在一起，统计失去意义。见 ADR-019。

用法
    python migrate_hist.py                              # 池内股票，起点 2015-01-01
    python migrate_hist.py --start 2010-01-01
    python migrate_hist.py --codes 600519,000001
    python migrate_hist.py --check-only                 # 只查 max_bi_num 余量
"""
import argparse
import shutil
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

import storage_kline as sk
import watchlist_store as ws
from min_loop import MAX_BI_NUM, MIN_BI_LEN

BACKUP = Path("data/_backup_before_hist")


def backup_once():
    if BACKUP.exists():
        print("  备份已存在，跳过：%s" % BACKUP)
        return
    BACKUP.mkdir(parents=True)
    n = 0
    for p in Path("data/raw").glob("*.parquet"):
        shutil.copy2(p, BACKUP / p.name)
        n += 1
    db = Path("data/chan_agent.db")
    if db.exists():
        shutil.copy2(db, BACKUP / db.name)
    print("  已备份 %d 个 Parquet + %s -> %s" % (n, "1 个 DB" if db.exists() else "无 DB", BACKUP))


def check_bi_headroom(codes, verbose=True):
    """czsc 的 max_bi_num 是静默截断，必须主动查余量。"""
    import czsc
    worst = 0
    bad = []
    for c in codes:
        q = sk.load_kline(c)
        if q.empty:
            continue
        q = q.copy()
        for x in ("open", "high", "low", "close"):
            q[x] = q[x] / q["qfq_factor"]
        qq = q.rename(columns={"volume": "vol"}).copy()
        qq["dt"] = pd.to_datetime(qq["date"])
        qq["symbol"] = c
        b = czsc.format_standard_kline(qq, freq=czsc.Freq.D)
        n_cap = len(czsc.CZSC(b, min_bi_len=MIN_BI_LEN, max_bi_num=MAX_BI_NUM).bi_list)
        n_all = len(czsc.CZSC(b, min_bi_len=MIN_BI_LEN, max_bi_num=99999).bi_list)
        worst = max(worst, n_all)
        if n_cap != n_all:
            bad.append(c)
        if verbose:
            print("    %s  柱 %4d  笔(上限%d)=%3d  笔(不限)=%3d%s"
                  % (c, len(q), MAX_BI_NUM, n_cap, n_all, "   <-- 截断!" if n_cap != n_all else ""))
    pct_used = 100.0 * worst / MAX_BI_NUM
    print("  池内最大笔数 %d / 上限 %d（已用 %.0f%%）" % (worst, MAX_BI_NUM, pct_used))
    if bad or pct_used > 80:
        print("  ⚠ 余量不足或有截断 —— 请调大 min_loop.MAX_BI_NUM（股票：%s）" % (bad or "无"))
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01", help="历史起点，默认 2015-01-01")
    ap.add_argument("--end", default=str(date.today()))
    ap.add_argument("--codes", default=None, help="逗号分隔；默认取股票池")
    ap.add_argument("--check-only", action="store_true", help="只检查 max_bi_num 余量")
    a = ap.parse_args()

    codes = ([c.strip() for c in a.codes.split(",")] if a.codes else ws.load_watchlist())
    start = date(*[int(x) for x in a.start.split("-")])
    end = date(*[int(x) for x in a.end.split("-")])

    if a.check_only:
        print("=== max_bi_num 余量检查（上限 %d）===" % MAX_BI_NUM)
        return 0 if check_bi_headroom(codes) else 1

    print("=== 1/3 备份 ===")
    backup_once()

    print()
    print("=== 2/3 扩展 %d 只到 %s ===" % (len(codes), start))
    t_all = time.time()
    fails = []
    for c in codes:
        before = sk.load_kline(c)
        t0 = time.time()
        try:
            sk.update_kline(c, start, end, verbose=False)
        except Exception as e:
            print("  %s 失败: %s: %s" % (c, type(e).__name__, str(e)[:70]))
            fails.append(c)
            continue
        d = sk.load_kline(c)
        print("  %s  %4d -> %4d 行  (%s ~ %s)  %5.1fs"
              % (c, len(before), len(d), d["date"].min().date(), d["date"].max().date(),
                 time.time() - t0), flush=True)
    print("  合计 %.1fs%s" % (time.time() - t_all, ("  失败 %s" % fails) if fails else ""))

    print()
    print("=== 3/3 max_bi_num 余量检查（上限 %d）===" % MAX_BI_NUM)
    ok = check_bi_headroom(codes)
    print()
    print("下一步（本脚本**不会**自动做）：")
    print("  1. 清空 signals / backtest / reports")
    print("  2. python main.py --no-llm        # 用新历史整体重建")
    print("  3. 重跑 verify_edge / verify_robustness / verify_level")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
