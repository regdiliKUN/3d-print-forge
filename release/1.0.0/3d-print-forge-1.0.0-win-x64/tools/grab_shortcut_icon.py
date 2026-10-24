"""把 .lnk 在资源管理器里实际显示的图标抓出来，确认新图标已生效。

用 SHGetFileInfoW 拿系统为该快捷方式解析出的 HICON，再 DrawIconEx 到 DIB，
最后存 PNG。这样验证的是「用户眼睛看到的那个图标」，而不是 ico 文件本身。
"""
from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_int, c_void_p, wintypes

from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
shell32 = ctypes.windll.shell32

SHGFI_ICON = 0x000000100
SHGFI_LARGEICON = 0x000000000
SHGFI_SMALLICON = 0x000000001
DI_NORMAL = 0x0003
DIB_RGB_COLORS = 0


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon", wintypes.HICON),
        ("iIcon", c_int),
        ("dwAttributes", wintypes.DWORD),
        ("szDisplayName", wintypes.WCHAR * 260),
        ("szTypeName", wintypes.WCHAR * 80),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def grab(lnk_path: str, size: int = 32, small: bool = False) -> Image.Image:
    # 64 位下必须显式声明签名，否则指针参数会被截断导致调用失败
    shell32.SHGetFileInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                       POINTER(SHFILEINFOW), wintypes.UINT, wintypes.UINT]
    shell32.SHGetFileInfoW.restype = ctypes.c_size_t
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.DrawIconEx.argtypes = [wintypes.HDC, c_int, c_int, wintypes.HICON, c_int, c_int,
                                  wintypes.UINT, wintypes.HANDLE, wintypes.UINT]
    user32.DrawIconEx.restype = wintypes.BOOL
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, POINTER(BITMAPINFO), wintypes.UINT,
                                       POINTER(c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ

    shfi = SHFILEINFOW()
    flags = SHGFI_ICON | (SHGFI_SMALLICON if small else SHGFI_LARGEICON)
    shell32.SHGetFileInfoW(ctypes.c_wchar_p(lnk_path), 0, byref(shfi),
                           ctypes.sizeof(shfi), flags)
    hicon = shfi.hIcon
    if not hicon:
        raise RuntimeError("SHGetFileInfo 没有返回图标")

    hdc = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = size
    bmi.bmiHeader.biHeight = -size
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    bits = c_void_p()
    hbmp = gdi32.CreateDIBSection(hdc, byref(bmi), DIB_RGB_COLORS, byref(bits), None, 0)
    old = gdi32.SelectObject(mem, hbmp)
    gdi32.PatBlt(mem, 0, 0, size, size, 0x00FF0062)  # WHITENESS
    user32.DrawIconEx(mem, 0, 0, hicon, size, size, 0, None, DI_NORMAL)
    buf = ctypes.string_at(bits, size * size * 4)
    gdi32.SelectObject(mem, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(None, hdc)
    user32.DestroyIcon(hicon)
    return Image.frombuffer("RGBA", (size, size), buf, "raw", "BGRA", 0, 1)


if __name__ == "__main__":
    import sys

    src = sys.argv[1] if len(sys.argv) > 1 else r"D:\Desktop\3D 打印建模工具.lnk"
    dst = sys.argv[2] if len(sys.argv) > 2 else r"I:\PrinterApp\assets\icon\shortcut-icon-proof.png"
    big = grab(src, 32, small=False)
    small = grab(src, 16, small=True)
    canvas = Image.new("RGBA", (32 + 16 + 36, 48), (255, 255, 255, 0))
    canvas.paste(big, (4, 8), big)
    canvas.paste(small, (44, 16), small)
    canvas.save(dst)
    print("saved", dst)
