"""补洞平滑度 / GPU 后端 / 修复后复检 的回归测试。"""
from __future__ import annotations

import sys
from pathlib import Path
from collections import defaultdict

import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import analysis, gpu, repair, store  # noqa: E402


def _sphere_with_hole(radius=20.0, cut_z=15.0):
    m = trimesh.creation.icosphere(subdivisions=3, radius=radius)
    m.update_faces(m.triangles_center[:, 2] < cut_z)  # 削掉顶部一块，形成洞
    return m


def _new_faces(m0, m1):
    f0 = {tuple(sorted(t)) for t in np.asarray(m0.faces)}
    f1 = np.asarray(m1.faces)
    return np.array([t for t in f1 if tuple(sorted(t)) not in f0])


def _patch_metrics(m0, m1):
    new = _new_faces(m0, m1)
    V = np.asarray(m1.vertices)
    e = np.vstack([new[:, [0, 1]], new[:, [1, 2]], new[:, [2, 0]]])
    max_edge = float(np.linalg.norm(V[e[:, 0]] - V[e[:, 1]], axis=1).max())
    # 相邻补面法线夹角（越小越顺）
    em = defaultdict(list)
    for i, t in enumerate(new):
        for k in range(3):
            a, b = int(t[k]), int(t[(k + 1) % 3])
            em[(min(a, b), max(a, b))].append(i)
    nrm = np.cross(V[new[:, 1]] - V[new[:, 0]], V[new[:, 2]] - V[new[:, 0]])
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-12
    angs = []
    for fs in em.values():
        if len(fs) == 2:
            cos = abs(float(nrm[fs[0]] @ nrm[fs[1]]))
            angs.append(np.degrees(np.arccos(min(1.0, cos))))
    return max_edge, float(np.mean(angs)) if angs else 0.0


def test_gpu_info_shape():
    info = gpu.info()
    assert info["backend"] in ("cupy", "torch", "cpu")
    assert isinstance(info["available"], bool)
    assert info["name"]


def test_smooth_laplacian_relaxes_poked_vertex():
    verts = np.array([[x, y, 0.0] for y in range(3) for x in range(3)], dtype=np.float64)
    verts[4] = [1, 1, 1.0]  # 中间顶点被顶起来
    edges = []
    for y in range(3):
        for x in range(3):
            i = y * 3 + x
            if x < 2:
                edges.append((i, i + 1))
            if y < 2:
                edges.append((i, i + 3))
    edges = np.array(edges)
    fixed = np.ones(len(verts), dtype=bool)
    fixed[4] = False
    out = gpu.smooth_laplacian(verts, edges, fixed, iters=8)
    assert abs(out[4, 2]) < abs(verts[4, 2])  # 被拉平
    assert np.allclose(out[fixed], verts[fixed])  # 固定点不动


def test_smooth_fill_watertight_and_smoother_than_planar():
    base = _sphere_with_hole()
    base.merge_vertices()
    ms, si = repair.fill_holes_smooth(base)
    mp, pi = repair.fill_holes_manual(base)
    hs = analysis.edge_histogram(np.asarray(ms.faces), len(ms.vertices))
    hp = analysis.edge_histogram(np.asarray(mp.faces), len(mp.vertices))
    assert hs["boundary"] == 0 and hp["boundary"] == 0
    assert ms.is_watertight and mp.is_watertight
    assert si.get("added_verts", 0) > 0  # 平滑补洞会加内部顶点
    e_s, d_s = _patch_metrics(base, ms)
    e_p, d_p = _patch_metrics(base, mp)
    assert e_s < e_p  # 补面更短 → 拉扯更小
    assert d_s < d_p  # 补面过渡更顺 → 没有明显折痕


def test_act_fill_holes_uses_smooth():
    base = _sphere_with_hole()
    base.merge_vertices()
    out, info = repair.act_fill_holes(base, {})
    assert info["filled"] is True
    assert info.get("method") == "smooth"
    assert "gpu" in info
    h = analysis.edge_histogram(np.asarray(out.faces), len(out.vertices))
    assert h["boundary"] == 0


def _dihedral_std(mesh):
    V = np.asarray(mesh.vertices)
    F = np.asarray(mesh.faces)
    n = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    em = defaultdict(list)
    for i, t in enumerate(F):
        for k in range(3):
            a, b = int(t[k]), int(t[(k + 1) % 3])
            em[(min(a, b), max(a, b))].append(i)
    angs = []
    for fs in em.values():
        if len(fs) == 2:
            angs.append(np.degrees(np.arccos(min(1.0, abs(float(n[fs[0]] @ n[fs[1]]))))))
    return float(np.std(angs)) if angs else 0.0


def test_voxel_remesh_taubin_reduces_stair_steps():
    from app import voxelops

    sphere = trimesh.creation.icosphere(subdivisions=3, radius=20)
    m0 = voxelops.remesh(sphere, pitch=0.6, smooth_iters=0)
    m3 = voxelops.remesh(sphere, pitch=0.6, smooth_iters=3)
    assert _dihedral_std(m3) < _dihedral_std(m0) * 0.85  # 台阶纹明显减少
    # Taubin 保体积：体积几乎不变
    assert abs(m3.volume - m0.volume) / max(m0.volume, 1e-9) < 0.02
    assert m3.is_watertight


def test_repair_job_rechecks_and_clears_stale_advice(tmp_path):
    m = _sphere_with_hole()
    p = tmp_path / "hole.stl"
    m.export(str(p))
    proj = store.create("hole", p)
    # 模拟「修复前」的旧状态：非水密报告 + 旧建议
    store.update(proj["id"], advice={"stale": True})

    class Job:
        def __init__(self):
            self.id = "t-" + proj["id"]
            self.project_id = proj["id"]
            self.steps = {}

    class Ctx:
        def __init__(self):
            self.job = Job()

        def tick(self, *a, **k):
            pass

        def log(self, *a, **k):
            pass

        def complete_step(self, *a, **k):
            pass

        def fail_step(self, *a, **k):
            pass

    repair.run_job(Ctx(), {"actions": ["merge_vertices", "fill_holes", "fix_normals"]})
    p2 = store.get(proj["id"])
    assert p2["advice"] is None  # 旧建议已作废
    assert p2["analysis"] is not None
    wt = next(i for i in p2["analysis"]["items"] if i["id"] == "watertight")
    assert wt["severity"] == "ok"  # 修复后复检不再报不水密
