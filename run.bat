@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo ================================================================
echo   缠论股票分析 Agent  --  一键运行
echo   项目目录: %CD%
echo   （可加参数，例如: run.bat --no-llm   或   run.bat --stocks 600519,000001）
echo ================================================================
echo.

python main.py %*
set RC=%ERRORLEVEL%

echo.
echo ----------------------------------------------------------------
echo 运行结束（退出码 %RC%）
echo 报告目录 : %CD%\reports
echo 运行日志 : %CD%\logs
echo ----------------------------------------------------------------
echo.

for /f "delims=" %%d in ('dir /b /ad /o-n "%CD%\reports" 2^>nul') do (
    echo 正在打开最新报告: reports\%%d\report.md
    start "" "%CD%\reports\%%d\report.md"
    goto :opened
)
echo 还没有任何报告。
:opened

echo.
pause
endlocal
