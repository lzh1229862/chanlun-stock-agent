"""run_queue.py —— 长任务队列（B9 / ADR-036）。

### 为什么需要

项目里有几件**跑一次要几十分钟**的事：

| 任务 | 首次耗时 |
| --- | --- |
| 全市场买点扫描（5020 只） | 约 4.5 小时（要拉 5000 只的 2 年历史） |
| 退市股取数（252 只） | 约 50 分钟 |
| 退市股信号重算（252 只） | 约 35 分钟 |
| 幸存者偏差对比 | 几秒（只读缓存） |
| 每日批处理 | 约 12 分钟 |

**它们在 agent 工具调用里跑不了** —— 工具单次有 10 分钟上限，而进程也挂不住后台。
但在**你自己的终端里没有这个限制**。

所以这个队列的作用是：**一条命令，把该跑的都跑完，可中断、可续跑、有进度**。

### 用法

    python run_queue.py              # 依次跑完所有未完成的任务
    python run_queue.py --status     # 只看进度，不跑
    python run_queue.py --only scan  # 只跑一个
    python run_queue.py --force scan # 强制重跑某个（无视已完成）
    python run_queue.py --list       # 列出任务

中断（Ctrl+C）随时可以，重跑会自动跳过已完成的 —— 每个任务背后都有各自的续跑机制：
    scan            -> scan_state 表记录已扫过的股票
    delist_fetch    -> data/delisted/*.parquet 文件是否存在
    delist_compute  -> data/survivorship_cache.json 里的条目
"""
import argparse
import json
import sys
import time
import traceback
from datetime import date
from pathlib import Path

STATE = Path("data/queue_state.json")
CACHE = Path("data/survivorship_cache.json")
SURV_RESULT = Path("config/survivorship_result.json")


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- 各任务的 进度 / 执行 ----------------

def _scan_date():
    import market_scan as ms
    return str(ms.data_end_date() or ms.last_trading_day())


def scan_progress():
    import market_scan as ms
    try:
        sd = _scan_date()
        return len(ms.done_codes(sd)), len(ms.universe())
    except Exception:
        return 0, 1


def scan_run():
    import market_scan as ms
    ms.scan_market(sleep=0.0, batch=True)


def delist_fetch_progress():
    import delisted_pool as dp
    pool = dp.load_pool()
    have = sum(1 for s in pool if not dp.load_delisted(s["code"]).empty)
    return have, len(pool)


def delist_fetch_run():
    import delisted_pool as dp
    dp.fetch_batch(limit=10 ** 9)


def delist_compute_progress():
    import delisted_pool as dp
    if not CACHE.exists():
        return 0, len(dp.load_pool())
    try:
        c = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return 0, len(dp.load_pool())
    return len([k for k in c if k.startswith("del|")]), len(dp.load_pool())


def delist_compute_run():
    import verify_survivorship as vs
    vs._save_cache(vs._load_cache())
    from delisted_pool import load_pool
    vs.run_delisted(load_pool(), verbose=True, limit=None)


def surv_progress():
    """对比的「完成」= 结果比缓存新 **且** 上游的重算已经跑满。

    少了后面那个条件，队列会在 delist_compute 只算了一部分时就认为对比已完成、
    然后跳过它 —— 那样最终留下的是部分样本的结果。
    """
    d, n = delist_compute_progress()
    upstream_done = n > 0 and d >= n
    fresh = (SURV_RESULT.exists() and CACHE.exists()
             and SURV_RESULT.stat().st_mtime >= CACHE.stat().st_mtime)
    return (1 if (fresh and upstream_done) else 0), 1


