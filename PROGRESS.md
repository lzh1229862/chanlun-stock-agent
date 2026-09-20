# 项目进度（缠论股票分析 Agent）

> 需求见 [`docs/PRD.md`](docs/PRD.md)，关键技术决策见 [`docs/技术决策记录.md`](docs/技术决策记录.md)。
> 最后更新：2026-09-20

## 已完成

| 轮次 | 任务 | 产物 |
| --- | --- | --- |
| 第 1 轮 | 可行性验证 | `verify_akshare.py` / `verify_czsc.py` / `verify_deepseek.py` / `calib_zs.py` |
| 第 2 轮 | 最小闭环 | `min_loop.py`（统一输出格式 + 信号映射） |
| 第 3 轮 | 加存储 | `storage_kline.py` / `storage_signal.py` / `run_round3.py` |
| 第 4~6 轮 | 过滤 / 回测 / 报告 / LLM / 编排 / 定时任务 | `signal_filter.py` / `backtest.py` / `report_builder.py` / `llm_client.py` / `agent_graph.py` / `main.py` |
| 第 7 轮 | Web UI + 公共分析函数 + 数据兜底 | `app.py` / `run_ui.bat` / `analyzer.py` / `verify_fallback.py` |
| 第 8 轮 | 科技风双皮肤界面（含 run_ui 启动修复） | `skin.py` / `.streamlit/config.toml` / `docs/design/`（静态稿） |
| 第 9 轮 | 基本面字段用起来（行业分类 / 财务摘要 / 估值快照） | `fundamentals.py` / `storage_fundamental.py`（ADR-017） |
| 第 10 轮 | 有效性对照工具：基准 / 成本 / 置信区间 / 分布 | `verify_edge.py`（T10-1） |

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
- **修复**：`.streamlit/config.toml` 里误留的 `headless = true` 会让 `run_ui.bat` 只打印地址、不打开浏览器
  （表现为"双击 run_ui.bat 没反应"）；已改为 `headless = false`，并用打桩实测确认 Streamlit 确实调用了
  `webbrowser.open`（`true` 时压根不调）。排查三步写进 README「配置」一节
- **双皮肤界面**（T8）：`skin.py` 提供白天 / 夜晚两套配色令牌 + 全局样式；侧边栏左下角 🌙/☀️ 开关切换，
  靠 `.stApp:has(开关:checked)` 纯 CSS 联动（不弹窗、不闪白），会话内记忆；
  图表色随皮肤切换（`skin.chart_colors`）；原生组件基调由 `.streamlit/config.toml` 的 `[theme]` 定；
  版式统一成「头部带 + 读数卡 + 01/02/03 编号分节 + 等宽数字」；离线单测 +4 项（共 27 项）守住令牌完整性
- **基本面**（T9 / ADR-017）：`fundamentals.py` 取行业分类（中证四级）+ 财务摘要（近 6 期）+ 估值快照
  （PE / PB / 市值 / 换手率），缓存进 `storage_fundamental.py` 的 profiles / fundamentals 两表；
  数据源走巨潮 + 同花顺 + 腾讯，**全部绕开东财**（东财在本机被服务端重置，同 ADR-001）；
  腾讯估值来自 `fetch_quote` 缓存的整包行情（88 字段），**不额外联网**，字段下标用财务数据反算校验过；
  单股页新增「02 公司基本面」分节，报告每只股票加一段，LLM 提示词加 `{{fundamentals}}` + 硬约束 #9；
  **不进过滤与回测**（财报披露滞后，会造成前视偏差）；离线单测 +16 项（共 43 项）
