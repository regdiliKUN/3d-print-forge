"""版本对比：量化两版模型之间的差异。

顶点云比较是近似的——拓扑一变（减面、重网格），顶点就对不上了。
所以结论里会明确区分「同一拓扑上的位移」和「拓扑已改变」两种情况。
"""
from __future__ import annotations

import numpy as np
import trimesh
from scipy.spatial import cKDTree

from . import meshio


def compare(mesh_a: trimesh.Trimesh, mesh_b: trimesh.Trimesh, tol: float = 0.05) -> dict:
    sa = meshio.mesh_stats(mesh_a)
    sb = meshio.mesh_stats(mesh_b)
    va = np.asarray(mesh_a.vertices, dtype=np.float64)
    vb = np.asarray(mesh_b.vertices, dtype=np.float64)
    if len(va) == 0 or len(vb) == 0:
        return {"a": sa, "b": sb, "error": "空的网格，无法比较"}

    d_new, _ = cKDTree(va).query(vb, k=1)      # 新模型的顶点离旧模型多远
    d_old, _ = cKDTree(vb).query(va, k=1)      # 旧模型的顶点离新模型多远
    moved = d_new > tol
    removed = d_old > tol

    face_ratio = abs(sb["faces"] - sa["faces"]) / max(1, sa["faces"])
    topology_changed = face_ratio > 0.2 or (len(vb) / max(1, len(va)) > 1.5 or len(vb) / max(1, len(va)) < 0.67)

    return {
        "a": sa,
        "b": sb,
        "delta": {
            "faces": int(sb["faces"] - sa["faces"]),
            "vertices": int(len(vb) - len(va)),
            "volume_mm3": round(float(sb["volume_mm3"] - sa["volume_mm3"]), 1),
            "area_mm2": round(float(sb["area_mm2"] - sa["area_mm2"]), 1),
        },
        "vertices": {
            "a": int(len(va)),
            "b": int(len(vb)),
            "moved": int(moved.sum()),
            "moved_ratio": round(float(moved.mean()), 4),
            "max_displacement": round(float(d_new.max()), 3),
            "removed_regions": int(removed.sum()),
        },
        "topology_changed": bool(topology_changed),
        "identical": int(moved.sum()) == 0 and int(removed.sum()) == 0,
        "note": (
            "拓扑已改变（面数或顶点数大幅变化），顶点位移只能说明「改了多少」，不能说明「哪里改厚了」"
            if topology_changed
            else "拓扑未变，顶点位移可以定位具体改动区域"
        ),
    }


def diff_files(path_a, path_b, tol: float = 0.05) -> dict:
    a = meshio.load_mesh(path_a)
    b = meshio.load_mesh(path_b)
    return compare(a, b, tol)