def surv_run():
    import subprocess
    r = subprocess.run([sys.executable, "verify_survivorship.py", "--limit", "0"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r.stdout[-2000:] if r.stdout else "")
    if r.returncode != 0:
        raise RuntimeError("verify_survivorship 返回 %d：%s" % (r.returncode, r.stderr[-300:]))


def daily_progress():
    """今天的日报出来了就算完成（不是「过了 18 点」）。"""
    return (1 if (Path("reports") / date.today().isoformat() / "report.md").exists() else 0), 1


def daily_run():
    import subprocess
    r = subprocess.run([sys.executable, "main.py", "--date", date.today().isoformat(),
                        "--no-llm"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    print((r.stdout or "")[-2000:])
    if r.returncode != 0:
        raise RuntimeError("main.py 返回 %d" % r.returncode)


TASKS = [
    {"id": "delist_fetch", "name": "退市股取数（252 只，约 50 分钟）",
     "progress": delist_fetch_progress, "run": delist_fetch_run},
    {"id": "delist_compute", "name": "退市股信号重算（252 只，约 35 分钟）",
     "progress": delist_compute_progress, "run": delist_compute_run},
    {"id": "survivorship", "name": "幸存者偏差对比（读缓存，几秒）",
     "progress": surv_progress, "run": surv_run},
    {"id": "scan", "name": "全市场买点扫描（5020 只，首次约 4.5 小时）",
     "progress": scan_progress, "run": scan_run},
    {"id": "daily", "name": "每日批处理（约 12 分钟）",
     "progress": daily_progress, "run": daily_run},
]


def find(tid):
    for t in TASKS:
        if t["id"] == tid:
            return t
    return None


def show_status(state=None):
    state = state or load_state()
    print("%-16s %-34s %-12s %-10s %s" % ("id", "任务", "进度", "状态", "上次结果"))
    print("-" * 108)
    for t in TASKS:
        try:
            d, n = t["progress"]()
        except Exception as e:
            d, n = -1, -1
        st = state.get(t["id"]) or {}
        status = st.get("status", "未跑")
        if n > 0 and d >= n:
            status = "已完成"
        note = (st.get("note") or "")[:28]
        pct = ("%d/%d" % (d, n)) if n >= 0 else "?"
        print("%-16s %-34s %-12s %-10s %s" % (t["id"], t["name"][:32], pct, status, note))


def run_task(t, state, verbose=True):
    d, n = t["progress"]()
    if n > 0 and d >= n:
        print("  [跳过] %s 已完成（%d/%d）" % (t["id"], d, n))
        return True
    print("  [开始] %s（当前 %d/%d）" % (t["id"], d, n), flush=True)
    t0 = time.time()
    rec = {"status": "运行中", "started": time.strftime("%Y-%m-%d %H:%M:%S"),
           "from": d, "total": n}
    state[t["id"]] = rec
    save_state(state)
    try:
        t["run"]()
    except KeyboardInterrupt:
        rec.update({"status": "被中断", "note": "可重跑续上"})
        state[t["id"]] = rec
        save_state(state)
        raise
    except Exception as e:
        rec.update({"status": "失败", "note": "%s: %s" % (type(e).__name__, str(e)[:60]),
                    "trace": traceback.format_exc()[-800:]})
        state[t["id"]] = rec
        save_state(state)
        print("  [失败] %s -> %s" % (t["id"], rec["note"]))
        return False
    d2, n2 = t["progress"]()
    rec.update({"status": "完成" if (n2 > 0 and d2 >= n2) else "部分完成",
                "to": d2, "total": n2, "seconds": round(time.time() - t0, 1),
                "finished": time.strftime("%Y-%m-%d %H:%M:%S")})
    state[t["id"]] = rec
    save_state(state)
    print("  [完成] %s  %d/%d  用时 %.0fs" % (t["id"], d2, n2, time.time() - t0), flush=True)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", default=None)
    ap.add_argument("--force", default=None)
    a = ap.parse_args()

    if a.list:
        for t in TASKS:
            print("  %-16s %s" % (t["id"], t["name"]))
        return 0

    if a.status:
        show_status()
        return 0

    state = load_state()
    todo = TASKS
    if a.only:
        t = find(a.only)
        if not t:
            print("没有这个任务：%s" % a.only)
            return 1
        todo = [t]
    if a.force:
        t = find(a.force)
        if not t:
            print("没有这个任务：%s" % a.force)
            return 1
        state.pop(a.force, None)
        save_state(state)
        todo = [t]

    print("=== 长任务队列 ===")
    print("  说明：Ctrl+C 随时可中断，重跑会自动跳过已完成的")
    print()
    show_status(state)
    print()
    ok = 0
    for t in todo:
        if run_task(t, state):
            ok += 1
    print()
    print("=== 结束：%d/%d 个任务成功 ===" % (ok, len(todo)))
    print()
    show_status(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
