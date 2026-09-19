"""T5-5 报告归档 + 三日期落库。

归档目录结构
    reports/YYYY-MM-DD/
        report.md     完整报告
        signals.csv   本次涉及的全部信号（含 signal_date / confirm_date / entry_ref_price）
        meta.json     运行统计（时间、股票数、token 消耗、失败数）

SQLite reports 表（与 signals/backtest 同库 data/chan_agent.db）
    id, report_date, stock_code, content, llm_summary,
    signal_type, signal_date, confirm_date, entry_ref_price, created_at
    UNIQUE(report_date, stock_code)

    粒度说明：一只股票一天一行。三日期取该股【最近一条信号】的三日期
    （与报告里「最近一条信号的三日期」一致）；全部信号的完整三日期在 signals.csv 里。

运行
    python storage_report.py --list                 # 列出所有归档日期
    python storage_report.py --date 2026-09-19      # 查某日报告
    python storage_report.py --lagged               # 只查 signal_date != confirm_date 的记录
    python storage_report.py --stock 600519         # 按股票查
"""
import argparse
import csv
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from storage_signal import DB_PATH, connect

OUTPUT_DIR = Path("reports")

DDL = """
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date     TEXT    NOT NULL,
    stock_code      TEXT    NOT NULL,
    content         TEXT    NOT NULL DEFAULT '',
    llm_summary     TEXT    NOT NULL DEFAULT '',
    signal_type     TEXT,
    signal_date     TEXT,
    confirm_date    TEXT,
    entry_ref_price REAL,
    created_at      TEXT    NOT NULL,
    UNIQUE (report_date, stock_code)
);
CREATE INDEX IF NOT EXISTS idx_reports_date ON reports (report_date);
CREATE INDEX IF NOT EXISTS idx_reports_stock ON reports (stock_code);
"""

FIELDS = ["id", "report_date", "stock_code", "content", "llm_summary", "signal_type",
          "signal_date", "confirm_date", "entry_ref_price", "created_at"]

CSV_FIELDS = ["stock_code", "signal_date", "confirm_date", "entry_ref_price", "signal_type",
              "is_tradable", "is_primary", "filter_version", "signal_reason"]


def init_db():
    with closing(connect()) as conn, conn:
        conn.executescript(DDL)
    return DB_PATH


def save_rows(rows):
    """写入 reports 表。rows: [{report_date, stock_code, content, ...}]。返回变更行数。"""
    init_db()
    now = datetime.now().isoformat(timespec="seconds")
    data = [(r["report_date"], r["stock_code"], r.get("content", ""), r.get("llm_summary", ""),
             r.get("signal_type"), r.get("signal_date"), r.get("confirm_date"),
             r.get("entry_ref_price"), r.get("created_at", now)) for r in rows]
    with closing(connect()) as conn, conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR REPLACE INTO reports (report_date, stock_code, content, llm_summary,"
            " signal_type, signal_date, confirm_date, entry_ref_price, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", data)
        return conn.total_changes - before


def query_reports(report_date=None, stock_code=None):
    init_db()
    sql, args = f"SELECT {', '.join(FIELDS)} FROM reports WHERE 1=1", []
    if report_date:
        sql += " AND report_date = ?"
        args.append(str(report_date))
    if stock_code:
        sql += " AND stock_code = ?"
        args.append(str(stock_code))
    sql += " ORDER BY report_date DESC, stock_code"
    with closing(connect()) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def query_lagged(report_date=None):
    """只返回「信号日 != 确认日」的记录（ADR-011 的滞后信号）。

    同时给出滞后天数（自然日差）便于排序；确认日为空（待确认）的不算滞后。
    """
    init_db()
    sql = (f"SELECT {', '.join(FIELDS)},"
           " CAST(julianday(confirm_date) - julianday(signal_date) AS INT) AS lag_days"
           " FROM reports WHERE confirm_date IS NOT NULL AND signal_date IS NOT NULL"
           " AND signal_date <> confirm_date")
    args = []
    if report_date:
        sql += " AND report_date = ?"
        args.append(str(report_date))
    sql += " ORDER BY report_date DESC, lag_days DESC, stock_code"
    with closing(connect()) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def list_report_dates():
    init_db()
    with closing(connect()) as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT report_date FROM reports ORDER BY report_date DESC")]


# ==================== 归档 ====================

