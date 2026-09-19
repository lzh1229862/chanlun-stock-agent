@echo off
chcp 65001 >nul
setlocal
set TASKNAME=ChanAgentDaily
schtasks /Delete /TN "%TASKNAME%" /F
if errorlevel 1 (
  echo [失败] 删除失败，任务可能不存在或需要管理员权限。
) else (
  echo [成功] 已删除计划任务 %TASKNAME%。
)
echo.
pause
