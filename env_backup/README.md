# 环境快照

本目录用于记录 Python 依赖状态，便于安装新包后回滚。

| 文件 | 内容 |
| --- | --- |
| `pip-freeze-before-langgraph.txt` | 安装 langgraph 前的完整依赖快照（107 个包） |
| `pip-freeze-after-langgraph.txt` | 安装 langgraph 后的完整依赖快照 |
| `python-version.txt` | Python 版本（3.12.10） |

## 回滚方式

    python -m pip install -r env_backup/pip-freeze-before-langgraph.txt

## 安装 langgraph 带来的变化（实测 diff）

新增 17 个包：

- langgraph 1.2.11
- langchain-core 1.6.3
- langgraph-checkpoint 4.2.0 / langgraph-prebuilt 1.1.0 / langgraph-sdk 0.4.4
- langsmith 0.13.0 / langchain-protocol 0.0.19
- httpx 0.28.1 / httpcore 1.0.9
- orjson / ormsgpack / uuid-utils / xxhash / zstandard / jsonpatch / jsonpointer / distro

## 一个重要结论

`openai 3.15.0` 依赖的是 **httpx2 / httpcore2**（另一套包名），
而 langgraph 引入的是 **httpx / httpcore**，两者**互不冲突**。

装完后已实测：`openai -> DeepSeek` 真实调用成功，`czsc` / `akshare` / `pandas` / `pyarrow` 均正常 import。

## 安装 streamlit（UI 用）

备份：pip-freeze-before-streamlit.txt / pip-freeze-after-streamlit.txt

新增 12 个包：streamlit 1.64.0 / altair 6.3.0 / attrs / jsonschema /
jsonschema-specifications / protobuf / pydeck / python-multipart / referencing /
rpds-py / toml / watchdog

**plotly 早已存在**（由 czsc 带入），所以画图无需额外安装。
装完后已实测 streamlit / plotly / openai / czsc / akshare / pandas / langgraph 全部可 import。

回滚：python -m pip install -r env_backup/pip-freeze-before-streamlit.txt
