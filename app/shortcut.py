"""桌面快捷方式：给这个工具在 Windows 桌面上放一个「一键启动」的图标。

只在 Windows 生效。两条路径：

- **打包版（frozen）**：快捷方式直接指向 EXE 本身，图标取自 EXE 内嵌资源，
  右键「打开文件所在位置」就落在程序包目录。
- **源码版**：在项目根生成/更新一个启动脚本（.cmd），快捷方式指向它，
  用项目自带 venv 的 python 起服务。

.lnk 的写入走 `app/lnk.py`（ctypes 直连 IShellLink COM），不经 PowerShell——
既绕过 ExecutionPolicy，也避免打包后子进程权限上下文变化导致 Save 失败。
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
    """找桌面目录。Windows 桌面可能被 OneDrive / 别的盘重定向，优先问系统。"""
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
    # 兜底：读注册表里的桌面路径（比猜 Desktop/OneDrive 可靠，支持重定向到 D:\ 等）
    try:
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            raw = winreg.QueryValueEx(k, "Desktop")[0]
        p = Path(os.path.expandvars(raw))
        if p.exists():
            return p
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


def _target() -> tuple[str, str, str]:
    """快捷方式三件套：(目标, 工作目录, 图标)。

    打包成 EXE 后直接指向 EXE 本身（图标也取自 EXE 内嵌资源），
    不再经过 .cmd / venv，彻底不依赖本机 Python。
    """
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        return str(exe), str(exe.parent), f"{exe},0"
    return str(ensure_launcher()), str(config.ROOT), f"{_python_exe()},0"


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

    lnk = desktop / LNK_NAME
    target, workdir, icon = _target()
    icon_path, _, icon_idx = icon.rpartition(",")
    if not icon_path:
        icon_path, icon_idx = icon, "0"

    try:
        from . import lnk as lnk_mod

        lnk_mod.create_lnk(
            str(lnk), target, workdir=workdir,
            icon=icon_path, icon_index=int(icon_idx or 0),
            description="3D 打印建模工具（机型预设 + 参数建议）",
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"创建快捷方式失败：{e}"}

    if not lnk.exists():
        return {"ok": False, "error": "快捷方式文件没有写入成功"}

    return {
        "ok": True,
        "lnk": str(lnk),
        "launcher": target,
        "desktop": str(desktop),
        "root": str(config.BASE),
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
        "launcher": str(Path(_target()[0])),
        "root": str(config.BASE),
    }
