"""T5-2 DeepSeek 调用封装：结构化信号 -> 自然语言总结。

输入  结构化 payload（结构 + 信号三日期 + 回测统计）
输出  自然语言总结 + 元信息（token / 费用 / 是否命中缓存 / 重试次数）

配置（见下方常量）
    模型      deepseek-flash（ADR-004）
    API key   环境变量 DEEPSEEK_API_KEY
    思考模式  强制 disabled（ADR-004：默认开启会让输出 token 与费用成倍）
    缓存      data/llm_cache/{sha256}.json —— 按输入 hash 去重
    用量日志  data/llm_usage.jsonl —— 每次调用追加一行，可统计 token 与费用

运行
    python llm_client.py                # 示例输入演示（含缓存命中对比）
    python llm_client.py --no-cache     # 跳过缓存读写
    python llm_client.py --clear-cache  # 清空缓存
    python llm_client.py --usage        # 查看用量日志汇总
"""
import argparse
import hashlib
import json
import os
import shutil
import time
from datetime import datetime
from datetime import time as dtime
from pathlib import Path

# ==================== 配置 ====================

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
API_KEY_ENV = "DEEPSEEK_API_KEY"

TIMEOUT = 30.0          # 单次请求超时（秒）
MAX_RETRIES = 3         # 最多尝试 3 次
BACKOFF = 2.0           # 重试间隔基数（秒），第 n 次等待 BACKOFF * n
MAX_TOKENS = 2000

CACHE_DIR = Path("data/llm_cache")
USAGE_LOG = Path("data/llm_usage.jsonl")

# 单价：元 / 百万 tokens，(空闲时段, 高峰时段)。来源 docs/技术决策记录.md ADR-004
PRICES = {
    "deepseek-flash": {"cache_hit": (0.02, 0.04), "cache_miss": (1.0, 2.0), "output": (4.0, 8.0)},
    "deepseek-v4-pro": {"cache_hit": (0.15, 0.30), "cache_miss": (4.5, 9.0), "output": (13.5, 27.0)},
}

SYSTEM_PROMPT = (
    "你是一名缠论结构讲解员，只负责把程序算出的结构化数据翻译成通顺的中文解读。"
    "你不做买卖判断，不给操作建议，不预测涨跌。"
)


# ==================== 计费 ====================

