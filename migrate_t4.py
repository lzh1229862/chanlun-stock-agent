"""T4-0 signals 表字段迁移（独立脚本，幂等，可审计）。

迁移内容
    给 signals 表补三列：
        filter_version   TEXT     默认 'v0_no_filter'
        is_primary       INTEGER  默认 1
        signal_group_id  TEXT     回填为 stock_code || '_' || signal_date

特性
    - 迁移前自动备份库文件
    - 打印迁移前后：列清单 / 行数 / 原始字段指纹 / 完整性检查
    - 原始字段指纹一致 => 可证明旧数据一行未丢、一列未改
    - 幂等：已迁移过的库再跑只会报告「无需迁移」

运行
    python migrate_t4.py                      # 迁移默认库 data/chan_agent.db
    python migrate_t4.py --db data/other.db   # 迁移指定库
    python migrate_t4.py --dry-run            # 只报告计划，不写入
    python migrate_t4.py --no-backup          # 跳过备份
"""
import argparse
import hashlib
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from storage_signal import DB_PATH, NEW_COLUMNS, _migrate, connect

# 迁移不应该触碰的原始字段（用于指纹比对）
ORIGINAL_FIELDS = ["id", "stock_code", "signal_date", "signal_type",
                   "signal_reason", "is_tradable", "created_at"]

NEW_FIELDS = [name for name, _ in NEW_COLUMNS]


def snapshot(conn):
    """采集迁移前后可比对的指标。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(signals)")]
    rows = conn.execute(f"SELECT {', '.join(ORIGINAL_FIELDS)} FROM signals ORDER BY id").fetchall()
    h = hashlib.md5()
    for r in rows:
        h.update("|".join(str(v) for v in r).encode("utf-8"))
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    return {"cols": cols, "n": len(rows), "fingerprint": h.hexdigest(), "integrity": integrity}


def null_report(conn):
    out = {}
    for f in NEW_FIELDS:
        col = f
        if f == "signal_group_id":
            out[f] = conn.execute(
                f"SELECT COUNT(*) FROM signals WHERE {col} IS NULL OR {col} = ''").fetchone()[0]
        else:
            out[f] = conn.execute(f"SELECT COUNT(*) FROM signals WHERE {col} IS NULL").fetchone()[0]
    return out


def show(tag, snap):
    print(f"--- {tag} ---")
    print(f"  列清单      : {snap['cols']}")
    print(f"  行数        : {snap['n']}")
    print(f"  原始字段指纹: {snap['fingerprint']}")
    print(f"  完整性检查  : {snap['integrity']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    print(f"=== T4-0 signals 表字段迁移 ===")
    print(f"库文件 {db.resolve()}")
    print()
    if not db.exists():
        print("库文件不存在，无需迁移。")
        return

    with closing(connect()) as _:
        pass

    conn = sqlite3.connect(db)
    try:
        before = snapshot(conn)
        missing = [f for f in NEW_FIELDS if f not in before["cols"]]
        show("迁移前", before)
        print()

        if not missing:
            n_null = null_report(conn)
            print("--- 迁移计划 ---")
            print(f"  三列均已存在，无需 ALTER")
            print(f"  回填检查    : {n_null}")
            print()
            print("==> 无需迁移")
            return

        print("--- 迁移计划 ---")
        print(f"  新增列      : {missing}")
        print(f"  默认值      : filter_version='v0_no_filter'、is_primary=1")
        print(f"  回填        : signal_group_id = stock_code || '_' || signal_date")
        print(f"  备份        : {'跳过 (--no-backup)' if args.no_backup else '自动备份'}")
        print()

        if args.dry_run:
            print("==> --dry-run：未做任何写入")
            return

        if not args.no_backup:
            bak = db.with_suffix(db.suffix + ".bak_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
            shutil.copy2(db, bak)
            print(f"已备份到 {bak.name}")
            print()

        _migrate(conn)
        conn.commit()

        after = snapshot(conn)
        show("迁移后", after)
        print(f"  NULL/空值    : {null_report(conn)}")
        row = conn.execute("SELECT id, filter_version, is_primary, signal_group_id"
                           " FROM signals ORDER BY id LIMIT 1").fetchone()
        print(f"  样本         : id={row[0]}  filter_version={row[1]!r}"
              f"  is_primary={row[2]}  signal_group_id={row[3]!r}")
        print()

        print("=== 结论 ===")
        print(f"  行数        {before['n']} -> {after['n']}      {'✅ 未丢数据' if before['n'] == after['n'] else '❌ 行数变化！'}")
        print(f"  原始字段指纹 {'一致' if before['fingerprint'] == after['fingerprint'] else '不一致'}          "
              f"{'✅ 旧字段一行未改' if before['fingerprint'] == after['fingerprint'] else '❌ 原始数据被改动！'}")
        print(f"  新字段默认值 {'已就位' if all(v == 0 for v in null_report(conn).values()) else '仍有空值'}")
        print(f"  完整性检查  {after['integrity']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
