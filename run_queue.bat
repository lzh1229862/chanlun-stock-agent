@echo off
REM 长任务队列（B9 / ADR-036）。一条命令跑完扫描 / 退市取数 / 重算 / 对比 / 每日批处理。
REM Ctrl+C 可随时中断，重跑自动续上。
cd /d "%~dp0"
python run_queue.py %*
pause
