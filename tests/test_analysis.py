"""检测引擎测试：用人工构造的缺陷模型验证每一项判定。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import analysis, meshio  # noqa: E402


def _save(mesh: trimesh.Trimesh, path: Path) -> Path:
    mesh.export(str(path))
    return path


def _box(ext=(20, 20, 20), translate=(0, 0, 0)) -> trimesh.Trimesh:
    m = trimesh.creation.box(extents=ext)
    m.apply_translation(translate)
    return m


@pytest.fixture()
def clean_cube(tmp_path):
    return _save(_box(), tmp_path / "cube.stl")


@pytest.fixture()
def holed_cube(tmp_path):
    m = _box()
    m.update_faces(np.arange(len(m.faces)) != 0)  # 删掉一个面
    return _save(m, tmp_path / "hole.stl")


# ---------------------------------------------------------------- 加载
def test_load_binary_stl_roundtrip(clean_cube):
    v, f = meshio.load_vertices_faces(clean_cube)
    assert v.shape == (8, 3)
    assert f.shape == (12, 3)
    assert v.dtype == np.float32 and f.dtype == np.uint32


def test_mesh_stats(clean_cube):
    m = meshio.load_mesh(clean_cube)
    s = meshio.mesh_stats(m)
    assert s["faces"] == 12
    assert abs(s["volume_mm3"] - 8000.0) < 1.0
    assert s["is_watertight"] is True
    assert s["max_dim"] == 20.0


# ---------------------------------------------------------------- 水密性
def test_watertight_ok(clean_cube):
    m = meshio.load_mesh(clean_cube)
    r = analysis.check_watertight(m, {})
    assert r["severity"] == "ok"
    assert r["value"]["boundary_edges"] == 0


def test_watertight_detects_hole(holed_cube):
    m = meshio.load_mesh(holed_cube)
    r = analysis.check_watertight(m, {})
    assert r["severity"] == "error"
    assert r["value"]["boundary_edges"] == 3
    assert r["fix"]["action"] == "fill_holes"


# ---------------------------------------------------------------- 法线
def test_normals_ok(clean_cube):
    m = meshio.load_mesh(clean_cube)
    r = analysis.check_normals(m, {})
    assert r["severity"] == "ok"
    assert r["value"]["signed_volume"] > 0


def test_normals_detect_inverted(clean_cube):
    v, f = meshio.load_vertices_faces(clean_cube)
    flipped = trimesh.Trimesh(vertices=v, faces=f[:, ::-1], process=False)
    r = analysis.check_normals(flipped, {})
    assert r["severity"] == "error"
    assert r["value"]["signed_volume"] < 0
    assert r["fix"]["action"] == "fix_normals"


# ---------------------------------------------------------------- 非流形
def test_nonmanifold_detects_shared_edge(tmp_path):
    v = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1]], dtype=np.float32)
    f = np.array([[0, 1, 2], [1, 0, 3], [0, 1, 4]], dtype=np.uint32)
    m = trimesh.Trimesh(vertices=v, faces=f, process=False)
    r = analysis.check_nonmanifold(m, {})
    assert r["value"]["count"] >= 1
    assert r["severity"] in ("warning", "error")


# ---------------------------------------------------------------- 自相交
def test_self_intersection_detected(tmp_path):
    # 轴对齐的两个立方体只是「面落在体内」，表面并不穿插；用错位球体造真自交
    a = trimesh.creation.icosphere(subdivisions=2, radius=8)
    b = trimesh.creation.icosphere(subdivisions=2, radius=8)
    b.apply_translation([6, 0, 0])
    m = trimesh.util.concatenate([a, b])
    res = analysis.find_self_intersections(
        np.asarray(m.vertices, np.float64), np.asarray(m.faces)
    )
    assert res["count"] > 0


def test_sat_ignores_coplanar_contact():
    """共面相邻三角形不算自交——体素重网格会产出大量这种面。"""
    T1 = np.array([[[0, 0, 0], [1, 0, 0], [0, 1, 0]]], dtype=np.float64)
    T2 = np.array([[[1, 0, 0], [1, 1, 0], [0, 1, 0]]], dtype=np.float64)
    assert analysis.sat_overlap(T1, T2)[0] is np.False_


def test_sat_detects_crossing_pair():
    T1 = np.array([[[0, 0, 0], [2, 0, 0], [0, 2, 0]]], dtype=np.float64)
    T2 = np.array([[[0.5, 0.5, -1], [0.5, 0.5, 1], [1.5, 0.5, 1]]], dtype=np.float64)
    assert bool(analysis.sat_overlap(T1, T2)[0]) is True


def test_no_self_intersection_on_clean(clean_cube):
    v, f = meshio.load_vertices_faces(clean_cube)
    res = analysis.find_self_intersections(v.astype(np.float64), f)
    assert res["count"] == 0


# ---------------------------------------------------------------- 连通分量
def test_components_single(clean_cube):
    m = meshio.load_mesh(clean_cube)
    r = analysis.check_components(m, {})
    assert r["value"]["components"] == 1


def test_components_two_parts(tmp_path):
    a = _box((10, 10, 10), (0, 0, 0))
    b = _box((10, 10, 10), (40, 0, 0))
    p = _save(trimesh.util.concatenate([a, b]), tmp_path / "two.stl")
    m = meshio.load_mesh(p)
    r = analysis.check_components(m, {})
    assert r["value"]["components"] == 2


# ---------------------------------------------------------------- 壁厚
def test_thickness_on_cube(clean_cube):
    m = meshio.load_mesh(clean_cube)
    r = analysis.check_thickness(m, {"sample_faces": 200})
    assert r["severity"] == "ok"
    # 20mm 的实心件走粗筛快速通道：只回答「比告警线两倍厚」，不再给精确中位数
    assert r["value"]["mode"] == "coarse"
    assert r["value"]["min"] >= 1.6


def test_thickness_thin_plate(tmp_path):
    m = _box((30, 30, 0.3))
    p = _save(m, tmp_path / "plate.stl")
    mm = meshio.load_mesh(p)
    r = analysis.check_thickness(mm, {"sample_faces": 400})
    assert r["severity"] == "error"
    # 薄板的侧面射线会横穿 30mm，所以看最薄处与低分位，而不是中位数
    assert r["value"]["min"] < 0.5
    assert r["value"]["p5"] < 0.5


# ---------------------------------------------------------------- 单位
@pytest.mark.parametrize(
    "ext,expect",
    [((20, 20, 20), "mm"), ((0.02, 0.02, 0.02), "m"), ((2000, 2000, 2000), "cm?")],
)
def test_infer_units(ext, expect):
    assert analysis.infer_units(ext)["unit"] == expect


def test_units_rescale_factor():
    r = analysis.infer_units((0.02, 0.02, 0.02))
    assert r["scale"] == 1000.0


# ---------------------------------------------------------------- 汇总
def test_summarize_severity():
    items = [
        {"id": "a", "severity": "ok"},
        {"id": "b", "severity": "error", "fix": {"action": "x"}},
        {"id": "c", "severity": "warning"},
    ]
    s = analysis.summarize(items)
    assert s["worst"] == "error"
    assert s["blocking"] is True
    assert s["actionable"] == ["b"]
    assert s["counts"]["error"] == 1


def test_all_checks_run_on_clean_mesh(clean_cube):
    m = meshio.load_mesh(clean_cube)
    items = [fn(m, {"sample_faces": 100}) for fn in analysis.CHECKS]
    assert len(items) == 7
    for it in items:
        assert it["severity"] in ("ok", "info", "warning", "error")
        assert "message" in it and it["message"]
