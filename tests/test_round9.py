"""第九轮：支撑密度场、打印记录。"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import store  # noqa: E402


@pytest.fixture()
def overhang_proj(tmp_path):
    post = trimesh.creation.box(extents=(6, 6, 30))
    post.apply_translation([0, 0, 15])
    cap = trimesh.creation.box(extents=(40, 40, 3))
    cap.apply_translation([0, 0, 31.5])
    src = tmp_path / "tee.stl"
    trimesh.util.concatenate([post, cap]).export(str(src))
    proj = store.create("tee.stl", src)
    yield proj
    store.delete(proj["id"])


# ---------------------------------------------------------------- 支撑密度
def test_support_density_field(overhang_proj):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.get(f"/api/projects/{overhang_proj['id']}/heatmap?kind=support&max_faces=30000")
        assert r.status_code == 200
        d = r.json()
        assert d["kind"] == "support"
        meta = d["meta"]
        assert meta["count"] > 0, "悬臂结构应该生成支撑"
        assert meta["max"] > 0
        vals = np.frombuffer(base64.b64decode(d["values"]), dtype=np.float32)
        assert len(vals) == d["vertex_count"]
        assert vals.max() > 0


def test_support_density_empty_for_flat_box(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    src = tmp_path / "box.stl"
    trimesh.creation.box(extents=(20, 20, 20)).export(str(src))
    proj = store.create("box.stl", src)
    try:
        with TestClient(app) as c:
            d = c.get(f"/api/projects/{proj['id']}/heatmap?kind=support&max_faces=20000").json()
            assert d["meta"]["count"] == 0
            assert "没有生成支撑" in d["meta"]["note"]
    finally:
        store.delete(proj["id"])


# ---------------------------------------------------------------- 打印记录
def test_print_record_accumulates(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    src = tmp_path / "p.stl"
    trimesh.creation.box(extents=(10, 10, 10)).export(str(src))
    proj = store.create("p.stl", src)
    try:
        with TestClient(app) as c:
            r1 = c.post(f"/api/projects/{proj['id']}/print-record",
                        json={"result": "success", "note": "第一版"})
            assert r1.status_code == 200
            r2 = c.post(f"/api/projects/{proj['id']}/print-record",
                        json={"result": "fail", "note": "支撑倒了"})
            d = r2.json()
            assert d["total"] == 2
            assert d["success"] == 1
            assert d["rate"] == pytest.approx(50.0)
            got = c.get(f"/api/projects/{proj['id']}/print-record").json()
            assert len(got["prints"]) == 2
            assert got["prints"][1]["note"] == "支撑倒了"
    finally:
        store.delete(proj["id"])


def test_print_record_rejects_bad_result(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    src = tmp_path / "q.stl"
    trimesh.creation.box(extents=(5, 5, 5)).export(str(src))
    proj = store.create("q.stl", src)
    try:
        with TestClient(app) as c:
            assert c.post(f"/api/projects/{proj['id']}/print-record",
                          json={"result": "maybe"}).status_code == 400
    finally:
        store.delete(proj["id"])


def test_print_record_capped(tmp_path):
    """记录不能无限增长，只保留最近 30 条。"""
    from fastapi.testclient import TestClient

    from app.main import app

    src = tmp_path / "r.stl"
    trimesh.creation.box(extents=(5, 5, 5)).export(str(src))
    proj = store.create("r.stl", src)
    try:
        with TestClient(app) as c:
            for i in range(35):
                c.post(f"/api/projects/{proj['id']}/print-record",
                       json={"result": "success", "note": f"#{i}"})
            d = c.get(f"/api/projects/{proj['id']}/print-record").json()
            assert d["total"] == 30
            assert d["prints"][-1]["note"] == "#34"
    finally:
        store.delete(proj["id"])


def test_print_record_missing_project():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        assert c.get("/api/projects/nope/print-record").status_code == 404
        assert c.post("/api/projects/nope/print-record", json={"result": "success"}).status_code == 404
