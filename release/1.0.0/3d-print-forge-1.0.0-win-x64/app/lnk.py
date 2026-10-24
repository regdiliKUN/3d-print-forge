"""生成 Windows 桌面快捷方式（.lnk），直接调用 Shell COM 接口。

为什么不用 PowerShell + WScript.Shell：
- 受 ExecutionPolicy / 组策略影响，某些机器上跑不通；
- 打包成 EXE 后子进程继承的临时目录、权限上下文可能变，`$sc.Save()` 会抛
  UnauthorizedAccessException；
- 多一次进程启动，反馈慢。

这里用 ctypes 直接调 IShellLinkW + IPersistFile，纯进程内完成，
不依赖 pywin32、也不依赖 PowerShell。
"""
from __future__ import annotations

import ctypes
from ctypes import POINTER, WINFUNCTYPE, byref, c_void_p, wintypes

ole32 = ctypes.oledll.ole32


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> GUID:
    g = GUID()
    ole32.CLSIDFromString(ctypes.c_wchar_p(text), byref(g))
    return g


CLSID_SHELLLINK = _guid("{00021401-0000-0000-C000-000000000046}")
IID_ISHELLLINKW = _guid("{000214F9-0000-0000-C000-000000000046}")
IID_IPERSISTFILE = _guid("{0000010B-0000-0000-C000-000000000046}")

CLSCTX_INPROC_SERVER = 1


def _fn(punk, index: int, restype, *argtypes):
    """按 vtable 下标取方法并绑定签名。"""
    vtable = ctypes.cast(punk, POINTER(POINTER(c_void_p)))[0]
    proto = WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(vtable[index])


def create_lnk(lnk_path: str, target: str, workdir: str | None = None,
               icon: str | None = None, icon_index: int = 0,
               description: str = "", arguments: str = "") -> None:
    """创建/覆盖一个 .lnk。失败抛 OSError。"""
    ole32.CoInitialize(None)
    psl = c_void_p()
    ole32.CoCreateInstance(byref(CLSID_SHELLLINK), None, CLSCTX_INPROC_SERVER,
                           byref(IID_ISHELLLINKW), byref(psl))
    if not psl:
        raise OSError("无法创建 ShellLink 实例")

    ppf = c_void_p()
    try:
        # 20=SetPath 11=SetArguments 9=SetWorkingDirectory 17=SetIconLocation 7=SetDescription
        _fn(psl, 20, ctypes.c_long, wintypes.LPCWSTR, wintypes.DWORD)(psl, target, 0)
        if arguments:
            _fn(psl, 11, ctypes.c_long, wintypes.LPCWSTR)(psl, arguments)
        if workdir:
            _fn(psl, 9, ctypes.c_long, wintypes.LPCWSTR)(psl, workdir)
        if icon:
            _fn(psl, 17, ctypes.c_long, wintypes.LPCWSTR, ctypes.c_int)(psl, icon, icon_index)
        if description:
            _fn(psl, 7, ctypes.c_long, wintypes.LPCWSTR)(psl, description[:255])

        # 必须从同一个对象 QueryInterface 拿 IPersistFile（另建实例会丢掉上面 Set* 的数据）
        _fn(psl, 0, ctypes.c_long, ctypes.POINTER(GUID), ctypes.POINTER(c_void_p))(
            psl, byref(IID_IPERSISTFILE), byref(ppf))
        if not ppf:
            raise OSError("无法取得 IPersistFile 接口")
        # 6=Save(pszFileName, fRemember)
        _fn(ppf, 6, ctypes.c_long, wintypes.LPCWSTR, ctypes.c_int)(ppf, str(lnk_path), 1)
    finally:
        if ppf:
            _fn(ppf, 2, ctypes.c_ulong)(ppf)
        _fn(psl, 2, ctypes.c_ulong)(psl)


def read_lnk(lnk_path: str) -> dict:
    """读回 .lnk 的目标/工作目录/图标，用于自检。"""
    ole32.CoInitialize(None)
    psl = c_void_p()
    ole32.CoCreateInstance(byref(CLSID_SHELLLINK), None, CLSCTX_INPROC_SERVER,
                           byref(IID_ISHELLLINKW), byref(psl))
    ppf = c_void_p()
    buf = ctypes.create_unicode_buffer(1024)
    try:
        _fn(psl, 0, ctypes.c_long, ctypes.POINTER(GUID), ctypes.POINTER(c_void_p))(
            psl, byref(IID_IPERSISTFILE), byref(ppf))
        _fn(ppf, 5, ctypes.c_long, wintypes.LPCWSTR, ctypes.c_int)(ppf, str(lnk_path), 0)
        out = {}
        _fn(psl, 3, ctypes.c_long, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)(
            psl, buf, 1024, None, 0)
        out["target"] = buf.value
        wbuf = ctypes.create_unicode_buffer(1024)
        _fn(psl, 8, ctypes.c_long, ctypes.c_wchar_p, ctypes.c_int)(psl, wbuf, 1024)
        out["workdir"] = wbuf.value
        ibuf = ctypes.create_unicode_buffer(1024)
        idx = ctypes.c_int(0)
        _fn(psl, 16, ctypes.c_long, ctypes.c_wchar_p, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int))(psl, ibuf, 1024, byref(idx))
        out["icon"] = ibuf.value
        out["icon_index"] = idx.value
        return out
    finally:
        if ppf:
            _fn(ppf, 2, ctypes.c_ulong)(ppf)
        _fn(psl, 2, ctypes.c_ulong)(psl)
