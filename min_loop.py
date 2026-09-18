"""T2-2 统一缠论输出格式：把 czsc 对象转成普通 dict/list，让上层不再依赖 czsc 数据结构。

运行：python min_loop.py

环境：Python 3.12.10  +  czsc 1.0.1  +  akshare 1.18.96
取法：min_bi_len=5, max_bi_num=500, 中枢延伸上限 9 笔（见 docs/技术决策记录.md ADR-005）
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

# ---------------- 统一格式转换（唯一接触 czsc 的地方） ----------------

def fx_to_dict(fx):
    """czsc FX -> {date, price, type}"""
    return {"date": fx.dt.date().isoformat(), "price": round(float(fx.fx), 2), "type": str(fx.mark)}


def bi_to_dict(bi):
    """czsc BI -> {start_date, end_date, start_price, end_price, direction}"""
    return {
        "start_date": bi.sdt.date().isoformat(),
        "end_date": bi.edt.date().isoformat(),
        "start_price": round(float(bi.fx_a.fx), 2),
        "end_price": round(float(bi.fx_b.fx), 2),
        "direction": str(bi.direction),
    }


def zs_to_dict(z):
    """中枢（由 BI 序列按 ADR-005 取法推导）-> {start_date, end_date, high, low}"""
    return {
        "start_date": z["sdt"].date().isoformat(),
        "end_date": z["edt"].date().isoformat(),
        "high": round(z["zg"], 2),
        "low": round(z["zd"], 2),
    }


def build_zs(bis, max_bis=MAX_ZS_BIS):
    """3 笔重叠起步，与 [zd, zg] 有重叠则延伸，最多 max_bis 笔（缠论延伸不超过 9 段）。"""
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


def build_signals(bis, zss):
    """三类买卖点，输出 {date, price, type, reason}。"""
    out, fb, fs = [], [], []

    for i in range(2, len(bis)):
        cur, prev, mid = bis[i], bis[i - 2], bis[i - 1]
        if str(prev.direction) != str(cur.direction) or str(mid.direction) == str(cur.direction):
            continue
        weaker = cur.power_price < prev.power_price and (
            cur.power_volume < prev.power_volume or cur.length < prev.length)
        if str(cur.direction) == DOWN and cur.low < prev.low and weaker:
            fb.append(i)
            out.append({"date": cur.edt.date().isoformat(), "price": round(float(cur.low), 2),
                        "type": "第一类买点",
                        "reason": f"下跌笔创新低 {cur.low:.2f} < 前低 {prev.low:.2f}，力度 {cur.power_price:.3f} < {prev.power_price:.3f}"})
        if str(cur.direction) == UP and cur.high > prev.high and weaker:
            fs.append(i)
            out.append({"date": cur.edt.date().isoformat(), "price": round(float(cur.high), 2),
                        "type": "第一类卖点",
                        "reason": f"上涨笔创新高 {cur.high:.2f} > 前高 {prev.high:.2f}，力度 {cur.power_price:.3f} < {prev.power_price:.3f}"})

    for j in fb:
        if j + 2 < len(bis) and str(bis[j + 1].direction) == UP and str(bis[j + 2].direction) == DOWN \
                and bis[j + 2].low > bis[j].low:
            b2 = bis[j + 2]
            out.append({"date": b2.edt.date().isoformat(), "price": round(float(b2.low), 2),
                        "type": "第二类买点",
                        "reason": f"一买 {bis[j].low:.2f} 后回抽不破前低，回踩低点 {b2.low:.2f}"})
    for j in fs:
        if j + 2 < len(bis) and str(bis[j + 1].direction) == DOWN and str(bis[j + 2].direction) == UP \
                and bis[j + 2].high < bis[j].high:
            b2 = bis[j + 2]
            out.append({"date": b2.edt.date().isoformat(), "price": round(float(b2.high), 2),
                        "type": "第二类卖点",
                        "reason": f"一卖 {bis[j].high:.2f} 后反抽不破前高，反抽高点 {b2.high:.2f}"})

    for i in range(len(bis) - 1):
        b, nb = bis[i], bis[i + 1]
        z = next((x for x in reversed(zss) if x["sdt"] <= b.sdt), None)
        if z is None:
            continue
        if str(b.direction) == UP and b.high > z["zg"] and str(nb.direction) == DOWN and nb.low > z["zg"]:
            out.append({"date": nb.edt.date().isoformat(), "price": round(float(nb.low), 2),
                        "type": "第三类买点",
                        "reason": f"向上突破中枢上沿 {z['zg']:.2f}，回踩低点 {nb.low:.2f} 未回中枢"})
        if str(b.direction) == DOWN and b.low < z["zd"] and str(nb.direction) == UP and nb.high < z["zd"]:
            out.append({"date": nb.edt.date().isoformat(), "price": round(float(nb.high), 2),
                        "type": "第三类卖点",
                        "reason": f"向下跌破中枢下沿 {z['zd']:.2f}，反抽高点 {nb.high:.2f} 未回中枢"})

    out.sort(key=lambda s: s["date"])
    return out


# ---------------- 1. 取数 ----------------
end = date.today()
start = end - timedelta(days=365 * YEARS)
raw = ak.stock_zh_a_hist_tx(symbol=TS_SYMBOL, start_date=start.strftime("%Y%m%d"),
                            end_date=end.strftime("%Y%m%d"), adjust="qfq")
raw = raw.rename(columns={"volume": "vol"})
raw["dt"] = pd.to_datetime(raw["date"])
raw["symbol"] = SYMBOL
bars = czsc.format_standard_kline(raw, freq=czsc.Freq.D)

print(f"Python {platform.python_version()}   czsc {czsc.__version__}   akshare {ak.__version__}")
print(f"标的 {SYMBOL}   前复权(qfq)   K线 {len(bars)} 根   {bars[0].dt.date()} ~ {bars[-1].dt.date()}   最新收盘 {bars[-1].close:.2f}")
print(f"取法 min_bi_len={MIN_BI_LEN}  max_bi_num={MAX_BI_NUM}  中枢延伸上限 {MAX_ZS_BIS} 笔 (ADR-005)")
print()

# ---------------- 2. czsc 计算 ----------------
c = czsc.CZSC(bars, min_bi_len=MIN_BI_LEN, max_bi_num=MAX_BI_NUM)
fx_raw, bi_raw = list(c.fx_list), list(c.bi_list)
zs_raw = build_zs(bi_raw)

# ---------------- 3. 转统一格式（普通 dict/list） ----------------
fxs = [fx_to_dict(x) for x in fx_raw]
bis = [bi_to_dict(x) for x in bi_raw]
zss = [zs_to_dict(x) for x in zs_raw]
sigs = build_signals(bi_raw, zs_raw)

print("=== 字段映射：czsc 原始 -> 统一格式 ===")
print("  分型   FX.dt          -> date            FX.fx            -> price")
print("         FX.mark        -> type")
print("  笔     BI.sdt         -> start_date      BI.edt           -> end_date")
print("         BI.fx_a.fx     -> start_price     BI.fx_b.fx       -> end_price")
print("         BI.direction   -> direction")
print("  中枢   由 BI 序列推导（ADR-005 取法）")
print("         sdt            -> start_date      edt              -> end_date")
print("         zg(上沿)       -> high            zd(下沿)         -> low")
print("  买卖点 自研规则计算，czsc 1.0.1 无对应字段 -> date/price/type/reason")
print()
print("=== czsc 原始对象示例（仅列映射用到的字段）===")
print(f"  FX  : FX(dt={fx_raw[-1].dt.date()}, mark={fx_raw[-1].mark}, fx={fx_raw[-1].fx})")
print(f"  BI  : BI(sdt={bi_raw[-1].sdt.date()}, edt={bi_raw[-1].edt.date()}, "
      f"fx_a.fx={bi_raw[-1].fx_a.fx}, fx_b.fx={bi_raw[-1].fx_b.fx}, direction={bi_raw[-1].direction})")
print(f"  ZS  : czsc 原生 ZS 属性 zd/zg/zz/gg/dd/sdt/edt（语义同统一格式的 low/high）")
print()

print("=== 统一格式输出 ===")
print()
print(f"[分型] {len(fxs)} 条，最近 5 条        key: date / price / type")
for x in fxs[-5:]:
    print(f"  {x}")
print()
print(f"[笔] {len(bis)} 条，最近 5 条          key: start_date / end_date / start_price / end_price / direction")
for x in bis[-5:]:
    print(f"  {x}")
print()
print(f"[中枢] {len(zss)} 条，全部            key: start_date / end_date / high / low")
for x in zss:
    print(f"  {x}")
print()
print(f"[买卖点] {len(sigs)} 条，全部          key: date / price / type / reason")
for x in sigs:
    print(f"  {x}")
print()
print("=== 可 JSON 序列化（证明已与 czsc 解耦）===")
for name, obj in [("分型", fxs[-1]), ("笔", bis[-1]), ("中枢", zss[-1]), ("买卖点", sigs[-1])]:
    print(f"  {name}: {json.dumps(obj, ensure_ascii=False)}")
