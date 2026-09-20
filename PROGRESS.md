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
| 第 10 轮 | 有效性对照工具：基准 / 成本 / 置信区间 / 分布 + 样本外切分 | `verify_edge.py`（T10-1 / T10-2） |
| 第 11 轮 | 参数敏感性与信号稳定性（证伪类） | `verify_robustness.py`（T11-1 / T11-2） |
| 第 12 轮 | 级别共振（周线）+ `min_bi_len` 分档实证 | `verify_level.py` / ADR-005 补记（T12-1 / T12-2） |
| 第 13 轮 | 指数数据 + 市场状态分层 + 真 alpha | `storage_index.py` / `market_regime.py`（ADR-018） |

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
- **参数敏感性与信号稳定性**（T11-1 / T11-2 / `verify_robustness.py`）：两个**证伪**用的检查，
  共用同一套「按给定参数在给定 K 线上重算信号」核心，**开工前先做口径自检**
  （`signals_on()` 必须与 `confirm_dates.compute_signals()` 逐条一致，防两边漂移）；单测 +6 项（共 67 项）
  - **`min_bi_len` 高度数值敏感**：5→4 时 Jaccard 掉到 **0.163**（80% 以上信号换了一批），
    5→8 掉到 0.104；且超额随参数**单调变化** +0.52% → +1.81% → +2.40% → +2.48% → **+2.79%**（5 倍）
    → **不能靠调这个参数来「提高准确率」**，那是在换一套笔划分，不是把规则改好
  - **中枢延伸上限与 POWER_TOL 稳健**：Jaccard 分别 0.82~1.00 / 0.94~1.00，超额只在 ±0.15% 内浮动
  - **信号稳定性满分**：555 条已确认历史信号，K 线增量更新后 **0 条被改写、0 条新出现** → 增量使用安全
- **级别共振与 `min_bi_len` 分档**（T12-1 / T12-2 / `verify_level.py` + ADR-005 补记）：
  - **`min_bi_len` 是分档开关不是可调参数**：1/2/3/4 完全等价（都是「不设限」，167 笔恒定），
    5 是第一个生效值且一刀砍掉 60%（167→67），4→5 之间存在断崖
  - **ADR-005 理由 1 的措辞需要更正**：它把 czsc 的 `BI.length` 当「笔跨几根 K 线」用了，
    实测两者量纲不同（`length` 中位 4 时实际跨 K 线中位 3，只有 3% 满足 `length+1==根数`）；
    但结论未必要改 —— 理由 2（中枢含笔数落在 [3,9]）独立成立，且 mbl=4 会产生 126 个中枢 vs 47
  - **周线级别共振未获支持**：12 只 / 221 条信号 / 5 日，18 个组合里「配合组显著更好」0 个；
    唯一擦边的是 W1×第一类买点（差值 +2.27%，CI [+0.01%, +4.54%]），但对照组只有 8 条，不能下结论
  - 周线从日线重采样（**零新数据依赖**）；**60 分钟级别已验证数据可得**（新浪 `stock_zh_a_minute`
    拿到 600519 共 1970 根，2024-09~2026-09；东财分钟线被封），尚未做
  - 新增测试 +11 项（共 78 项），其中 `test_weekly_index_never_sees_current_week` 专门钉防未来函数边界
- **指数数据与市场状态分层**（T13-1 / ADR-018 / `storage_index.py` + `market_regime.py`）：
  - 数据源：**新浪 `stock_zh_index_daily` 0.5 秒拿全历史**（沪深300 自 2002 年 5996 行）；东财两个指数接口同样被封
  - 指数单独存 `data/index/{code}.parquet` —— **代码会撞车**（000001 既是指数也是平安银行），绝不与 `data/raw/` 混放
  - 市场状态 = 指数收盘 vs MA200 + 20 日动量，三档（上行/震荡/下行）；**天然无未来函数**（滚动量只用当日及之前）
  - **真 alpha = 方向调整后的信号收益 − 方向调整后的指数收益**；指数收益取同一入场日开盘→退出日收盘
  - 实测：信号分布 **上行 333 / 震荡 259 / 下行 37** → **下行只占 5.5%，样本里几乎没有熊市**
  - 实测：**买点在任何状态下都没有显著 alpha**（之前看到的「买点超额」大部分是 beta）；
    **卖点的正 alpha 稳定**（震荡 5 日 +1.15%、上行 10 日 +1.37%、上行 20 日 +2.28%）
  - 一个被指数基准当场揭穿的例子：买点/下行/5 日「超额(同股)」+1.18% 看着不错，换成指数基准后 alpha 仅 **+0.21%**
  - 反直觉发现：「下行」状态之后指数自身 5/10/20 日反而涨得最多（+1.20%/+2.03%/+3.17%）——
    说明这个定义捕捉的是**超跌**而非持续下跌（37 条样本，不过度解读）
  - 新增测试 +13 项（共 91 项）：指数前缀映射、过期判定、落盘往返、状态三档边界、`row_on` 不得取未来
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
