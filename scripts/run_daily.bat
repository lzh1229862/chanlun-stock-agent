@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist logs mkdir logs
set LOG=logs\scheduler.log
echo. >> "%LOG%"
echo ================ %date% %time% run start ================ >> "%LOG%"
python main.py --trading-day-only >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo ================ %date% %time% run end (exit %RC%) ================ >> "%LOG%"
endlocal & exit /b %RC%
