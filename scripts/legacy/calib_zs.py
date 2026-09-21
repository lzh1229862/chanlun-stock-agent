"""T2-2 中枢取法校准：对比不同中枢划分方案，为 PRD F2.3 选定标准取法。

运行：python calib_zs.py

评判依据（缠论标准）：
  1. 笔：至少 5 根 K 线（顶分型 3 根 + 底分型 3 根，共用 1 根）
  2. 中枢：至少 3 个连续次级别走势重叠，延伸不超过 9 段
"""
from datetime import date, timedelta

import akshare as ak
import pandas as pd

import czsc

STOCKS = ["600519", "000001", "300750", "601318", "000858"]
YEARS = 2
UP, DOWN = "向上", "向下"
MAX_ZS_BIS = 9  # 缠论经典规则：中枢延伸不超过 9 段（9 笔）
MIN_ZS_BIS = 3


def ts_code(code):
    return ("sh" if code[0] == "6" else "sz") + code


def fetch(code):
    end = date.today()
    start = end - timedelta(days=365 * YEARS)
    df = ak.stock_zh_a_hist_tx(symbol=ts_code(code), start_date=start.strftime("%Y%m%d"),
                               end_date=end.strftime("%Y%m%d"), adjust="qfq")
    df = df.rename(columns={"volume": "vol"})
    df["dt"] = pd.to_datetime(df["date"])
    df["symbol"] = code
    return czsc.format_standard_kline(df, freq=czsc.Freq.D)


def czsc_zs(c):
    return [{"sdt": z.sdt, "edt": z.edt, "zg": z.zg, "zd": z.zd, "n": len(z.bis)} for z in c.zs_list]


def capped_zs(bis, max_bis=MAX_ZS_BIS):
    """3 笔重叠起步，与 [zd, zg] 有重叠则延伸，最多 max_bis 笔。"""
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


def count_signals(bis, zss):
    """三类买卖点计数（规则与 min_loop.py 一致）。"""
    n = {"一买": 0, "二买": 0, "三买": 0, "一卖": 0, "二卖": 0, "三卖": 0}
    fb, fs = [], []

    for i in range(2, len(bis)):
        cur, prev, mid = bis[i], bis[i - 2], bis[i - 1]
        if str(prev.direction) != str(cur.direction) or str(mid.direction) == str(cur.direction):
            continue
        weaker = cur.power_price < prev.power_price and (
            cur.power_volume < prev.power_volume or cur.length < prev.length)
        if str(cur.direction) == DOWN and cur.low < prev.low and weaker:
            n["一买"] += 1
            fb.append(i)
        if str(cur.direction) == UP and cur.high > prev.high and weaker:
            n["一卖"] += 1
            fs.append(i)

    for j in fb:
        if j + 2 < len(bis) and str(bis[j + 1].direction) == UP and str(bis[j + 2].direction) == DOWN \
                and bis[j + 2].low > bis[j].low:
            n["二买"] += 1
    for j in fs:
        if j + 2 < len(bis) and str(bis[j + 1].direction) == DOWN and str(bis[j + 2].direction) == UP \
                and bis[j + 2].high < bis[j].high:
            n["二卖"] += 1

    for i in range(len(bis) - 1):
        b, nb = bis[i], bis[i + 1]
        z = next((x for x in reversed(zss) if x["sdt"] <= b.sdt), None)
        if z is None:
            continue
        if str(b.direction) == UP and b.high > z["zg"] and str(nb.direction) == DOWN and nb.low > z["zg"]:
            n["三买"] += 1
        if str(b.direction) == DOWN and b.low < z["zd"] and str(nb.direction) == UP and nb.high < z["zd"]:
            n["三卖"] += 1
    return n


METHODS = [
    ("A 原生默认     mbl=6 cap=50 ", 6, 50, False),
    ("B 原生         mbl=5 cap=500", 5, 500, False),
    ("C 原生         mbl=4 cap=500", 4, 500, False),
    ("D 原生+9笔切分 mbl=5 cap=500", 5, 500, True),
    ("E 原生+9笔切分 mbl=4 cap=500", 4, 500, True),
]

