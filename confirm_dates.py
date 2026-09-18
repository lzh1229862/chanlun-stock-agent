"""T5-0 信号三日期：signal_date / confirm_date / entry_ref_price 的计算与回填。

字段定义（ADR-012）
    signal_date      缠论信号实际发生日 = 触发笔结束日 = BI.edt ≡ BI.fx_b.dt（实测 67/67 笔等价）
    confirm_date     信号可被确认的日期 = signal_date + 最小确认延迟（交易日）
                     延迟由「逐日放行 K 线重跑信号生成」实测得出，实测 1~2 个交易日（ADR-011）
    entry_ref_price  入场参考价 = confirm_date 次一交易日的【不复权】开盘价

为什么这么定
    - 缠论「笔」需要后续 K 线才能确认，故 confirm_date 必然晚于 signal_date（0 条信号能在信号日当天确认）
    - 确认日收盘才可知信号 -> 最早的成交机会是次日开盘
    - 存不复权价：用户行情软件里看到的就是它；且不复权价永不变化（幂等），
      前复权价会随除权除息回溯变化（ADR-002 的教训）
    - 与 backtest.entry_price（前复权）用途不同，不可混用；历史数据上两者可能差 5%~6%

策略（方案 A：实测 + 增量回填）
    写入时 signal_date 立即写；confirm_date / entry_ref_price 写 NULL 并给 backfill_note
    每次运行时对 confirm_date IS NULL 的信号重试回填，直至确认
"""
from collections import defaultdict
from pathlib import Path

import pandas as pd

from storage_kline import load_kline

MAX_DELAY = 30
PENDING_NOTE = "待确认：信号日过近，等待后续 K 线确认（方案 A / ADR-012）"


def load_frames(code):
    """返回 (不复权 df, 前复权 df)，均按日期升序。"""
    df = load_kline(code)
    if df.empty:
        return df, df
    raw = df.sort_values("date").reset_index(drop=True)
    q = raw.copy()
    for c in ("open", "high", "low", "close"):
        q[c] = q[c] / q["qfq_factor"]
    return raw, q


def compute_signals(bars_sub, code):
    """在给定（已截断的）前复权 K 线上重跑信号生成，返回 {(日期, 类型)}。"""
    import czsc

    from min_loop import MAX_BI_NUM, MIN_BI_LEN, build_signals, build_zs

    q = bars_sub.rename(columns={"volume": "vol"}).copy()
    q["dt"] = pd.to_datetime(q["date"])
    q["symbol"] = code
    b = czsc.format_standard_kline(q, freq=czsc.Freq.D)
    cz = czsc.CZSC(b, min_bi_len=MIN_BI_LEN, max_bi_num=MAX_BI_NUM)
    bis = list(cz.bi_list)
    return {(x["date"], x["type"]) for x in build_signals(bis, build_zs(bis))}


def confirmation_delay(qbars, i_sig, code, signal_date, signal_type, max_k=MAX_DELAY):
    """信号日之后还需几个交易日才能确认该信号；测不出返回 None。

    判据：把 K 线截断到「信号日 + k 个交易日」重跑信号生成，若该信号出现则延迟为 k。
    """
    for k in range(max_k + 1):
        if i_sig + k >= len(qbars):
            return None
        sub = qbars[qbars["date"] <= qbars.at[i_sig + k, "date"]]
        if (signal_date, signal_type) in compute_signals(sub, code):
            return k
    return None


def confirm_entry(code, signals, max_k=MAX_DELAY):
    """为一批信号算出 (confirm_date, entry_ref_price, backfill_note)。

    返回 {(signal_date, signal_type): (confirm_date|None, entry_ref_price|None, note)}
    """
    raw, q = load_frames(code)
    out = {}
    if raw.empty:
        for s in signals:
            out[(s["signal_date"], s["signal_type"])] = (
                None, None, "无本地 K 线，无法计算确认日与入场价")
        return out

    pos = {d: i for i, d in enumerate(q["date"])}
    for s in signals:
        key = (s["signal_date"], s["signal_type"])
        i = pos.get(pd.Timestamp(s["signal_date"]))
        if i is None:
            out[key] = (None, None, "本地 K 线中无该信号日，无法计算")
            continue
        if i >= len(q) - 1:
            out[key] = (None, None, PENDING_NOTE)
            continue

        k = confirmation_delay(q, i, code, s["signal_date"], s["signal_type"], max_k)
        if k is None:
            out[key] = (None, None,
                        f"{max_k} 个交易日内未复现，确认延迟未知（可能仍待确认或规则已变）")
            continue

        i_conf = i + k
        conf_date = str(q.at[i_conf, "date"].date())
        i_entry = i_conf + 1
        if i_entry >= len(q):
            out[key] = (conf_date, None, f"确认日 {conf_date}；入场日尚未到来")
            continue

        price = float(raw.at[i_entry, "open"])          # 不复权开盘价
        entry_d = str(raw.at[i_entry, "date"].date())
        out[key] = (conf_date, round(price, 4),
                    f"实测确认延迟 {k} 交易日；入场价取 {entry_d} 不复权开盘")
    return out


def backfill(db_path=None, dry_run=False, verbose=True):
    """对 confirm_date 仍为 NULL 的信号重试回填（方案 A 的增量回填）。

    返回 [(code, signal_date, signal_type, confirm_date, entry_ref_price, note)]
    """
    import storage_signal
    from storage_signal import connect, query_signals

    if db_path:
        storage_signal.DB_PATH = Path(db_path)

    pending = [r for r in query_signals() if r.get("confirm_date") in (None, "")]
    by_code = defaultdict(list)
    for r in pending:
        by_code[r["stock_code"]].append(r)

    results = []
    for code, sigs in sorted(by_code.items()):
        info = confirm_entry(code, sigs)
        for s in sigs:
            cd, pr, note = info[(s["signal_date"], s["signal_type"])]
            results.append({"stock_code": code, "date": s["signal_date"], "type": s["signal_type"],
                            "confirm_date": cd, "entry_ref_price": pr, "backfill_note": note})

    filled = sum(1 for r in results if r["confirm_date"])
    if verbose:
        print(f"  待回填信号 {len(pending)} 条 -> 本次算出确认日 {filled} 条，仍待确认 {len(pending) - filled} 条")

    if not dry_run and results:
        from storage_signal import update_confirm_dates
        update_confirm_dates(results)
    return results
