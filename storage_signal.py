"""T3-3 用 SQLite 存缠论信号。

库路径  data/chan_agent.db
表      signals
字段    id, stock_code, signal_date, signal_type, signal_reason, is_tradable, created_at,
        filter_version, is_primary, signal_group_id
去重    UNIQUE(stock_code, signal_date, signal_type) —— 同一股票 + 同一日期 + 同一类型 不重复插入

运行：
    python storage_signal.py                       # 演示：写入 / 查询 / 重复写入不翻倍
    python storage_signal.py list                  # 列出全部信号
    python storage_signal.py list 600519           # 按股票列出
    python storage_signal.py list 600519 2026-01-01 2026-12-31   # 按股票 + 日期区间
"""
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

DB_PATH = Path("data/chan_agent.db")

DDL = """
CREATE TABLE IF NOT EXISTS signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code     TEXT    NOT NULL,
    signal_date    TEXT    NOT NULL,
    signal_type    TEXT    NOT NULL,
    signal_reason  TEXT    NOT NULL DEFAULT '',
    is_tradable    INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT    NOT NULL,
    filter_version TEXT    NOT NULL DEFAULT 'v0_no_filter',
    is_primary     INTEGER NOT NULL DEFAULT 1,
    signal_group_id TEXT   NOT NULL DEFAULT '',
    UNIQUE (stock_code, signal_date, signal_type)
);
CREATE INDEX IF NOT EXISTS idx_signals_code_date ON signals (stock_code, signal_date);
"""

# 老库补齐用的列（ADD COLUMN 只能给常量默认值）
NEW_COLUMNS = [
    ("filter_version", "TEXT NOT NULL DEFAULT 'v0_no_filter'"),
    ("is_primary", "INTEGER NOT NULL DEFAULT 1"),
    ("signal_group_id", "TEXT NOT NULL DEFAULT ''"),
]

FIELDS = ["id", "stock_code", "signal_date", "signal_type", "signal_reason", "is_tradable",
          "created_at", "filter_version", "is_primary", "signal_group_id"]

DEFAULT_FILTER_VERSION = "v0_no_filter"


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn):
    """老库补列 + 回填 signal_group_id（幂等）。"""
    have = {r[1] for r in conn.execute("PRAGMA table_info(signals)")}
    for name, ddl in NEW_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {name} {ddl}")
    conn.execute("UPDATE signals SET signal_group_id = stock_code || '_' || signal_date"
                 " WHERE signal_group_id = ''")


def init_db():
    """建库建表 + 补列（幂等）。返回库文件路径。"""
    with closing(connect()) as conn, conn:
        conn.executescript(DDL)
        _migrate(conn)
    return DB_PATH


def _mark_primary(signals):
    """同日多信号的主信号判定。

    v0：优先级规则【尚未定义】，全部标记为主信号（is_primary=1）。
    规则确定后只改本函数，例如「同日同股票只保留优先级最高的一条为 1，其余为 0」。
    返回 {(date, type): 0/1}
    """
    return {(s["date"], s["type"]): 1 for s in signals}


def save_signals(code, signals, is_tradable=1, filter_version=DEFAULT_FILTER_VERSION):
    """写入信号，按 (stock_code, signal_date, signal_type) 去重。

    signals: [{date, type, reason, is_tradable?}, ...]
    返回 (实际新增行数, 因重复被跳过行数)
    """
    init_db()
    now = datetime.now().isoformat(timespec="seconds")
    primary = _mark_primary(signals)
    rows = [(code, s["date"], s["type"], s.get("reason", ""),
             int(s.get("is_tradable", is_tradable)), now,
             s.get("filter_version", filter_version),
             int(primary[(s["date"], s["type"])]),
             f"{code}_{s['date']}") for s in signals]

    with closing(connect()) as conn, conn:
        before = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO signals"
            " (stock_code, signal_date, signal_type, signal_reason, is_tradable, created_at,"
            "  filter_version, is_primary, signal_group_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        after = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]

    added = after - before
    return added, len(rows) - added


