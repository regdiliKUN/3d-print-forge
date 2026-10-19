"""项目持久化：每个导入的模型一个 JSON 记录，原子写入。"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path(pid: str) -> Path:
    return config.PROJECTS / f"{pid}.json"


def create(name: str, source: Path) -> dict:
    pid = uuid.uuid4().hex[:12]
    proj = {
        "id": pid,
        "name": name,
        "created": _now(),
        "updated": _now(),
        "source": str(source),
        "original": str(source),
        "current": str(source),
        "history": [],
        "analysis": None,
        "advice": None,
        "stats": {},
        "tags": [],
    }
    with _LOCK:
        _write(proj)
    return proj


def _write(proj: dict) -> None:
    proj["updated"] = _now()
    p = _path(proj["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(proj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def save(proj: dict) -> dict:
    with _LOCK:
        _write(proj)
    return proj


def get(pid: str) -> dict | None:
    p = _path(pid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def update(pid: str, **kw: Any) -> dict | None:
    with _LOCK:
        proj = get(pid)
        if proj is None:
            return None
        proj.update(kw)
        _write(proj)
        return proj


def add_history(pid: str, action: str, params: dict, out: Path, note: str = "") -> dict | None:
    with _LOCK:
        proj = get(pid)
        if proj is None:
            return None
        proj["history"].append(
            {"at": _now(), "action": action, "params": params, "out": str(out), "note": note}
        )
        proj["current"] = str(out)
        _write(proj)
        return proj


def list_all() -> list[dict]:
    out = []
    for p in sorted(config.PROJECTS.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append(
                {
                    "id": d["id"],
                    "name": d["name"],
                    "created": d["created"],
                    "updated": d["updated"],
                    "issues": (d.get("analysis") or {}).get("summary", {}).get("total", None),
                    "severity": (d.get("analysis") or {}).get("summary", {}).get("worst", None),
                    "history": len(d.get("history", [])),
                }
            )
        except Exception:
            continue
    return out


def delete(pid: str) -> bool:
    with _LOCK:
        p = _path(pid)
        if p.exists():
            p.unlink()
            return True
    return False