- **有效性对照**（T10-1 / `verify_edge.py`）：回答「信号比同一只股票随便哪天入场好多少」——
  输出**超额收益**（信号 − 同股票同期无条件基准）+ **扣费净值**（佣金/印花税/过户费/滑点，5 日往返约 0.2%）
  + **置信区间**（均值 t 区间、胜率 Wilson 区间）+ **收益分布**（盈亏比 / 最大连亏 / 最差 5%）；
  **不改任何分析逻辑**，复用 `backtest.evaluate` 保证入场口径一致；离线单测 +11 项（共 54 项）
  - 首次实测结论：**12 只样本股的 5 日无条件平均收益本身就是 +0.12%~+1.49%** —— 原先看到的
    「信号后 +0.96%」里一大半是 beta 不是 alpha；扣掉基准后多数类型的超额置信区间**跨 0**
    （统计上分不清是规则还是运气，n 只有 21~43）；**第三类卖点**在 10/20 日超额仅 +0.15%/+0.23%，
    接近随机；**第一类卖点**超额最稳（+1.18% / +1.94% / +3.22% 且 10/20 日 CI 不跨 0）
  - **表 C 样本外切分**（`--split 0.6`）：按入场日切观察期/验证期，基准在各期自己跨度内重算 ——
    5 日窗口下**「两段一致显著」0 个组合、「方向相反」1 个**（第三类卖点：观察期 +0.11% / 验证期 −0.53%）
  - **表 D 时间分块**（`--blocks 4`）：买点超额 +0.49% / +0.38% / +2.19% / **+0.05%**（波动 40 倍）；
    卖点第 2 段超额为负（−0.43%，信号说跌结果涨）。**典型陷阱**：买点第 4 段「扣费后 +1.05%」看着不错，
    但同段基准 +1.20% —— 随机入场拿得更多，超额只有 +0.05%
  - ⚠️ 尚未做真正的市场状态分层（用大盘趋势/波动率分段）；`--blocks` 只是等分时间
- **公共分析函数**：analyzer.py 提供 analyze_stock / load_from_db / generate_llm_summary / load_ohlc，CLI 与 UI 共用
- **一键流程**：`python run_round3.py`，约 1 秒；重复运行不重复拉取、不重复插入
- **数据兜底**（T7-1b / ADR-015）：`analyzer.ensure_kline` 统一取数 —— 本地最新则走缓存（0 次网络请求）、缺失或过期则自动增量拉取、失败则返回结构化错误不抛异常；
  `analyze_stock` / `load_from_db` 结果新增 `data_source`（cache / fetched / None）与 `data_error`；
  验收脚本 `verify_fallback.py`（22 项断言，`--offline` 可跳过联网用例）；`analyze_stock` 改为永不抛异常（薄包装 + `_analyze_impl`）
- **CI**：`.github/workflows/ci.yml`（GitHub Actions，ubuntu-latest + Python 3.12）—— 装依赖 → `compileall` 语法检查 → 离线单测；
  `tests/test_offline.py` 22 项断言、0.4 秒、不联网（所有出网口打桩），README 顶部挂 CI 徽章
- **自选股与日报**（T8 / ADR-016）：Web UI 加模式切换 —— 「自选股与日报」页可编辑股票池（上限 10 只，试拉校验 + 预热，两步确认）；
  池子写 `config/watchlist.local.yaml`（不进版本库，local 优先于 settings.yaml），`watchlist_store.py` 为唯一读写入口；
  同页下半部分列出 `reports/` 所有日报并可点开渲染 `report.md` / 下载 `signals.csv`

## 已知待办

- [x] ~~is_tradable 全为 1，等 F3 过滤后回填~~ → T4-1 实现过滤，T4-2 已回填（filter_version=v1_f3）
- [x] ~~同日多重信号多行存储，回测需双口径统计~~ -> T4-4 双口径回测已落地（signal / day 两个 scope）
- [x] ~~需定义同日多信号优先级规则~~ → T4-3 已定义（ADR-010）：类型分 三类>二类>一类，同分卖点优先
- [x] ~~UI 提示「请先跑一次 main.py」~~ → T7-1b 数据兜底（ADR-015），analyzer 自动补齐并给结构化错误
- [ ] F4.6 按实测胜率覆盖信号优先级（当前 629 条样本，单一市场环境、无费用滑点，暂不调）
- [ ] F5.4 K 线独立大图 / 多周期（P2）

> 另有规则校准类待办（信号映射不确定点、中枢取法遗留、价格/因子来源待评估等），
> 散见于 `docs/技术决策记录.md` 各 ADR 的「遗留 / 待评估」小节。
