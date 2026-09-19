@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
echo ================================================================
echo   缠论分析 Agent  --  Web UI
echo   浏览器将自动打开 http://localhost:8501
echo   关闭本窗口即停止服务
echo ================================================================
echo.
python -m streamlit run app.py
pause
endlocal
