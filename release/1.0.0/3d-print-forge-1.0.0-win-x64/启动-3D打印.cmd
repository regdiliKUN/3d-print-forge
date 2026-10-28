@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
title 3D Print Forge
echo 正在启动 3D 打印模型处理工具集…
echo 浏览器会在约 3 秒后自动打开 http://127.0.0.1:8000
rem 延时开浏览器，等服务器起来，避免「连接被拒绝」
start "" cmd /c "ping -n 4 127.0.0.1 >nul & start "" http://127.0.0.1:8000"
"%PY%" "%~dp0run.py"
echo.
echo 服务已停止。按任意键关闭这个窗口。
pause >nul
