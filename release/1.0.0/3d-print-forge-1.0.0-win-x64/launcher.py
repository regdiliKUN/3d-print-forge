"""独立版启动入口（PyInstaller 打包目标）。

与 run.py 的区别：不依赖源码树和 venv，双击即用。启动流程——

1. 把 EXE 所在目录加入 sys.path（用户可在旁边放插件/配置）；
2. 探测可用端口（默认 8000，被占用就顺延，不跟别的服务抢）；
3. 后台线程轮询端口，服务一通就打开默认浏览器；
4. 前台跑 uvicorn，控制台保留日志；窗口关闭即停止服务。

数据目录由 app.config 决定：打包后固定为 EXE 同级的 data/，
换机器只要整个文件夹拷走，模型和项目记录都跟着走。
"""
from __future__ import annotations

import ctypes
import os
import socket
import sys
import threading
import time
import webbrowser

APP_NAME = "3D Print Forge"
VERSION = "1.0.0"
DEFAULT_PORT = 8000


def _setup_console() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleTitleW(f"{APP_NAME}  v{VERSION}")
        except Exception:
            pass


def _bootstrap_path() -> None:
    """打包后 _MEIPASS 已由引导器加入 sys.path；这里再补上 EXE 同级目录。"""
    if getattr(sys, "frozen", False):
        import pathlib

        base = str(pathlib.Path(sys.executable).resolve().parent)
        if base not in sys.path:
            sys.path.insert(1, base)


def _pick_port(preferred: int) -> int:
    """从 preferred 起找一个能绑上的端口（Windows 下不要设 SO_REUSEADDR，会误判）。"""
    for port in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def _open_browser_when_ready(port: int, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.4):
                break
        except OSError:
            time.sleep(0.2)
    else:
        print("  [!] 服务在 90 秒内没有就绪，请手动打开上面的地址。")
        return
    try:
        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception:
        pass


def main() -> int:
    _setup_console()
    _bootstrap_path()

    print()
    print(f"  {APP_NAME}  v{VERSION}")
    print("  " + "-" * 46)
    print("  正在加载运行库…" if getattr(sys, "frozen", False) else "  正在启动…")

    host = os.environ.get("TPF_HOST", "127.0.0.1")
    want = int(os.environ.get("TPF_PORT") or DEFAULT_PORT)
    port = _pick_port(want) if host in ("127.0.0.1", "localhost") else want

    try:
        from app import config
        from app.main import app as fastapi_app
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print()
        print(f"  [x] 启动失败：{exc}")
        print("  把上面的报错发给开发者，或换个目录重新解压程序包。")
        try:
            input("  按回车退出…")
        except Exception:
            pass
        return 1

    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"  地址   http://{shown}:{port}")
    if port != want:
        print(f"         （{want} 被占用，已自动换到 {port}）")
    print(f"  数据   {config.DATA}")
    print(f"  资源   {config.ROOT}")
    print("  " + "-" * 46)
    print("  浏览器会自动打开；关闭这个窗口即停止服务。")
    print()

    threading.Thread(target=_open_browser_when_ready, args=(port,), daemon=True).start()

    import uvicorn

    try:
        uvicorn.run(fastapi_app, host=host, port=port, log_level="warning",
                    access_log=False)
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"\n  [x] 服务异常退出：{exc}")
        try:
            input("  按回车退出…")
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
