"""market_scan.py —— 全市场买点扫描器（T18 / ADR-022）。

### 它做什么

扫全市场（沪深主板 / 创业板 / 科创板，排除北交所与 ST），**只挑买点**，
把「最近若干交易日已确认的买点」筛出来存进**独立的表**，供页面展示。

点击某只股票 → 跳转到单股分析页（页面侧负责）。

### 为什么复用主流水线是浪费

| 节点 | 单只成本 | 全市场 5000 只 | 扫描器要不要 |
| --- | --- | --- | --- |
| chan（算结构 + 信号） | ~0.16s | ~13 分钟 | **只要这个** |
| filter（build_context，联网） | 2.26s | ~3 小时 | 不要 |
| backtest（confirm_entry 前缀重算） | 9.40s | ~13 小时 | 不要 |
| report / 基本面 / LLM | — | 合计 ~5 小时 | 不要 |

而且全市场 5000 只的信号如果灌进 " + BT + "signals" + BT + " 表，会把辛苦建立的 4184 条统计样本污染掉。
所以扫描器**走独立路径、写独立的表**（" + BT + "scan_results" + BT + " / " + BT + "scan_state" + BT + "）。

### 两道零成本的质量门槛

1. **排除 ST / 退市 / 北交所** —— 从全市场名录按名字与代码前缀筛，不用联网
2. **流动性下限** —— 用本地 K 线的成交额，不用联网

### 输出只放「顺手就有」的字段

代码 / 名称 / 买点类型 / 信号日 / 确认日 / 距中枢结束天数。
**不算回测、不抓基本面、不调 LLM** —— 那些才是真正贵的。

### 为什么窗口用「确认日」而不是「信号日」

ADR-011：缠论的笔要 1~2 个交易日才能确认。按信号日筛窗口，会把**还没确认**的信号
也放进来 —— 用户今天看到它，明天它可能就不成立了。实测：已确认的信号 100% 稳定，
没确认的会变。所以窗口按确认日算，**未确认的不入选**。

### 用法

    python market_scan.py --limit 30            # 先扫 30 只看效果
    python market_scan.py                       # 全市场（耗时较长，可中断后 --resume）
    python market_scan.py --no-resume           # 无视已完成，重扫
    python market_scan.py --codes 600519,000001
    python market_scan.py --window 3 --min-amount 1.0
    python market_scan.py --report              # 只看已扫结果
"""
import argparse
import sqlite3
import sys
import time
from contextlib import closing
from datetime import date
from pathlib import Path

import pandas as pd

import signal_score as sc
import structure_gap as sg
from batch_quote import merge_last_bars
from confirm_dates import confirm_entry, load_frames
from min_loop import build_signals
from storage_kline import load_kline
from storage_signal import DB_PATH

# 历史深度：扫描器只需要算「当前结构」，不需要 11 年。
# 实测 2 年（约 490 根）足够，且首次全量拉取快得多。
DEFAULT_YEARS = 2
DEFAULT_WINDOW = 5        # 最近几个**已确认**的交易日
DEFAULT_MIN_AMOUNT = 0.5  # 近 20 日日均成交额下限（亿元）
DEFAULT_SLEEP = 0.2       # 每次拉取之间的间隔（限速）
MIN_BARS = 150            # K 线少于这个数直接跳过（算不出像样的结构）

OK_PREFIX = ("60", "68", "00", "30")

SCAN_DDL = """
CREATE TABLE IF NOT EXISTS scan_results (
    scan_date    TEXT NOT NULL,
    stock_code   TEXT NOT NULL,
    stock_name   TEXT,
    signal_date  TEXT NOT NULL,
    confirm_date TEXT NOT NULL,
    signal_type  TEXT NOT NULL,
    is_primary   INTEGER DEFAULT 1,
    zs_gap_days  INTEGER,
    zs_width_pct REAL,
    score        INTEGER,
    amount_yi    REAL,
    scanned_at   TEXT,
    PRIMARY KEY (scan_date, stock_code, signal_date, signal_type)
);
CREATE INDEX IF NOT EXISTS idx_scan_date ON scan_results (scan_date, signal_type);

CREATE TABLE IF NOT EXISTS scan_state (
    scan_date  TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    status     TEXT NOT NULL,
    n_hits     INTEGER DEFAULT 0,
    note       TEXT,
    scanned_at TEXT,
    PRIMARY KEY (scan_date, stock_code)
);
"""


