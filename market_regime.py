"""市场状态判断（纯函数，便于离线测试，见 ADR-018）。

刻意选最透明、最容易解释的定义 —— 而且它**天然没有未来函数**：
滚动量只用到当日及之前的收盘价。

    MA200   指数收盘的 200 日均线（交易日）
    动量    指数收盘 / 20 个交易日前收盘 - 1
    上行    收盘 > MA200  且  动量 > 0
    下行    收盘 < MA200  且  动量 < 0
    震荡    其余
    未知    样本不足 MA200（前 200 根）

两个方向都留了口子：above_ma（二档，更稳但更粗）和 regime（三档，更细）。
实测里两者都看，免得三档切出来每组样本太少时误判。
"""
import pandas as pd

MA_WIN = 200
MOM_WIN = 20
UNKNOWN = "未知"


def with_indicators(df, ma_win=MA_WIN, mom_win=MOM_WIN):
    """给指数日线加上 ma / mom / above_ma / regime 四列。"""
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df
    d = df.sort_values("date").reset_index(drop=True).copy()
    d["ma"] = d["close"].rolling(ma_win, min_periods=ma_win).mean()
    d["mom"] = d["close"] / d["close"].shift(mom_win) - 1
    d["above_ma"] = d["close"] > d["ma"]
    d["regime"] = [_label(a, m, c, ma) for a, m, c, ma in
                   zip(d["above_ma"], d["mom"], d["close"], d["ma"])]
    return d


def _label(above_ma, mom, close, ma):
    if pd.isna(ma) or pd.isna(mom):
        return UNKNOWN
    if bool(above_ma) and mom > 0:
        return "上行"
    if (not bool(above_ma)) and mom < 0:
        return "下行"
    return "震荡"


def row_on(reg_df, date):
    """取 <= date 的最后一根指数行（dict）；没有返回 None。

    用「<= date」而不是「== date」：信号日是非交易日（或指数缺数据）时也要能取到状态。
    """
    if reg_df is None or reg_df.empty:
        return None
    ok = reg_df.index[reg_df["date"] <= pd.Timestamp(date)]
    if not len(ok):
        return None
    return reg_df.iloc[int(ok[-1])].to_dict()


def regime_on(reg_df, date):
    r = row_on(reg_df, date)
    return UNKNOWN if r is None else str(r.get("regime") or UNKNOWN)


def above_ma_on(reg_df, date):
    r = row_on(reg_df, date)
    return None if r is None else bool(r.get("above_ma"))


def regime_counts(reg_df, dates, key="regime"):
    """一批日期的状态分布。"""
    out = {}
    for d in dates:
        r = row_on(reg_df, d)
        k = UNKNOWN if r is None else str(r.get(key) or UNKNOWN)
        out[k] = out.get(k, 0) + 1
    return out
