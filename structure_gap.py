"""structure_gap.py —— 信号的「距中枢结束天数」（H3 / ADR-021 补记）。

### 这个字段是什么

H3 实测（4135 条信号 / 45 只股票 / 11 年）：
    信号日距最近中枢结束 **≥10 个交易日**时，5 日超额从 **+1.30% 掉到 +0.73%**
    （差值 −0.56%，95%CI [−1.09%, −0.02%] 不跨 0，且分半验证同向）
这是目前**唯一**经得起「预注册 + 多重比较 + 分半验证」三道闸门的发现。

### ⚠️ 为什么不能省掉前缀重算（这里有个坑）

被验证的特征 A =「信号日 − **确认日结构**里最后一个中枢的 edt」。

很容易顺手写成 B =「信号日 − 全序列里『已结束』的最后一个中枢的 edt」——
**实测 A 与 B 只有 64% 一致**，因为 A 会取到「正在形成、还没结束」的中枢（此时 gap 常为 0）。
两者是两个不同的量，**不能互相替代**。

所以本模块照 A 的口径做：**每条信号在它自己的确认日上重算一次结构**，实测 34ms/条。
一只股票 100 条信号约 3.4 秒。

### 为什么结构读「确认日」而不是「信号日」

信号日当天那根触发笔**还没成形**（ADR-011 的确认延迟）。实测：600519 信号日 2015-03-06，
只用当天为止的数据，最后一笔结束在 2015-02-27 —— 读信号日会读个空。
"""
import argparse
import sys

import pandas as pd

from min_loop import MAX_BI_NUM, MIN_BI_LEN, build_zs
from storage_kline import load_kline

# H3 的阈值。**注意**：10 是 LLM 拍的，本轮没做阈值敏感性分析（8 或 12 是否同样成立未验证）。
GAP_THRESHOLD = 10

NOTE_FAR = "距中枢较远"
NOTE_NEAR = "距中枢较近"


def qfq_bars(code):
    """前复权 K 线（与信号生成同一口径）。"""
    df = load_kline(code)
    if df.empty:
        return df
    q = df.sort_values("date").reset_index(drop=True).copy()
    for c in ("open", "high", "low", "close"):
        q[c] = q[c] / q["qfq_factor"]
    return q


def structure_at(code, q, i):
    """只用 q[:i+1] 算笔与中枢。"""
    import czsc
    sub = q.iloc[:i + 1]
    s = sub.rename(columns={"volume": "vol"}).copy()
    s["dt"] = pd.to_datetime(s["date"])
    s["symbol"] = code
    b = czsc.format_standard_kline(s, freq=czsc.Freq.D)
    cz = czsc.CZSC(b, min_bi_len=MIN_BI_LEN, max_bi_num=MAX_BI_NUM)
    return list(cz.bi_list), build_zs(list(cz.bi_list))


def signal_gaps(code, sigs, verbose=False):
    """给一批信号算 H3 的距中枢结束天数。

    sigs: [{"signal_date": "YYYY-MM-DD", "signal_type": ..., "confirm_date": ...}, ...]
    返回 {(signal_date, signal_type): gap 或 None}
        gap = 信号日 − 确认日结构里最后一个中枢的结束日（交易日数）
              None = 确认日缺失 / 该日无中枢 / 日期对不上
    """
    out = {}
    q = qfq_bars(code)
    if q.empty:
        return {k: None for k in [(s["signal_date"], s["signal_type"]) for s in sigs]}
    pos = {str(d.date()): i for i, d in enumerate(q["date"])}
    cache = {}
    for s in sigs:
        key = (s["signal_date"], s["signal_type"])
        i = pos.get(s["signal_date"])
        ic = pos.get(s.get("confirm_date") or "")
        if i is None or ic is None:
            out[key] = None
            continue
        if ic not in cache:
            _, zss = structure_at(code, q, ic)
            edt = pd.Timestamp(zss[-1]["edt"]).normalize() if zss else None
            k = q.index[q["date"] == edt] if edt is not None else []
            cache[ic] = int(k[0]) if len(k) else None
        j = cache[ic]
        out[key] = None if j is None else i - j
    if verbose:
        got = [v for v in out.values() if v is not None]
        print("    [structure_gap] %s 算了 %d 条（有效 %d）" % (code, len(out), len(got)))
    return out


