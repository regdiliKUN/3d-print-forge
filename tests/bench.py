"""性能基准：量化各阶段耗时与内存峰值，用于判断优化是否真的有效。

运行：python tests/bench.py
"""
from __future__ import annotations

import gc
import os
import sys
import time
from pathlib import Path

import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import analysis, meshio, process, repair, voxelops  # noqa: E402

try:
    import psutil

    _proc = psutil.Process(os.getpid())
except Exception:  # pragma: no cover
    _proc = None

TMP = ROOT / "data" / "outputs" / "_bench"
TMP.mkdir(parents=True, exist_ok=True)

ROWS: list[tuple[str, str, float, float]] = []


def bench(label: str, group: str, fn):
    gc.collect()
    rss0 = _proc.memory_info().rss / 1048576 if _proc else 0.0
    t0 = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - t0
    rss1 = _proc.memory_info().rss / 1048576 if _proc else 0.0
    ROWS.append((group, label, dt, max(0.0, rss1 - rss0)))
    print(f"  {group:10s} {label:26s} {dt * 1000:8.0f} ms   RSS +{max(0.0, rss1 - rss0):6.1f} MB")
    return out


def make_models():
    small = trimesh.creation.icosphere(subdivisions=3, radius=15)
    mid = trimesh.creation.icosphere(subdivisions=5, radius=30)
    p_small = TMP / "small.stl"
    p_mid = TMP / "mid.stl"
    p_ascii = TMP / "mid_ascii.stl"
    small.export(str(p_small))
    mid.export(str(p_mid))
    mid.export(str(p_ascii), file_type="stl_ascii")
    return small, mid, p_small, p_mid, p_ascii


def main() -> int:
    print("\n=== 性能基准 ===\n")
    small, mid, p_small, p_mid, p_ascii = make_models()
    print(f"  小模型 {len(small.faces):,} 面 / 中模型 {len(mid.faces):,} 面\n")

    meshio.clear_cache()
    v_s, f_s = bench("二进制 STL 小", "加载", lambda: meshio.load_vertices_faces(p_small))
    meshio.clear_cache()
    v_m, f_m = bench("二进制 STL 中", "加载", lambda: meshio.load_vertices_faces(p_mid))
    meshio.clear_cache()
    bench("ASCII STL 中", "加载", lambda: meshio.load_vertices_faces(p_ascii))

    m = trimesh.Trimesh(vertices=v_m, faces=f_m, process=False)
    params = {"sample_faces": 3000, "check_self_intersection": True}
    for fn in analysis.CHECKS:
        bench(fn.__name__.replace("check_", ""), "检测", lambda fn=fn: fn(m, params, None))

    bench("壁厚 粗筛600", "壁厚", lambda: analysis.sample_wall_thickness(m, 600, multi_dir=1))
    bench("壁厚 全量6000(无截断)", "壁厚", lambda: analysis.sample_wall_thickness(m, 6000))
    bench("壁厚 全量6000(截断12mm)", "壁厚",
          lambda: analysis.sample_wall_thickness(m, 6000, max_dist=12.0))
    bench("check_thickness 默认", "壁厚",
          lambda: analysis.check_thickness(m, {"sample_faces": 1500}))

    broken = trimesh.creation.icosphere(subdivisions=3, radius=15)
    broken.update_faces(np.arange(len(broken.faces)) > 2)
    bench("补洞", "修复", lambda: repair.fill_holes_manual(broken))
    bench("分体定向", "修复", lambda: repair.act_fix_normals(broken, {}))
    bench("连通分量", "修复", lambda: repair.act_remove_small(m, {}))

    bench("预览减面→20k", "减面", lambda: meshio.decimate_arrays(
        np.asarray(m.vertices, np.float32), np.asarray(m.faces, np.uint32), 20000))
    bench("聚类减面→20k", "减面", lambda: meshio._cluster_decimate(
        np.asarray(m.vertices, np.float32), np.asarray(m.faces, np.uint32), 20000))

    sphere = trimesh.creation.icosphere(subdivisions=4, radius=30)
    bench("镂空 2mm", "处理", lambda: voxelops.hollow_shell(sphere, 2.0))
    plate = trimesh.creation.box(extents=(60, 60, 3))
    plate.apply_translation([0, 0, 40])
    bench("支撑 自动", "处理", lambda: process.build_supports(plate, 45.0, 1.0, 3.0, 200))
    bench("摆放 6 候选", "处理", lambda: process.auto_orient(sphere, 45.0))

    total = sum(r[2] for r in ROWS)
    peak = max(r[3] for r in ROWS)
    print(f"\n  合计 {total * 1000:.0f} ms，单步内存峰值 {peak:.1f} MB")

    for f in TMP.glob("*"):
        f.unlink()
    TMP.rmdir()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
