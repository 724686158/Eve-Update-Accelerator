@echo off
rem 便捷入口：Windows 默认的 PowerShell 执行策略（Restricted）会拒绝运行 .ps1，
rem 这里用 -ExecutionPolicy Bypass 只对本次进程放开，不改系统设置。
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_win10.ps1" %*
echo.
if errorlevel 1 (
  echo 构建失败，请查看上面的输出。
) else (
  echo 构建完成。
)
pause
