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

- 左侧输入 6 位代码 → 「开始分析」：**默认先查本地历史**（快、不花钱），没有才实时计算
- 勾「强制刷新」可跳过本地历史重新算
- 勾「分析时立即调用 LLM」才会当次生成 AI 总结，否则先看规则结果、需要时再点按钮
- K 线图上标注分型 / 笔 / 买卖点

---

## 目录结构

```
├── main.py                CLI 入口
├── analyzer.py            公共分析函数（CLI / UI 共用）+ 数据兜底 ensure_kline
├── app.py                 Streamlit 网页界面
├── agent_graph.py         6 节点编排 + 4 条条件分支（原生 / LangGraph 双引擎）
├── min_loop.py            最小闭环：czsc 结构 + 信号映射
├── signal_filter.py       F3 过滤规则 + 交易日历
├── backtest.py            轻量回测
├── report_builder.py      报告生成
├── llm_client.py          DeepSeek 调用封装
├── storage_kline.py       Parquet 行情存取 + 增量更新
├── storage_signal.py      SQLite 信号 / 回测读写
├── storage_report.py      SQLite 报告归档
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

- `watchlist`：股票池（仓库里放了 12 只覆盖沪深主板 / 创业板 / 科创板的分散样本，**非推荐**）
- `filter.volume`：三条可选量能规则，`enable: false` 时全部不生效

提示词模板在 `config/prompts/report_summary.txt`，用 `=== SYSTEM ===` / `=== USER ===` 分段，变量写成 `{{var}}`。

---

## 关键设计（ADR 摘要）

完整决策记录见 [`docs/技术决策记录.md`](docs/技术决策记录.md)，共 15 条。

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

其中 ADR-011 / ADR-012 是本项目比较特别的地方：**信号日当天往往还不知道信号成立**，因为笔的成立要靠后续 K 线确认。所以回测入场统一取「确认日的次一交易日开盘」，而不是「信号日收盘」。

---

## 自测与验收脚本

```bash
python verify_czsc.py         # 缠论库能否算出预期的分型 / 笔 / 中枢
python verify_akshare.py      # 数据源连通性与字段
python verify_deepseek.py     # LLM 连通性（需要 DEEPSEEK_API_KEY）
python verify_fallback.py     # 数据兜底 22 项断言
python verify_fallback.py --offline   # 跳过需要联网的用例
```

`verify_fallback.py` 会自己备份 → 删掉本地 Parquet → 调 `analyze_stock` → 检查是否自动拉回 → 模拟断网 → 最后还原原文件，跑完数据不会丢。

除此之外还有一套**完全离线**的单元测试（不联网、不需要本地行情数据，0.3 秒跑完）：

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
