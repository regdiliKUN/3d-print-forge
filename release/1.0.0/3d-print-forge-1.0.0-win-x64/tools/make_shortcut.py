"""在桌面创建带自定义图标的快捷方式，并把结果回读校验一遍。

关键点：快捷方式的**目标必须是包体目录里的启动器**，不能指向 python.exe。
Windows 的「打开文件所在的位置」跳的是目标文件所在的文件夹——
目标要是 python.exe，就跳到 Python 安装目录去了，而不是包体目录。

用法（在包体根目录执行）：
    .venv\\Scripts\\python.exe tools\\make_shortcut.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "启动-3D打印.cmd"
ICON = ROOT / "icon.ico"
LNK_NAME = "3D 打印建模工具.lnk"


def desktop_dir() -> Path | None:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
            capture_output=True, text=True, encoding="mbcs", errors="replace", timeout=15,
        )
        p = (out.stdout or "").strip()
        if p and Path(p).exists():
            return Path(p)
    except Exception:
        pass
    home = Path.home()
    for c in (home / "Desktop", home / "OneDrive" / "Desktop", home / "桌面"):
        if c.exists():
            return c
    return None


def create(lnk: Path) -> dict:
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{lnk}'); "
        f"$sc.TargetPath = '{LAUNCHER}'; "
        f"$sc.WorkingDirectory = '{ROOT}'; "
        f"$sc.IconLocation = '{ICON},0'; "
        "$sc.Description = '3D Print Forge - 3D 打印建模工具（机型预设 + 参数建议）'; "
        "$sc.WindowStyle = 1; "
        "$sc.Save()"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True, text=True, encoding="mbcs", errors="replace", timeout=40,
    )
    return {"rc": r.returncode, "err": (r.stderr or r.stdout or "").strip()[:400]}


def verify(lnk: Path) -> dict:
    """把快捷方式读回来，逐个字段跟包体实际位置比对。"""
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{lnk}'); "
        "[pscustomobject]@{"
        "Target=$sc.TargetPath;Workdir=$sc.WorkingDirectory;"
        "Icon=$sc.IconLocation;Desc=$sc.Description"
        "} | ConvertTo-Json -Compress"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True, text=True, encoding="mbcs", errors="replace", timeout=30,
    )
    try:
        return json.loads((r.stdout or "{}").strip())
    except Exception:
        return {"_raw": (r.stdout or r.stderr or "").strip()[:300]}


def main() -> int:
    if not LAUNCHER.exists():
        print(f"找不到启动器：{LAUNCHER}")
        return 1
    if not ICON.exists():
        print(f"找不到图标：{ICON}")
        return 1
    desk = desktop_dir()
    if desk is None:
        print("找不到桌面目录")
        return 1

    lnk = desk / LNK_NAME
    # 旧的同名快捷方式可能被资源管理器占用，Save 会报 UnauthorizedAccess；
    # 先删掉再建，比原地覆盖稳。
    if lnk.exists():
        try:
            lnk.unlink()
        except OSError as e:
            print(f"旧快捷方式删不掉（{e}），尝试原地覆盖")
    res = create(lnk)
    if res["rc"] != 0 or not lnk.exists():
        print("创建失败：", res["err"])
        return 1

    info = verify(lnk)
    checks = {
        "快捷方式存在": lnk.exists(),
        "目标 = 包体内的启动器": Path(info.get("Target", "")).resolve() == LAUNCHER.resolve(),
        "工作目录 = 包体根": Path(info.get("Workdir", "")).resolve() == ROOT.resolve(),
        "图标 = 包体内的 icon.ico": info.get("Icon", "").split(",")[0].strip().lower()
        == str(ICON).lower(),
        "目标确实在 I 盘包体目录内": str(LAUNCHER).lower().endswith("\\printerapp\\启动-3d打印.cmd"),
    }

    print("=" * 60)
    print("快捷方式校验")
    print("=" * 60)
    print(f"  位置      : {lnk}")
    print(f"  目标      : {info.get('Target')}")
    print(f"  工作目录  : {info.get('Workdir')}")
    print(f"  图标      : {info.get('Icon')}")
    print(f"  描述      : {info.get('Desc')}")
    print("-" * 60)
    ok = True
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
        ok = ok and v
    print("=" * 60)
    print("结论：" + ("全部通过，右键「打开文件所在的位置」会跳到 I:\\PrinterApp" if ok else "有项目未通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
