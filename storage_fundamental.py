"""基本面缓存（SQLite，见 ADR-017）。

与信号/回测共用 data/chan_agent.db，但**单独两张表**，不动既有表结构：
    profiles       code 主键，公司概况 / 行业 / 上市日期
    fundamentals   (code, report_period) 主键，每期财务摘要一行

注意：sqlite3 的 `with conn:` 只是**事务**上下文，不会关连接。这里统一用
`with _db() as c:`（提交 + 关闭），否则连接会泄漏，Windows 下还会把库文件锁住。
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path("data/chan_agent.db")

FIN_COLS = ["net_profit", "net_profit_yoy", "revenue", "revenue_yoy", "eps", "bps",
            "roe", "gross_margin", "net_margin", "debt_ratio"]

_DDL = [
    """CREATE TABLE IF NOT EXISTS profiles (
        code TEXT PRIMARY KEY, name TEXT, market TEXT, industry TEXT,
        industry_cninfo TEXT, listing_date TEXT, main_business TEXT,
        updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS fundamentals (
        code TEXT NOT NULL, report_period TEXT NOT NULL,
        net_profit REAL, net_profit_yoy REAL, revenue REAL, revenue_yoy REAL,
        eps REAL, bps REAL, roe REAL, gross_margin REAL, net_margin REAL,
        debt_ratio REAL, updated_at TEXT,
        PRIMARY KEY (code, report_period))""",
]


@contextmanager
def _db():
    """提交并**关闭**连接。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with _db() as c:
        for ddl in _DDL:
            c.execute(ddl)


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_stale(updated_at, ttl_days):
    """缓存是否过期。updated_at 缺失或解析不了都算过期（宁可多拉一次）。"""
    if not updated_at:
        return True
    try:
        t = datetime.strptime(str(updated_at)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return datetime.now() - t > timedelta(days=ttl_days)


def save_profile(p):
    init_db()
    with _db() as c:
        c.execute(
            "INSERT INTO profiles (code,name,market,industry,industry_cninfo,"
            "listing_date,main_business,updated_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, market=excluded.market,"
            " industry=excluded.industry, industry_cninfo=excluded.industry_cninfo,"
            " listing_date=excluded.listing_date, main_business=excluded.main_business,"
            " updated_at=excluded.updated_at",
            (p.get("code"), p.get("name"), p.get("market"), p.get("industry"),
             p.get("industry_cninfo"), p.get("listing_date"), p.get("main_business"),
             now_str()))
    return p


def load_profile(code):
    init_db()
    with _db() as c:
        row = c.execute("SELECT * FROM profiles WHERE code=?", (code,)).fetchone()
    return dict(row) if row else None


def save_financials(code, rows):
    """rows 是 fundamentals.fetch_financials 的返回值，按报告期覆盖写。"""
    if not rows:
        return 0
    init_db()
    cols = ["code", "report_period"] + FIN_COLS + ["updated_at"]
    sql = "INSERT INTO fundamentals (%s) VALUES (%s) ON CONFLICT(code,report_period) " % (
        ",".join(cols), ",".join("?" * len(cols)))
    sql += "DO UPDATE SET " + ",".join("%s=excluded.%s" % (k, k) for k in FIN_COLS + ["updated_at"])
    ts = now_str()
    with _db() as c:
        for r in rows:
            c.execute(sql, [code, r.get("report_period")] + [r.get(k) for k in FIN_COLS] + [ts])
    return len(rows)


def load_financials(code, limit=8):
    """最近 limit 期，**报告期倒序**（最近的在前）。"""
    init_db()
    with _db() as c:
        cur = c.execute(
            "SELECT * FROM fundamentals WHERE code=? ORDER BY report_period DESC LIMIT ?",
            (code, limit))
        return [dict(r) for r in cur.fetchall()]
