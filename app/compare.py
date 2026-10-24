"""版本对比：量化两版模型之间的差异。

顶点云比较是近似的——拓扑一变（减面、重网格），顶点就对不上了。
所以结论里会明确区分「同一拓扑上的位移」和「拓扑已改变」两种情况。
"""
from __future__ import annotations

import numpy as np
import trimesh
from scipy.spatial import cKDTree

from . import meshio


def _surface_distance(mesh: trimesh.Trimesh, points: np.ndarray) -> np.ndarray | None:
    """点到**表面**的距离，而不是点到最近顶点的距离。

    这个区别很关键：把一张球面细分一级，所有新顶点都精确落在原表面上，
    但离最近的旧顶点有半个边长的距离。用顶点-顶点距离，实测同一形状的
    细分前后会报「74.8% 的顶点移动了、最大位移 1.63mm」——完全失真的结论。
    点到表面距离在这种情况是 0。
    """
    try:
        _closest, dist, _tri = trimesh.proximity.closest_point(mesh, points)
        return np.asarray(dist, dtype=np.float64)
    except Exception:  # noqa: BLE001
        return None


def compare(mesh_a: trimesh.Trimesh, mesh_b: trimesh.Trimesh, tol: float = 0.05) -> dict:
    sa = meshio.mesh_stats(mesh_a)
    sb = meshio.mesh_stats(mesh_b)
    va = np.asarray(mesh_a.vertices, dtype=np.float64)
    vb = np.asarray(mesh_b.vertices, dtype=np.float64)
    if len(va) == 0 or len(vb) == 0:
        return {"a": sa, "b": sb, "error": "空的网格，无法比较"}

    # 优先用点到表面；退化/非流形导致求最近点失败时退回点到点（并标注口径）
    d_new = _surface_distance(mesh_a, vb)      # 新模型顶点离旧模型表面多远
    d_old = _surface_distance(mesh_b, va)      # 旧模型顶点离新模型表面多远
    metric = "surface"
    if d_new is None or d_old is None:
        d_new, _ = cKDTree(va).query(vb, k=1)
        d_old, _ = cKDTree(vb).query(va, k=1)
        metric = "vertex"

    moved = d_new > tol
    removed = d_old > tol

    face_ratio = abs(sb["faces"] - sa["faces"]) / max(1, sa["faces"])
    topology_changed = face_ratio > 0.2 or (len(vb) / max(1, len(va)) > 1.5 or len(vb) / max(1, len(va)) < 0.67)

    # 尺寸变化单独算：整体放大 1.5 倍会让所有顶点「位移」，但形状一点没变。
    # 不把这两件事分开，用户看到「74% 的顶点移动了」根本判断不出改了什么。
    ext_a = sa.get("extents") or [0, 0, 0]
    ext_b = sb.get("extents") or [0, 0, 0]
    ratios = [(float(b) / float(a)) if abs(float(a)) > 1e-9 else 1.0
              for a, b in zip(ext_a, ext_b)]
    uniform = max(ratios) / max(1e-9, min(ratios)) < 1.02 if ratios else True
    scale = float(np.mean(ratios)) if ratios else 1.0
    size_changed = abs(scale - 1.0) > 0.01 and uniform

    note = ("拓扑已改变（面数或顶点数大幅变化），位移只能说明「改了多少」，不能说明「哪里改厚了」"
            if topology_changed
            else "拓扑未变，位移可以定位具体改动区域")
    if size_changed:
        note += f"；整体等比缩放 ×{scale:.3f}，位移主要来自尺寸变化而不是形状变化"
    if metric == "vertex":
        note += "；最近点求解失败，已退回顶点-顶点距离（数值偏大）"

    return {
        "a": sa,
        "b": sb,
        "delta": {
            "faces": int(sb["faces"] - sa["faces"]),
            "vertices": int(len(vb) - len(va)),
            "volume_mm3": round(float(sb["volume_mm3"] - sa["volume_mm3"]), 1),
            "area_mm2": round(float(sb["area_mm2"] - sa["area_mm2"]), 1),
        },
        "size": {
            "extents_a": ext_a,
            "extents_b": ext_b,
            "axis_ratios": [round(r, 4) for r in ratios],
            "uniform_scale": round(scale, 4),
            "uniform": bool(uniform),
            "changed": bool(size_changed or not uniform),
        },
        "vertices": {
            "a": int(len(va)),
            "b": int(len(vb)),
            "moved": int(moved.sum()),
            "moved_ratio": round(float(moved.mean()), 4),
            "max_displacement": round(float(d_new.max()), 3),
            "removed_regions": int(removed.sum()),
        },
        "distance_metric": metric,
        "tolerance_mm": float(tol),
        "topology_changed": bool(topology_changed),
        "identical": int(moved.sum()) == 0 and int(removed.sum()) == 0,
        "note": note,
    }


def diff_files(path_a, path_b, tol: float = 0.05) -> dict:
    a = meshio.load_mesh(path_a)
    b = meshio.load_mesh(path_b)
    return compare(a, b, tol)
