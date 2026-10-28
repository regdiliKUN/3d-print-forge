"""第四轮：大模型性能、支撑采样密度、射线缓存身份校验、手动点选。"""
from __future__ import annotations

import gc
import sys
import time
from pathlib import Path

import numpy as np
import pytest
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import analysis, process  # noqa: E402


# ---------------------------------------------------------------- 射线缓存
def test_raycast_cache_checks_object_identity():
    """缓存 key 用 id() 时必须校验对象身份。

    id() 是内存地址，前一个数组被回收后地址会被复用，
    只按 id 做 key 会拿到别的模型的 rtree 索引，射线全部落空。
    """
    a = trimesh.creation.box(extents=(30, 30, 5))
    va = np.asarray(a.vertices, dtype=np.float64)
    fa = np.asarray(a.faces)
    # box(30,30,5) 中心在原点，z 从 +2.5 到 -2.5，从顶面往下穿 5mm
    d1 = analysis._raycast_nearest(va, fa, np.array([[0.0, 0, 2.5]]), np.array([[0.0, 0, -1.0]]), 1e-4, 10.0)
    assert np.isfinite(d1[0]) and d1[0] == pytest.approx(5.0, abs=0.02)
    del a, va, fa
    gc.collect()

    # 换一个尺寸完全不同的模型，起点落在它内部
    b = trimesh.creation.icosphere(subdivisions=2, radius=5)
    vb = np.asarray(b.vertices, dtype=np.float64)
    fb = np.asarray(b.faces)
    d2 = analysis._raycast_nearest(vb, fb, np.array([[0.0, 0, 4.0]]), np.array([[0.0, 0, -1.0]]), 1e-4, 10.0)
    assert np.isfinite(d2[0]), "缓存串了：拿到的是上一个模型的索引"
    assert d2[0] == pytest.approx(9.0, abs=0.6)


def test_raycast_returns_inf_when_miss():
    m = trimesh.creation.box(extents=(10, 10, 10))
    v = np.asarray(m.vertices, dtype=np.float64)
    f = np.asarray(m.faces)
    d = analysis._raycast_nearest(v, f, np.array([[0.0, 0, 100.0]]), np.array([[0.0, 0, -1.0]]), 1e-4, 5.0)
    assert not np.isfinite(d[0])


# ---------------------------------------------------------------- 大模型性能
def test_thickness_coarse_path_is_fast_on_big_mesh():
    """32 万面模型上，粗筛必须在一秒级完成（曾经要 15.8 秒）。"""
    m = trimesh.creation.icosphere(subdivisions=7, radius=40)
    t0 = time.perf_counter()
    r = analysis.check_thickness(m, {"sample_faces": 1500})
    dt = time.perf_counter() - t0
    assert dt < 6.0, f"粗筛太慢：{dt:.1f}s"
    assert r["severity"] == "ok"
    assert r["value"]["mode"] == "coarse"


def test_thickness_coarse_still_catches_thin_wall():
    """快速通道不能放过薄壁：0.5mm 的板必须报警。"""
    m = trimesh.creation.box(extents=(30, 30, 0.5))
    r = analysis.check_thickness(m, {"sample_faces": 800})
    assert r["severity"] in ("warning", "error")
    assert r["value"]["min"] < 0.8


def test_thickness_coarse_reports_lower_bound():
    m = trimesh.creation.box(extents=(20, 20, 20))
    r = analysis.check_thickness(m, {"sample_faces": 400})
    assert r["value"].get("lower_bound") is True
    assert r["value"]["min"] >= 1.6


# ---------------------------------------------------------------- 支撑采样密度
def test_support_density_scales_with_area_not_face_count():
    """40×40 的悬空底面只有 2 个三角形，也必须撒出足够的支撑点。

    旧实现「每面取质心再去重」在这个模型上只生成 2 根柱。
    """
    post = trimesh.creation.box(extents=(6, 6, 30))
    post.apply_translation([0, 0, 15])
    cap = trimesh.creation.box(extents=(40, 40, 3))
    cap.apply_translation([0, 0, 31.5])
    _, info = process.build_supports(trimesh.util.concatenate([post, cap]), 45.0, 1.0, 4.0, 400)
    # 1600mm² / (4mm)² ≈ 100 个点
    assert info["count"] >= 60
    assert info["count"] <= 400


def test_support_density_respects_spacing():
    post = trimesh.creation.box(extents=(6, 6, 30))
    post.apply_translation([0, 0, 15])
    cap = trimesh.creation.box(extents=(40, 40, 3))
    cap.apply_translation([0, 0, 31.5])
    m = trimesh.util.concatenate([post, cap])
    _, dense = process.build_supports(m, 45.0, 1.0, 2.0, 800)
    _, sparse = process.build_supports(m, 45.0, 1.0, 8.0, 800)
    assert dense["count"] > sparse["count"] * 2


def test_supports_land_on_platform():
    plate = trimesh.creation.box(extents=(20, 20, 2))
    plate.apply_translation([0, 0, 20])
    _, info = process.build_supports(plate, 45.0, 1.0, 4.0, 200)
    for s in info["segments"]:
        assert abs(s[2]) < 0.01          # 落点在 z=0 平台
        assert s[3] >= 19.0 - 0.05       # 顶点在板底 z=19 附近


# ---------------------------------------------------------------- 手动点选
def test_manual_points_not_filtered():
    """手动点选是用户明确要的，不能因为「贴着平台」被悄悄丢掉。"""
    m = trimesh.creation.icosphere(subdivisions=2, radius=10)
    # z=-8 距球底 2mm，够长；用 -9.8 会被 h<0.6 的短支撑过滤挡掉（合理）
    _, info = process.build_supports(m, 45.0, 1.0, 4.0, 50,
                                     points=[[0.0, 0.0, -8.0]], avoid_model=False)
    assert info["count"] == 1
    seg = info["segments"][0]
    assert seg[3] == pytest.approx(-8.0, abs=0.05)


def test_manual_points_skip_solid_check():
    """手动点选时 avoid_model=False，即使下方是实体也照常生成。"""
    m = trimesh.creation.box(extents=(20, 20, 20))
    _, auto = process.build_supports(m, 45.0, 1.0, 4.0, 50)
    _, manual = process.build_supports(m, 45.0, 1.0, 4.0, 50,
                                       points=[[0.0, 0.0, -9.0]], avoid_model=False)
    assert manual["count"] >= 1


# ---------------------------------------------------------------- 拆分后的模块
def test_viewer_module_exists_and_imports():
    """3D 渲染抽成独立模块后，app.js 要能通过 import 拿到它。"""
    vj = (ROOT / "static" / "viewer.js").read_text(encoding="utf-8")
    aj = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "export { Viewer" in vj
    assert 'from "./viewer.js"' in aj
    assert "class Viewer" not in aj, "Viewer 类没有从 app.js 移出去"
    # 界面逻辑会随功能增长，这里只守住「3D 部分必须独立出去」这条线
    assert len(vj.splitlines()) > 300, "viewer.js 太薄，可能没把渲染逻辑搬过去"
    assert len(vj.splitlines()) < len(aj.splitlines())
