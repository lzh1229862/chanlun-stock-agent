# 项目进度（缠论股票分析 Agent）

> 需求见 [`docs/PRD.md`](docs/PRD.md)，关键技术决策见 [`docs/技术决策记录.md`](docs/技术决策记录.md)。
> 最后更新：2026-09-18

## 已完成

| 轮次 | 任务 | 产物 |
| --- | --- | --- |
| 第 1 轮 | 可行性验证 | `verify_akshare.py` / `verify_czsc.py` / `verify_deepseek.py` / `calib_zs.py` |
| 第 2 轮 | 最小闭环 | `min_loop.py`（统一输出格式 + 信号映射） |
| 第 3 轮 | 加存储 | `storage_kline.py` / `storage_signal.py` / `run_round3.py` |

## 当前状态

- **数据**：600519 两年日线 487 行，存 `data/raw/600519.parquet`（不复权 + 复权因子，见 ADR-002）
- **信号**：16 条写入 `data/chan_agent.db` 的 `signals` 表（同日多信号已按信号组归集）
- **一键流程**：`python run_round3.py`，约 1 秒；重复运行不重复拉取、不重复插入

## 已知待办

- [ ] is_tradable 全为 1，等 F3 过滤后回填
- [ ] 同日多重信号多行存储，回测需双口径统计
- [ ] 需定义同日多信号优先级规则

> 另有规则校准类待办（信号映射不确定点、中枢取法遗留、价格/因子来源待评估等），
> 散见于 `docs/技术决策记录.md` 各 ADR 的「遗留 / 待评估」小节。
