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
- **过滤**：F3 过滤已实现（ST / 次新 / 涨跌停），历史信号已回填，filter_version=v1_f3
- **回测**：双口径（按信号 / 按交易日）x 5/10/20 日，结果在 backtest 表；入场按确认日次一交易日开盘（ADR-011）
- **三日期**：signals 表已加 confirm_date / entry_ref_price / backfill_note，历史 16 条已回填（ADR-012）
- **一键流程**：`python run_round3.py`，约 1 秒；重复运行不重复拉取、不重复插入

## 已知待办

- [x] ~~is_tradable 全为 1，等 F3 过滤后回填~~ → T4-1 实现过滤，T4-2 已回填（filter_version=v1_f3）
- [x] ~~同日多重信号多行存储，回测需双口径统计~~ -> T4-4 双口径回测已落地（signal / day 两个 scope）
- [x] ~~需定义同日多信号优先级规则~~ → T4-3 已定义（ADR-010）：类型分 三类>二类>一类，同分卖点优先

> 另有规则校准类待办（信号映射不确定点、中枢取法遗留、价格/因子来源待评估等），
> 散见于 `docs/技术决策记录.md` 各 ADR 的「遗留 / 待评估」小节。
