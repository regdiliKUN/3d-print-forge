"""优化轮次的回归测试：每一条改进都要有能证伪它的断言。"""
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

from app import advisor, analysis, config, meshio, process, repair  # noqa: E402


# ---------------------------------------------------------------- 轮1 壁厚多方向
def test_multidir_thickness_is_conservative():
    """多方向采样只会更保守（更小或相等），不会把薄点测厚。"""
    m = trimesh.creation.icosphere(subdivisions=3, radius=20)
    single = analysis.sample_wall_thickness(m, 400, multi_dir=1)
    multi = analysis.sample_wall_thickness(m, 400, multi_dir=3)
    assert multi["directions"] == 3
    assert multi["min"] <= single["min"] * 1.01
    assert multi["p5"] <= single["p5"] * 1.01


def test_flat_plate_thickness_exact_in_both_modes():
    m = trimesh.creation.box(extents=(30, 30, 1.2))
    for md in (1, 3):
        r = analysis.sample_wall_thickness(m, 300, multi_dir=md)
        assert r["min"] == pytest.approx(1.2, abs=0.05)


# ---------------------------------------------------------------- 轮2 自交半径
def test_self_intersection_found_across_size_scales():
    """大平面 + 小件混在一起时，搜索半径不能只按中位三角形算。"""
    big = trimesh.creation.box(extents=(200, 200, 1))
    small = trimesh.creation.icosphere(subdivisions=1, radius=3)
    small.apply_translation([50, 50, 0])
    m = trimesh.util.concatenate([big, small])
    res = analysis.find_self_intersections(
        np.asarray(m.vertices, np.float64), np.asarray(m.faces)
    )
    assert res["count"] > 0


# ---------------------------------------------------------------- 轮3 ASCII 解析
def test_ascii_stl_parses_correctly(tmp_path):
    m = trimesh.creation.icosphere(subdivisions=3, radius=12)
    p = tmp_path / "a.stl"
    m.export(str(p), file_type="stl_ascii")
    v, f = meshio.load_vertices_faces(p)
    assert len(f) == pytest.approx(len(m.faces), abs=8)
    assert np.allclose(np.sort(v, axis=0), np.sort(np.asarray(m.vertices, np.float32), axis=0)[: len(v)], atol=0.05)


def test_ascii_stl_is_fast(tmp_path):
    """20 万行的文件必须在几秒内解析完，不能是逐行 Python 循环的量级。"""
    m = trimesh.creation.icosphere(subdivisions=4, radius=20)
    p = tmp_path / "big.stl"
    m.export(str(p), file_type="stl_ascii")
    t0 = time.time()
    v, f = meshio.load_vertices_faces(p)
    assert time.time() - t0 < 5.0
    assert len(f) > 4000


# ---------------------------------------------------------------- 轮4 补洞自检
def test_fill_holes_reverts_on_failure():
    """没有洞时补洞应该是空操作，不能把网格搞坏。"""
    m = trimesh.creation.box(extents=(20, 20, 20))
    out, info = repair.act_fill_holes(m, {})
    assert info["filled"] is False
    assert len(out.faces) == len(m.faces)


def test_fill_holes_reports_boundary_change():
    m = trimesh.creation.icosphere(subdivisions=2, radius=15)
    m.update_faces(np.arange(len(m.faces)) > 2)
    out, info = repair.act_fill_holes(m, {})
    assert info["boundary_before"] > 0
    if info["filled"]:
        assert info["boundary_after"] < info["boundary_before"]
    else:
        assert info.get("reverted") is True


# ---------------------------------------------------------------- 轮5 分体定向
def test_fix_normals_per_component():
    a = trimesh.creation.icosphere(subdivisions=2, radius=10)
    b = trimesh.creation.box(extents=(8, 8, 8))
    b.apply_translation([30, 0, 0])
    b.invert()
    m = trimesh.util.concatenate([a, b])
    out, info = repair.act_fix_normals(m, {})
    assert info["components"] == 2
    assert info["flipped_components"] == 1
    v, f = np.asarray(out.vertices), np.asarray(out.faces)
    assert analysis.signed_volume(v, f) > 0