bars_map = {s: fetch(s) for s in STOCKS}
print(f"取数：{len(STOCKS)} 只股票 × {YEARS} 年日线（腾讯源，前复权）")
print()

agg, detail_d = {}, None
for name, mbl, mbn, use_cap in METHODS:
    print(f"===== {name} =====")
    print(f"  {'股票':>6} {'笔数':>5} {'笔长中位':>8} {'中枢数':>6} {'中枢笔数':>9} {'合规':>5} "
          f"{'最长中枢天':>10} {'一买':>4}{'二买':>4}{'三买':>4}{'一卖':>4}{'二卖':>4}{'三卖':>4}")
    tot = {"nbi": 0, "nzs": 0, "days": 0, **{k: 0 for k in ["一买", "二买", "三买", "一卖", "二卖", "三卖"]}}
    ok_all = True
    for s in STOCKS:
        c = czsc.CZSC(bars_map[s], min_bi_len=mbl, max_bi_num=mbn)
        bis = list(c.bi_list)
        zss = capped_zs(bis) if use_cap else czsc_zs(c)
        ns = sorted(z["n"] for z in zss) or [0]
        ok = all(MIN_ZS_BIS <= x <= MAX_ZS_BIS for x in ns) and len(ns) > 0
        ok_all = ok_all and ok
        days = max(((z["edt"] - z["sdt"]).days for z in zss), default=0)
        sg = count_signals(bis, zss)
        lens = sorted(b.length for b in bis) or [0]
        tot["nbi"] += len(bis)
        tot["nzs"] += len(zss)
        tot["days"] = max(tot["days"], days)
        for k in sg:
            tot[k] += sg[k]
        print(f"  {s:>6} {len(bis):>5} {lens[len(lens)//2]:>8} {len(zss):>6} "
              f"{f'{min(ns)}~{max(ns)}':>9} {'✓' if ok else '✗':>5} {days:>10} "
              f"{sg['一买']:>4}{sg['二买']:>4}{sg['三买']:>4}{sg['一卖']:>4}{sg['二卖']:>4}{sg['三卖']:>4}")
        if s == "600519" and use_cap and mbl == 5:
            detail_d = zss
    tot["ok"] = ok_all
    print(f"  {'合计':>6} {tot['nbi']:>5} {'':>8} {tot['nzs']:>6} {'':>9} "
          f"{'✓' if ok_all else '✗':>5} {tot['days']:>10} "
          f"{tot['一买']:>4}{tot['二买']:>4}{tot['三买']:>4}{tot['一卖']:>4}{tot['二卖']:>4}{tot['三卖']:>4}")
    print()
    agg[name] = tot

print("===== 汇总（5 只股票合计）=====")
print(f"  {'取法':<30}{'中枢总数':>8}{'合规':>5}{'最长中枢天':>10}{'一买':>5}{'二买':>5}{'三买':>5}"
      f"{'一卖':>5}{'二卖':>5}{'三卖':>5}{'三买+三卖':>9}")
for name, _, _, _ in METHODS:
    t = agg[name]
    print(f"  {name:<30}{t['nzs']:>8}{'✓' if t['ok'] else '✗':>5}{t['days']:>10}{t['一买']:>5}{t['二买']:>5}"
          f"{t['三买']:>5}{t['一卖']:>5}{t['二卖']:>5}{t['三卖']:>5}{t['三买'] + t['三卖']:>9}")
print()

print("===== 推荐方案 D 的样本（600519 中枢明细）=====")
for i, z in enumerate(detail_d or []):
    print(f"  #{i:<2} {z['sdt'].date()} -> {z['edt'].date()}  {(z['edt']-z['sdt']).days:>4}天  "
          f"区间[{z['zd']:>8.2f}, {z['zg']:>8.2f}]  含笔={z['n']}")
