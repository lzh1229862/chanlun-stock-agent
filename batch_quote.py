"""batch_quote.py —— 腾讯行情批量取「最后一根日线」（T26 / ADR-028）。

### 为什么

日更时每只股票只需要**新增的那一根 K 线**。逐只发请求 = 5020 次、每次 1~2.5 秒，
全市场外推 3.5 小时。而腾讯行情**支持一次查 50 只**（实测 50 只 0.32 秒）
-> 约 100 次请求，全市场降到几分钟。

### 字段位置（实测 88 字段，已核对）

    [1] 名称   [2] 代码   [3] 当前价   [4] 昨收   [5] 今开
    [30] 时间戳(YYYYMMDDHHMMSS)   [33] 最高   [34] 最低
    [36] 成交量(手)   [37] 成交额(万元)

### ⚠️ 只在**收盘后**可用（这是生死线）

实测盘中拿到的时间戳是 20260921120537，此时 [3]/[33]/[34] 是**未完成的当日 bar**。
写进本地就会污染 K 线、进而污染信号与回测。所以必须检查时间戳 >= 15:00 才采用，
盘中直接跳过（让上层走逐只的 update_kline，那里会正确判断"源头无新数据"）。

### 另外两道守卫

1. **只补「恰好缺 1 个交易日」的情况** —— 缺更多天说明扫描停过，交给逐只的增量更新
2. **昨收与本地最后收盘不一致时也交给逐只** —— 那是除权除息，因子要重算，不能用旧因子凑
"""
import time
from datetime import date, datetime

import requests

from signal_filter import trading_calendar
from storage_kline import COLUMNS, load_kline, save_kline, ts_code

QUOTE_URL = "https://qt.gtimg.cn/q="
CHUNK = 50
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
CLOSE_HHMM = 1500


def _num(s, default=None):
    try:
        v = float(s)
        return v if v == v else default      # 过滤 NaN
    except (TypeError, ValueError):
        return default


def parse_line(line):
    """把一行 v_sh600519="..." 解析成 dict。解析失败返回 None。"""
    if '="' not in line:
        return None
    head, payload = line.split('="', 1)
    code = head.replace("v_", "").strip()[-6:]
    f = payload.rstrip('";').split("~")
    if len(f) < 38:
        return None
    ts = f[30] if len(f) > 30 else ""
    dt = None
    if len(ts) >= 12:
        try:
            dt = datetime.strptime(ts[:14], "%Y%m%d%H%M%S")
        except ValueError:
            dt = None
    return {
        "code": code,
        "name": f[1],
        "dt": dt,
        "open": _num(f[5]),
        "prev_close": _num(f[4]),
        "close": _num(f[3]),
        "high": _num(f[33]) if len(f) > 34 else None,
        "low": _num(f[34]) if len(f) > 35 else None,
        "volume": _num(f[36]) * 100 if len(f) > 36 and _num(f[36]) is not None else None,
        "amount": _num(f[37]) * 10000 if len(f) > 37 and _num(f[37]) is not None else None,
    }


def fetch_quotes(codes, chunk=CHUNK, timeout=20, verbose=False):
    """批量取行情。返回 {code: bar}。网络异常按批跳过，不抛。"""
    out, batches = {}, 0
    for i in range(0, len(codes), chunk):
        part = codes[i:i + chunk]
        url = QUOTE_URL + ",".join(ts_code(c) for c in part)
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.encoding = "gbk"
        except Exception:
            continue
        batches += 1
        for line in r.text.strip().split(";"):
            b = parse_line(line)
            if b:
                out[b["code"]] = b
    if verbose:
        print("    [batch_quote] %d 只 / %d 批 -> 解析到 %d 条" % (len(codes), batches, len(out)))
    return out


def is_final(bar):
    """行情时间戳是否已过收盘（15:00）。盘中/异常一律视为未完成。"""
    dt = bar.get("dt")
    if dt is None:
        return False
    return dt.hour * 100 + dt.minute >= CLOSE_HHMM


def _prev_trading_day(cal, d):
    prev = [x for x in cal if x.date() < d]
    return prev[-1].date() if prev else None


def merge_last_bars(codes, verbose=True):
    """用批量行情给本地补上最后一根日线。

    返回 {"appended": n, "already": n, "deferred": n, "not_final": n}
      appended  本次补上的
      already   本地已有
      deferred  缺不止一天 / 除权除息 / 本地无数据 -> 交给逐只 update_kline
      not_final 行情还没收盘（盘中）或不完整
    """
    stat = {"appended": 0, "already": 0, "deferred": 0, "not_final": 0}
    if not codes:
        return stat
    quotes = fetch_quotes(codes, verbose=verbose)
    cal = trading_calendar()
    t0 = time.time()
    for code in codes:
        bar = quotes.get(code)
        if bar is None or not is_final(bar):
            stat["not_final"] += 1
            continue
        if bar["open"] is None or bar["close"] is None or bar["high"] is None or bar["low"] is None:
            stat["not_final"] += 1
            continue
        local = load_kline(code)
        if local.empty:
            stat["deferred"] += 1
            continue
        bd = bar["dt"].date()
        last = local["date"].iloc[-1].date()
        if last >= bd:
            stat["already"] += 1
            continue
        if last != _prev_trading_day(cal, bd):
            stat["deferred"] += 1          # 缺不止一天
            continue
        lc = float(local["close"].iloc[-1])
        pc = bar["prev_close"]
        if pc and lc and abs(pc - lc) / lc > 0.002:
            stat["deferred"] += 1          # 除权除息 -> 因子要重算
            continue
        row = {"date": bar["dt"].normalize(), "open": bar["open"], "high": bar["high"],
               "low": bar["low"], "close": bar["close"], "volume": bar["volume"] or 0.0,
               "amount": bar["amount"] or 0.0,
               "qfq_factor": float(local["qfq_factor"].iloc[-1])}
        import pandas as pd
        merged = pd.concat([local[COLUMNS], pd.DataFrame([row])], ignore_index=True)
        save_kline(code, merged)
        stat["appended"] += 1
    if verbose:
        print("    [batch_quote] 补齐 %d 只 / 已有 %d / 延后 %d / 未收盘 %d   用时 %.1fs"
              % (stat["appended"], stat["already"], stat["deferred"], stat["not_final"],
                 time.time() - t0))
    return stat


if __name__ == "__main__":
    import sys
    codes = sys.argv[1:] or ["600519", "000001", "300750"]
    q = fetch_quotes(codes, verbose=True)
    for c, b in q.items():
        print("  %s %-8s %s  开 %s 高 %s 低 %s 收 %s  收盘后=%s"
              % (c, b["name"], b["dt"], b["open"], b["high"], b["low"], b["close"],
                 is_final(b)))
    print()
    print(merge_last_bars(codes))
