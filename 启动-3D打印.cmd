@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 3D Print Forge - 3D 打印建模工具
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo.
echo   3D Print Forge  正在启动...
echo   浏览器会在约 3 秒后自动打开  http://127.0.0.1:8000
echo   关闭这个窗口即停止服务。
echo.
start "" /min cmd /c "ping -n 4 127.0.0.1 >nul & start "" http://127.0.0.1:8000"
"%PY%" "%~dp0run.py"
echo.
echo 服务已停止。按任意键关闭。
pause >nul
