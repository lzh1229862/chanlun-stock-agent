# 缠论股票分析 Agent

[![CI](https://github.com/lzh1229862/chanlun-stock-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/lzh1229862/chanlun-stock-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

本地运行的 A 股缠论分析 Agent：自动获取日线 → 计算分型 / 笔 / 中枢 → 识别三类买卖点 → 信号过滤 → 轻量回测 → 生成中文报告（可选 LLM 总结）→ 归档为 Markdown / CSV / SQLite。另有 Streamlit 网页界面与 Windows 定时任务。

> 本项目是**技术验证与学习用工具**：按缠论规则机械地生成结构化描述，不预测涨跌、不承诺收益、不含自动下单。
> **不构成任何投资建议。**

---

## 功能

| 模块 | 说明 |
| --- | --- |
| 数据获取 | AKShare 腾讯日线（不复权）+ 新浪前复权因子；落地 Parquet，按缺口增量更新 |
| 数据兜底 | 本地缺失或过期时自动拉取；拉取失败返回结构化错误而不抛异常 |
| 缠论计算 | `czsc` 1.0.1（Rust）算分型 / 笔 / 中枢 / 走势类型；中枢延伸不超过 9 笔 |
| 买卖点 | 三类买卖点**自研规则**（czsc 1.0.1 无内置信号库，见 ADR-003） |
| 信号过滤 | ST / 次新 / 涨跌停，另有三条可选量能规则（停牌 / 流动性低 / 放量异常），默认关闭 |
| 轻量回测 | 双口径（按信号 / 按交易日）× 5 / 10 / 20 日；入场 = 确认日次一交易日开盘 |
| 报告 | 规则化 Markdown 报告 + DeepSeek 中文总结（无 key 时自动降级为纯规则报告） |
| Agent 编排 | 6 节点流水线 + 4 条条件分支；原生循环与 LangGraph 双引擎，共用同一套节点函数 |
| 网页界面 | Streamlit：搜索 / 分析 / 历史优先 / K 线缠论标注 / AI 总结按需生成 |
| 自选股管理 | 网页界面里直接编辑股票池（上限 10 只），写入 `config/watchlist.local.yaml`，不污染版本库 |
| 基本面 | 行业分类（中证四级）+ 财务摘要（近 6 期）+ 估值快照（PE / PB / 市值）；**只做展示与 LLM 上下文，不进过滤与回测**（ADR-017） |
| 定时任务 | Windows 计划任务，周一至周五 18:05，非交易日自动跳过 |

---

## 快速开始

**环境要求**：Windows + Python 3.12（其他平台未验证；依赖含 Rust 编译的 `czsc` 轮子）

```bash
git clone git@github.com:lzh1229862/chanlun-stock-agent.git
cd chanlun-stock-agent
python -m pip install -r requirements.txt
```

**第一步：跑通最小闭环**（拉数据 → 算信号 → 存库，约 1 秒）

```bash
python run_round3.py
```

**第二步：配置 LLM（可选）**——不配也能跑，只是报告里没有 AI 总结段落

```bat
set DEEPSEEK_API_KEY=sk-你的key
```

**第三步：跑一次完整流程**

```bash
python main.py --stocks 600519,601318
```

也可以直接双击：

| 文件 | 作用 |
| --- | --- |
| `run.bat` | 一键跑全流程，结束后自动打开最新报告 |
| `run_ui.bat` | 启动网页界面 http://localhost:8501 |

> 首次运行需要联网（拉了行情才会算信号）；之后重复运行不会重复拉取、不会重复入库。

---

## 命令行

`main.py` 是统一入口：

```bash
python main.py [选项]
```

| 选项 | 说明 |
| --- | --- |
| `--date YYYY-MM-DD` | 报告日 / 交易日，默认今天 |
| `--stocks 600519,601318` | 股票池，逗号分隔；默认读 `config/settings.yaml` |
| `--no-llm` | 不调用 LLM，只出规则报告 |
| `--analyze CODE` | 只分析单只股票（调 `analyzer.analyze_stock`），不跑批量流程 |
| `--nodes fetch,chan` | 只跑指定节点（调试用） |
| `--engine auto\|native\|langgraph` | 编排引擎选择，默认 `auto` |
| `--log-file PATH` | 运行日志路径，默认 `logs/<交易日>.json` |
| `--trading-day-only` | 非交易日直接跳过（供计划任务使用） |

示例：

```bash
python main.py --analyze 600519 --no-llm       # 单股快查
python main.py --date 2026-09-18 --engine langgraph
```

---

## 网页界面

```bat
run_ui.bat
```

### 界面皮肤：🌙 夜晚 / ☀️ 白天

侧边栏左下角一个开关，在**深色（夜晚）**与**浅色（白天）**两套界面间切换，默认夜晚。

- 两套配色都在 `skin.py` 里，是同一份 CSS 的两组令牌（底色 / 卡片 / 边框 / 文字 / 涨跌 / 强调色）
- 切换靠 `.stApp:has(开关 input:checked)` 的纯 CSS 联动，**不弹窗、不闪白**；选择同时写进 `session_state`
- 图表配色跟着皮肤走：K 线涨跌、笔、中枢、买卖点星标在浅色下改用深一档的同色系（`skin.chart_colors`）
- 系统深色偏好（`prefers-color-scheme`）在用户还没做过选择时自动生效
- 原生组件的底色由 `.streamlit/config.toml` 的 `[theme]` 定基调（深色），浅色下再用 CSS 覆盖弹层 / 日历 / dataframe

- 左侧输入 6 位代码 → 「开始分析」：**默认先查本地历史**（快、不花钱），没有才实时计算
- 勾「强制刷新」可跳过本地历史重新算
- 勾「分析时立即调用 LLM」才会当次生成 AI 总结，否则先看规则结果、需要时再点按钮
侧边栏顶部可以切换两个模式。

### 模式一：单股分析

- 输入 6 位代码 → 「开始分析」：**默认先查本地历史**（快、不花钱），没有才实时计算
- 勾「强制刷新」可跳过本地历史重新算
- 勾「分析时立即调用 LLM」才会当次生成 AI 总结，否则先看规则结果、需要时再点按钮
- K 线图上标注分型 / 笔 / 中枢 / 买卖点
- 分节：**01 K线** → **02 公司基本面** → **03 信号明细** → **04 回测统计** → **05 AI 总结** → **06 原始数据**

**02 公司基本面**一节展示：

- 读数卡：PE(TTM) / PE(静态) / PB / 总市值 / ROE / 营收同比 / 净利润同比
- 公司概况：所属行业（证监会门类）、中证四级行业分类、上市日期、主营业务
- 近 6 期财务趋势表：营收、净利润及各自同比、ROE

> ⚠️ 基本面**只用于交代背景**：不参与信号过滤，也不进回测。财报有披露滞后（半年报 8 月才出），
> 用最新财报评估历史信号会造成前视偏差（ADR-017）。喂给 LLM 时也加了硬约束，禁止据此推断涨跌方向。

### 模式二：自选股与日报

**① 编辑股票池**

- 每行一个代码（或逗号 / 空格分隔），**最多 10 只**
- 点「替换股票池」→ 弹确认框 → 再点「确认替换」才真的落盘；确认框里会写明**换池后前几天回测一栏是空的**
- 保存时逐只**试拉校验 + 预热历史数据**：拉不到的代码当场红字列出，第二天批处理就不用再临时拉
- 「恢复默认」一键回到仓库自带的 10 只
- 上限 10 只，仓库默认池同样是 10 只，两边一致；万一有人把 `settings.yaml` 改成超过 10 只，页面也只给一句说明而不会卡死
- 打开 `http://localhost:8501/?page=pool` 可直接进这一页，方便收藏

池子写在 `config/watchlist.local.yaml`（**不进版本库**，已在 .gitignore）。改完第二天（周一至周五 18:05 的计划任务）自动按新池扫。

**② 历史日报**（就在股票池编辑区下方）

- 列出 `reports/` 下所有日期（倒序），点「查看这份日报」直接渲染 `report.md`
- 附 `signals.csv` 下载按钮

> ⚠️ **换池后前几天，新股票的「回测统计」会是空的 —— 这是正常的。**
> 回测用的是数据库里**已有的历史信号**，新股票还没积累；而且缠论信号本身有 1~2 个交易日的确认延迟（ADR-011）。
> 结构、买卖点、过滤结果、AI 总结这些内容当天就有。

---

## 目录结构

```
├── main.py                CLI 入口
├── analyzer.py            公共分析函数（CLI / UI 共用）+ 数据兜底 ensure_kline
├── app.py                 Streamlit 网页界面
├── skin.py                双皮肤配色令牌 + 全局样式（白天 / 夜晚）
├── agent_graph.py         6 节点编排 + 4 条条件分支（原生 / LangGraph 双引擎）
├── min_loop.py            最小闭环：czsc 结构 + 信号映射
├── signal_filter.py       F3 过滤规则 + 交易日历
├── backtest.py            轻量回测
├── report_builder.py      报告生成
├── llm_client.py          DeepSeek 调用封装
├── storage_kline.py       Parquet 行情存取 + 增量更新
├── storage_signal.py      SQLite 信号 / 回测读写
├── storage_report.py      SQLite 报告归档
├── watchlist_store.py     自选股池读写（UI 与批处理共用，ADR-016）
├── fundamentals.py        基本面取数 + 解析（巨潮 / 同花顺 / 腾讯，ADR-017）
├── storage_fundamental.py SQLite 基本面缓存（profiles / fundamentals 两张表）
├── confirm_dates.py       信号确认延迟实测
├── mark_primary.py        同日多信号优先级标记
├── migrate_t4.py / t5.py  表结构迁移
├── backfill_t4.py         历史信号回填
├── calib_zs.py            中枢参数校准
├── calib_beichi.py        背驰容差校准
├── verify_akshare.py      数据源可行性验证
├── verify_czsc.py         缠论库可行性验证
├── verify_deepseek.py     LLM 连通性验证
├── verify_fallback.py     数据兜底验收（22 项断言）
├── config/
│   ├── settings.yaml      股票池 + 过滤参数
│   └── prompts/           提示词模板
├── scripts/               计划任务注册 / 注销 / 每日执行
├── docs/
│   ├── PRD.md             产品需求文档
│   └── 技术决策记录.md      ADR-001 ~ ADR-015
├── tests/                 离线单元测试（不联网，供 CI 用）
├── .github/workflows/     GitHub Actions CI 配置
├── env_backup/            依赖快照（pip freeze）
├── data/                  行情 Parquet + SQLite（不进版本库）
├── reports/               归档报告（不进版本库）
└── logs/                  运行日志（不进版本库）
```

---

## 数据与存储

**行情** `data/raw/{code}.parquet` —— 一只股票一个文件

| 列 | 说明 |
| --- | --- |
| `date open high low close volume amount` | 不复权行情 |
| `qfq_factor` | 前复权因子（单独存，不改原始价） |

存不复权价 + 因子，是为了让回测能用真实成交价、又能算复权收益率（见 ADR-002）。

**SQLite** `data/chan_agent.db`

| 表 | 内容 |
| --- | --- |
| `signals` | 买卖点信号、类型、is_primary、确认延时、三日期、过滤结果 |
| `backtest` | 双口径 × 5/10/20 日的回测记录 |
| `reports` | 报告归档索引（一天一股一行） |

**报告** `reports/YYYY-MM-DD/` —— `report.md` + `signals.csv` + `meta.json`

---

## 配置

`config/settings.yaml`：

- `watchlist`：**默认**股票池（仓库里放了 10 只覆盖沪深主板 / 创业板 / 科创板的分散样本，行业互不重复，**非推荐**；上限 10 只）
  如果 `config/watchlist.local.yaml` 存在，**以它为准**（网页界面保存的，不进版本库，见 ADR-016）；删掉它就回到这里的默认值
- `filter.volume`：三条可选量能规则，`enable: false` 时全部不生效

提示词模板在 `config/prompts/report_summary.txt`，用 `=== SYSTEM ===` / `=== USER ===` 分段，变量写成 `{{var}}`。
其中 `{{fundamentals}}` 是公司基本面（行业 / 财务 / 估值）。模板里给它配了硬约束 #9：
基本面数字只用于交代公司背景，不得据此推断涨跌方向、不得写「基本面良好所以看多」、不得换算或外推。

`.streamlit/config.toml`（界面主题，**不要删**）：

- `[theme]`：深色基调（夜晚 UI 的底色 / 卡片 / 主色），原生组件（下拉弹层、日历、dataframe 画布）跟随这里
- `[server] headless = false`：**启动后自动打开浏览器**。设成 `true` 会变成"只打印地址、不弹浏览器"，
  表现为双击 `run_ui.bat` 后看不到界面
- `[client] toolbarMode = "minimal"`：顶部菜单只留最少项（皮肤切换由侧边栏左下角开关负责）

遇到"双击 `run_ui.bat` 没反应"先按顺序查三件事：

1. 窗口里有没有 `Local URL: http://localhost:8501`？有 → 服务正常，问题在浏览器没被拉起，检查 `headless` 是不是 `true`
2. 地址被别的程序占了吗？`netstat -ano | findstr :8501`，占用了就换端口或结束那个进程
3. 窗口是不是一闪而过？说明 Python 或依赖有问题，在项目目录手动跑 `python -m streamlit run app.py` 看报错

---

## 关键设计（ADR 摘要）

完整决策记录见 [`docs/技术决策记录.md`](docs/技术决策记录.md)，共 17 条。

| 编号 | 决策 | 结论 |
| --- | --- | --- |
| 001 | 数据源 | 用 AKShare 腾讯接口；东财接口在本机被服务端重置 |
| 002 | 复权处理 | 存不复权 + 复权因子；价格取腾讯、因子取新浪（口径差 ~0.37%） |
| 003 | 缠论库用法 | `czsc` 1.0.1 无内置信号库 → 自研三类买卖点 |
| 004 | LLM 集成 | 模型 `deepseek-flash`、18:00 跑、强制关闭 thinking |
| 005 | 中枢取法 | `min_bi_len=5, max_bi_num=500` + 中枢延伸不超过 9 笔 |
| 006 | 力度容差 | `POWER_TOL = 0.03`，消除价格口径抖动导致的信号翻转 |
| 010 | 同日多信号优先级 | 类型分 三类 > 二类 > 一类；同分时卖点优先 |
| 011 | 确认延迟与入场 | 笔需后续 K 线确认，实测延迟 1~2 个交易日；入场 = 确认日次一交易日开盘 |
| 012 | 信号三日期 | `signal_date` 笔结束日 / `confirm_date` 实测延迟 / `entry_ref_price` 不复权开盘 |
| 014 | 量能过滤与定时任务 | 三条规则可独立开关且默认关；计划任务周一至周五 18:05 |
| 015 | 数据兜底 | `ensure_kline` 统一取数；本地最新走缓存不发请求，缺失 / 过期自动增量拉取，失败返回结构化错误 |
| 016 | 自选股池与日报回看 | 池子存 `config/watchlist.local.yaml`（不进版本库）；上限 10 只 + 试拉校验 + 两步确认 |
| 017 | 基本面数据 | 行业 / 财务 / 估值走巨潮 + 同花顺 + 腾讯（全绕开东财）；**不进过滤与回测**，避免前视偏差 |

其中 ADR-011 / ADR-012 是本项目比较特别的地方：**信号日当天往往还不知道信号成立**，因为笔的成立要靠后续 K 线确认。所以回测入场统一取「确认日的次一交易日开盘」，而不是「信号日收盘」。

---

## 自测与验收脚本

```bash
python verify_czsc.py         # 缠论库能否算出预期的分型 / 笔 / 中枢
python verify_akshare.py      # 数据源连通性与字段
python verify_deepseek.py     # LLM 连通性（需要 DEEPSEEK_API_KEY）
python verify_fallback.py     # 数据兜底 22 项断言
python verify_fallback.py --offline   # 跳过需要联网的用例
python verify_edge.py         # 信号有效性对照（基准 / 成本 / 置信区间 / 分布）
```

`verify_fallback.py` 会自己备份 → 删掉本地 Parquet → 调 `analyze_stock` → 检查是否自动拉回 → 模拟断网 → 最后还原原文件，跑完数据不会丢。

### `verify_edge.py`：信号到底有没有超出「随机入场」

**这是看任何胜率数字之前必须先跑的一个脚本。** 它回答：信号之后的收益，比「同一只股票随便哪天入场」好多少？

为什么需要它 —— 实测发现 12 只样本股的 **5 日无条件平均收益本身就是 +0.12% ~ +1.49%**。
也就是说「信号后平均 +0.96%」里有一大半是市场给的（beta），不是缠论规则给的。不扣掉这个基准，
任何「胜率提升」都可能只是行情变了。

输出五组：

| 表 | 内容 |
| --- | --- |
| **A** 超额收益 | 信号均值 − 同股票同期无条件基准均值 + 扣费净值 + 判断 |
| **B** 信号质量 | 胜率用 Wilson 区间、基准胜率、盈亏比、最大连亏、最差 5% 分位 |
| **C** 样本外切分 | 按入场日切成「观察期 / 验证期」，两段结论是否一致（`--split 0.6`） |
| **D** 时间分块 | 把样本期等分成 N 段，看超额在各 regime 下是否稳定（`--blocks 4`） |
| 汇总 | 买点 / 卖点分开看，避免互相抵消 |

> **表 A 只能说明「当下这批数据看起来如何」；表 C / 表 D 才回答「会不会只是这段行情碰巧」。**
> 只看表 A 很容易自我说服。

所有基准都在**该子样本自己的时间跨度内**重算 —— 用牛市的基准去衡量熊市的信号会得出完全错误的结论。

```bash
python verify_edge.py                       # 默认 signal 口径、5/10/20 日
python verify_edge.py --window 5            # 只看 5 日
python verify_edge.py --scope day           # 换按交易日口径
python verify_edge.py --zero-cost           # 不计任何交易成本
python verify_edge.py --slippage 0.001      # 单边滑点 0.1%
python verify_edge.py --split 0.6          # 追加样本外切分（前 60% 观察 / 后 40% 验证）
python verify_edge.py --blocks 4           # 追加 4 段时间分块
```

**它不改动任何分析逻辑**，只读 `backtest` 表 + 本地 Parquet，入场口径与回测完全一致
（复用 `backtest.evaluate`，确认日次一交易日开盘）。

> ⚠️ 还**没有做市场状态分层**（用大盘趋势/波动率分段），`--blocks` 只是等分时间，不是真正的 regime 划分。
> 样本期（2024-11 ~ 2026-09）整体上行，买点会系统性好看。也**不是策略有效性证据**。

**一个具体的坑（表 D 实测）**：买点在第 4 段均值 +1.25%、扣费后 +1.05%，看着挺好；
但同一段的基准是 **+1.20%** —— 也就是说**随机入场拿到的比信号还多**，超额只有 +0.05%。
只看「扣费后为正」会得出完全相反的结论。

除此之外还有一套**完全离线**的单元测试（不联网、不需要本地行情数据，1.5 秒跑完，54 项）：

```bash
python -m unittest discover -s tests -v
```

覆盖：

| 测试类 | 断言内容 |
| --- | --- |
| `TestAnalyzeStockContract` | 返回字段齐全；缺数据时给结构化错误；`allow_fetch=False` 报对原因；**任何内部异常都被转成 `ok=False + error`，绝不抛出** |
| `TestEnsureKline` | 无数据 + 无网络 → 返回 `(None, 原因)`；日历取不到 + 本地有数据 → 判 `cache` 不联网 |
| `TestStorageKline` | Parquet 路径 / 代码前缀 / 缺失文件读成空表 |
| `TestModulesImport` | 12 个业务模块全部可 import（挡语法错、循环依赖、依赖缺失） |
| `TestFundamentals` / `TestStorageFundamental` | 基本面单位解析（`1.2万亿` 必须从长到短匹配）、缓存 upsert、三个数据源全挂也不抛异常 |
| `TestVerifyEdge` | 有效性对照里的统计公式：t 临界值、Wilson 区间、盈亏比、最大连亏、成本模型（**测量工具算错比没有更糟**） |
| `TestAppBoots` / `TestSkin` | app.py 能渲染出首屏；双皮肤令牌完整、混搭配色齐全 |

---

## 持续集成

推送到 `main` 或提 PR 时自动跑 `.github/workflows/ci.yml`（ubuntu-latest + Python 3.12）：

1. 按 `requirements.txt` 安装全部依赖（验证依赖可复现）
2. `python -m compileall -q .` —— 全部 .py 语法检查
3. `python -m unittest discover -s tests -v` —— 离线单元测试

CI **不访问任何行情接口**：测试里把所有出网口都打了桩，所以不会因为第三方接口抖动而红。
也正因为如此，CI 绿只代表「代码能装、能导入、契约没破」，**不代表策略有效**。

---

## 已知限制

- **仅 Windows 验证过**；计划任务脚本是 `schtasks` + `.bat`，其他平台需自行改写
- 回测**不含手续费与滑点**，样本量也有限、且落在单一市场环境里，**不能当成策略有效性证据**
- 当前信号优先级（ADR-010）是按规则定的，尚未用实测胜率校准
- 报告中的方向判断由 LLM 生成，只做定性描述，不构成预测
- 行情来自第三方免费接口，可能延迟、缺失或改口径

---

## 免责声明

本项目仅用于技术学习与研究，所有输出均由程序按固定规则自动生成。
**不构成任何投资建议，不承诺任何收益，不含自动下单功能。据此操作风险自负。**

## 许可

本项目采用 [MIT License](LICENSE)：你可以自由使用、修改、分发、甚至商用，只需在副本中保留版权与许可声明。

> 许可证只覆盖**本项目代码本身**，不构成对任何投资行为的许可或担保；
> 行情数据来自第三方免费接口，其使用条款请自行遵守。