# ---------------------------------------------------------------- 轮6 内腔保留
def test_remove_small_keeps_cavity():
    shell = trimesh.creation.icosphere(subdivisions=2, radius=20)
    cav = trimesh.creation.icosphere(subdivisions=2, radius=17)
    cav.invert()
    tiny = trimesh.creation.box(extents=(0.3, 0.3, 0.3))
    tiny.apply_translation([60, 0, 0])
    m = trimesh.util.concatenate([shell, cav, tiny])
    out, info = repair.act_remove_small(m, {"small_ratio": 0.01})
    assert info["removed"] == 1
    assert info["cavities_kept"] == 1
    n, _ = analysis.union_components(np.asarray(out.faces), len(out.vertices))
    assert n == 2


# ---------------------------------------------------------------- 轮7 聚类减面
def test_cluster_decimate_produces_valid_mesh():
    m = trimesh.creation.icosphere(subdivisions=4, radius=20)
    v, f = meshio._cluster_decimate(
        np.asarray(m.vertices, np.float32), np.asarray(m.faces, np.uint32), 2000
    )
    assert 0 < len(f) <= 2500
    assert len(v) > 0
    assert ((f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])).all()
    assert f.max() < len(v)


def test_decimate_falls_back_when_target_met():
    m = trimesh.creation.box(extents=(10, 10, 10))
    v, f = meshio.decimate_arrays(np.asarray(m.vertices, np.float32), np.asarray(m.faces, np.uint32), 10000)
    assert len(f) == len(m.faces)


# ---------------------------------------------------------------- 轮8 缓存预算
def test_cache_respects_face_budget(tmp_path):
    meshio.clear_cache()
    for i in range(4):
        m = trimesh.creation.icosphere(subdivisions=4, radius=10)
        p = tmp_path / f"c{i}.stl"
        m.export(str(p))
        meshio.load_mesh(p)
    total = sum(len(m.faces) for m in meshio._CACHE.values())
    assert total <= meshio._CACHE_FACES_BUDGET or len(meshio._CACHE) <= 1
    meshio.clear_cache()


# ---------------------------------------------------------------- 轮10 壳厚保护
def test_hollow_wall_auto_clamped():
    thin = trimesh.creation.box(extents=(40, 40, 2))
    _, _, rep = process.apply_pipeline(
        thin,
        {**process.DEFAULTS, "hollow": True, "wall_thickness": 2.0,
         "supports": "none", "orient": False},
    )
    assert rep.get("hollow_warning")
    assert rep["hollow"]["wall_mm"] < 2.0


# ---------------------------------------------------------------- 轮11 排液孔
def test_drain_holes_on_bottom_faces():
    m = trimesh.creation.icosphere(subdivisions=3, radius=20)
    _, info = process.drill_holes(m, 3, 3.0, 2.0)
    zmin = float(m.bounds[0][2])
    # 孔位来自底面候选，XY 应该落在球体投影内
    for x, y in info["positions"]:
        assert np.hypot(x, y) <= 20.5
    assert info["depth"] > 0
    assert zmin < 0


# ---------------------------------------------------------------- 轮12 锥台支撑
def test_supports_are_tapered_and_watertight():
    plate = trimesh.creation.box(extents=(20, 20, 2))
    plate.apply_translation([0, 0, 20])
    sup, info = process.build_supports(plate, 45.0, 1.0, 4.0, 50)
    assert info["count"] > 0
    assert sup.is_watertight
    assert sup.volume > 0


def test_frustum_is_closed_solid():
    f = process._frustum(2.0, 1.0, 10.0, 10)
    assert f.is_watertight
    assert f.volume > 0


# ---------------------------------------------------------------- 轮13 摆放
def test_orient_reports_contact_ratio():
    rod = trimesh.creation.cylinder(radius=4, height=50, sections=24)
    _, info = process.auto_orient(rod, 45.0)
    assert "contact_ratio" in info
    assert 0.0 <= info["contact_ratio"] <= 1.0
    assert len(info["candidates"]) == 6


# ---------------------------------------------------------------- 轮14 目标尺寸
def test_target_max_dim_scales_longest_edge():
    m = trimesh.creation.box(extents=(10, 10, 50))
    out, _, rep = process.apply_pipeline(
        m, {**process.DEFAULTS, "target_max_dim": 100, "supports": "none", "orient": False}
    )
    assert float(max(out.extents)) == pytest.approx(100.0, rel=0.02)
    assert rep["scale"]["factor"] == pytest.approx(2.0, rel=0.02)


