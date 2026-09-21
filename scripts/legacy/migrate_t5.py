"""T5-0 信号三日期字段迁移 + 历史回填（独立脚本，幂等，可审计）。

新增字段（ADR-012）
    confirm_date     TEXT   信号可被确认的日期 = 信号日 + 实测确认延迟（交易日）
    entry_ref_price  REAL   入场参考价 = 确认日次一交易日的【不复权】开盘价
    backfill_note    TEXT   回填依据 / NULL 原因

回填策略（方案 A：实测 + 增量回填）
    能算的算；算不了的留 NULL 并在 backfill_note 说明原因
    每次运行都会对 confirm_date IS NULL 的信号重试，直至确认

运行
    python migrate_t5.py --dry-run       # 只看计划与影响，不写库
    python migrate_t5.py                 # 正式迁移 + 回填
    python migrate_t5.py --db data/x.db
    python migrate_t5.py --no-backup
"""
import argparse
import hashlib
import shutil
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime
from pathlib import Path

from confirm_dates import backfill
from storage_signal import DB_PATH, NEW_COLUMNS, _migrate, connect

# 迁移前就存在的字段（用于证明「老数据一行未改」）
PRE_FIELDS = ["id", "stock_code", "signal_date", "signal_type", "signal_reason",
              "is_tradable", "created_at", "filter_version", "is_primary", "signal_group_id"]
NEW_FIELDS = ["confirm_date", "entry_ref_price", "backfill_note"]


def snapshot(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(signals)")]
    rows = conn.execute(f"SELECT {', '.join(PRE_FIELDS)} FROM signals ORDER BY id").fetchall()
    h = hashlib.md5()
    for r in rows:
        h.update("|".join(str(v) for v in r).encode("utf-8"))
    return {"cols": cols, "n": len(rows), "fingerprint": h.hexdigest(),
            "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0]}


def show(tag, s):
    print(f"--- {tag} ---")
    print(f"  列清单      : {s['cols']}")
    print(f"  行数        : {s['n']}")
    print(f"  老字段指纹  : {s['fingerprint']}")
    print(f"  完整性检查  : {s['integrity']}")


def sample(conn, n=3):
    return conn.execute(
        f"SELECT stock_code, signal_date, signal_type, confirm_date, entry_ref_price, backfill_note"
        f" FROM signals ORDER BY signal_date LIMIT {n}").fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    a = ap.parse_args()

    db = Path(a.db)
    print("=== T5-0 信号三日期字段迁移 ===")
    print(f"库文件 {db.resolve()}")
    print(f"模式   {'--dry-run（不写库）' if a.dry_run else '正式迁移 + 回填'}")
    print()
    if not db.exists():
        print("库文件不存在，无需迁移。")
        return

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        before = snapshot(conn)
        missing = [f for f in NEW_FIELDS if f not in before["cols"]]
        show("迁移前", before)
        print()

        print("--- 迁移计划 ---")
        print(f"  新增列      : {missing if missing else '（三列均已存在）'}")
        print(f"  回填        : confirm_date 与 entry_ref_price（方案 A：实测确认延迟）")
        print(f"  回填不了    : confirm_date=NULL + backfill_note 说明原因")
        print(f"  备份        : {'跳过' if a.no_backup else '自动备份'}")
        print()

        if a.dry_run:
            pend = [r for r in conn.execute("SELECT COUNT(*) FROM signals").fetchall()]
            cnt = conn.execute("SELECT COUNT(*) FROM signals WHERE confirm_date IS NULL").fetchone()[0] \
                if "confirm_date" in before["cols"] else before["n"]
            print(f"  待回填信号  : {cnt} 条")
            print()
            print("==> --dry-run：未做任何写入")
            return

        if not a.no_backup:
            bak = db.with_suffix(db.suffix + ".bak_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
            shutil.copy2(db, bak)
            print(f"已备份到 {bak.name}")
            print()

        # 1) 补列
        _migrate(conn)
        conn.commit()
        after_cols = [r[1] for r in conn.execute("PRAGMA table_info(signals)")]
        print(f"补列完成，现有列数 {len(after_cols)}")
        print()

        # 2) 回填
        print("--- 回填 ---")
        results = backfill(db_path=db, verbose=True)
        print()

        rows = conn.execute(
            "SELECT stock_code, signal_date, signal_type, confirm_date, entry_ref_price, backfill_note"
            " FROM signals ORDER BY signal_date LIMIT 5").fetchall()
        print("--- 回填后示例（前 5 条）---")
        for r in rows:
            print(f"  {r['stock_code']} {r['signal_date']} {r['signal_type']:<12}"
                  f" confirm={r['confirm_date'] or 'NULL':<12} entry={r['entry_ref_price'] or 'NULL'}")
            print(f"        note: {r['backfill_note']}")
        print()

        after = snapshot(conn)
        show("迁移后", after)
        print()

        print("--- NULL 情况说明 ---")
        n_null = conn.execute("SELECT COUNT(*) FROM signals WHERE confirm_date IS NULL").fetchone()[0]
        if n_null == 0:
            print("  全部信号已算出确认日与入场价，无 NULL ✅")
        else:
            print(f"  仍有 {n_null} 条 confirm_date 为 NULL，原因分布：")
            for note, c in conn.execute(
                    "SELECT backfill_note, COUNT(*) FROM signals WHERE confirm_date IS NULL"
                    " GROUP BY 1 ORDER BY 2 DESC"):
                print(f"    {c:>3} 条  {note}")
        print()

        print("=== 结论 ===")
        print(f"  行数        {before['n']} -> {after['n']}      "
              f"{'✅ 未丢数据' if before['n'] == after['n'] else '❌ 行数变化！'}")
        print(f"  老字段指纹  {'一致' if before['fingerprint'] == after['fingerprint'] else '不一致'}          "
              f"{'✅ 老数据一行未改' if before['fingerprint'] == after['fingerprint'] else '❌ 老数据被改动！'}")
        print(f"  完整性检查  {after['integrity']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