def is_peak(now=None):
    """高峰时段 = 北京时间周一至周五 9:00-12:00、14:00-18:00（ADR-004）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (dtime(9, 0) <= t < dtime(12, 0)) or (dtime(14, 0) <= t < dtime(18, 0))


def cost_of(model, usage, slot):
    p = PRICES.get(model)
    if not p:
        return None
    return (usage["cache_hit"] * p["cache_hit"][slot]
            + usage["cache_miss"] * p["cache_miss"][slot]
            + usage["completion_tokens"] * p["output"][slot]) / 1_000_000


def normalize_usage(u):
    hit = getattr(u, "prompt_cache_hit_tokens", 0) or 0
    miss = getattr(u, "prompt_cache_miss_tokens", None)
    if miss is None:
        miss = (getattr(u, "prompt_tokens", 0) or 0) - hit
    return {"prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
            "total_tokens": getattr(u, "total_tokens", 0) or 0,
            "cache_hit": hit, "cache_miss": miss}


# ==================== 提示词（无模板系统，直接拼接） ====================

def build_prompt(payload):
    L = [f"股票：{payload.get('stock_code')} {payload.get('stock_name', '')}",
         f"报告日期：{payload.get('report_date')}"]

    st = payload.get("structure") or {}
    if st:
        L += ["", "【当前结构】",
              f"- 最新交易日 {st.get('last_date')}，最新收盘 {st.get('close')}",
              f"- 价格位置：{st.get('position')}"]
        z = st.get("zs")
        if z:
            L.append(f"- 最近中枢：{z['sdt']} ~ {z['edt']}，区间 [{z['zd']}, {z['zg']}]")
        for b in st.get("bis", []):
            L.append(f"- 笔 {b['sdt']} -> {b['edt']} {b['direction']} {b['low']}~{b['high']}")

    L += ["", "【信号（含三日期）】"]
    for s in payload.get("signals", []):
        L.append(f"- 信号日 {s['signal_date']} / 确认日 {s.get('confirm_date') or '尚未确认'} / "
                 f"入场参考价 {s.get('entry_ref_price')} / 类型 {s['signal_type']} / "
                 f"可交易 {'是' if s['is_tradable'] else '否'} / "
                 f"主信号 {'是' if s['is_primary'] else '否'} / 理由 {s['signal_reason']}")

    L += ["", "【回测统计】口径：入场 = 确认日次一交易日开盘"]
    for b in payload.get("backtest", []):
        L.append(f"- {b['signal_type']} {b['window']}日：样本 {b['n']}，"
                 f"平均收益 {b['avg_return']:.2%}，胜率 {b['win_rate']:.1%}，"
                 f"平均最大回撤 {b['avg_mdd']:.2%}")

    L += ["", "输出要求：",
          "1. 用 150~250 字解读上述结构与信号，只解释含义，不给买卖建议、不预测涨跌。",
          "2. 必须点明信号的【信号日】与【确认日】，并说明两者可能相差 1~2 个交易日"
          "（缠论「笔」需后续 K 线确认）；若确认日为「尚未确认」，必须说明该信号暂不可作为操作依据。",
          "3. 回测样本少于 30 时，必须提示统计值不具解释力。",
          "4. 最后单独一行写：非投资建议。"]
    return "\n".join(L)


# ==================== 缓存 ====================

def cache_key(payload, model=None):
    """输入 hash：payload 规范化 JSON + 模型名。"""
    body = json.dumps({"model": model or MODEL, "payload": payload},
                      ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def cache_path(key):
    return CACHE_DIR / f"{key}.json"


def cache_get(key):
    p = cache_path(key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def cache_put(key, payload, text, usage, cost, prompt):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path(key).write_text(json.dumps({
        "key": key, "model": MODEL, "created_at": datetime.now().isoformat(timespec="seconds"),
        "text": text, "usage": usage, "cost": cost,
        "prompt_preview": prompt[:200],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_cache():
    if CACHE_DIR.exists():
        n = len(list(CACHE_DIR.glob("*.json")))
        shutil.rmtree(CACHE_DIR)
        return n
    return 0


# ==================== 调用 ====================

def _retryable_types():
    import openai
    names = ["APIConnectionError", "APITimeoutError", "RateLimitError",
             "InternalServerError", "APIStatusError"]
    return tuple(t for t in (getattr(openai, n, None) for n in names) if t)


def call_deepseek(prompt, model=None, timeout=TIMEOUT, max_retries=MAX_RETRIES, verbose=True):
    """调用 DeepSeek，返回 (text, usage, attempts)。失败抛异常。"""
    import openai

    model = model or MODEL
    key = os.getenv(API_KEY_ENV)
    if not key:
        raise SystemExit(
            f"未找到环境变量 {API_KEY_ENV}。\n"
            f"  临时: set {API_KEY_ENV}=sk-xxxx\n"
            f"  永久: setx {API_KEY_ENV} \"sk-xxxx\"   然后重开终端")

    client = openai.OpenAI(api_key=key, base_url=BASE_URL, timeout=timeout)
    retryable = _retryable_types()
    last = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": prompt}],
                max_tokens=MAX_TOKENS,
                extra_body={"thinking": {"type": "disabled"}},   # ADR-004：必须关闭
            )
            return resp.choices[0].message.content, normalize_usage(resp.usage), attempt
        except openai.AuthenticationError:
            raise                                            # 认证失败不重试
        except retryable as e:
            last = e
            if verbose:
                print(f"    第 {attempt}/{max_retries} 次失败: {type(e).__name__}: {str(e)[:80]}")
            if attempt < max_retries:
                time.sleep(BACKOFF * attempt)
    raise last


def log_usage(record):
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with USAGE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def summarize(payload, use_cache=True, verbose=True):
    """结构化输入 -> 自然语言总结。

    返回 dict: text / usage / cost / saved / cached / model / attempts / key
    """
    init_needed = True
    key = cache_key(payload)

    if use_cache:
        hit = cache_get(key)
        if hit:
            if verbose:
                print(f"    缓存命中 {cache_path(key).name}")
            rec = {"ts": datetime.now().isoformat(timespec="seconds"), "model": hit.get("model", MODEL),
                   "cached": True, "key": key, "cost": 0.0, "saved": hit.get("cost"),
                   "usage": hit.get("usage"), "stock": payload.get("stock_code")}
            log_usage(rec)
            return {"text": hit["text"], "usage": hit.get("usage"), "cost": 0.0,
                    "saved": hit.get("cost"), "cached": True, "model": hit.get("model", MODEL),
                    "attempts": 0, "key": key}

    prompt = build_prompt(payload)
    text, usage, attempts = call_deepseek(prompt, verbose=verbose)
    slot = 1 if is_peak() else 0
    cost = cost_of(MODEL, usage, slot)

    if use_cache:
        cache_put(key, payload, text, usage, cost, prompt)

    log_usage({"ts": datetime.now().isoformat(timespec="seconds"), "model": MODEL,
               "cached": False, "key": key, "cost": cost, "usage": usage,
               "slot": "高峰" if slot else "空闲", "attempts": attempts,
               "prompt_chars": len(prompt), "stock": payload.get("stock_code")})

    return {"text": text, "usage": usage, "cost": cost, "saved": None,
            "cached": False, "model": MODEL, "attempts": attempts, "key": key}


def usage_summary():
    if not USAGE_LOG.exists():
        return {"calls": 0}
    rows = [json.loads(l) for l in USAGE_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    real = [r for r in rows if not r.get("cached")]
    tok = {"prompt": 0, "completion": 0, "total": 0}
    for r in real:
        u = r.get("usage") or {}
        tok["prompt"] += u.get("prompt_tokens", 0)
        tok["completion"] += u.get("completion_tokens", 0)
        tok["total"] += u.get("total_tokens", 0)
    return {"calls": len(rows), "real_calls": len(real), "cache_hits": len(rows) - len(real),
            "tokens": tok, "cost": sum(r.get("cost") or 0 for r in real),
            "saved": sum(r.get("saved") or 0 for r in rows if r.get("cached"))}


# ==================== 从库组装示例输入 ====================

def build_payload_from_db(code="600519", report_date=None, max_signals=5):
    from collections import defaultdict

    from backtest import query_backtest
    from report_builder import stock_structure
    from signal_filter import fetch_name
    from storage_signal import query_signals

    report_date = str(report_date or datetime.now().date())
    sigs = [s for s in query_signals(code=code)]
    sigs.sort(key=lambda x: x["signal_date"])
    show = sigs[-max_signals:]

    st = stock_structure(code)
    structure = None
    if st:
        structure = {
            "last_date": st["last_date"], "close": round(st["close_raw"], 2),
            "position": st["position"], "n_bi": st["n_bi"], "n_zs": st["n_zs"],
            "zs": ({"sdt": str(st["zs"]["sdt"].date()), "edt": str(st["zs"]["edt"].date()),
                    "zd": round(st["zs"]["zd"], 2), "zg": round(st["zs"]["zg"], 2)}
                   if st["zs"] else None),
            "bis": [{"sdt": str(b.sdt.date()), "edt": str(b.edt.date()), "direction": str(b.direction),
                     "low": round(b.low, 2), "high": round(b.high, 2)} for b in st["bis"]],
        }

    types = {s["signal_type"] for s in show}
    agg = defaultdict(list)
    for r in query_backtest(scope="signal"):
        if r["signal_type"] in types:
            agg[(r["signal_type"], r["window"])].append(r)
    bt = []
    for (t, w), v in sorted(agg.items()):
        rets = [x["return_pct"] for x in v]
        bt.append({"signal_type": t, "window": w, "n": len(v),
                   "avg_return": sum(rets) / len(rets),
                   "win_rate": sum(x["is_win"] for x in v) / len(v),
                   "avg_mdd": sum(x["max_drawdown"] for x in v) / len(v)})

    return {"stock_code": code, "stock_name": fetch_name(code), "report_date": report_date,
            "structure": structure,
            "signals": [{"signal_date": s["signal_date"], "confirm_date": s["confirm_date"],
                         "entry_ref_price": s["entry_ref_price"], "signal_type": s["signal_type"],
                         "is_tradable": s["is_tradable"], "is_primary": s["is_primary"],
                         "signal_reason": s["signal_reason"]} for s in show],
            "backtest": bt}


# ==================== 主流程 ====================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="600519")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--no-retry", action="store_true")
    ap.add_argument("--clear-cache", action="store_true")
    ap.add_argument("--usage", action="store_true")
    a = ap.parse_args()

    if a.clear_cache:
        print(f"已清空缓存 {clear_cache()} 个文件（目录 {CACHE_DIR}）")
        return
    if a.usage:
        s = usage_summary()
        print(f"=== 用量汇总（{USAGE_LOG}）===")
        print(f"  调用记录 {s['calls']} 次：真实调用 {s.get('real_calls', 0)}，缓存命中 {s.get('cache_hits', 0)}")
        t = s.get("tokens") or {}
        print(f"  累计 token：输入 {t.get('prompt', 0)}，输出 {t.get('completion', 0)}，合计 {t.get('total', 0)}")
        print(f"  累计费用 ¥{s.get('cost', 0):.6f}   缓存节省 ¥{s.get('saved', 0):.6f}")
        return

    payload = build_payload_from_db(a.code)
    print(f"=== T5-2 DeepSeek 调用封装 ===")
    print(f"模型 {MODEL}   时段 {'高峰' if is_peak() else '空闲'}   缓存 {'关闭' if a.no_cache else '开启'}"
          f"   （缓存目录 {CACHE_DIR}）")
    print(f"输入：股票 {payload['stock_code']} 信号 {len(payload['signals'])} 条 "
          f"回测行 {len(payload['backtest'])} 条")
    print()

    if a.no_retry:
        global MAX_RETRIES
        MAX_RETRIES = 1

    for i in (1, 2):
        print(f"--- 第 {i} 次调用 ---")
        t0 = time.perf_counter()
        r = summarize(payload, use_cache=not a.no_cache)
        dt = time.perf_counter() - t0
        print(f"  缓存命中: {'是' if r['cached'] else '否'}   重试次数: {r['attempts']}   耗时 {dt:.2f}s")
        u = r["usage"] or {}
        print(f"  token: 输入(命中/未命中) {u.get('cache_hit', 0)}/{u.get('cache_miss', 0)}"
              f"   输出 {u.get('completion_tokens', 0)}   合计 {u.get('total_tokens', 0)}")
        if r["cached"]:
            print(f"  费用: ¥0.000000（命中缓存，本次未产生费用；原价 ¥{(r['saved'] or 0):.6f}）")
        else:
            print(f"  费用: ¥{r['cost']:.6f}")
        print()
        print("=== 自然语言总结 ===")
        print(r["text"])
        print()
    print(f"缓存文件: {cache_path(cache_key(payload))}")


if __name__ == "__main__":
    main()
