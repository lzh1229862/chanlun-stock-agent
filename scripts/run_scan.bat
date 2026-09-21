@echo off
REM 全市场买点扫描（T18 / ADR-022）。断点续跑：中断后重跑会自动跳过已完成的股票。
REM 首次约 4~5 小时（要拉 5000 只的 2 年历史），之后数据已缓存约 1 小时。
cd /d "%~dp0.."
python market_scan.py --sleep 0.05 > logs\scan_full.log 2>&1
echo done >> logs\scan_full.log
