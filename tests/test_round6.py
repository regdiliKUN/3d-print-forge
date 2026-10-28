"""第六轮：质量评分、截面曲线、模型库字段、批量打包、报告增强。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pytest
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import analysis, store  # noqa: E402


def _report(mesh, sample=600):
    items = [fn(mesh, {"sample_faces": sample}) for fn in analysis.CHECKS]
    return {"items": items}


# ---------------------------------------------------------------- 质量评分
def test_quality_score_clean_mesh_is_a():
    q = analysis.quality_score(_report(trimesh.creation.box(extents=(30, 30, 30))))
    assert q["score"] >= 95
    assert q["grade"] == "A"
    assert "可以直接打印" in q["verdict"]
    assert len(q["parts"]) == 7
    assert sum(p["max"] for p in q["parts"]) == 100


def test_quality_score_thin_wall_deducts():
    q = analysis.quality_score(_report(trimesh.creation.box(extents=(30, 30, 0.5))))
    assert q["score"] < 95
    thick = next(p for p in q["parts"] if p["name"] == "壁厚")
    assert thick["score"] < thick["max"]


def test_quality_score_broken_mesh_is_c_or_worse():
    m = trimesh.creation.icosphere(subdivisions=2, radius=15)
    m.update_faces(np.arange(len(m.faces)) > 2)
    m.invert()
    q = analysis.quality_score(_report(m))
    assert q["grade"] in ("C", "D")
    assert q["score"] < 75


def test_quality_parts_sum_to_score():
    q = analysis.quality_score(_report(trimesh.creation.icosphere(subdivisions=2, radius=12)))
    assert q["score"] == pytest.approx(sum(p["score"] for p in q["parts"]), abs=0.15)


def test_analyze_report_contains_quality(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    src = tmp_path / "q.stl"
    trimesh.creation.box(extents=(20, 20, 20)).export(str(src))
    proj = store.create("q.stl", src)
    try:
        with TestClient(app) as c:
            jid = c.post(f"/api/projects/{proj['id']}/analyze", json={}).json()["id"]
            for _ in range(120):
                st = c.get(f"/api/jobs/{jid}").json()
                if st["status"] in ("done", "failed"):
                    break
                time.sleep(0.2)
            rep = c.get(f"/api/projects/{proj['id']}").json()["analysis"]
            assert rep["quality"]["grade"] in ("A", "B", "C", "D")
    finally:
        store.delete(proj["id"])


# ---------------------------------------------------------------- 截面曲线
def test_section_profile_monotonic_for_cone(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    src = tmp_path / "cone.stl"
    cone = trimesh.creation.cone(radius=25, height=80, sections=48)
    cone.apply_translation([0, 0, 40])
    cone.export(str(src))
    proj = store.create("cone.stl", src)
    try:
        with TestClient(app) as c:
            d = c.get(f"/api/projects/{proj['id']}/section-profile?layers=20").json()
            areas = [r["area"] for r in d["layers"]]
            assert len(areas) == 20
            # 圆锥从下往上截面积递减（允许噪声）
            assert areas[0] > areas[-1]
            assert d["max_area"] >= d["min_area"]
    finally:
        store.delete(proj["id"])


def test_section_profile_layers_bounds(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    src = tmp_path / "b.stl"
    trimesh.creation.box(extents=(10, 10, 10)).export(str(src))
    proj = store.create("b.stl", src)
    try:
        with TestClient(app) as c:
            assert c.get(f"/api/projects/{proj['id']}/section-profile?layers=2").status_code == 422
            assert c.get(f"/api/projects/{proj['id']}/section-profile?layers=500").status_code == 422
            assert c.get(f"/api/projects/{proj['id']}/section-profile?layers=30").status_code == 200
    finally:
        store.delete(proj["id"])


# ---------------------------------------------------------------- 模型库字段
def test_patch_favorite_and_tags(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    src = tmp_path / "f.stl"
    trimesh.creation.box(extents=(10, 10, 10)).export(str(src))
    proj = store.create("f.stl", src)
    try:
        with TestClient(app) as c:
            r = c.patch(f"/api/projects/{proj['id']}",
                        json={"favorite": True, "tags": ["手办", "测试"], "name": "新名"})
            assert r.status_code == 200
            assert r.json()["favorite"] is True
            assert r.json()["tags"] == ["手办", "测试"]
            assert r.json()["name"] == "新名"
            listed = [x for x in c.get("/api/projects").json()["projects"] if x["id"] == proj["id"]][0]
            assert listed["favorite"] is True
            assert listed["tags"] == ["手办", "测试"]
    finally:
        store.delete(proj["id"])


def test_patch_rejects_empty_name(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    src = tmp_path / "g.stl"
    trimesh.creation.box(extents=(5, 5, 5)).export(str(src))
    proj = store.create("g.stl", src)
    try:
        with TestClient(app) as c:
            assert c.patch(f"/api/projects/{proj['id']}", json={"name": "   "}).status_code == 400
            assert c.patch(f"/api/projects/{proj['id']}", json={}).status_code == 400
    finally:
        store.delete(proj["id"])


def test_patch_does_not_touch_current(tmp_path):
    """白名单之外字段必须被忽略，防止误改工作副本路径。"""
    from fastapi.testclient import TestClient

    from app.main import app

    src = tmp_path / "h.stl"
    trimesh.creation.box(extents=(5, 5, 5)).export(str(src))
    proj = store.create("h.stl", src)
    try:
        with TestClient(app) as c:
            c.patch(f"/api/projects/{proj['id']}", json={"current": "/etc/passwd", "favorite": True})
            assert c.get(f"/api/projects/{proj['id']}").json()["current"] == str(src)
    finally:
        store.delete(proj["id"])


# ---------------------------------------------------------------- 批量打包
def test_batch_export_zip(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    a = tmp_path / "a.stl"
    b = tmp_path / "b.stl"
    trimesh.creation.box(extents=(10, 10, 10)).export(str(a))
    trimesh.creation.icosphere(subdivisions=1, radius=6).export(str(b))
    p1 = store.create("a.stl", a)
    p2 = store.create("b.stl", b)
    try:
        with TestClient(app) as c:
            r = c.get(f"/api/batch/export.zip?ids={p1['id']},{p2['id']}")
            assert r.status_code == 200
            assert r.headers["content-type"] == "application/zip"
            import io
            import zipfile

            z = zipfile.ZipFile(io.BytesIO(r.content))
            names = z.namelist()
            assert any(n.endswith(".stl") for n in names)
            assert "manifest.json" in names
    finally:
        store.delete(p1["id"])
        store.delete(p2["id"])


def test_batch_export_empty(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.get("/api/batch/export.zip?ids=nonexistent")
        assert r.status_code == 404


# ---------------------------------------------------------------- 报告增强
def test_report_contains_quality_block(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    src = tmp_path / "r.stl"
    trimesh.creation.box(extents=(20, 20, 20)).export(str(src))
    proj = store.create("r.stl", src)
    try:
        with TestClient(app) as c:
            jid = c.post(f"/api/projects/{proj['id']}/analyze", json={}).json()["id"]
            for _ in range(120):
                if c.get(f"/api/jobs/{jid}").json()["status"] in ("done", "failed"):
                    break
                time.sleep(0.2)
            html = c.get(f"/api/projects/{proj['id']}/report.html").text
            assert "网格质量" in html
            assert "/100" in html
            assert "级" in html
    finally:
        store.delete(proj["id"])
