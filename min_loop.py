"""T2-3 czsc 信号映射到三类买卖点：原始信号 -> 缠论术语。

运行：python min_loop.py

环境：Python 3.12.10  +  czsc 1.0.1  +  akshare 1.18.96
取法：min_bi_len=5, max_bi_num=500, 中枢延伸上限 9 笔（见 docs/技术决策记录.md ADR-005）
说明：czsc 1.0.1 无内置信号库（ADR-003），本脚本按 czsc 0.10.x 的命名约定产出原始信号，
      再由 map_signal() 映射为缠论术语，映射规则集中在下方两张表，可人工校准。
"""
import json
import platform
from datetime import date, timedelta

import akshare as ak
import pandas as pd

import czsc

SYMBOL = "600519"
TS_SYMBOL = "sh600519"
YEARS = 2
UP, DOWN = "向上", "向下"
MIN_BI_LEN = 5
MAX_BI_NUM = 500
MAX_ZS_BIS = 9
# 一类买卖点背驰的「力度容差」：要求当前笔的价格力度至少比对比笔弱 (1-POWER_TOL) 倍。
# 0 表示不容差（严格小于）。取值依据见 docs/技术决策记录.md ADR-006 与 calib_beichi.py。
POWER_TOL = 0.03
FREQ = "日线"

# ============================================================
# 映射规则表（人工校准入口，只改这里即可）
# ============================================================

# 规则 1：按 v1 语义值映射。czsc 各版本 v1 命名稳定，作为主规则。
V1_TERM = {
    "一买": "第一类买点", "一卖": "第一类卖点",
    "二买": "第二类买点", "二卖": "第二类卖点",
    "三买": "第三类买点", "三卖": "第三类卖点",
}

# 规则 2：按 k3 动作码映射。czsc 各版本 k3 命名不统一，仅作兜底与交叉校验。
K3_TERM = {
    "BUY1": "第一类买点", "SELL1": "第一类卖点",
    "BUY2": "第二类买点", "SELL2": "第二类卖点",
    "BUY3": "第三类买点", "SELL3": "第三类卖点",
    "三买辅助V230228": "第三类买点",
    "BS2辅助V230320": None,   # 买卖方向由 v1 决定
    "BS3辅助V230318": None,
    "BS3辅助V230319": None,
}

# 规则 3：泛买卖点，无第一/二/三类归属
V1_GENERIC = {"买点": "泛买点", "卖点": "泛卖点"}

# 规则 0：czsc 占位值，表示条件未满足或无语义值，必须先于规则 2 排除
V1_NOT_TRIGGERED = {"其他"}
V1_PLACEHOLDER = {"任意", "无", ""}

# 附加提示：v1 命中这些关键词时不直接映射，但值得人工校准
V1_HINT = {"背": "含『背』字，可能与背驰/一买卖点判定相关，待校准"}


def map_signal(raw_signal):
    """czsc 风格原始信号 -> 缠论术语。

    信号串格式（czsc 约定：键 {freq}_{k2}_{k3} 与值 {v1}_{v2}_{v3}_{score} 扁平拼接）：
        [{symbol}_]{freq}_{k2}_{k3}_{v1}_{v2}_{v3}_{score}
    例：600519_日线_D1B_BUY1_一买_3笔_任意_0
        15分钟_D1B_BUY1_一买_5笔_任意_0      （czsc 文档中的无标的形式）

    返回 dict，永不抛异常；term=None 表示未映射。
    """
    parts = raw_signal.split("_")
    symbol = ""
    if parts and len(parts[0]) == 6 and parts[0].isdigit():
        symbol, parts = parts[0], parts[1:]
    freq = parts[0] if len(parts) > 0 else ""
    k2 = parts[1] if len(parts) > 1 else ""
    k3 = parts[2] if len(parts) > 2 else ""
    v1 = parts[3] if len(parts) > 3 else ""

    term, status, rule, note = None, "未映射", "无规则命中", ""

    if v1 in V1_NOT_TRIGGERED:
        status, rule = "未触发", "规则0: 占位值"
        note = f"czsc 占位值『{v1}』表示条件未满足，不是买卖点信号"
    elif v1 in V1_PLACEHOLDER:
        status, rule = "无语义值", "规则0: 占位值"
        note = f"v1『{v1}』无买卖点语义"
    elif v1 in V1_TERM:
        term, status, rule = V1_TERM[v1], "已映射", "规则1: v1 语义值"
        if k3 in K3_TERM and K3_TERM[k3] and K3_TERM[k3] != term:
            status, note = "映射冲突", f"k3={k3} 指向 {K3_TERM[k3]}，与 v1={v1} 不一致，需人工确认"
        elif k3 not in K3_TERM:
            note = f"k3={k3} 未登记，仅凭 v1 映射，建议人工确认"
    elif v1 in V1_GENERIC:
        status, rule = "泛买卖点", "规则3: v1 泛值"
        note = f"{V1_GENERIC[v1]}，无法归入第一/二/三类"
    elif k3 in K3_TERM and K3_TERM[k3]:
        term, status, rule = K3_TERM[k3], "已映射", "规则2: k3 动作码"
        note = f"v1={v1} 非标准买卖点值，仅凭 k3 映射"
    else:
        for kw, hint in V1_HINT.items():
            if kw in v1:
                note = hint
                break
        if not note:
            note = "非买卖点信号（辅助过滤/形态/均线等），不进入缠论买卖点"

    return {"raw": raw_signal, "symbol": symbol, "freq": freq, "k2": k2, "k3": k3, "v1": v1,
            "term": term, "status": status, "rule": rule, "note": note}