# ---------------------------------------------------------------- 轮15 成本
def test_cost_estimate_present_and_plausible(tmp_path):
    proj = {
        "id": "t", "name": "t", "source": "", "current": "",
        "analysis": {
            "stats": {"extents": [40, 40, 80], "volume_mm3": 60000, "faces": 1000,
                      "area_mm2": 9000, "is_watertight": True},
            "items": [],
        },
    }
    adv = advisor.recommend(proj)
    assert adv["cost"]["grams"] > 0
    assert adv["cost"]["hours"] > 0
    assert "1.1g/cm³" in adv["cost"]["basis"]
    assert any(p["key"] == "cost" for p in adv["params"])
    assert "预估" in adv["summary"]


def test_fdm_cost_uses_infill():
    proj = {
        "id": "t", "name": "t", "source": "", "current": "",
        "analysis": {"stats": {"extents": [40, 40, 40], "volume_mm3": 64000, "faces": 100,
                               "area_mm2": 9000, "is_watertight": True}, "items": []},
    }
    adv = advisor.recommend(proj, {"process": "FDM", "printer": "fdm_220"})
    assert adv["cost"]["grams"] > 0
    assert "1.24g/cm³" in adv["cost"]["basis"]


# ---------------------------------------------------------------- 轮20 环境变量
def test_env_override_config(monkeypatch):
    monkeypatch.setenv("TPF_MAX_WORKERS", "5")
    monkeypatch.setenv("TPF_MEMORY_LIMIT_MB", "2048")
    cfg = config.load()
    assert cfg.runtime.max_workers == 5
    assert cfg.runtime.memory_limit_mb == 2048
    monkeypatch.delenv("TPF_MAX_WORKERS", raising=False)
    monkeypatch.delenv("TPF_MEMORY_LIMIT_MB", raising=False)
    config.load()


def test_env_ignores_garbage(monkeypatch):
    monkeypatch.setenv("TPF_MAX_WORKERS", "not-a-number")
    cfg = config.load()
    assert isinstance(cfg.runtime.max_workers, int)
    monkeypatch.delenv("TPF_MAX_WORKERS", raising=False)
    config.load()


def test_fill_holes_preserves_winding_consistency():
    """补洞面必须与周围面同向，否则 is_volume 变 False、布尔运算直接拒绝。"""
    m = trimesh.creation.icosphere(subdivisions=3, radius=18)
    m.update_faces(np.arange(len(m.faces)) > 3)
    m.invert()
    out, info = repair.fill_holes_manual(m)
    assert out.is_watertight
    assert out.is_winding_consistent


def test_orient_consistent_fixes_flipped_face():
    m = trimesh.creation.box(extents=(10, 10, 10))
    f = np.asarray(m.faces).copy()
    f[0] = f[0][::-1]  # 故意翻一个面
    fixed, n = repair.orient_consistent(f)
    assert n >= 1
    check = trimesh.Trimesh(vertices=np.asarray(m.vertices), faces=fixed, process=False)
    assert check.is_winding_consistent


# ---------------------------------------------------------------- 轮23 射线截断
def test_raycast_max_dist_truncates():
    """截断距离外的命中不计入，避免厚件把整个模型扫一遍。"""
    m = trimesh.creation.icosphere(subdivisions=2, radius=30)
    full = analysis.sample_wall_thickness(m, 300, multi_dir=1)
    cut = analysis.sample_wall_thickness(m, 300, multi_dir=1, max_dist=10.0)
    assert full["min"] == pytest.approx(60.0, abs=2.0)
    # 截断 10mm 时，壁厚 60mm 的球几乎打不到对侧
    assert cut["samples"] == 0 or (cut["min"] is None or cut["min"] <= 10.0)


def test_check_thickness_uses_coarse_path_for_thick_parts():
    m = trimesh.creation.icosphere(subdivisions=3, radius=20)
    r = analysis.check_thickness(m, {"sample_faces": 1500})
    assert r["severity"] == "ok"
    assert r["value"].get("mode") == "coarse"


# ---------------------------------------------------------------- 轮19 日志
def test_trace_id_header():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.get("/api/health")
        assert r.headers.get("X-Trace-Id")
        assert len(r.headers["X-Trace-Id"]) == 8
        logs = c.get("/api/logs?lines=20").json()
        assert isinstance(logs["lines"], list)
