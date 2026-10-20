"""第十二轮：分件装配预览、API 版本化别名。"""
from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import numpy as np
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
def tall_proj(tmp_path):
    src = tmp_path / "tall.stl"
    trimesh.creation.box(extents=(40, 40, 120)).export(str(src))
    proj = store.create("tall.stl", src)
    yield proj
    store.delete(proj["id"])


# ---------------------------------------------------------------- 装配预览
def test_assembly_requires_split(tall_proj):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.get(f"/api/projects/{tall_proj['id']}/assembly")
        assert r.status_code == 404
        assert "分件" in r.json()["detail"]


def test_assembly_after_split(tall_proj):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        jid = c.post(f"/api/projects/{tall_proj['id']}/split",
                     json={"max_size": 50, "axis": "z"}).json()["id"]
        st = _wait(c, jid)
        assert st["status"] == "done", st

        r = c.get(f"/api/projects/{tall_proj['id']}/assembly")
        assert r.status_code == 200, r.text
        d = r.json()
        assert len(d["parts"]) == 3
        assert d["face_count"] > 0
        assert d["vertex_count"] > 0

        verts = np.frombuffer(base64.b64decode(d["vertices"]), dtype=np.float32).reshape(-1, 3)
        colors = np.frombuffer(base64.b64decode(d["colors"]), dtype=np.float32).reshape(-1, 3)
        assert len(verts) == d["vertex_count"]
        assert len(colors) == d["vertex_count"]
        # 三块颜色不同，前端才能区分
        uniq = {tuple(np.round(c, 3)) for c in colors[::max(1, len(colors) // 50)]}
        assert len(uniq) >= 2


def test_assembly_restores_original_z(tall_proj):
    """装配预览要按切割时的位置摆回去，整体 z 范围应接近原始 0~120。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        jid = c.post(f"/api/projects/{tall_proj['id']}/split",
                     json={"max_size": 50, "axis": "z"}).json()["id"]
        assert _wait(c, jid)["status"] == "done"
        d = c.get(f"/api/projects/{tall_proj['id']}/assembly").json()
        verts = np.frombuffer(base64.b64decode(d["vertices"]), dtype=np.float32).reshape(-1, 3)
        zmin, zmax = float(verts[:, 2].min()), float(verts[:, 2].max())
        # trimesh 的 box 默认居中在原点，原始 z 范围就是 -60..60；
        # 装配后必须回到这个范围，而不是三块各自落在 0..40
        assert zmin == pytest.approx(-60.0, abs=1.0)
        assert zmax == pytest.approx(60.0, abs=1.0)


# ---------------------------------------------------------------- API 版本
def test_api_v1_alias():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        a = c.get("/api/health")
        b = c.get("/api/v1/health")
        assert a.status_code == 200 and b.status_code == 200
        assert a.json()["version"] == b.json()["version"]
        assert c.get("/api/v1/projects").status_code == 200
        assert c.get("/api/v1/workflow").status_code == 200


def test_api_v1_alias_keeps_query():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.get("/api/v1/logs?lines=5")
        assert r.status_code == 200
        assert isinstance(r.json()["lines"], list)


def test_api_v1_unknown_route_still_404():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        assert c.get("/api/v1/nonexistent-route").status_code == 404
