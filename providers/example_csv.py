"""示例数据源：从本地 CSV 读。**照着改就能接你自己的源**。

目录结构（默认 `data/local_csv/`，可用环境变量 `LOCAL_CSV_DIR` 改）：

    data/local_csv/600519.csv          # 不复权日线，必须有表头
    data/local_csv/600519.factor.csv   # 可选：前复权因子

日线 CSV 表头（列顺序无所谓，多出来的列会被忽略）：

    date,open,high,low,close,volume,amount
    2026-09-18,1450.0,1470.0,1441.0,1465.0,32000,470000000

因子 CSV 表头：

    date,qfq_factor
    2015-01-05,1.0
    2023-06-30,0.812

因子是**阶梯函数**：只在除权除息日有记录，中间靠前向填充（`storage_kline.attach_factor` 做）。
没有因子文件也没关系 —— 返回空表，上层按 1.0 处理（等于不复权）。

启用方式（config/settings.yaml）：

    datasource:
      name: local_csv

或者临时跑：python verify_datasource.py --provider local_csv
"""
import os
from datetime import date
from pathlib import Path

import pandas as pd

import datasource as ds

ROOT = Path(os.environ.get("LOCAL_CSV_DIR", "data/local_csv"))


class LocalCsvSource(ds.DataSource):
    name = "local_csv"
    description = "本地 CSV（示例：照着这个改就能接自己的源）"
    provides = ("kline", "factor")

    def kline(self, code, start, end):
        p = ROOT / ("%s.csv" % code)
        if not p.exists():
            return ds.empty_kline()
        df = pd.read_csv(p)
        df = ds.normalize_kline(df)
        s = pd.Timestamp(start).normalize()
        e = pd.Timestamp(end).normalize()
        return df[(df["date"] >= s) & (df["date"] <= e)].reset_index(drop=True)

    def factor(self, code, start, end):
        p = ROOT / ("%s.factor.csv" % code)
        if not p.exists():
            return None                       # None = 这个源没有因子
        return ds.normalize_factor(pd.read_csv(p))


ds.register(LocalCsvSource())