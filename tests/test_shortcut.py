"""桌面快捷方式模块的轻量测试（不真的建 .lnk，避免依赖桌面环境）。"""
from __future__ import annotations

from app import shortcut


def test_ensure_launcher_creates_cmd():
    p = shortcut.ensure_launcher()
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "run.py" in text
    assert ".venv" in text  # 优先用项目自带 venv


def test_status_shape():
    s = shortcut.shortcut_status()
    for k in ("supported", "desktop", "lnk", "exists", "launcher", "root"):
        assert k in s
    assert str(s["launcher"]).endswith(".cmd")
    assert str(s["root"])  # 项目根非空


def test_launcher_name_constants():
    assert shortcut.LAUNCHER_NAME.endswith(".cmd")
    assert shortcut.LNK_NAME.endswith(".lnk")
