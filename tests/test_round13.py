"""第十三轮：参数变更对比、分件间隙检查。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import store  # noqa: E402


def _wait(c, jid, tries=120):
    for _ in range(tries):
        st = c.get(f"/api/jobs/{jid}").json()
        if st["status"] in ("done", "failed", "cancelled"):
            return st
        time.sleep(0.2)
    return {"status": "timeout"}


@pytest.fixture()
def split_proj(tmp_path):
    src = tmp_path / "gap.stl"
    trimesh.creation.box(extents=(40, 40, 120)).export(str(src))
    proj = store.create("gap.stl", src)
    yield proj
    store.delete(proj["id"])


# ---------------------------------------------------------------- 参数对比
def test_param_history_tracks_changes(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    src = tmp_path / "p.stl"
    trimesh.creation.box(extents=(20, 20, 20)).export(str(src))
    proj = store.create("p.stl", src)
    try:
        with TestClient(app) as c:
            # 造两条带参数的历史，第二条改了壳厚
            out1 = tmp_path / "o1.stl"
            out2 = tmp_path / "o2.stl"
            trimesh.creation.box(extents=(20, 20, 20)).export(str(out1))
            trimesh.creation.box(extents=(20, 20, 20)).export(str(out2))
            store.add_history(proj["id"], "process",
                              {"wall_thickness": 2.0, "hollow": False}, out1, note="第一次")
            store.add_history(proj["id"], "process",
                              {"wall_thickness": 1.5, "hollow": True}, out2, note="第二次")

            d = c.get(f"/api/projects/{proj['id']}/param-history").json()
            assert d["count"] == 2
            rows = d["rows"]
            # 第一条没有「上一次」，不该有变更
            assert rows[0]["changed"] == {}
            # 第二条应该记下两处变化
            assert rows[1]["changed"]["wall_thickness"] == {"from": 2.0, "to": 1.5}
            assert rows[1]["changed"]["hollow"] == {"from": False, "to": True}
    finally:
        store.delete(proj["id"])


def test_param_history_ignores_new_keys(tmp_path):
    """新出现的参数不算「改变」，否则第一次出现的每个键都会刷屏。"""
    from fastapi.testclient import TestClient

    from app.main import app

    src = tmp_path / "q.stl"
    trimesh.creation.box(extents=(10, 10, 10)).export(str(src))
    proj = store.create("q.stl", src)
    try:
        with TestClient(app) as c:
            o1, o2 = tmp_path / "a.stl", tmp_path / "b.stl"
            trimesh.creation.box(extents=(10, 10, 10)).export(str(o1))
            trimesh.creation.box(extents=(10, 10, 10)).export(str(o2))
            store.add_history(proj["id"], "process", {"wall_thickness": 2.0}, o1)
            store.add_history(proj["id"], "process",
                              {"wall_thickness": 2.0, "new_param": 7}, o2)
            rows = c.get(f"/api/projects/{proj['id']}/param-history").json()["rows"]
            assert rows[1]["changed"] == {}
    finally:
        store.delete(proj["id"])


def test_param_history_empty(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    src = tmp_path / "e.stl"
    trimesh.creation.box(extents=(5, 5, 5)).export(str(src))
    proj = store.create("e.stl", src)
    try:
        with TestClient(app) as c:
            d = c.get(f"/api/projects/{proj['id']}/param-history").json()
            assert d["count"] == 0
            assert d["rows"] == []
    finally:
        store.delete(proj["id"])


# ---------------------------------------------------------------- 间隙检查
def test_check_assembly_requires_split(split_proj):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post(f"/api/projects/{split_proj['id']}/check-assembly", json={})
        assert r.status_code == 404
        assert "分件" in r.json()["detail"]


def test_check_assembly_no_interference(split_proj):
    """正常切开的两块不该有重叠体积。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        jid = c.post(f"/api/projects/{split_proj['id']}/split",
                     json={"max_size": 50, "axis": "z"}).json()["id"]
        assert _wait(c, jid)["status"] == "done"

        r = c.post(f"/api/projects/{split_proj['id']}/check-assembly", json={})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["parts"] == 3
        assert d["pairs_checked"] == 3  # C(3,2)
        assert d["ok"] is True
        assert d["interferences"] == []


def test_check_assembly_detects_overlap(split_proj):
    """人为把某块偏移挪小，让相邻两块重叠，检查必须报出来。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        jid = c.post(f"/api/projects/{split_proj['id']}/split",
                     json={"max_size": 50, "axis": "z"}).json()["id"]
        assert _wait(c, jid)["status"] == "done"

        p = store.get(split_proj["id"])
        asm = dict(p["assembly"])
        offs = [list(x) for x in asm["offsets"]]
        # 把第二块往下压 20mm，和第一块重叠
        offs[1][2] -= 20.0
        asm["offsets"] = offs
        store.update(split_proj["id"], assembly=asm)

        d = c.post(f"/api/projects/{split_proj['id']}/check-assembly", json={}).json()
        assert d["ok"] is False
        assert len(d["interferences"]) >= 1
        pair = d["interferences"][0]
        assert pair["volume_mm3"] > 0.5


def test_check_assembly_tolerance(split_proj):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        jid = c.post(f"/api/projects/{split_proj['id']}/split",
                     json={"max_size": 50, "axis": "z"}).json()["id"]
        assert _wait(c, jid)["status"] == "done"
        d = c.post(f"/api/projects/{split_proj['id']}/check-assembly",
                   json={"tolerance_mm3": 100.0}).json()
        assert d["tolerance_mm3"] == 100.0
