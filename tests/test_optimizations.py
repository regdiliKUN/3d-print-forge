"""性能与健壮性优化的回归用例。

每个用例守住一项「优化后必须仍然正确」的行为：
- 支撑密度批量查询与逐点暴力计数一致
- 镂空体积预估与内腔重建共用同一次距离场，结果不变
- 原始网格只解析一次，不同预览档从缓存派生
- 装配检查先用包围盒粗筛；布尔失败必须报 degraded，不能谎报「无干涉」
"""
from __future__ import annotations

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient

from app import meshio, process, store, voxelops


# ---------------------------------------------------------------- 支撑密度
def test_support_density_matches_bruteforce(monkeypatch):
    from app import main

    mesh = trimesh.creation.box(extents=[20, 20, 20])
    segs = [
        [-5.0, 0.0, 0.0, 5.0],
        [0.0, 0.0, 0.0, 5.0],
        [5.0, 0.0, 0.0, 5.0],
    ]

    monkeypatch.setattr(
        process,
        "build_supports",
        lambda *_a, **_k: (None, {"segments": segs, "count": len(segs)}),
    )
    radius = 6.0
    field = main._support_density_field(mesh, radius=radius)

    v = np.asarray(mesh.vertices, dtype=np.float64)
    pts = np.asarray(segs, dtype=np.float64)[:, :2]
    brute = np.array([
        sum(np.linalg.norm(p - q) <= radius for q in pts) for p in v[:, :2]
    ], dtype=np.float64)
    assert field["values"] == np.round(brute, 2).tolist()
    assert field["max"] == round(float(brute.max()), 1)
    assert field["count"] == len(segs)


# ---------------------------------------------------------------- 镂空共用距离场
def test_hollow_stats_matches_cavity_volume():
    mesh = trimesh.creation.icosphere(subdivisions=2, radius=10.0)
    expected = voxelops.cavity_volume(mesh, 1.6, pitch=1.0)
    stats: dict = {}
    voxelops.hollow_shell(mesh, 1.6, pitch=1.0, stats=stats)
    assert stats["cavity_mm3"] == pytest.approx(expected, rel=1e-12)
    assert stats["pitch_mm"] > 0


def test_pipeline_does_not_compute_cavity_twice(monkeypatch):
    mesh = trimesh.creation.icosphere(subdivisions=2, radius=10.0)
    called = {"hollow": 0, "cavity": 0}

    def fake_hollow(m, wall, pitch=None, coarse=2, stats=None):
        called["hollow"] += 1
        if stats is not None:
            stats["cavity_mm3"] = 123.4
            stats["pitch_mm"] = 1.0
        return m, None

    def boom(*_a, **_k):
        called["cavity"] += 1
        raise AssertionError("hollow 已经能顺带给出体积，不应再跑一遍 cavity_volume")

    monkeypatch.setattr(voxelops, "hollow_shell", fake_hollow)
    monkeypatch.setattr(voxelops, "cavity_volume", boom)

    p = dict(process.DEFAULTS)
    p.update({"hollow": True, "wall_thickness": 1.6, "drain_holes": 0,
              "supports": "none", "orient": False})
    _out, _sup, report = process.apply_pipeline(mesh.copy(), p)
    assert called == {"hollow": 1, "cavity": 0}
    assert report["hollow"]["cavity_mm3"] == 123.4


# ---------------------------------------------------------------- 两级网格缓存
def test_preview_levels_share_one_parse(tmp_path, monkeypatch):
    src = tmp_path / "ico.stl"
    trimesh.creation.icosphere(subdivisions=2, radius=10.0).export(src)
    meshio.clear_cache()

    calls = {"n": 0}
    original = meshio.load_vertices_faces

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(meshio, "load_vertices_faces", counted)
    low = meshio.load_mesh(src, preview_faces=80)
    high = meshio.load_mesh(src, preview_faces=160)
    again = meshio.load_mesh(src, preview_faces=80)

    assert calls["n"] == 1, "同一文件的不同预览档不应重复解析 STL"
    assert len(low.faces) <= 90 and len(high.faces) <= 170
    assert again is low


def test_preview_cache_clears_with_public_api(tmp_path):
    src = tmp_path / "ico.stl"
    trimesh.creation.icosphere(subdivisions=1, radius=10.0).export(src)
    meshio.clear_cache()
    meshio.load_mesh(src, preview_faces=40)
    assert meshio._CACHE and meshio._PREVIEW_CACHE
    meshio.clear_cache()
    assert not meshio._CACHE and not meshio._PREVIEW_CACHE


# ---------------------------------------------------------------- 装配检查
@pytest.fixture()
def assembly_project(tmp_path):
    """两个方块 + 一个带 assembly 字段的项目记录，用完自己清理。"""
    a = tmp_path / "a.stl"
    b = tmp_path / "b.stl"
    trimesh.creation.box(extents=[10, 10, 10]).export(a)
    trimesh.creation.box(extents=[10, 10, 10]).export(b)
    proj = store.create("a.stl", a)
    store.update(
        proj["id"],
        assembly={
            "files": [str(a), str(b)],
            "offsets": [[0, 0, 5], [100, 0, 5]],
            "names": ["a", "b"],
        },
    )
    try:
        yield proj["id"], [a, b]
    finally:
        store.delete(proj["id"])


def test_assembly_aabb_prefilter_skips_boolean(assembly_project, monkeypatch):
    from app import main

    pid, _files = assembly_project
    monkeypatch.setattr(
        trimesh.boolean, "intersection",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("包围盒不相交时不应做布尔")),
    )
    with TestClient(main.app) as c:
        d = c.post(f"/api/projects/{pid}/check-assembly", json={"tolerance_mm3": 0.5}).json()
    assert d["ok"] is True
    assert d["pairs_checked"] == 1
    assert d["pairs_geometry_checked"] == 0
    assert d["pairs_skipped_by_aabb"] == 1
    assert d["degraded"] == []


def test_assembly_boolean_failure_is_not_reported_as_clean(assembly_project, monkeypatch):
    from app import main

    pid, _files = assembly_project
    # 把两块摆到重叠位置，逼出一次布尔求交，然后让布尔失败
    proj = store.get(pid)
    proj["assembly"]["offsets"] = [[0, 0, 5], [0, 0, 5]]
    store.save(proj)
    monkeypatch.setattr(
        trimesh.boolean, "intersection",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("manifold exploded")),
    )
    with TestClient(main.app) as c:
        d = c.post(f"/api/projects/{pid}/check-assembly", json={"tolerance_mm3": 0.5}).json()
    assert d["ok"] is False, "布尔失败不能当成无干涉"
    assert d["interferences"] == []
    assert len(d["degraded"]) == 1
    assert "manifold exploded" in d["degraded"][0]["reason"]
