#!/usr/bin/env python3
"""一键启动：python run.py [端口]"""
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
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    import uvicorn

    print(f"\n  3D Print Forge  →  http://127.0.0.1:{port}\n")
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
