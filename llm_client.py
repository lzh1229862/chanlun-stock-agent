"""T5-2/T5-3 DeepSeek 调用封装：结构化信号 -> 自然语言总结。

输入  结构化 payload（结构 + 信号三日期 + 回测统计 + 过滤情况）
输出  自然语言总结 + 元信息（token / 费用 / 是否命中缓存 / 重试次数）

配置（见下方常量）
    模型      deepseek-flash（ADR-004）
    API key   环境变量 DEEPSEEK_API_KEY
    思考模式  强制 disabled（ADR-004：默认开启会让输出 token 与费用成倍）
    提示词    config/prompts/report_summary.txt —— 与代码分离，改完保存即生效
    缓存      data/llm_cache/{sha256}.json —— 按 输入+模型+模板 的 hash 去重
    用量日志  data/llm_usage.jsonl —— 每次调用追加一行，可统计 token 与费用

运行
    python llm_client.py                # 示例输入演示（含缓存命中对比）
    python llm_client.py --show-prompt  # 只打印渲染后的提示词，不调用模型
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

TIMEOUT = 30.0
MAX_RETRIES = 3
BACKOFF = 2.0
MAX_TOKENS = 2000
MAX_CHARS = 250         # 输出字符数上限
MIN_CHARS = 150         # 输出字符数下限（输入稀疏时如待确认/无回测，过高的下限会逼模型注水）

CACHE_DIR = Path("data/llm_cache")
USAGE_LOG = Path("data/llm_usage.jsonl")
TEMPLATE_PATH = Path("config/prompts/report_summary.txt")

PRICES = {
    "deepseek-flash": {"cache_hit": (0.02, 0.04), "cache_miss": (1.0, 2.0), "output": (4.0, 8.0)},
    "deepseek-v4-pro": {"cache_hit": (0.15, 0.30), "cache_miss": (4.5, 9.0), "output": (13.5, 27.0)},
}

SYSTEM_PROMPT = (
    "你是一名缠论结构讲解员，只负责把程序算出的结构化数据翻译成通顺的中文解读。"
    "你不做买卖判断，不给操作建议，不预测涨跌。"
)


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


# ==================== 提示词模板（与代码分离） ====================

def load_template(path=None):
    """读取提示词模板，返回 {"system": ..., "user": ...}。

    约定：以 === SYSTEM === / === USER === 分节；# 开头的行为注释，不发送给模型。
    每次调用都重新读盘 —— 改完模板保存即生效，无需重启、无需清缓存。
    """
    text = Path(path or TEMPLATE_PATH).read_text(encoding="utf-8")
    sec = {"system": [], "user": []}
    cur = None
    for ln in text.splitlines():
        t = ln.strip()
        if t.startswith("===") and t.strip("=").strip().lower() in ("system", "user"):
            cur = t.strip("=").strip().lower()
            continue
        if t.startswith("#") or cur is None:
            continue
        sec[cur].append(ln)
    return {k: "\n".join(v).strip() for k, v in sec.items()}


def render_template(tpl, variables):
    """把 {{name}} 替换成变量值；返回 (结果, 模板中未提供的变量名列表)。"""
    out = tpl
    for k, v in variables.items():
        out = out.replace("{{" + k + "}}", str(v))
    missing = []
    while "{{" in out:
        i = out.index("{{")
        j = out.index("}}", i)
        missing.append(out[i + 2:j])
        out = out[:i] + "（缺失）" + out[j + 2:]
    return out, sorted(set(missing))


def _fmt_structure(st):
    if not st:
        return "（无结构数据）"
    L = [f"最新交易日 {st.get('last_date')}，最新收盘 {st.get('close')}",
         f"价格位置：{st.get('position')}",
         f"结构规模：{st.get('n_bi')} 笔 / {st.get('n_zs')} 个中枢"]
    z = st.get("zs")
    if z:
        L.append(f"最近中枢：{z['sdt']} ~ {z['edt']}，区间 [{z['zd']}, {z['zg']}]")
    for b in st.get("bis", []):
        L.append(f"笔 {b['sdt']} -> {b['edt']} {b['direction']} {b['low']}~{b['high']}")
    return "\n".join(L)


def _fmt_signals(sigs):
    if not sigs:
        return "（无信号）"
    return "\n".join(
        f"- {x['signal_type']}：信号日 {x['signal_date']}，"
        f"确认日 {x.get('confirm_date') or '尚未确认'}，"
        f"入场参考价 {x.get('entry_ref_price')}，"
        f"{'可交易' if x['is_tradable'] else '不可交易'}，"
        f"{'主信号' if x['is_primary'] else '次信号'}，理由：{x['signal_reason']}"
        for x in sigs)


def _fmt_backtest(bt):
    if not bt:
        return "（无回测数据）"
    return "\n".join(
        f"- {x['signal_type']} {x['window']}日：样本 {x['n']}，"
        f"平均收益 {x['avg_return']:.2%}，胜率 {x['win_rate']:.1%}，"
        f"平均最大回撤 {x['avg_mdd']:.2%}" for x in bt)


def _fmt_filter_note(sigs):
    bad = [x for x in sigs if not x["is_tradable"]]
    if not bad:
        return "全部信号通过 F3 过滤，标记为可交易"
    return "；".join(f"{x['signal_date']} {x['signal_type']} 不可交易"
                    f"（filter_version={x.get('filter_version', '未记录')}）" for x in bad)


def _gap(sd, cd):
    import pandas as pd

    from signal_filter import trading_calendar
    a, b = pd.Timestamp(sd), pd.Timestamp(cd)
    return sum(1 for d in trading_calendar() if a < d <= b)


def template_vars(payload, max_chars=None):
    """把结构化 payload 摊平成模板变量。"""
    sigs = payload.get("signals") or []
    last = sigs[-1] if sigs else {}
    sd, cd = last.get("signal_date"), last.get("confirm_date")
    gap = payload.get("confirm_gap_days")
    if gap is None and sd and cd:
        gap = _gap(sd, cd)
    return {
        "stock_code": payload.get("stock_code", ""),
        "stock_name": payload.get("stock_name", ""),
        "report_date": payload.get("report_date", ""),
        "signal_type": last.get("signal_type", "（无信号）"),
        "signal_date": sd or "（无）",
        "confirm_date": cd or "尚未确认",
        "entry_ref_price": last.get("entry_ref_price", "（无）"),
        "confirm_gap_days": gap if gap is not None else "（待确认）",
        "structure": _fmt_structure(payload.get("structure")),
        "signals": _fmt_signals(sigs),
        "backtest": _fmt_backtest(payload.get("backtest")),
        "filter_note": payload.get("filter_note") or _fmt_filter_note(sigs),
        "max_chars": max_chars or payload.get("max_chars") or MAX_CHARS,
        "min_chars": payload.get("min_chars") or MIN_CHARS,
    }


def build_prompts(payload, path=None, max_chars=None):
    """渲染模板，返回 (system, user, 未提供的变量名列表)。两段都会渲染变量。"""
    tpl = load_template(path)
    v = template_vars(payload, max_chars)
    sys_t, m1 = render_template(tpl["system"], v)
    usr_t, m2 = render_template(tpl["user"], v)
    missing = sorted(set(m1 + m2))
    if missing:
        print(f"    ⚠️ 模板变量缺失: {missing}")
    return (sys_t or SYSTEM_PROMPT), usr_t, missing


def build_prompt(payload, path=None, max_chars=None):
    """结构化 payload -> 用户提示（渲染自外部模板）。"""
    return build_prompts(payload, path, max_chars)[1]


# ==================== 缓存 ====================

def cache_key(payload, model=None):
    """输入 hash：payload + 模型名 + 模板内容。

    把模板也算进 hash —— 改了提示词，缓存自然失效，不会拿旧提示词的结果。
    """
    try:
        tpl = Path(TEMPLATE_PATH).read_text(encoding="utf-8")
    except Exception:
        tpl = ""
    body = json.dumps({"model": model or MODEL, "payload": payload, "template": tpl},
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
        "text": text, "usage": usage, "cost": cost, "prompt_preview": prompt[:200],
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


def call_deepseek(prompt, system=None, model=None, timeout=TIMEOUT,
                  max_retries=MAX_RETRIES, verbose=True):
    """调用 DeepSeek，返回 (text, usage, attempts)。失败抛异常。"""
    import openai

    model = model or MODEL
    key = os.getenv(API_KEY_ENV)
    if not key:
        raise SystemExit(
            f"未找到环境变量 {API_KEY_ENV}。\n"
            f"  临时: set {API_KEY_ENV}=sk-xxxx\n"
            f"  永久: setx {API_KEY_ENV} \"sk-xxxx\"   然后重开终端")

    if system is None:
        try:
            system = load_template()["system"] or SYSTEM_PROMPT
        except Exception:
            system = SYSTEM_PROMPT

    client = openai.OpenAI(api_key=key, base_url=BASE_URL, timeout=timeout)
    retryable = _retryable_types()
    last = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": prompt}],
                max_tokens=MAX_TOKENS,
                extra_body={"thinking": {"type": "disabled"}},
            )
            return resp.choices[0].message.content, normalize_usage(resp.usage), attempt
        except openai.AuthenticationError:
            raise
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


def summarize(payload, use_cache=True, verbose=True, max_chars=None):
    """结构化输入 -> 自然语言总结。

    返回 dict: text / usage / cost / saved / cached / model / attempts / key
    """
    key = cache_key(payload)

    if use_cache:
        hit = cache_get(key)
        if hit:
            if verbose:
                print(f"    缓存命中 {cache_path(key).name}")
            log_usage({"ts": datetime.now().isoformat(timespec="seconds"),
                       "model": hit.get("model", MODEL), "cached": True, "key": key,
                       "cost": 0.0, "saved": hit.get("cost"), "usage": hit.get("usage"),
                       "stock": payload.get("stock_code")})
            return {"text": hit["text"], "usage": hit.get("usage"), "cost": 0.0,
                    "saved": hit.get("cost"), "cached": True,
                    "model": hit.get("model", MODEL), "attempts": 0, "key": key}

    system, prompt, _ = build_prompts(payload, max_chars=max_chars)
    text, usage, attempts = call_deepseek(prompt, system=system, verbose=verbose)
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
    sigs = sorted(query_signals(code=code), key=lambda x: x["signal_date"])[-max_signals:]

    st = stock_structure(code)
    structure = None
    if st:
        structure = {
            "last_date": st["last_date"], "close": round(st["close_raw"], 2),
            "position": st["position"], "n_bi": st["n_bi"], "n_zs": st["n_zs"],
            "zs": ({"sdt": str(st["zs"]["sdt"].date()), "edt": str(st["zs"]["edt"].date()),
                    "zd": round(st["zs"]["zd"], 2), "zg": round(st["zs"]["zg"], 2)}
                   if st["zs"] else None),
            "bis": [{"sdt": str(b.sdt.date()), "edt": str(b.edt.date()),
                     "direction": str(b.direction), "low": round(b.low, 2),
                     "high": round(b.high, 2)} for b in st["bis"]],
        }

    types = {s["signal_type"] for s in sigs}
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
                         "filter_version": s["filter_version"],
                         "signal_reason": s["signal_reason"]} for s in sigs],
            "backtest": bt}


# ==================== 主流程 ====================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="600519")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--no-retry", action="store_true")
    ap.add_argument("--clear-cache", action="store_true")
    ap.add_argument("--usage", action="store_true")
    ap.add_argument("--show-prompt", action="store_true")
    a = ap.parse_args()

    if a.clear_cache:
        print(f"已清空缓存 {clear_cache()} 个文件（目录 {CACHE_DIR}）")
        return
    if a.usage:
        s = usage_summary()
        print(f"=== 用量汇总（{USAGE_LOG}）===")
        print(f"  调用记录 {s['calls']} 次：真实调用 {s.get('real_calls', 0)}，"
              f"缓存命中 {s.get('cache_hits', 0)}")
        t = s.get("tokens") or {}
        print(f"  累计 token：输入 {t.get('prompt', 0)}，输出 {t.get('completion', 0)}，"
              f"合计 {t.get('total', 0)}")
        print(f"  累计费用 ¥{s.get('cost', 0):.6f}   缓存节省 ¥{s.get('saved', 0):.6f}")
        return

    payload = build_payload_from_db(a.code)

    if a.show_prompt:
        tpl = load_template()
        print(f"模板文件 {TEMPLATE_PATH.resolve()}")
        print(f"  system {len(tpl['system'])} 字符 / user {len(tpl['user'])} 字符")
        print("=== 渲染后的 USER 提示 ===")
        print(build_prompt(payload))
        return

    print("=== T5-2/T5-3 DeepSeek 调用封装 ===")
    print(f"模型 {MODEL}   时段 {'高峰' if is_peak() else '空闲'}   "
          f"缓存 {'关闭' if a.no_cache else '开启'}（{CACHE_DIR}）")
    print(f"提示词模板 {TEMPLATE_PATH}")
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
            print(f"  费用: ¥0.000000（命中缓存；原价 ¥{(r['saved'] or 0):.6f}）")
        else:
            print(f"  费用: ¥{r['cost']:.6f}")
        print()
        print("=== 自然语言总结 ===")
        print(r["text"])
        print()
    print(f"缓存文件: {cache_path(cache_key(payload))}")


if __name__ == "__main__":
    main()
