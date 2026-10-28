"""项目持久化：每个导入的模型一个 JSON 记录，原子写入。"""
from __future__ import annotations

import copy
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
        "analysis_stale": False,
        "advice": None,
        "stats": {},
        "tags": [],
        "favorite": False,
    }
    with _LOCK:
        _write(proj)
    return proj


class Record(dict):
    """读出来的项目记录，带一份「读的时候长什么样」的快照。

    有了快照，`save()` 就能算出调用方到底改了哪几个字段，
    写回时只覆盖这几个字段，而不是把整个对象拍回去。
    没有快照时（例如 create() 刚造出来的新记录）退回整条写入。
    """

    base: dict | None = None


def _write(proj: dict) -> None:
    proj["updated"] = _now()
    proj["rev"] = int(proj.get("rev") or 0) + 1
    p = _path(proj["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(proj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    base = getattr(proj, "base", None)
    if base is not None:
        base.clear()
        base.update({k: v for k, v in proj.items() if k != "base"})


def save(proj: dict) -> dict:
    """写回一条记录；只覆盖调用方真正改过的字段。

    旧实现是整条覆盖。两个请求各改不同字段时，后写的会把先写的抹掉：
    实测「另一个请求刚打了标签 + 收藏」，紧接着一次 save 就把它们清空了，
    而调用方只是改了个名字。这类丢数据不报错、也查不出来。
    """
    with _LOCK:
        base = getattr(proj, "base", None)
        if base is None:
            _write(proj)
            return proj
        cur = get(proj["id"])
        if cur is None:
            _write(proj)
            return proj
        changed = {k: v for k, v in proj.items() if k != "base" and base.get(k) != v}
        if not changed:
            return cur
        cur.update(changed)
        cur["rev"] = max(int(cur.get("rev") or 0), int(proj.get("rev") or 0))
        if changed.get("analysis"):
            cur["analysis_stale"] = False
        _write(cur)
        return cur


def get(pid: str, snapshot: bool = True) -> dict | None:
    p = _path(pid)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not snapshot:
        return data
    rec = Record(data)
    rec.base = copy.deepcopy(data)
    return rec


def update(pid: str, **kw: Any) -> dict | None:
    with _LOCK:
        proj = get(pid)
        if proj is None:
            return None
        proj.update(kw)
        # 写入了新报告就等于重新量过几何，过期标记要清掉，
        # 否则修复后的复检报告仍会带着「已过期」横幅
        if kw.get("analysis"):
            proj["analysis_stale"] = False
        _write(proj)
        return proj


def add_history(pid: str, action: str, params: dict, out: Path, note: str = "") -> dict | None:
    """记一条操作历史，并把 current 指向新文件。

    任何写进历史的动作都改了几何（缩放/旋转/居中/打孔/布尔/合并/切片/处理/修复），
    因此**旧的检测报告与参数建议立即作废**：报告里存的是上一版文件的
    extents/volume/壁厚，建议层又拿这些数字去算耗材。
    实测：20mm 立方体缩放到 60mm 之后，建议仍按 20mm 给出 9.9g，
    真实值约 79g——差了 8 倍，而且界面上一切正常，没有任何过期提示。
    这里只打标记、不删报告：报告本身还有参考价值（比如「当时有哪些问题」），
    由展示层决定怎么提示；建议层看到标记就重新量几何。
    """
    with _LOCK:
        proj = get(pid)
        if proj is None:
            return None
        proj["history"].append(
            {"at": _now(), "action": action, "params": params, "out": str(out), "note": note}
        )
        proj["current"] = str(out)
        proj["analysis_stale"] = True
        proj["advice"] = None
        _write(proj)
        return proj


def list_all() -> list[dict]:
    out = []
    for p in sorted(config.PROJECTS.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            # 文件列表要展示「类型 / 大小」：类型跟显示名走（最贴近用户认知），
            # 大小优先取原始文件（导入的那个），没有再退回工作副本。
            orig = Path(d.get("original") or d.get("source") or "")
            cur = Path(d.get("current") or d.get("source") or "")
            pick = orig if str(orig) and orig.exists() else cur
            try:
                size_bytes = pick.stat().st_size if str(pick) and pick.exists() else 0
            except OSError:
                size_bytes = 0
            suffix = Path(d.get("name") or "").suffix.lower().lstrip(".") or pick.suffix.lower().lstrip(".")
            out.append(
                {
                    "id": d["id"],
                    "name": d["name"],
                    "created": d["created"],
                    "updated": d["updated"],
                    # issues 以前取 summary.total，那是「检测项数量」恒等于 7，
                    # 前端按它排序等于没排。这里改成真正有问题的项数。
                    "issues": _issue_count(d.get("analysis") or {}),
                    "severity": (d.get("analysis") or {}).get("summary", {}).get("worst", None),
                    "analysis_stale": bool(d.get("analysis_stale")),
                    "history": len(d.get("history", [])),
                    "favorite": bool(d.get("favorite")),
                    "tags": d.get("tags") or [],
                    "volume_cm3": round(((d.get("analysis") or {}).get("stats") or {}).get("volume_mm3", 0) / 1000, 2),
                    "size_bytes": size_bytes,
                    "file_type": (suffix or "?").upper(),
                }
            )
        except Exception as e:  # noqa: BLE001
            # 记录坏了不能就当它不存在：文件还在磁盘上、还能删，
            # 但旧实现直接 continue，界面上这个项目凭空消失，
            # 用户既看不到也删不掉，只能去翻 data/state/projects。
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
                size = p.stat().st_size
            except OSError:
                mtime, size = None, 0
            out.append({
                "id": p.stem,
                "name": f"{p.stem}（记录损坏）",
                "created": mtime,
                "updated": mtime,
                "issues": None,
                "severity": "error",
                "history": 0,
                "favorite": False,
                "tags": [],
                "volume_cm3": 0,
                "size_bytes": size,
                "file_type": "JSON",
                "damaged": True,
                "damaged_reason": f"{type(e).__name__}: {str(e)[:120]}",
            })
    return out


def _issue_count(rep: dict) -> int | None:
    """有问题的检测项数（warning + error），没检测过就是 None。

    别拿 summary.total 充数：那是「跑了 7 项」，不是「有 7 个问题」。
    """
    items = rep.get("items") or []
    if not items:
        return None
    return sum(1 for it in items if it.get("severity") in ("warning", "error"))


def find_by_hash(sha256: str) -> dict | None:
    """按内容哈希找已导入项目（只认新版本写入的 stats.sha256）。"""
    if not sha256:
        return None
    for p in config.PROJECTS.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (d.get("stats") or {}).get("sha256") == sha256:
            return d
    return None


def _referenced_paths(exclude_pid: str | None = None) -> set[str]:
    """收集所有项目仍在引用的文件绝对路径（用于安全清理孤立文件）。"""
    refs: set[str] = set()
    for p in config.PROJECTS.glob("*.json"):
        if exclude_pid and p.stem == exclude_pid:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for key in ("source", "original", "current"):
            if d.get(key):
                refs.add(str(Path(d[key]).resolve()))
        for h in d.get("history") or []:
            if isinstance(h, dict) and h.get("out"):
                refs.add(str(Path(h["out"]).resolve()))
    return refs


def _inside_data(path: Path) -> bool:
    try:
        path.resolve().relative_to(config.DATA.resolve())
        return True
    except (ValueError, OSError):
        return False


def cleanup_history(pid: str, keep: int = 5) -> dict | None:
    """清理较老的历史工作文件，保留最近 keep 个版本。

    历史记录本身保留（谁在什么时候做了什么仍有审计价值），只把已被新版本
    取代、且没有任何项目再引用的中间 STL 删掉，并在记录上标记 out_removed。
    当前文件、原始文件、源文件永远不删；被其它项目引用的共享文件也不删。
    """
    with _LOCK:
        proj = get(pid)
        if proj is None:
            return None
        hist = proj.get("history") or []
        keep = max(1, min(int(keep), 50))
        protected: set[str] = set()
        for key in ("source", "original", "current"):
            if proj.get(key):
                try:
                    protected.add(str(Path(proj[key]).resolve()))
                except OSError:
                    pass
        for h in hist[-keep:] if hist else []:
            if isinstance(h, dict) and h.get("out"):
                try:
                    protected.add(str(Path(h["out"]).resolve()))
                except OSError:
                    pass
        refs = _referenced_paths(exclude_pid=pid)
        removed = 0
        freed = 0
        for h in (hist[:-keep] if len(hist) > keep else []):
            if not isinstance(h, dict) or h.get("out_removed"):
                continue
            out = h.get("out")
            if not out:
                continue
            try:
                rp = Path(out).resolve()
            except OSError:
                continue
            if str(rp) in protected or str(rp) in refs:
                continue
            if not (rp.exists() and rp.is_file() and _inside_data(rp)):
                h["out_removed"] = True
                continue
            try:
                size = rp.stat().st_size
                rp.unlink()
                freed += size
                removed += 1
                h["out_removed"] = True
            except OSError:
                continue
        if removed:
            _write(proj)
        return {
            "ok": True,
            "removed": removed,
            "freed_bytes": freed,
            "kept": min(len(hist), keep),
            "history": len(hist),
        }


def delete(pid: str, cleanup_files: bool = True) -> bool:
    """删除项目记录；默认顺带清理不再被任何项目引用的工作文件。

    只清理 data/ 目录内的文件：同一份导入文件可能被「复制项目」共享，
    因此必须先确认没有其他项目还引用它，绝不能直接 unlink。
    """
    with _LOCK:
        p = _path(pid)
        if not p.exists():
            return False
        proj: dict = {}
        try:
            proj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            proj = {}
        p.unlink()
        if not cleanup_files:
            return True
        # 先删记录再扫描引用，这样本项目自己的路径不会出现在 refs 里
        refs = _referenced_paths(exclude_pid=pid)
        candidates: set[Path] = set()
        for key in ("source", "original", "current"):
            if proj.get(key):
                candidates.add(Path(proj[key]))
        for h in proj.get("history") or []:
            if isinstance(h, dict) and h.get("out"):
                candidates.add(Path(h["out"]))
        for f in candidates:
            try:
                rp = f.resolve()
                if rp.exists() and rp.is_file() and _inside_data(rp) and str(rp) not in refs:
                    rp.unlink()
            except OSError:
                continue
        return True