def connect(db_path=None):
    p = Path(db_path) if db_path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    return conn


# 后加的列（scan_results 可能已存在旧版本）
SCAN_NEW_COLUMNS = [("zs_width_pct", "REAL"), ("score", "INTEGER"),
                    ("industry", "TEXT")]


def init_db(db_path=None):
    with closing(connect(db_path)) as conn, conn:
        conn.executescript(SCAN_DDL)
        have = {r[1] for r in conn.execute("PRAGMA table_info(scan_results)")}
        for name, ddl in SCAN_NEW_COLUMNS:
            if name not in have:
                conn.execute("ALTER TABLE scan_results ADD COLUMN %s %s" % (name, ddl))


def ok_code(code, name):
    """是不是可扫标的。抽成纯函数是为了能离线测。

    用**白名单**前缀（60/68/00/30）—— 踩过坑：黑名单（排除 4/8）会漏掉北交所的 92 开头。
    """
    c = str(code or "")
    if not (len(c) == 6 and c.isdigit()):
        return False
    if not c.startswith(OK_PREFIX):
        return False
    n = str(name or "").replace(" ", "").upper()
    return "ST" not in n and "退" not in n


def universe(limit=None):
    """全市场可扫标的：排除北交所 / ST / 退市。返回 [(code, name), ...]

    用**白名单**前缀（60/68/00/30）而不是黑名单 —— 踩过坑：北交所现在也用 92 开头。
    """
    import akshare as ak
    df = ak.stock_info_a_code_name()
    df.columns = ["code", "name"]
    df["code"] = df["code"].astype(str).str.zfill(6)
    out = []
    for _, r in df.iterrows():
        code, name = r["code"], str(r["name"]).replace(" ", "")
        if ok_code(code, name):
            out.append((code, name))
    out.sort()
    return out[:limit] if limit else out


def last_trading_day(today=None):
    """交易日历里 <= today 的最后一个交易日。"""
    from signal_filter import trading_calendar
    today = pd.Timestamp(today or date.today())
    prev = [d for d in trading_calendar() if d <= today]
    return prev[-1].date() if prev else None


def data_end_date(probe=None):
    """本地数据**实际**到哪一天。

    不能直接用 last_trading_day()：如果今天的数据还没发布（盘中、或早盘跑），
    日历会说今天是交易日，但本地根本没有今天的 K 线 —— 那样扫描日会标成一个
    「还没有数据的日子」，窗口也会悄悄往前错。**实测踩到过**（标成 09-21，数据只到 09-18）。

    取一批**已按日更新**的股票（自选股池）里最靠后的那个日期。
    """
    import watchlist_store as ws
    cands = list(probe or []) + list(ws.load_watchlist())
    best = None
    for c in cands:
        try:
            df = load_kline(c)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        d = df["date"].iloc[-1].date()
        if best is None or d > best:
            best = d
    return best