# ============================================================
# 结构计算
# ============================================================

def build_zs(bis, max_bis=MAX_ZS_BIS):
    out, i = [], 0
    while i + 2 < len(bis):
        core = bis[i:i + 3]
        zg = min(b.high for b in core)
        zd = max(b.low for b in core)
        if zg <= zd:
            i += 1
            continue
        seg, j = list(core), i + 3
        while j < len(bis) and len(seg) < max_bis:
            b = bis[j]
            if b.low <= zg and b.high >= zd:
                seg.append(b)
                j += 1
            else:
                break
        out.append({"sdt": seg[0].sdt, "edt": seg[-1].edt, "zg": zg, "zd": zd, "n": len(seg)})
        i = j
    return out


def build_signals(bis, zss, power_tol=POWER_TOL):
    """三类买卖点，同时产出统一格式记录与 czsc 风格原始信号。"""
    rec, fb, fs = [], [], []

    def add(dt, price, trade, reason, v2="任意"):
        code = {"一买": "BUY1", "一卖": "SELL1", "二买": "BUY2",
                "二卖": "SELL2", "三买": "BUY3", "三卖": "SELL3"}[trade]
        k2 = "D1B" if trade in ("一买", "一卖", "二买", "二卖") else "D1"
        raw = f"{SYMBOL}_{FREQ}_{k2}_{code}_{trade}_{v2}_任意_0"
        rec.append({"date": dt.date().isoformat(), "price": round(float(price), 2),
                    "type": {"一买": "第一类买点", "一卖": "第一类卖点", "二买": "第二类买点",
                             "二卖": "第二类卖点", "三买": "第三类买点", "三卖": "第三类卖点"}[trade],
                    "reason": reason, "raw_signal": raw})

    for i in range(2, len(bis)):
        cur, prev, mid = bis[i], bis[i - 2], bis[i - 1]
        if str(prev.direction) != str(cur.direction) or str(mid.direction) == str(cur.direction):
            continue
        weaker = cur.power_price < prev.power_price * (1 - power_tol) and (
            cur.power_volume < prev.power_volume or cur.length < prev.length)
        if str(cur.direction) == DOWN and cur.low < prev.low and weaker:
            fb.append(i)
            add(cur.edt, cur.low, "一买",
                f"下跌笔创新低 {cur.low:.2f} < 前低 {prev.low:.2f}，力度 {cur.power_price:.3f} < {prev.power_price:.3f}", "3笔")
        if str(cur.direction) == UP and cur.high > prev.high and weaker:
            fs.append(i)
            add(cur.edt, cur.high, "一卖",
                f"上涨笔创新高 {cur.high:.2f} > 前高 {prev.high:.2f}，力度 {cur.power_price:.3f} < {prev.power_price:.3f}", "3笔")

    for j in fb:
        if j + 2 < len(bis) and str(bis[j + 1].direction) == UP and str(bis[j + 2].direction) == DOWN \
                and bis[j + 2].low > bis[j].low:
            b2 = bis[j + 2]
            add(b2.edt, b2.low, "二买", f"一买 {bis[j].low:.2f} 后回抽不破前低，回踩低点 {b2.low:.2f}")
    for j in fs:
        if j + 2 < len(bis) and str(bis[j + 1].direction) == DOWN and str(bis[j + 2].direction) == UP \
                and bis[j + 2].high < bis[j].high:
            b2 = bis[j + 2]
            add(b2.edt, b2.high, "二卖", f"一卖 {bis[j].high:.2f} 后反抽不破前高，反抽高点 {b2.high:.2f}")

    for i in range(len(bis) - 1):
        b, nb = bis[i], bis[i + 1]
        z = next((x for x in reversed(zss) if x["sdt"] <= b.sdt), None)
        if z is None:
            continue
        if str(b.direction) == UP and b.high > z["zg"] and str(nb.direction) == DOWN and nb.low > z["zg"]:
            add(nb.edt, nb.low, "三买", f"向上突破中枢上沿 {z['zg']:.2f}，回踩低点 {nb.low:.2f} 未回中枢")
        if str(b.direction) == DOWN and b.low < z["zd"] and str(nb.direction) == UP and nb.high < z["zd"]:
            add(nb.edt, nb.high, "三卖", f"向下跌破中枢下沿 {z['zd']:.2f}，反抽高点 {nb.high:.2f} 未回中枢")

    rec.sort(key=lambda x: x["date"])
    return rec