def query_signals(code=None, start=None, end=None, primary_only=False):
    """查询信号。参数全部可选，返回 list[dict]。"""
    init_db()
    sql, args = f"SELECT {', '.join(FIELDS)} FROM signals WHERE 1=1", []
    if code:
        sql += " AND stock_code = ?"
        args.append(code)
    if start:
        sql += " AND signal_date >= ?"
        args.append(str(start))
    if end:
        sql += " AND signal_date <= ?"
        args.append(str(end))
    if primary_only:
        sql += " AND is_primary = 1"
    sql += " ORDER BY signal_date, signal_type"
    with closing(connect()) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ---------------- 命令行查看 ----------------

def print_table(rows):
    if not rows:
        print("  (无记录)")
        return
    print(f"  {'id':>4}  {'stock':<8}{'date':<12}{'type':<12}{'prim':<6}{'trad':<6}"
          f"{'group':<18}{'filter_ver':<15}reason")
    for r in rows:
        print(f"  {r['id']:>4}  {r['stock_code']:<8}{r['signal_date']:<12}{r['signal_type']:<12}"
              f"{r['is_primary']:<6}{r['is_tradable']:<6}{r['signal_group_id']:<18}"
              f"{r['filter_version']:<15}{r['signal_reason'][:34]}")


# ---------------- 演示 ----------------

def _600519_signals():
    """与 run_round3 同一口径：Parquet 不复权 + 复权因子 -> 前复权 -> 缠论信号。

    注意：不要另起炉灶直接用 akshare 的 qfq，否则与主流程口径不同（实测差 ~0.37%，会多出信号）。
    """
    from datetime import date, timedelta

    import pandas as pd

    import czsc
    from min_loop import MAX_BI_NUM, MIN_BI_LEN, SYMBOL, build_signals, build_zs
    from storage_kline import load_kline, update_kline

    end = date.today()
    start = end - timedelta(days=365 * 2)
    update_kline(SYMBOL, start, end, verbose=False)
    df = load_kline(SYMBOL)
    for col in ("open", "high", "low", "close"):
        df[col] = df[col] / df["qfq_factor"]
    df["dt"] = pd.to_datetime(df["date"])
    df["symbol"] = SYMBOL
    df = df.rename(columns={"volume": "vol"})
    bars = czsc.format_standard_kline(df, freq=czsc.Freq.D)
    c = czsc.CZSC(bars, min_bi_len=MIN_BI_LEN, max_bi_num=MAX_BI_NUM)
    bis = list(c.bi_list)
    return SYMBOL, build_signals(bis, build_zs(bis))


def demo():
    print(f"=== storage_signal 演示  库文件 {DB_PATH.resolve()} ===")
    init_db()
    print(f"演示前库中已有 {len(query_signals())} 条信号")
    print()

    code, signals = _600519_signals()
    signals = [{"date": s["date"], "type": s["type"], "reason": s["reason"]} for s in signals]
    print(f"--- 1) 从管道取到 {code} 信号 {len(signals)} 条 ---")
    print()

    print("--- 2) 首次写入 ---")
    added, skipped = save_signals(code, signals)
    print(f"    新增 {added} 条，因重复跳过 {skipped} 条；库中合计 {len(query_signals())} 条")
    print()

    print("--- 3) 同年同日的信号组（检查 is_primary / signal_group_id）---")
    groups = {}
    for r in query_signals(code=code):
        groups.setdefault(r["signal_group_id"], []).append(r)
    multi = {g: v for g, v in groups.items() if len(v) > 1}
    if multi:
        for g, v in multi.items():
            print(f"    组 {g}  共 {len(v)} 条:")
            for r in v:
                print(f"      {r['signal_type']:<12} is_primary={r['is_primary']}  {r['signal_reason'][:36]}")
    else:
        print("    无同日多信号")
    print()

    print("--- 4) 重复写入同一批（应 0 新增）---")
    added2, skipped2 = save_signals(code, signals)
    print(f"    新增 {added2} 条，因重复跳过 {skipped2} 条；库中合计 {len(query_signals())} 条")
    print()

    print("--- 5) 库结构 ---")
    with closing(connect()) as conn:
        print("    列:", [r[1] for r in conn.execute("PRAGMA table_info(signals)")])
        print("    索引:", [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='signals'")])


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "list":
        rows = query_signals(*argv[1:4]) if len(argv) > 1 else query_signals()
        print(f"{DB_PATH.resolve()}  共 {len(rows)} 条")
        print_table(rows)
    else:
        demo()
