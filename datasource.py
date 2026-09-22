"""数据源抽象（ADR-039）—— 让有别的数据源的人能接上，并且**自己验得出来**。

为什么要有这一层：本项目所有统计结论都建立在**腾讯日线（不复权）+ 新浪复权因子**上。
换一个源，结论不一定一样 —— 可能是源更好，也可能只是口径不同。
与其让你改一堆文件，不如实现四个方法，然后跑：

    python verify_datasource.py --provider 你的名字

它会给你三件事：
1. **契约检查** —— 你的返回是否符合下面的约定
2. **交叉对比** —— 和内置源在重叠区间的价格/因子偏差
3. **信号一致性** —— 同一套缠论规则，两个源给出的买卖点有多大差别（这一条最有说服力）

### 最少要实现什么

只有 `kline` 是必须的。其余不实现就保持默认（抛 `NotSupported`），上层会报告「该源不支持」。

### 约定

```
kline(code, start, end) -> DataFrame      # start/end 是任何 pd.Timestamp 能解析的值
    列 = RAW_COLUMNS（顺序无所谓），**不复权**
    date: datetime64[ns]（零点）、升序、无重复
    volume 单位**股**，amount 单位**元**
    无数据 -> 返回列齐全的**空表**（不要抛异常，也不要返回 None）

factor(code, start, end) -> DataFrame | None
    列 = ["date", "qfq_factor"]，**前复权因子**（阶梯函数，只在除权除息日有记录）
    None 或空表 = 这个源没有复权因子 -> 上层按 1.0 处理（等价于不复权）

index_kline(code) -> DataFrame      # code 形如 "000300"，全历史
universe() -> list[(code, name)]    # 全市场名录，**不要**预先按 ST / 北交所过滤
```

过滤逻辑（ST、北交所、成交额门槛）是**我们的规则**，不是数据源的事，所以名录要返回原样。
"""
from pathlib import Path

RAW_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]
FACTOR_COLUMNS = ["date", "qfq_factor"]
DEFAULT_NAME = "builtin"
SETTINGS_PATH = Path("config/settings.yaml")
PROVIDER_DIR = Path(__file__).resolve().parent / "providers"


class NotSupported(Exception):
    """数据源不提供这一项。上层据此降级，而不是崩掉。"""


class DataSource:
    """数据源协议。**只实现你有的那部分**。"""

    name = "unnamed"
    description = ""
    provides = ()            # 例如 ("kline", "factor")，用于上层判断而不必 try/except

    def supports(self, what):
        return what in self.provides

    def _no(self, what):
        raise NotSupported("数据源 %s 不提供 %s" % (self.name, what))

    # ---- 必须 ----
    def kline(self, code, start, end):
        self._no("kline")

    # ---- 可选 ----
    def factor(self, code, start, end):
        return None                     # None 的语义是「没有复权因子」，不是「失败」

    def index_kline(self, code):
        self._no("index_kline")

    def universe(self):
        self._no("universe")

    def quotes(self, codes):
        self._no("quotes")             # 当日快照，未接线（见 ADR-039 遗留）

    def industry(self):
        self._no("industry")           # 行业映射，未接线（见 ADR-039 遗留）


_REGISTRY = {}
_ACTIVE = None
_DISCOVERED = False
_LOAD_ERRORS = []


def register(src, name=None):
    """注册一个数据源（实例或类）。返回 name。"""
    if isinstance(src, type):
        src = src()
    key = name or getattr(src, "name", None)
    if not key or key == "unnamed":
        raise ValueError("数据源必须有 name")
    src.name = key
    _REGISTRY[key] = src
    return key


def discover(force=False):
    """导入 providers/ 下的每个模块，让它们有机会 register()。

    加载失败的模块**记录下来**（`load_errors()`），不静默吞掉 ——
    第三方 provider 写错了要能看见。
    """
    global _DISCOVERED
    if _DISCOVERED and not force:
        return
    _DISCOVERED = True
    _LOAD_ERRORS.clear()
    if not PROVIDER_DIR.is_dir():
        return
    import importlib
    import pkgutil
    try:
        import providers
    except Exception as e:
        _LOAD_ERRORS.append(("providers", repr(e)))
        return
    # 注册表是空的 = 要么第一次，要么刚被 reset() 过。两种情况都得 reload：
    # 模块已在 sys.modules 里，光 import 不会重跑它的 ds.register(...)。
    need_reload = force or not _REGISTRY
    for m in sorted(pkgutil.iter_modules(providers.__path__), key=lambda x: x.name):
        if m.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module("providers." + m.name)
            if need_reload:
                importlib.reload(mod)
        except Exception as e:
            _LOAD_ERRORS.append((m.name, repr(e)))


def load_errors():
    discover()
    return list(_LOAD_ERRORS)


def available():
    discover()
    return sorted(_REGISTRY)


def configured_name():
    """config/settings.yaml -> datasource.name。读不到就用内置。"""
    try:
        import yaml
        cfg = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
        return (cfg.get("datasource") or {}).get("name") or DEFAULT_NAME
    except Exception:
        return DEFAULT_NAME


def get(name=None):
    """按名字取数据源；不给名字就取配置里的那个。"""
    discover()
    key = name or configured_name()
    if key not in _REGISTRY:
        raise KeyError("没有数据源 %r；可用：%s" % (key, available()))
    return _REGISTRY[key]


def active():
    """当前活动的数据源（进程内缓存）。"""
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = get()
    return _ACTIVE


def use(name):
    """切换活动数据源。给测试和 verify_datasource 用。"""
    global _ACTIVE
    _ACTIVE = get(name)
    return _ACTIVE


def reset():
    """清空注册表与缓存（测试用）。

    清完之后要调 `discover(force=True)` 才能把 providers/ 重新装回来。
    """
    global _ACTIVE, _DISCOVERED
    _REGISTRY.clear()
    _LOAD_ERRORS.clear()
    _ACTIVE = None
    _DISCOVERED = False


# ============ 归一化：把任何源给的东西收成约定形状 ============

def as_date(s):
    import pandas as pd
    return pd.to_datetime(s).astype("datetime64[ns]")


def empty_kline():
    import pandas as pd
    return pd.DataFrame(columns=RAW_COLUMNS)


def normalize_kline(df):
    """列选择 + 类型收口。**宽容但不猜** —— 缺列会 KeyError，那正是我们想要的。"""
    import pandas as pd
    if df is None or len(df) == 0:
        return empty_kline()
    out = df[list(RAW_COLUMNS)].copy()
    out["date"] = as_date(out["date"])
    for c in RAW_COLUMNS[1:]:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype(float)
    return out.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


def normalize_factor(df):
    import pandas as pd
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=FACTOR_COLUMNS)
    out = df[list(FACTOR_COLUMNS)].copy()
    out["date"] = as_date(out["date"])
    out["qfq_factor"] = pd.to_numeric(out["qfq_factor"], errors="coerce").astype(float)
    return out.dropna(subset=["qfq_factor"]).sort_values("date") \
        .drop_duplicates("date", keep="last").reset_index(drop=True)