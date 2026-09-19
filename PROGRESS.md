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
- **报告**：report_builder.py 生成 reports/YYYY-MM-DD/report.md（规则生成；三日期+时效提示+过滤说明+回测参考；未接 LLM）
- **LLM**：report_builder 已接入 DeepSeek（仅对有可交易信号的股票调用；失败降级为纯规则报告；--no-llm 可关闭）；提示词在 config/prompts/report_summary.txt
- **归档**：报告落 reports/YYYY-MM-DD/（report.md + signals.csv + meta.json），三日期写入 SQLite reports 表（ADR-013）
- **编排**：agent_graph.py 线性 6 节点（fetch/validate/chan/filter/backtest/report）+ 4 条条件分支（B1~B4）；未用 langgraph，节点签名 state->dict 可直接接入
- **入口**：main.py（PRD F7.3）支持 --date / --stocks / --no-llm / --nodes / --engine；运行日志落 logs/YYYY-MM-DD.json
- **量能过滤**：F3.4 三条可选规则（susp/illiquid/volspike），默认关；配置在 config/settings.yaml
- **定时任务**：scripts/install_schedule.bat 注册 ChanAgentDaily（周一至周五 18:05，非交易日自动跳过）
- **Web UI**：app.py（Streamlit）搜索/分析/历史优先/K线标注/AI 按需；双击 run_ui.bat 启动 http://localhost:8501
- **公共分析函数**：analyzer.py 提供 analyze_stock / load_from_db / generate_llm_summary / load_ohlc，CLI 与 UI 共用
- **一键流程**：`python run_round3.py`，约 1 秒；重复运行不重复拉取、不重复插入

## 已知待办

- [x] ~~is_tradable 全为 1，等 F3 过滤后回填~~ → T4-1 实现过滤，T4-2 已回填（filter_version=v1_f3）
- [x] ~~同日多重信号多行存储，回测需双口径统计~~ -> T4-4 双口径回测已落地（signal / day 两个 scope）
- [x] ~~需定义同日多信号优先级规则~~ → T4-3 已定义（ADR-010）：类型分 三类>二类>一类，同分卖点优先

> 另有规则校准类待办（信号映射不确定点、中枢取法遗留、价格/因子来源待评估等），
> 散见于 `docs/技术决策记录.md` 各 ADR 的「遗留 / 待评估」小节。
