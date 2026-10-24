# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 3D Print Forge 打成单文件 EXE。

设计取舍：
- **excludes 掉 cupy / torch / nvidia-***：venv 里装了 cupy-cuda12x（含数 GB 的 CUDA 运行时），
  但 app/gpu.py 与 app/jobs.py 对 cupy 都是惰性 import + try/except，缺了会静默退回 CPU。
  不排除的话 EXE 会从 ~200MB 涨到 3GB 以上，普通用户根本拷不动。
- **把 app 全部子模块显式收集**：uvicorn 用字符串导入、部分模块是动态拼的，
  只靠静态分析容易漏，collect_submodules 一次收全。
- **static/ 作为数据目录**：FastAPI 挂载的是磁盘路径，不是 Python 包资源。
"""
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.getcwd())

datas = [(os.path.join(ROOT, "static"), "static")]
for extra in ("README.md",):
    p = os.path.join(ROOT, extra)
    if os.path.exists(p):
        datas.append((p, "."))

hiddenimports = collect_submodules("app") + [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
]

EXCLUDES = [
    "cupy", "cupy_backends", "cupyx",
    "torch", "torchvision", "torchaudio",
    "nvidia", "cuda", "numba", "llvmlite",
    "matplotlib", "pandas", "IPython", "jupyter", "notebook",
    "tkinter", "test", "unittest", "pydoc_data",
    "pytest", "_pytest", "setuptools", "pip", "wheel",
    "scipy.spatial._ckdtree_test", "skimage.data", "skimage.feature._orb_descriptor_positions",
]

a = Analysis(
    [os.path.join(ROOT, "launcher.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="3D打印建模工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "assets", "icon", "icon.ico"),
)
