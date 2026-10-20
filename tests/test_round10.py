"""第十轮：任意平面分割、手动挖孔、支撑去重、项目打包迁移。"""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import process, store  # noqa: E402


@pytest.fixture()
def cube(tmp_path):
    src = tmp_path / "cube.stl"
    trimesh.creation.box(extents=(40, 40, 40)).export(str(src))
    proj = store.create("cube.stl", src)
    yield proj
    store.delete(proj["id"])


# ---------------------------------------------------------------- 平面分割
def test_slice_plane_halves_volume(cube):
    from fastapi.testclient import TestClient

    from app.main import app

    other_id = None
    try:
        with TestClient(app) as c:
            r = c.post(f"/api/projects/{cube['id']}/slice-plane",
                       json={"origin": [0, 0, 0], "normal": [0, 0, 1]})
            assert r.status_code == 200, r.text
            d = r.json()
            other_id = d["other"]["id"]
            total = d["keep"]["volume_mm3"] + d["other"]["volume_mm3"]
            assert total == pytest.approx(64000, rel=0.02)
            assert d["keep"]["volume_mm3"] == pytest.approx(32000, rel=0.05)
            # 另一侧成为独立项目
            assert c.get(f"/api/projects/{other_id}").status_code == 200
    finally:
        if other_id:
            store.delete(other_id)


def test_slice_plane_diagonal(cube):
    from fastapi.testclient import TestClient

    from app.main import app

    other_id = None
    try:
        with TestClient(app) as c:
            r = c.post(f"/api/projects/{cube['id']}/slice-plane",
                       json={"origin": [0, 0, 0], "normal": [1, 1, 0]})
            assert r.status_code == 200
            other_id = r.json()["other"]["id"]
            # 斜切后两半体积仍应守恒
            total = r.json()["keep"]["volume_mm3"] + r.json()["other"]["volume_mm3"]
            assert total == pytest.approx(64000, rel=0.02)
    finally:
        if other_id:
            store.delete(other_id)


def test_slice_plane_outside_model(cube):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post(f"/api/projects/{cube['id']}/slice-plane",
                   json={"origin": [0, 0, 500], "normal": [0, 0, 1]})
        assert r.status_code == 400


def test_slice_plane_zero_normal(cube):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post(f"/api/projects/{cube['id']}/slice-plane",
                   json={"origin": [0, 0, 0], "normal": [0, 0, 0]})
        assert r.status_code == 400


# ---------------------------------------------------------------- 挖孔
def test_bore_through_hole_removes_volume(cube):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post(f"/api/projects/{cube['id']}/bore",
                   json={"points": [[0, 0, 20]], "diameter": 8, "depth": 0})
        assert r.status_code == 200, r.text
        d = r.json()
        # φ8 穿透 40mm 的圆柱约 2011mm³
        removed = 64000 - d["volume_mm3"]
        assert removed == pytest.approx(2011, rel=0.12)
        assert d["watertight"] is True


def test_bore_blind_hole(cube):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post(f"/api/projects/{cube['id']}/bore",
                   json={"points": [[15, 15, 20]], "diameter": 4, "depth": 10})
        assert r.status_code == 200
        removed = 64000 - r.json()["volume_mm3"]
        # φ4 × 深 10 ≈ 126mm³
        assert 60 < removed < 220


def test_bore_rejects_empty_points(cube):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        assert c.post(f"/api/projects/{cube['id']}/bore",
                      json={"points": [], "diameter": 3}).status_code == 400
        assert c.post(f"/api/projects/{cube['id']}/bore",
                      json={"diameter": 3}).status_code == 400


def test_bore_rejects_zero_diameter(cube):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        assert c.post(f"/api/projects/{cube['id']}/bore",
                      json={"points": [[0, 0, 20]], "diameter": 0}).status_code == 400


# ---------------------------------------------------------------- 支撑去重
def test_support_points_deduped():
    """面积加权采样会重复抽到同一个大面，同一位置不能叠多根柱。"""
    post = trimesh.creation.box(extents=(6, 6, 30))
    post.apply_translation([0, 0, 15])
    cap = trimesh.creation.box(extents=(40, 40, 3))
    cap.apply_translation([0, 0, 31.5])
    m = trimesh.util.concatenate([post, cap])
    _, info = process.build_supports(m, 45.0, 1.0, 3.0, 400)
    segs = info["segments"]
    assert len(segs) > 0
    pts = np.asarray([[s[0], s[1]] for s in segs])
    # 任意两根柱的 XY 间距不应小于 diameter*1.5 的一半
    for i in range(min(len(pts), 120)):
        for j in range(i + 1, min(len(pts), 120)):
            d = float(np.hypot(*(pts[i] - pts[j])))
            assert d > 0.5, f"第 {i} 和第 {j} 根支撑重叠，间距 {d:.2f}mm"


# ---------------------------------------------------------------- 打包迁移
def test_bundle_roundtrip(cube):
    from fastapi.testclient import TestClient

    from app.main import app

    new_id = None
    try:
        with TestClient(app) as c:
            r = c.get(f"/api/projects/{cube['id']}/bundle")
            assert r.status_code == 200
            z = zipfile.ZipFile(io.BytesIO(r.content))
            names = z.namelist()
            assert "project.json" in names
            assert "history.json" in names
            assert any(n.startswith("model.") for n in names)

            r2 = c.post("/api/projects/import-bundle",
                        files={"file": ("b.zip", r.content, "application/zip")})
            assert r2.status_code == 200, r2.text
            new_id = r2.json()["id"]
            assert c.get(f"/api/projects/{new_id}").status_code == 200
    finally:
        if new_id:
            store.delete(new_id)


def test_bundle_rejects_zip_slip():
    """包内不能有 ../ 这类路径，否则解压能写到项目目录外。"""
    from fastapi.testclient import TestClient

    from app.main import app

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("project.json", '{"name":"evil"}')
        z.writestr("model.stl", b"solid x\nendsolid x\n")
        z.writestr("../../evil.txt", "pwned")
    with TestClient(app) as c:
        r = c.post("/api/projects/import-bundle",
                   files={"file": ("evil.zip", buf.getvalue(), "application/zip")})
        assert r.status_code == 400
        assert "非法路径" in r.json()["detail"]


def test_bundle_rejects_missing_manifest():
    from fastapi.testclient import TestClient

    from app.main import app

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("model.stl", b"solid x\nendsolid x\n")
    with TestClient(app) as c:
        r = c.post("/api/projects/import-bundle",
                   files={"file": ("x.zip", buf.getvalue(), "application/zip")})
        assert r.status_code == 400
        assert "project.json" in r.json()["detail"]


def test_bundle_rejects_non_zip():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post("/api/projects/import-bundle",
                   files={"file": ("x.zip", b"not a zip", "application/zip")})
        assert r.status_code == 400