def main():
    end = date.today()
    start = end - timedelta(days=365 * YEARS)
    raw_df = ak.stock_zh_a_hist_tx(symbol=TS_SYMBOL, start_date=start.strftime("%Y%m%d"),
                                   end_date=end.strftime("%Y%m%d"), adjust="qfq")
    raw_df = raw_df.rename(columns={"volume": "vol"})
    raw_df["dt"] = pd.to_datetime(raw_df["date"])
    raw_df["symbol"] = SYMBOL
    bars = czsc.format_standard_kline(raw_df, freq=czsc.Freq.D)

    print(f"Python {platform.python_version()}   czsc {czsc.__version__}   akshare {ak.__version__}")
    print(f"标的 {SYMBOL}   前复权(qfq)   K线 {len(bars)} 根   {bars[0].dt.date()} ~ {bars[-1].dt.date()}   最新收盘 {bars[-1].close:.2f}")
    print(f"取法 min_bi_len={MIN_BI_LEN}  max_bi_num={MAX_BI_NUM}  中枢延伸上限 {MAX_ZS_BIS} 笔 (ADR-005)")
    print()

    c = czsc.CZSC(bars, min_bi_len=MIN_BI_LEN, max_bi_num=MAX_BI_NUM)
    bi_raw = list(c.bi_list)
    zs_raw = build_zs(bi_raw)
    records = build_signals(bi_raw, zs_raw)
    sigs = [{k: r[k] for k in ("date", "price", "type", "reason")} for r in records]

    print(f"=== T2-2 统一格式买卖点（{len(sigs)} 条，前 5 条）===")
    for x in sigs[:5]:
        print(f"  {x}")
    print()

    print("=== T2-3 原始信号 -> 缠论术语 映射对照（600519）===")
    print(f"  {'日期':<12}{'原始信号 (czsc 风格)':<48}{'v1':<6}{'->':<4}{'缠论术语':<12}{'状态'}")
    mapped_all = [map_signal(r["raw_signal"]) for r in records]
    for r, m in zip(records, mapped_all):
        print(f"  {r['date']:<12}{m['raw']:<48}{m['v1']:<6}{'->':<4}{str(m['term']):<12}{m['status']}")
    print()

    print("=== czsc 0.10.x 真实历史信号样本 -> 映射结果（验证映射能力边界）===")
    CZSC_REAL = [
        "15分钟_D1B_BUY1_一买_5笔_任意_0",
        "15分钟_D1B_SELL1_一卖_17笔_任意_0",
        "15分钟_D1#SMA#21_BS2辅助V230320_二买_任意_任意_0",
        "15分钟_D1#SMA#21_BS2辅助V230320_二卖_任意_任意_0",
        "15分钟_D1_三买辅助V230228_三买_14笔_任意_0",
        "15分钟_D1#SMA#34_BS3辅助V230318_三买_任意_任意_0",
        "15分钟_D1#SMA#34_BS3辅助V230318_三卖_任意_任意_0",
        "60分钟_神奇九转N9_BS辅助V240616_买点_9转_任意_0",
        "60分钟_神奇九转N9_BS辅助V240616_卖点_9转_任意_0",
        "15分钟_N5M21#SMA_双均线过滤V240330_看空_第03次_任意_0",
        "日线_D1N100MD1_MACD交叉数量V230624_0轴上金叉第1次_0轴上死叉第1次_任意_0",
        "日线_D1三笔_形态V230618_向下盘背_任意_任意_0",
        "15分钟_D1F_分型强弱_中顶_有中枢_任意_0",
        "15分钟_D0质数窗口MO3_BE辅助V230320_看多_17K_任意_0",
        # 以下为人为构造的边界用例
        "日线_D1B_BUY1_一卖_3笔_任意_0",
        "日线_D1B_BUY1_其他_任意_任意_0",
        "日线_D9_未知辅助V999999_二买_任意_任意_0",
        "日线_D1B_XXX9_foo_bar_任意_0",
    ]
    print(f"  {'原始信号':<52}{'->':<4}{'缠论术语':<12}{'状态':<10}{'命中规则/说明'}")
    for s in CZSC_REAL:
        m = map_signal(s)
        tail = m["note"] or m["rule"]
        print(f"  {s:<52}{'->':<4}{str(m['term']):<12}{m['status']:<10}{tail[:44]}")
    print()

    print("=== 映射统计 ===")
    allm = mapped_all + [map_signal(s) for s in CZSC_REAL]
    cnt = {}
    for m in allm:
        cnt[m["status"]] = cnt.get(m["status"], 0) + 1
    for k in ["已映射", "映射冲突", "泛买卖点", "未触发", "无语义值", "未映射"]:
        if k in cnt:
            print(f"  {k:<8} {cnt[k]:>3} 条")
    print()

    print("=== 映射规则表（校准入口）===")
    print("  规则1 v1 语义值 ->", json.dumps(V1_TERM, ensure_ascii=False))
    print("  规则2 k3 动作码 ->", json.dumps(K3_TERM, ensure_ascii=False))
    print("  规则3 泛买卖点  ->", json.dumps(V1_GENERIC, ensure_ascii=False))


if __name__ == "__main__":
    main()
