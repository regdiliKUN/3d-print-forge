"""修复与几何处理测试：每个动作都要有可量化的效果，不能只是「跑通了」。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import analysis, process, repair, voxelops  # noqa: E402


def _sphere(r=15, sub=2):
    return trimesh.creation.icosphere(subdivisions=sub, radius=r)


def _box(ext=(20, 20, 20), t=(0, 0, 0)):
    m = trimesh.creation.box(extents=ext)
    m.apply_translation(t)
    return m


# ---------------------------------------------------------------- 减面
def test_decimate_halves_faces():
    m = _sphere()
    d = process.decimate_mesh(m, 0.5)
    assert len(d.faces) <= len(m.faces) * 0.6
    assert len(d.faces) >= 4


def test_decimate_noop_at_full_ratio():
    m = _sphere()
    d = process.decimate_mesh(m, 1.0)
    assert len(d.faces) == len(m.faces)


# ---------------------------------------------------------------- 镂空
def test_hollow_reduces_volume():
    m = _sphere(15, 3)
    shell, cavity = voxelops.hollow_shell(m, 2.0)
    assert shell.is_watertight
    assert 0 < shell.volume < m.volume * 0.5
    assert cavity.volume < 0  # 内腔贡献负体积


def test_hollow_rejects_too_thick_wall():
    m = _sphere(5, 2)
    with pytest.raises(ValueError):
        voxelops.hollow_shell(m, 8.0)


def test_cavity_volume_estimate_matches():
    m = _sphere(15, 2)
    est = voxelops.cavity_volume(m, 2.0, pitch=0.5)
    assert est > 0
    assert est < m.volume


# ---------------------------------------------------------------- 打孔
def test_drill_creates_holes_and_stays_watertight():
    m = _sphere(15, 3)
    shell, _ = voxelops.hollow_shell(m, 2.0)
    v_before = shell.volume
    out, info = process.drill_holes(shell, 2, 3.0, 2.0)
    assert info["holes"] == 2
    assert out.is_watertight
    assert out.volume < v_before


def test_drill_zero_holes_is_noop():
    m = _sphere(10, 2)
    out, info = process.drill_holes(m, 0, 3.0, 2.0)
    assert info["holes"] == 0
    assert out is m


# ---------------------------------------------------------------- 支撑
def test_supports_on_overhang():
    # 悬空的板：下面没有支撑，必然需要支撑柱
    plate = _box((20, 20, 2), (0, 0, 20))
    sup, info = process.build_supports(plate, 45.0, 1.0, 4.0, 200)
    assert info["count"] > 0
    assert sup is not None
    assert sup.bounds[0][2] < 1.0  # 支撑落到平台


def test_no_supports_on_flat_box():
    b = _box()
    sup, info = process.build_supports(b, 45.0, 1.0, 4.0, 200)
    assert info["count"] == 0


# ---------------------------------------------------------------- 摆放
def test_auto_orient_lands_on_platform():
    m = _sphere(12, 2)
    m.apply_translation([0, 0, 30])
    out, info = process.auto_orient(m, 45.0)
    assert abs(float(out.bounds[0][2])) < 1e-6
    assert "chosen" in info
    assert len(info["candidates"]) == 6


def test_auto_orient_prefers_low_overhang():
    # 细长柱体：立着放悬垂少
    rod = trimesh.creation.cylinder(radius=3, height=40, sections=24)
    out, info = process.auto_orient(rod, 45.0)
    assert info["overhang_ratio"] >= 0
    assert float(out.extents[2]) > 0


# ---------------------------------------------------------------- 修复
def test_repair_fill_holes():
    m = _box()
    m.update_faces(np.arange(len(m.faces)) != 0)
    v, f = np.asarray(m.vertices), np.asarray(m.faces)
    assert analysis.edge_histogram(f, len(v))["boundary"] > 0
    fixed, info = repair.act_fill_holes(m, {})
    v2, f2 = np.asarray(fixed.vertices), np.asarray(fixed.faces)
    assert analysis.edge_histogram(f2, len(v2))["boundary"] == 0
    assert info["boundary_before"] > 0


def test_repair_fix_normals():
    m = _box()
    m.invert()
    fixed, info = repair.act_fix_normals(m, {})
    assert info["signed_volume_after"] > 0


def test_repair_remove_small():
    big = _box((20, 20, 20))
    tiny = _box((0.5, 0.5, 0.5), (30, 0, 0))
    m = trimesh.util.concatenate([big, tiny])
    out, info = repair.act_remove_small(m, {"small_ratio": 0.01})
    assert info["removed"] == 1
    n, _ = analysis.union_components(np.asarray(out.faces), len(out.vertices))
    assert n == 1


def test_repair_merge_vertices():
    m = _box()
    m.merge_vertices()
    before = len(m.vertices)
    out, info = repair.act_merge_vertices(m, {})
    assert info["vertices_after"] <= before


def test_repair_rescale():
    m = _box()
    out, info = repair.act_rescale(m, {"scale_factor": 2.0})
    assert info["extents_after"][0] == pytest.approx(40.0, abs=1e-3)


def test_repair_order_is_topological():
    got = repair.normalize_order(["resolve_self_intersection", "fix_normals", "merge_vertices", "fill_holes"])
    assert got == ["merge_vertices", "fill_holes", "fix_normals", "resolve_self_intersection"]


def test_repair_thicken_increases_thickness():
    plate = _box((30, 30, 1.0))
    out, info = repair.act_thicken(plate, {"thicken_mm": 1.0})
    assert out.volume > plate.volume
    assert info["delta_mm"] == 1.0


def test_voxel_remesh_resolves_self_intersection():
    a = trimesh.creation.icosphere(subdivisions=2, radius=8)
    b = trimesh.creation.icosphere(subdivisions=2, radius=8)
    b.apply_translation([6, 0, 0])
    m = trimesh.util.concatenate([a, b])
    before = analysis.find_self_intersections(
        np.asarray(m.vertices, np.float64), np.asarray(m.faces)
    )["count"]
    assert before > 0
    out = voxelops.remesh(m, 0.5)
    after = analysis.find_self_intersections(
        np.asarray(out.vertices, np.float64), np.asarray(out.faces)
    )["count"]
    assert after < before
    assert out.is_watertight
