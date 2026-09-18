"""T3-3 用 SQLite 存缠论信号。

库路径  data/chan_agent.db
表      signals
字段    id, stock_code, signal_date, signal_type, signal_reason, is_tradable, created_at
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
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code    TEXT    NOT NULL,
    signal_date   TEXT    NOT NULL,
    signal_type   TEXT    NOT NULL,
    signal_reason TEXT    NOT NULL DEFAULT '',
    is_tradable   INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL,
    UNIQUE (stock_code, signal_date, signal_type)
);
CREATE INDEX IF NOT EXISTS idx_signals_code_date ON signals (stock_code, signal_date);
"""

FIELDS = ["id", "stock_code", "signal_date", "signal_type", "signal_reason", "is_tradable", "created_at"]


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """建库建表（幂等）。返回库文件路径。"""
    with closing(connect()) as conn, conn:
        conn.executescript(DDL)
    return DB_PATH


def save_signals(code, signals, is_tradable=1):
    """写入信号，按 (stock_code, signal_date, signal_type) 去重。

    signals: [{date, type, reason, is_tradable?}, ...]
    返回 (实际新增行数, 因重复被跳过行数)
    """
    init_db()
    now = datetime.now().isoformat(timespec="seconds")
    rows = [(code, s["date"], s["type"], s.get("reason", ""),
             int(s.get("is_tradable", is_tradable)), now) for s in signals]

    with closing(connect()) as conn, conn:
        before = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO signals"
            " (stock_code, signal_date, signal_type, signal_reason, is_tradable, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)", rows)
        after = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]

    added = after - before
    return added, len(rows) - added


def query_signals(code=None, start=None, end=None):
    """查询信号。参数全部可选。返回 list[dict]。"""
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
    sql += " ORDER BY signal_date, signal_type"
    with closing(connect()) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ---------------- 命令行查看 ----------------

def print_table(rows):
    if not rows:
        print("  (无记录)")
        return
    print(f"  {'id':>4}  {'stock':<8}{'date':<12}{'type':<12}{'tradable':<9}reason")
    for r in rows:
        print(f"  {r['id']:>4}  {r['stock_code']:<8}{r['signal_date']:<12}{r['signal_type']:<12}"
              f"{r['is_tradable']:<9}{r['signal_reason'][:44]}")


# ---------------- 演示 ----------------

def _600519_signals():
    """复用 T2-3 管道产出 600519 的统一格式信号。"""
    from datetime import date, timedelta

    import akshare as ak
    import pandas as pd

    import czsc
    from min_loop import MAX_BI_NUM, MIN_BI_LEN, SYMBOL, TS_SYMBOL, build_signals, build_zs

    end = date.today()
    start = end - timedelta(days=365 * 2)
    df = ak.stock_zh_a_hist_tx(symbol=TS_SYMBOL, start_date=start.strftime("%Y%m%d"),
                               end_date=end.strftime("%Y%m%d"), adjust="qfq")
    df = df.rename(columns={"volume": "vol"})
    df["dt"] = pd.to_datetime(df["date"])
    df["symbol"] = SYMBOL
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
    for s in signals[:3]:
        print(f"    {s}")
    print()

    print("--- 2) 首次写入 ---")
    added, skipped = save_signals(code, signals)
    print(f"    新增 {added} 条，因重复跳过 {skipped} 条；库中合计 {len(query_signals())} 条")
    print()

    print("--- 3) 查询（全部 / 按股票 / 按区间）---")
    print(f"    全部        : {len(query_signals())} 条")
    print(f"    code=600519 : {len(query_signals(code='600519'))} 条")
    rng = query_signals(code="600519", start="2026-01-01", end="2026-12-31")
    print(f"    2026 年内   : {len(rng)} 条")
    print()
    print_table(query_signals(code="600519")[:5])
    print()

    print("--- 4) 重复写入同一批（应 0 新增）---")
    added2, skipped2 = save_signals(code, signals)
    print(f"    新增 {added2} 条，因重复跳过 {skipped2} 条；库中合计 {len(query_signals())} 条")
    print()

    print("--- 5) 库结构 ---")
    with closing(connect()) as conn:
        for r in conn.execute("SELECT sql FROM sqlite_master WHERE name='signals'"):
            print("   ", r[0].replace("\n", "\n    "))
        print("    索引:", [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='signals'")])


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "list":
        rows = query_signals(*[a for a in argv[1:4]] if len(argv) > 1 else [])
        if len(argv) == 1:
            rows = query_signals()
        print(f"{DB_PATH.resolve()}  共 {len(rows)} 条")
        print_table(rows)
    else:
        demo()