def scan_stock(code, name, scan_date, window=DEFAULT_WINDOW,
               min_amount=DEFAULT_MIN_AMOUNT, years=DEFAULT_YEARS):
    """扫一只股票。返回 (hits, status, note)。status ∈ ok / skip / fail。"""
    from analyzer import ensure_kline

    try:
        src, err = ensure_kline(code, str(scan_date), allow_fetch=True,
                                years=years, verbose=False)
    except Exception as e:
        return [], "fail", "拉取异常 %s" % type(e).__name__
    if src is None:
        return [], "fail", err or "无数据"

    raw, q = load_frames(code)
    if q.empty or len(q) < MIN_BARS:
        return [], "skip", "K 线不足（%d 根）" % len(q)

    pos = {str(d.date()): i for i, d in enumerate(q["date"])}
    if str(scan_date) not in pos:
        scan_date = str(q["date"].iloc[-1].date())

    bis, zss = sg.structure_at(code, q, len(q) - 1)
    recs = [r for r in build_signals(bis, zss) if "买点" in r["type"]]
    if not recs:
        return [], "ok", "无买点"

    n = len(q)
    i_cut = n - (window + 6)          # 多留 6 个交易日给确认延迟
    cand = [r for r in recs if pos.get(r["date"], -1) >= i_cut]
    if not cand:
        return [], "ok", "窗口内无候选"

    try:
        ce = confirm_entry(code, [{"signal_date": r["date"], "signal_type": r["type"]}
                                  for r in cand])
    except Exception as e:
        return [], "fail", "确认日异常 %s" % type(e).__name__

    i_lo = max(0, n - window)
    picked = []
    for r in cand:
        cd, _price, _note = ce.get((r["date"], r["type"]), (None, None, ""))
        if not cd:
            continue                  # 还没确认 -> 不入选
        icd = pos.get(cd)
        if icd is None or icd < i_lo or icd > n - 1:
            continue
        picked.append((r, cd))
    if not picked:
        return [], "ok", "窗口内无已确认买点"

    amt = float(q["amount"].iloc[max(0, n - 20):n].mean()) / 1e8
    if min_amount and amt < min_amount:
        return [], "skip", "日均成交额 %.2f 亿 < %.2f" % (amt, min_amount)

    # 一次算出 gap 与 width（两者都依赖确认日结构），再合成打分
    gw = sg.signal_gap_width(code, [{"signal_date": r["date"], "signal_type": r["type"],
                                     "confirm_date": cd} for r, cd in picked])
    hits = []
    for r, cd in picked:
        g = gw.get((r["date"], r["type"])) or {}
        gap, wid = g.get("gap"), g.get("width")
        hits.append({
            "stock_code": code, "stock_name": name,
            "signal_date": r["date"], "confirm_date": cd,
            "signal_type": r["type"], "is_primary": 1,
            "zs_gap_days": gap, "zs_width_pct": wid,
            "score": sc.score_of(wid, gap),
            "amount_yi": round(amt, 2),
        })
    return hits, "ok", "%d 条" % len(hits)


