"""T0-3 可行性验证：DeepSeek API 调用（把缠论信号翻译成自然语言）。

运行：
    1) 配置 key:  set DEEPSEEK_API_KEY=sk-xxxxxxxx
    2) python verify_deepseek.py
"""
import os
from datetime import datetime, time

from openai import OpenAI

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
BASE_URL = "https://api.deepseek.com"
MAX_TOKENS = 2000

SIGNAL = "600519 日线出现第一类买点，当前处于中枢下方"

PROMPT = f"""下面是一条程序算出的缠论信号，请用 3 句话向散户解释它的含义和风险。

信号：{SIGNAL}

要求：
- 只解释这个结构本身，不要给出买卖建议，不要预测涨跌
- 结尾注明"非投资建议"
"""

# 单价：元 / 百万 tokens，格式 (空闲时段, 高峰时段)
# 来源 https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
PRICES = {
    "deepseek-flash": {"cache_hit": (0.02, 0.04), "cache_miss": (1.0, 2.0), "output": (4.0, 8.0)},
    "deepseek-v4-pro": {"cache_hit": (0.15, 0.30), "cache_miss": (4.5, 9.0), "output": (13.5, 27.0)},
}


def is_peak(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (time(9, 0) <= t < time(12, 0)) or (time(14, 0) <= t < time(18, 0))


api_key = os.getenv("DEEPSEEK_API_KEY")
if not api_key:
    raise SystemExit(
        "未找到 DEEPSEEK_API_KEY 环境变量。\n"
        "  临时生效(当前窗口) : set DEEPSEEK_API_KEY=sk-xxxxxxxx\n"
        "  永久生效(推荐)     : setx DEEPSEEK_API_KEY \"sk-xxxxxxxx\"   然后重开终端\n"
        "  获取地址           : https://platform.deepseek.com/api_keys"
    )

client = OpenAI(api_key=api_key, base_url=BASE_URL)

now = datetime.now()
slot = 1 if is_peak(now) else 0
print(f"模型       : {MODEL}")
print(f"调用时间   : {now:%Y-%m-%d %H:%M:%S}  当前为{'高峰时段' if slot else '空闲时段'}")
print(f"输入信号   : {SIGNAL}")
print()

resp = client.chat.completions.create(
    model=MODEL,
    messages=[{"role": "user", "content": PROMPT}],
    max_tokens=MAX_TOKENS,
    extra_body={"thinking": {"type": "disabled"}},
)

u = resp.usage
hit = getattr(u, "prompt_cache_hit_tokens", 0) or 0
miss = getattr(u, "prompt_cache_miss_tokens", None)
miss = u.prompt_tokens if miss is None else miss

print("=== 模型返回 ===")
print(resp.choices[0].message.content)
print()
print("=== Token 消耗 ===")
print(f"输入(缓存命中)   : {hit}")
print(f"输入(缓存未命中) : {miss}")
print(f"输出             : {u.completion_tokens}")
print(f"合计             : {u.total_tokens}")
print()

p = PRICES.get(MODEL)
if p:
    cost = (hit * p["cache_hit"][slot] + miss * p["cache_miss"][slot] + u.completion_tokens * p["output"][slot]) / 1_000_000
    other = (hit * p["cache_hit"][1 - slot] + miss * p["cache_miss"][1 - slot] + u.completion_tokens * p["output"][1 - slot]) / 1_000_000
    print("=== 费用估算 ===")
    print(f"本次调用          : ¥{cost:.6f}")
    print(f"  （若在{'空闲' if slot else '高峰'}时段  : ¥{other:.6f}）")
    print(f"每天 10 只股票    : ¥{cost * 10:.4f}      单只 1 次调用")
    print(f"每月 22 个交易日  : ¥{cost * 10 * 22:.3f}")
    print(f"每年 250 个交易日 : ¥{cost * 10 * 250:.2f}")
else:
    print(f"=== 费用估算 === 无 {MODEL} 的内置单价，跳过")
