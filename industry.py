"""行业分类（巨潮 中证行业分类，四级）—— 给扫描结果补行业，判断池子是不是分散的。

为什么需要它：全市场扫出 784 只买点，光看数字不知道是「普遍活跃」还是
「一个主题的几百只」。集中度必须拿**全市场当基准**才算得出来，
所以这里存的是**全市场映射**，不只是命中的那几百只。

### 源的选择（本机实测，2026-09-22）

| 源 | 结论 |
| --- | --- |
| **巨潮 中证行业分类**（本模块） | ✓ `stock_industry_change_cninfo` 给四级 `门类/次类/大类/中类`，
|  |   如 `信息技术 / 半导体 / 集成电路 / 集成电路制造`。~0.5s/只，逐只取 |
| 申万 `sw_index_third_info` 等 | ✗ 三个 info 接口**同时**变成 `NoneType.find_all`（HTML 结构变了或被限流），
|  |   连试两次都是固定 5.4s 超时；`sw_index_third_cons` 单个接口还能用，但拿不到行业清单 |
| 东财 `stock_board_industry_name_em` | ✗ 把**全层级混在一个列表**里（一级「半导体」和三级「数字芯片设计」并列），
|  |   逐板块取成分再 last-write-wins，会让每只落在哪一级**取决于遍历顺序** |
| 新浪 `stock_sector_spot` | ✗ 只覆盖 3035 只（60%），且含「次新股」「其它行业」伪板块 |

### 失败语义（照抄 ADR-022 那条教训）

- **抛异常（网络问题）** -> 不记录 -> 下次会重试
- **返回 None（源里就没有）** -> 记空串 -> 这是终局答案，不再重试

把两者混为一谈，就会出现「一次网络抖动被当成查过了」。

缓存 30 天（与 `fundamentals.PROFILE_TTL_DAYS` 同口径）。
"""
import json
import time
from pathlib import Path

CACHE = Path("data/industry.json")
TTL_DAYS = 30
SOURCE = "巨潮 中证行业分类"
# 只能串行：akshare 取巨潮用的是 py_mini_racer（内嵌 V8），它在多线程下初始化会
# 直接把进程崩掉 —— 实测 `FATAL: Check failed: !IsConfigurablePoolInitialized()`，
# 8 线程一开就死。所以这里固定单线程，靠**边取边落盘 + 断点续跑**分多次跑完。
WORKERS = 1
LEVELS = ("门类", "次类", "大类", "中类")


def norm(code):
    return str(code).split(".")[0].strip().zfill(6)


def split(path):
    """'信息技术 / 半导体 / 集成电路 / 集成电路制造' -> 四元组，缺的补空串。"""
    parts = [p.strip() for p in str(path or "").split("/") if p.strip()]
    return tuple((parts + [""] * 4)[:4])


def load_map():
    try:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        return d.get("map") or {}
    except Exception:
        return {}


def save(mapping):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(
        {"built_at": time.strftime("%Y-%m-%d %H:%M:%S"), "source": SOURCE,
         "n": len(mapping), "map": mapping}, ensure_ascii=False), encoding="utf-8")
    return CACHE


def fetch_one(code):
    """成功返回字符串，源里没有返回空串，网络问题**抛异常**。"""
    import fundamentals as fd
    return fd.fetch_industry_cninfo(code) or ""


def build(codes, workers=WORKERS, verbose=True, progress=200):
    """线程池逐只取，**边取边落盘**，所以被打断也能续跑。返回最终映射。"""
    from storage_fundamental import load_profile

    m = load_map()
    # 已有的 profiles 行可以直接当种子，省掉重复请求
    for c in codes:
        c = norm(c)
        if c in m:
            continue
        try:
            p = load_profile(c) or {}
            if p.get("industry_cninfo"):
                m[c] = p["industry_cninfo"]
        except Exception:
            pass

    todo = [norm(c) for c in codes if norm(c) not in m]
    if verbose:
        print("    已缓存 %d 只，本次要取 %d 只（%d 线程）" % (len(m), len(todo), workers))
    if not todo:
        return m

    ok = fail = 0
    t0 = time.time()
    for i, code in enumerate(todo, 1):
        try:
            m[code] = fetch_one(code)
            ok += 1
        except Exception:
            fail += 1                     # 网络问题：不记录，下次会重试
        if i % progress == 0:
            save(m)
            sp = i / max(time.time() - t0, 1e-6)
            if verbose:
                print("    %5d/%d  成功 %d 失败 %d  %.2f 只/秒  预计还要 %.1f 分钟"
                      % (i, len(todo), ok, fail, sp, (len(todo) - i) / max(sp, 1e-6) / 60))
    save(m)
    if verbose:
        print("    本轮完成：成功 %d，失败 %d（失败的不记录，下次重试）；已缓存 %d 只"
              % (ok, fail, len(m)))
    return m


def load(force=False, codes=None, verbose=False):
    """读缓存；过期或 force 时重建。取不到就返回已有的（可能是空的）。"""
    if CACHE.exists() and not force:
        try:
            if time.time() - CACHE.stat().st_mtime < TTL_DAYS * 86400:
                return load_map()
        except OSError:
            pass
    if codes is None:
        try:
            import market_scan as ms
            codes = [c for c, _ in ms.universe()]
        except Exception:
            codes = []
    if not codes:
        return load_map()
    return build(codes, verbose=verbose)


def level_of(code, level="次类", mapping=None):
    m = load_map() if mapping is None else mapping
    p = split(m.get(norm(code)))
    return p[LEVELS.index(level)] if level in LEVELS else ""


def concentration(hit_codes, universe_codes, level="次类", mapping=None, min_hits=3):
    """命中的行业分布 vs 全市场基准。

    `lift = 命中占比 / 全市场占比`：lift=1 说明这个行业的命中率就是平均水
    平，lift=3 说明是基准的 3 倍。**这才是「集中」的可比定义** ——
    光看命中数会被大行业天然占优误导。

    返回按 lift 降序的 list，只保留命中 >= min_hits 的行业。
    """
    from collections import Counter

    m = load_map() if mapping is None else mapping
    if not m:
        return []
    idx = LEVELS.index(level)
    hit, base = Counter(), Counter()
    for c in hit_codes:
        v = split(m.get(norm(c)))[idx]
        if v:
            hit[v] += 1
    for c in universe_codes:
        v = split(m.get(norm(c)))[idx]
        if v:
            base[v] += 1
    nh, nb = sum(hit.values()), sum(base.values())
    if not nh or not nb:
        return []
    out = []
    for ind, n in hit.items():
        if n < min_hits:
            continue
        b = base.get(ind, 0)
        out.append({"industry": ind, "hits": n, "base": b,
                    "hits_share": n / nh, "base_share": (b / nb) if b else 0.0,
                    "lift": (n / nh) / (b / nb) if b else None})
    out.sort(key=lambda x: (-(x["lift"] or 0), -x["hits"]))
    return out