def save_hits(scan_date, hits, db_path=None):
    if not hits:
        return 0
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    import industry as im
    m = im.load_map()                      # 没有行业数据时是空 dict，写成 None 即可
    rows = [(scan_date, h["stock_code"], h["stock_name"], h["signal_date"],
             h["confirm_date"], h["signal_type"], h["is_primary"],
             h.get("zs_gap_days"), h.get("zs_width_pct"), h.get("score"),
             h["amount_yi"], im.level_of(h["stock_code"], "次类", m) or None, ts)
             for h in hits]
    with closing(connect(db_path)) as conn, conn:
        conn.executemany(
            "INSERT OR REPLACE INTO scan_results (scan_date, stock_code, stock_name,"
            " signal_date, confirm_date, signal_type, is_primary, zs_gap_days,"
            " zs_width_pct, score, amount_yi, industry, scanned_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
    return len(rows)


def concentration_rows(scan_date, level="次类", rows=None, min_hits=4, db_path=None):
    """命中池 vs 基准的行业集中度。返回按 lift 降序的 list（可能为空）。

    **基准取 `scan_state` 里实际扫过的代码**，不去重新拉全市场名单 ——
    这样既不用联网，分母也正好是「这次扫描的总体」，比名录更对。
    """
    import industry as im
    m = im.load_map()
    if not m:
        return []
    if rows is None:
        rows, scan_date = query_scan(scan_date, db_path=db_path)
    base = sorted(done_codes(scan_date, db_path)) if scan_date else []
    if not base:
        return []
    hits = sorted({r["stock_code"] for r in rows})
    return im.concentration(hits, base, level=level, mapping=m, min_hits=min_hits)


def report_concentration(rows, scan_date=None, level="次类", top=10):
    """命中池的行业集中度 vs 全市场基准。

    只看命中数会被大行业天然占优误导（医药生物本来就有 230 只），
    所以要比的是 `lift = 命中占比 / 全市场占比`。lift 接近 1 = 池子在行业上是均匀的。
    """
    res = concentration_rows(scan_date, level=level, rows=rows)
    if not res:
        print("  （算不出行业集中度：缺行业映射，或这批扫描没有基准）")
        return []
    hits = sorted({r["stock_code"] for r in rows})
    uni = sorted(done_codes(scan_date)) if scan_date else []
    print()
    print("  行业集中度（%s 层级，命中 %d 只 / 基准 %d 只）" % (level, len(hits), len(uni)))
    print("  %-20s %5s %6s %8s %8s %7s" % ("行业", "命中", "基准", "命中占比", "基准占比", "lift"))
    for x in res[:top]:
        print("  %-20s %5d %6d %7.1f%% %7.1f%% %6.2fx"
              % (x["industry"], x["hits"], x["base"], 100 * x["hits_share"],
                 100 * x["base_share"], x["lift"]))
    print("  lift = 命中占比 / 基准占比；≈1 表示这个行业在池子里的权重和它在扫描总体里一样")
    return res


def backfill_industry(db_path=None):
    """给已有的命中行补行业。行业映射是**全市场**的，所以历史扫描日也能补。"""
    import industry as im
    m = im.load_map()
    if not m:
        return 0
    init_db(db_path)                      # 先跑迁移，否则老库还没有 industry 列
    with closing(connect(db_path)) as conn, conn:
        rows = conn.execute(
            "SELECT rowid, stock_code FROM scan_results"
            " WHERE industry IS NULL OR industry=''").fetchall()
        upd = [(im.level_of(r[1], "次类", m) or None, r[0]) for r in rows]
        if upd:
            conn.executemany(
                "UPDATE scan_results SET industry=? WHERE rowid=?", upd)
    return len(upd)


def save_state(scan_date, code, status, n_hits=0, note="", db_path=None):
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO scan_state (scan_date, stock_code, status, n_hits,"
            " note, scanned_at) VALUES (?,?,?,?,?,?)",
            (scan_date, code, status, n_hits, note, time.strftime("%Y-%m-%d %H:%M:%S")))


def done_codes(scan_date, db_path=None):
    """已算完成的代码 —— **失败的不算完成**。

    原来 status 不分好坏，fail 也当成「已完成」。于是一次网络抖动（全市场那次实测
    243 只 DNS 解析失败）就**永久**漏掉那些股票：重跑时被直接跳过，没人会知道。
    判据改成只有非 fail 才算完成，失败的下次会被重新捡起来。
    """
    with closing(connect(db_path)) as conn:
        return {r[0] for r in conn.execute(
            "SELECT stock_code FROM scan_state WHERE scan_date=? AND status<>'fail'",
            (scan_date,))}


def scan_market(scan_date=None, codes=None, limit=None, window=DEFAULT_WINDOW,
                min_amount=DEFAULT_MIN_AMOUNT, years=DEFAULT_YEARS,
                sleep=DEFAULT_SLEEP, resume=True, db_path=None, verbose=True,
                batch=True):
    """批量扫描。每只扫完立刻落库 —— 中断了可以接着跑（默认跳过已完成的）。"""
    init_db(db_path)
    # 扫描日 = **本地数据实际截止日**，不是日历上的今天（见 data_end_date 的注释）
    sd = str(scan_date or data_end_date() or last_trading_day())
    uni = universe()
    if codes:
        want = set(codes)
        uni = [(c, n) for c, n in uni if c in want]
    elif limit:
        uni = uni[:limit]

    skip = done_codes(sd, db_path) if resume else set()
    todo = [(c, n) for c, n in uni if c not in skip]

    print("=== 全市场买点扫描  扫描日 %s ===" % sd)
    print("  候选 %d 只（全市场名录 - 北交所 - ST）；已完成 %d 只；本次处理 %d 只"
          % (len(uni), len(skip), len(todo)))
    print("  窗口 = 最近 %d 个**已确认**交易日   流动性下限 = %.2f 亿   历史深度 = %d 年"
          % (window, min_amount, years))
    print()

    # —— 阶段 1：批量补最后一根日线（ADR-028）——
    # 日更时每只只需要那一根新 K 线。逐只发请求 = 5020 次；腾讯支持一次查 50 只 -> 约 100 次。
    # 只在收盘后生效（盘中拿到的是未完成 bar）；缺更多天/除权除息的股票会自动交给逐只更新。
    if batch and todo:
        print("=== 批量补当日 K 线 ===")
        merge_last_bars([c for c, _ in todo], verbose=verbose)
        print()

    t0 = time.time()
    stats, n_hits = {"ok": 0, "skip": 0, "fail": 0}, 0
    for i, (code, name) in enumerate(todo, 1):
        try:
            hits, status, note = scan_stock(code, name, sd, window=window,
                                            min_amount=min_amount, years=years)
        except Exception as e:
            hits, status, note = [], "fail", "%s: %s" % (type(e).__name__, str(e)[:60])
        stats[status] = stats.get(status, 0) + 1
        if hits:
            save_hits(sd, hits, db_path)
            n_hits += len(hits)
        save_state(sd, code, status, len(hits), note, db_path)
        if verbose and (i % 20 == 0 or i == len(todo)):
            print("  [%d/%d] 命中 %d 条  成功 %d 跳过 %d 失败 %d  用时 %.0fs"
                  % (i, len(todo), n_hits, stats["ok"], stats["skip"], stats["fail"],
                     time.time() - t0), flush=True)
        if sleep:
            time.sleep(sleep)

    print()
    print("=== 完成  用时 %.0fs ===" % (time.time() - t0))
    print("  命中 %d 条   成功 %d 只 / 跳过 %d 只 / 失败 %d 只"
          % (n_hits, stats["ok"], stats["skip"], stats["fail"]))
    return n_hits


def query_scan(scan_date=None, db_path=None, exclude_far=False, only_type=None,
               limit=None):
    """查扫描结果。默认取最新一次扫描日。返回 (rows, scan_date)。"""
    init_db(db_path)
    with closing(connect(db_path)) as conn:
        if scan_date is None:
            r = conn.execute("SELECT MAX(scan_date) FROM scan_results").fetchone()
            scan_date = r[0] if r and r[0] else None
        if scan_date is None:
            return [], None
        # 默认按打分从高到低排 —— 这是「排」不是「筛」，和筛选条件叠加使用
        rows = [dict(x) for x in conn.execute(
            "SELECT * FROM scan_results WHERE scan_date=?"
            " ORDER BY COALESCE(score,-1) DESC, signal_type, stock_code", (scan_date,))]
    if exclude_far:
        rows = [r for r in rows if r["zs_gap_days"] is None
                or r["zs_gap_days"] < sg.GAP_THRESHOLD]
    if only_type:
        rows = [r for r in rows if r["signal_type"] == only_type]
    return (rows[:limit] if limit else rows), scan_date


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-date", default=None)
    ap.add_argument("--codes", default=None, help="逗号分隔，只扫指定代码")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--min-amount", type=float, default=DEFAULT_MIN_AMOUNT)
    ap.add_argument("--years", type=int, default=DEFAULT_YEARS)
    ap.add_argument("--sleep", type=float, default=DEFAULT_SLEEP)
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--no-batch", action="store_true",
                    help="不用批量行情补当日 K 线（默认用；只在收盘后生效）")
    ap.add_argument("--report", action="store_true", help="只看已扫结果")
    ap.add_argument("--backfill-industry", action="store_true",
                    help="给已有的命中行补行业（从全市场行业映射）")
    a = ap.parse_args()

    if a.backfill_industry:
        print("已补 %d 行行业" % backfill_industry())
        return 0

    if a.report:
        rows, sd = query_scan(a.scan_date)
        if not rows:
            print("还没有扫描结果")
            return 0
        print("=== 扫描日 %s  共 %d 条买点 ===" % (sd, len(rows)))
        by = {}
        for r in rows:
            by[r["signal_type"]] = by.get(r["signal_type"], 0) + 1
        print("  按类型:", by)
        far = sum(1 for r in rows if (r["zs_gap_days"] or -1) >= sg.GAP_THRESHOLD)
        print("  距中枢 >= %d 日（H3 提示）: %d 条" % (sg.GAP_THRESHOLD, far))
        report_concentration(rows, sd)
        print()
        print("  %-8s %-8s %-11s %-12s %-12s %s"
              % ("代码", "名称", "类型", "信号日", "确认日", "距中枢"))
        for r in rows[:40]:
            print("  %-8s %-8s %-11s %-12s %-12s %s"
                  % (r["stock_code"], (r["stock_name"] or "")[:6], r["signal_type"],
                     r["signal_date"], r["confirm_date"], sg.label_of(r["zs_gap_days"])))
        if len(rows) > 40:
            print("  …（还有 %d 条）" % (len(rows) - 40))
        return 0

    codes = [c.strip() for c in a.codes.split(",")] if a.codes else None
    scan_market(scan_date=a.scan_date, codes=codes, limit=a.limit, window=a.window,
                min_amount=a.min_amount, years=a.years, sleep=a.sleep,
                resume=not a.no_resume, batch=not a.no_batch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
