"""桌面快捷方式：给这个工具在 Windows 桌面上放一个「一键启动」的图标。

只在 Windows 生效。做法两步：

1. 在项目根目录生成/更新一个启动脚本（.cmd）：用项目自带 venv 的 python
   起服务，等 3 秒后自动打开浏览器；服务停掉后窗口不立刻消失，方便看报错。
2. 用 PowerShell 的 WScript.Shell COM 在桌面创建指向该 .cmd 的 .lnk。

关键点：**程序本体始终留在项目所在的盘（当前是 I 盘）**，桌面只放一个
指向它的快捷方式。这样换机器/改路径时，只要改启动器，快捷方式不用动。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import config

LAUNCHER_NAME = "启动-3D打印.cmd"
LNK_NAME = "3D 打印建模工具.lnk"

# %~dp0 是本 .cmd 所在目录，也就是项目根（I 盘）。venv 优先，找不到再退回系统 python。
_LAUNCHER = r"""@echo off
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
"""


def _desktop_dir() -> Path | None:
    """找桌面目录。Windows 桌面可能被 OneDrive 重定向，优先问系统。"""
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
            capture_output=True, text=True, timeout=10,
        )
        p = (out.stdout or "").strip()
        if p and Path(p).exists():
            return Path(p)
    except Exception:
        pass
    home = Path.home()
    for cand in (home / "Desktop", home / "OneDrive" / "Desktop", home / "桌面"):
        if cand.exists():
            return cand
    return None


def _python_exe() -> str:
    venv_py = config.ROOT / ".venv" / "Scripts" / "python.exe"
    return str(venv_py) if venv_py.exists() else sys.executable


def ensure_launcher() -> Path:
    """在项目根生成/更新启动脚本，返回路径。"""
    path = config.ROOT / LAUNCHER_NAME
    if not path.exists() or path.read_text(encoding="utf-8", errors="ignore") != _LAUNCHER:
        # .cmd 用 GBK 保存更稳（中文 Windows 控制台默认代码页），但内容里有 chcp 65001，
        # 用 UTF-8 也能正常显示；这里统一 UTF-8，避免和仓库其它文件编码不一致。
        path.write_text(_LAUNCHER, encoding="utf-8", newline="\r\n")
    return path


def create_desktop_shortcut() -> dict:
    """在桌面创建/覆盖快捷方式。返回 {ok, lnk, launcher, desktop} 或 {ok:False, error}。"""
    if os.name != "nt":
        return {"ok": False, "error": "桌面快捷方式目前只支持 Windows"}
    desktop = _desktop_dir()
    if desktop is None:
        return {"ok": False, "error": "找不到桌面目录（可能被 OneDrive 重定向或权限受限）"}

    launcher = ensure_launcher()
    lnk = desktop / LNK_NAME
    icon = _python_exe()

    # 路径里没有单引号，PowerShell 单引号字面量安全
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{lnk}'); "
        f"$sc.TargetPath = '{launcher}'; "
        f"$sc.WorkingDirectory = '{config.ROOT}'; "
        f"$sc.IconLocation = '{icon},0'; "
        "$sc.Description = '3D 打印模型处理工具集（一键启动）'; "
        "$sc.WindowStyle = 1; "
        "$sc.Save()"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=25,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"调用 PowerShell 失败：{e}"}

    if r.returncode != 0 or not lnk.exists():
        msg = (r.stderr or r.stdout or "创建失败").strip()
        return {"ok": False, "error": msg[:300]}

    return {
        "ok": True,
        "lnk": str(lnk),
        "launcher": str(launcher),
        "desktop": str(desktop),
        "root": str(config.ROOT),
    }


def shortcut_status() -> dict:
    """当前快捷方式状态，供界面显示「已创建 / 未创建」。"""
    desktop = _desktop_dir()
    lnk = (desktop / LNK_NAME) if desktop else None
    return {
        "supported": os.name == "nt",
        "desktop": str(desktop) if desktop else None,
        "lnk": str(lnk) if lnk else None,
        "exists": bool(lnk and lnk.exists()),
        "launcher": str(config.ROOT / LAUNCHER_NAME),
        "root": str(config.ROOT),
    }