def write_signals_csv(path, by_code):
    """把信号写成 CSV。用 utf-8-sig 便于 Excel 直接打开（中文不乱码）。"""
    n = 0
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for code in sorted(by_code):
            for s in by_code[code]:
                w.writerow({"stock_code": code, **{k: s.get(k) for k in CSV_FIELDS}})
                n += 1
    return n


def archive_report(report_date, md, stats):
    """写 reports/YYYY-MM-DD/{report.md,signals.csv,meta.json} 并写 reports 表。

    返回归档目录 Path。
    """
    d = OUTPUT_DIR / report_date
    d.mkdir(parents=True, exist_ok=True)

    (d / "report.md").write_text(md, encoding="utf-8")
    n_csv = write_signals_csv(d / "signals.csv", stats.get("by_code") or {})

    by_code = stats.get("by_code") or {}
    llm_by_code = stats.get("llm_by_code") or {}
    sections = stats.get("sections") or {}

    meta = {
        "report_date": report_date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_seconds": stats.get("seconds"),
        "stocks": stats.get("codes") or sorted(by_code),
        "stock_count": len(stats.get("codes") or by_code),
        "signal_count": sum(len(v) for v in by_code.values()),
        "tradable_count": sum(1 for v in by_code.values() for s in v if s["is_tradable"]),
        "signals_csv_rows": n_csv,
        "llm": {"model": stats.get("model"), "called": stats.get("called", 0),
                "skipped": stats.get("skipped", 0), "failed": stats.get("failed", 0),
                "cached": stats.get("cached", 0), "tokens": stats.get("tokens", 0),
                "cost": round(stats.get("cost", 0.0), 6),
                "skip_detail": dict(stats.get("skip_detail") or {})},
        "files": {"report": "report.md", "signals": "signals.csv", "meta": "meta.json"},
    }
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = []
    for code in sorted(set(by_code) | set(sections)):
        sigs = by_code.get(code) or []
        last = sigs[-1] if sigs else None
        rows.append({
            "report_date": report_date, "stock_code": code,
            "content": sections.get(code, ""),
            "llm_summary": (llm_by_code.get(code) or {}).get("text") or "",
            "signal_type": last["signal_type"] if last else None,
            "signal_date": last["signal_date"] if last else None,
            "confirm_date": last["confirm_date"] if last else None,
            "entry_ref_price": last["entry_ref_price"] if last else None,
        })
    save_rows(rows)
    return d


# ==================== 命令行查询 ====================

def _show(rows, cols=None):
    if not rows:
        print("  (无记录)")
        return
    cols = cols or ["report_date", "stock_code", "signal_type", "signal_date",
                    "confirm_date", "entry_ref_price"]
    print("  " + "  ".join(f"{c}" for c in cols))
    for r in rows:
        print("  " + "  ".join(str(r.get(c)) for c in cols))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--date", default=None)
    ap.add_argument("--stock", default=None)
    ap.add_argument("--lagged", action="store_true")
    ap.add_argument("--show-summary", action="store_true", help="连同 llm_summary 一起打印")
    a = ap.parse_args()

    init_db()
    if a.list:
        ds = list_report_dates()
        print(f"=== 归档日期（{len(ds)} 天，目录 {OUTPUT_DIR.resolve()}）===")
        for d in ds:
            rows = query_reports(report_date=d)
            n_lag = len(query_lagged(report_date=d))
            print(f"  {d}   股票 {len(rows)} 只   滞后信号记录 {n_lag} 条   "
                  f"目录 {OUTPUT_DIR / d}")
        return

    if a.lagged:
        rows = query_lagged(a.date)
        print(f"=== 滞后信号（signal_date != confirm_date）共 {len(rows)} 条 ===")
        _show(rows, ["report_date", "stock_code", "signal_type", "signal_date",
                     "confirm_date", "lag_days", "entry_ref_price"])
        return

    rows = query_reports(a.date, a.stock)
    print(f"=== 报告记录 共 {len(rows)} 条 ===")
    _show(rows)
    if a.show_summary:
        for r in rows:
            print()
            print(f"--- {r['report_date']} {r['stock_code']} ---")
            print(f"LLM 总结：{(r['llm_summary'] or '(无)')[:200]}")
            print(f"报告正文长度：{len(r['content'])} 字符")


if __name__ == "__main__":
    main()
