#!/usr/bin/env python3
"""一键启动：python run.py [端口]

端口优先级：命令行参数 > TPF_PORT 环境变量 > 8000
监听地址：TPF_HOST（默认 127.0.0.1，容器里设 0.0.0.0）
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("TPF_PORT", "8000"))
    host = os.environ.get("TPF_HOST", "127.0.0.1")
    # 开发时设 TPF_RELOAD=1，改完代码自动重载，不用手动重启
    reload = os.environ.get("TPF_RELOAD", "").strip().lower() in ("1", "true", "yes", "on")
    import uvicorn

    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"\n  3D Print Forge  →  http://{shown}:{port}"
          f"{'  (自动重载已开启)' if reload else ''}\n")
    uvicorn.run("app.main:app", host=host, port=port, log_level="warning",
                reload=reload, reload_dirs=[str(ROOT / "app")] if reload else None)


if __name__ == "__main__":
    main()
