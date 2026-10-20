"""第二轮新功能测试：热力图、分件、版本对比、3MF、回滚。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import analysis, compare, split, store  # noqa: E402


# ---------------------------------------------------------------- 热力图
def test_thickness_field_thin_plate():
    """薄板的侧面射线会横穿板宽，必须用 min 聚合才不会把 0.6 冲淡成 24。"""
    m = trimesh.creation.box(extents=(40, 40, 0.6))
    f = analysis.thickness_field(m, 600)
    assert f["min"] == pytest.approx(0.6, abs=0.15)
    assert f["max"] == pytest.approx(0.6, abs=0.15)


def test_thickness_field_solid_sphere():
    m = trimesh.creation.icosphere(subdivisions=3, radius=20)
    f = analysis.thickness_field(m, 800)
    assert f["min"] == pytest.approx(40.0, abs=3.0)
    assert len(f["values"]) == len(m.vertices)


def test_thickness_field_step_plate():
    """台阶件：薄的那段必须被识别出来。"""
    a = trimesh.creation.box(extents=(20, 20, 1.0))
    b = trimesh.creation.box(extents=(20, 20, 8.0))
    b.apply_translation([0, 0, 4.5])
    m = trimesh.util.concatenate([a, b])
    f = analysis.thickness_field(m, 1200)
    assert f["min"] < 1.5


def test_overhang_field_range():
    m = trimesh.creation.icosphere(subdivisions=3, radius=15)
    f = analysis.overhang_field(m)
    assert 0 <= f["min"] <= 90
    assert f["max"] <= 180.5
    assert len(f["values"]) == len(m.vertices)


def test_overhang_field_ignores_platform_face():
    """平放的立方体底面贴平台，不该被标成需要支撑。"""
    m = trimesh.creation.box(extents=(20, 20, 20))
    f = analysis.overhang_field(m)
    vals = np.asarray(f["values"])
    # 底面四个顶点的值应该是 180（安全色），不是 0
    bottom = np.asarray(m.vertices)[:, 2] < -9
    # 底面顶点同时属于侧面，min 聚合后应停在 90°（竖直面），不会是纯红
    assert (vals[bottom] >= 85).all()


# ---------------------------------------------------------------- 分件
def test_split_plan_auto_axis():
    m = trimesh.creation.box(extents=(300, 80, 60))
    p = split.plan(m, 100, "auto")
    assert p["axis"] == "x"
    assert p["pieces"] == 3
    assert len(p["cuts"]) == 2
    assert p["needed"] is True


def test_split_plan_not_needed():
    m = trimesh.creation.box(extents=(50, 50, 50))
    p = split.plan(m, 100, "auto")
    assert p["needed"] is False
    assert p["pieces"] == 1


def test_split_preserves_volume_and_watertight():
    m = trimesh.creation.box(extents=(300, 80, 60))
    p = split.plan(m, 100, "auto")
    pieces = split.split_mesh(m, p["cuts"], p["axis_index"])
    assert len(pieces) == 3
    total = sum(float(x.volume) for x in pieces)
    assert total == pytest.approx(float(m.volume), rel=0.001)
    for piece in pieces:
        assert piece.is_watertight
        assert float(max(piece.extents)) <= 100.5


def test_split_section_centroid_inside_bounds():
    m = trimesh.creation.box(extents=(100, 40, 40))
    c = split.section_centroid(m, 0.0, 0)
    assert c is not None
    assert abs(c[0]) < 1e-6
    assert abs(c[1]) < 21 and abs(c[2]) < 21


# ---------------------------------------------------------------- 版本对比
def test_compare_detects_scaling():
    a = trimesh.creation.icosphere(subdivisions=3, radius=20)
    b = trimesh.creation.icosphere(subdivisions=3, radius=21)
    d = compare.compare(a, b)
    assert d["identical"] is False
    assert d["topology_changed"] is False
    assert d["vertices"]["max_displacement"] == pytest.approx(1.0, abs=0.05)
    assert d["delta"]["volume_mm3"] > 0


def test_compare_flags_topology_change():
    a = trimesh.creation.icosphere(subdivisions=3, radius=20)
    c = trimesh.creation.icosphere(subdivisions=1, radius=20)
    d = compare.compare(a, c)
    assert d["topology_changed"] is True
    assert d["delta"]["faces"] < 0


def test_compare_identical():
    a = trimesh.creation.box(extents=(10, 10, 10))
    d = compare.compare(a, a.copy())
    assert d["identical"] is True


# ---------------------------------------------------------------- 3MF / 回滚
def test_3mf_roundtrip(tmp_path):
    m = trimesh.creation.box(extents=(12, 12, 12))
    p = tmp_path / "a.3mf"
    m.export(str(p))
    assert p.stat().st_size > 500
    r = trimesh.load(str(p), force="mesh")
    assert len(r.faces) == len(m.faces)


def test_revert_restores_original(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    src = tmp_path / "orig.stl"
    trimesh.creation.box(extents=(20, 20, 20)).export(str(src))
    proj = store.create("revert.stl", src)
    pid = proj["id"]
    try:
        # 造一个"处理过"的版本
        out = tmp_path / "mod.stl"
        trimesh.creation.box(extents=(10, 10, 10)).export(str(out))
        store.add_history(pid, "process", {}, out, note="测试")

        with TestClient(app) as c:
            assert c.get(f"/api/projects/{pid}").json()["current"] == str(out)
            r = c.post(f"/api/projects/{pid}/revert", json={"index": -1})
            assert r.status_code == 200
            assert r.json()["label"] == "导入时"
            assert c.get(f"/api/projects/{pid}").json()["current"] == str(src)
    finally:
        store.delete(pid)


def test_revert_bad_index(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    src = tmp_path / "o.stl"
    trimesh.creation.box(extents=(5, 5, 5)).export(str(src))
    proj = store.create("bad.stl", src)
    try:
        with TestClient(app) as c:
            r = c.post(f"/api/projects/{proj['id']}/revert", json={"index": 99})
            assert r.status_code == 400
    finally:
        store.delete(proj["id"])
