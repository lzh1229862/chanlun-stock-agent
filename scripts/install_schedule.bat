@echo off
chcp 65001 >nul
setlocal
set TASKNAME=ChanAgentDaily
set RUNNER=%~dp0run_daily.bat
echo.
echo === 注册 Windows 计划任务 === 
echo   任务名: %TASKNAME%
echo   时间  : 每周一至周五 18:05（ADR-004：避开 DeepSeek 高峰计价时段）
echo   脚本  : %RUNNER%
echo.
schtasks /Create /TN "%TASKNAME%" /TR "\"%RUNNER%\"" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 18:05 /F
if errorlevel 1 (
  echo.
  echo [失败] 注册未成功。常见原因：
  echo   1^) 需要管理员权限 —— 右键本文件选"以管理员身份运行"
  echo   2^) 同名任务属别的用户
  pause
  exit /b 1
)
echo.
echo [成功] 已注册。当前计划任务：
schtasks /Query /TN "%TASKNAME%" /FO LIST
echo.
echo 手动跑一次测试 : schtasks /Run /TN "%TASKNAME%"
echo 查看运行日志   : type logs\scheduler.log
echo 取消定时任务   : 双击 uninstall_schedule.bat
echo.
pause