def backfill_gaps(db_path=None, dry_run=False, verbose=True):
    """给 signals 表里 zs_gap_days 仍为 NULL 的信号补齐。

    与 ADR-012 的确认日回填同一套路：**写入时算好，读时免费**。
    否则 UI 每次读库都要为上百条信号重算结构（21ms/条，一只股票 2 秒以上）。

    返回 [{stock_code, date, type, zs_gap_days}, ...]
    """
    from collections import defaultdict
    from pathlib import Path

    import storage_signal as ss
    if db_path:
        ss.DB_PATH = Path(db_path)

    conn = ss.connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT stock_code, signal_date, signal_type, confirm_date FROM signals "
        "WHERE zs_gap_days IS NULL")]
    conn.close()

    by_code = defaultdict(list)
    for r in rows:
        by_code[r["stock_code"]].append(r)

    updates = []
    for code, sigs in sorted(by_code.items()):
        gaps = signal_gaps(code, sigs)
        for s in sigs:
            g = gaps.get((s["signal_date"], s["signal_type"]))
            if g is None and s.get("confirm_date"):
                g = -1          # 已确认但没有中枢可参照 -> 存哨兵值，避免每次重算
            if g is None:
                continue        # 还没确认，留 NULL 等下次
            updates.append({"stock_code": code, "date": s["signal_date"],
                            "type": s["signal_type"], "zs_gap_days": g})

    if verbose:
        print("  待补 zs_gap_days %d 条 -> 本次算出 %d 条" % (len(rows), len(updates)))
    if not dry_run and updates:
        from storage_signal import update_gap_days
        n, changed = update_gap_days(updates)
        if verbose:
            print("  匹配 %d 行，实际变更 %d 行" % (n, changed))
    return updates


def note_of(gap):
    """把 zs_gap_days 变成一句人话。"""
    if gap is None:
        return ""
    if gap < 0:
        return "无中枢可参照"
    if gap >= GAP_THRESHOLD:
        return "%s（%d 个交易日）" % (NOTE_FAR, gap)
    return "%s（%d 个交易日）" % (NOTE_NEAR, gap)


def annotate(sigs, gaps):
    """把 gap 写回信号 dict，并加 H3 提示。**就地修改并返回**。"""
    for s in sigs:
        g = gaps.get((s.get("date") or s.get("signal_date"), s.get("type") or s.get("signal_type")))
        s["zs_gap_days"] = g
        s["zs_note"] = note_of(g)
        s["zs_far"] = bool(g is not None and g >= GAP_THRESHOLD)
    return sigs


def label_of(gap):
    """表格里用的短标签。"""
    if gap is None:
        return "—"
    if gap < 0:
        return "无中枢"
    return ("⚠️ %d 日" % gap) if gap >= GAP_THRESHOLD else ("%d 日" % gap)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只算不写库")
    a = ap.parse_args()
    print("=== 回填 zs_gap_days（H3：信号日距最近中枢结束的交易日数）===")
    print("  阈值 %d 个交易日；口径 = 每条信号在**自己的确认日**上重算结构（ADR-021 补记）"
          % GAP_THRESHOLD)
    up = backfill_gaps(dry_run=a.dry_run)
    if up:
        vals = [u["zs_gap_days"] for u in up if u["zs_gap_days"] >= 0]
        far = sum(1 for v in vals if v >= GAP_THRESHOLD)
        if vals:
            print("  分布：有效 %d 条，其中距中枢 >= %d 日的 %d 条（%.1f%%）"
                  % (len(vals), GAP_THRESHOLD, far, 100.0 * far / len(vals)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
