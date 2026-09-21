"""自选股池的读写（Web UI 与批处理共用，见 ADR-016）。

股票池来源优先级：
    config/watchlist.local.yaml   <- UI「替换股票池」写进去的；存在且非空则优先
    config/settings.yaml          <- 仓库自带的默认值（10 只样本股，与 MAX_STOCKS 对齐）

这样 UI 改池子不会污染版本库里的 settings.yaml，想恢复默认删掉 local 文件即可。
"""
import re
from pathlib import Path

import yaml

SETTINGS_PATH = Path("config/settings.yaml")
LOCAL_PATH = Path("config/watchlist.local.yaml")
MAX_STOCKS = 10
DEFAULT_POOL_NAME = "自选股池"
MAX_NAME_LEN = 24

_HEADER = """# 自选股池 —— 由 Web UI「替换股票池」生成，不进版本库
# 想恢复仓库默认值：删掉本文件，或在 UI 上点「恢复默认」
# 详见 docs/技术决策记录.md 的 ADR-016
"""


def load_default():
    """仓库自带的默认池（config/settings.yaml 的 watchlist）。"""
    if not SETTINGS_PATH.exists():
        return []
    cfg = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
    return [str(c).strip() for c in (cfg.get("watchlist") or []) if str(c).strip()]


def _read_local_cfg():
    """读 local 文件的完整配置（含 name）。不存在或坏掉返回 None。"""
    if not LOCAL_PATH.exists():
        return None
    try:
        cfg = yaml.safe_load(LOCAL_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    return cfg if isinstance(cfg, dict) else None


def _read_settings():
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _read_local():
    cfg = _read_local_cfg()
    if cfg is None:
        return None
    codes = [str(c).strip() for c in (cfg.get("watchlist") or []) if str(c).strip()]
    return codes or None


def load_pool_name():
    """当前池名称：local 的 name 优先，其次 settings 的 pool_name，最后默认值。"""
    cfg = _read_local_cfg()
    if cfg and str(cfg.get("name") or "").strip():
        return str(cfg["name"]).strip()[:MAX_NAME_LEN]
    nm = str(_read_settings().get("pool_name") or "").strip()
    return nm[:MAX_NAME_LEN] if nm else DEFAULT_POOL_NAME


def normalize_name(name):
    """池名称清洗：去空白、限长；空则回落默认名。"""
    nm = " ".join(str(name or "").split())
    return (nm or DEFAULT_POOL_NAME)[:MAX_NAME_LEN]


def is_custom():
    """当前是否在用自定义池（UI 保存过且内容有效）。"""
    return _read_local() is not None


def load_watchlist():
    """当前生效的股票池。"""
    return _read_local() or load_default()


def save_watchlist(codes, name=None):
    """把股票池（含名称）写到 config/watchlist.local.yaml，返回写入的代码列表。

    注意：**只改代码时必须保留原名称** —— 这个文件是整份重写的，早期版本
    会把用户设的池名称悄悄冲掉。name=None 表示「沿用当前名称」。
    """
    codes = [str(c).strip() for c in codes if str(c).strip()]
    nm = normalize_name(name) if name is not None else load_pool_name()
    LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump({"name": nm, "watchlist": codes},
                          allow_unicode=True, default_flow_style=False)
    LOCAL_PATH.write_text(_HEADER + "\n" + body, encoding="utf-8", newline="\n")
    return codes


def save_pool_name(name):
    """只改池名称（连同当前代码一起重写 local 文件）。返回清洗后的名称。"""
    nm = normalize_name(name)
    save_watchlist(load_watchlist(), name=nm)
    return nm


def reset_watchlist():
    """删掉自定义池，回落仓库默认值。删了才算成功，返回 True/False。"""
    if LOCAL_PATH.exists():
        LOCAL_PATH.unlink()
        return True
    return False


def parse_codes(text):
    """把用户输入拆成代码列表（换行 / 逗号 / 空格 / 顿号 / 分号都行），保序去重。"""
    parts = re.split(r"[^0-9A-Za-z]+", str(text or ""))
    out = []
    for p in parts:
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return out


def validate_codes(codes):
    """格式校验。返回 (合格列表, [(代码, 原因), ...])。"""
    ok, bad = [], []
    for c in codes:
        c = str(c).strip()
        if len(c) == 6 and c.isdigit():
            ok.append(c)
        else:
            bad.append((c, "必须是 6 位数字"))
    return ok, bad
