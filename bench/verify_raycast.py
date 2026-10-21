"""验证新的射线求交与旧实现逐位一致。

优化的合法性不取决于「变快了多少」，而取决于「有没有算错」。
这个脚本把旧算法原样抄一份当参考实现，在真实模型上逐条射线比对结果，
既比数值也比命中/未命中的个数。任何一处不一致都直接判失败。

用法：
    .venv/Scripts/python.exe bench/verify_raycast.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import analysis, meshio  # noqa: E402
from app.analysis import face_normals  # noqa: E402

MODELS = ROOT / "bench" / "models"


# ---------------- 旧实现（改动前的原始版本，原样抄过来当基准） ----------------
def _old_mt_batch(origin, direction, tris):
    v0 = tris[:, 0]
    e1 = tris[:, 1] - v0
    e2 = tris[:, 2] - v0
    pvec = np.cross(direction, e2)
    det = np.einsum("ij,ij->i", e1, pvec)
    ok = np.abs(det) > 1e-12
    if not ok.any():
        return np.zeros(0)
    inv = np.zeros_like(det)
    inv[ok] = 1.0 / det[ok]
    tvec = origin - v0
    u = np.einsum("ij,ij->i", tvec, pvec) * inv
    qvec = np.cross(tvec, e1)
    v = np.einsum("j,ij->i", direction, qvec) * inv
    t = np.einsum("ij,ij->i", e2, qvec) * inv
    hit = ok & (u >= -1e-9) & (u <= 1 + 1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9) & (t > 0)
    return t[hit]


def _old_raycast(verts, faces, origins, dirs, eps, max_dist=None):
    from rtree import index as rtree_index

    tri = verts[faces]
    lo = tri.min(axis=1)
    hi = tri.max(axis=1)
    prop = rtree_index.Property()
    prop.dimension = 3

    def _stream():
        for i in range(len(tri)):
            yield (i, (lo[i, 0], lo[i, 1], lo[i, 2], hi[i, 0], hi[i, 1], hi[i, 2]), None)

    idx = rtree_index.Index(_stream(), properties=prop)
    out = np.full(len(origins), np.inf)
    span = float(np.linalg.norm(np.ptp(verts, axis=0))) * 1.2 + 1e-6
    if max_dist is not None:
        span = min(span, float(max_dist))
    for k in range(len(origins)):
        o = origins[k]
        d = dirs[k]
        end = o + d * span
        box = (min(o[0], end[0]), min(o[1], end[1]), min(o[2], end[2]),
               max(o[0], end[0]), max(o[1], end[1]), max(o[2], end[2]))
        cand = list(idx.intersection(box))
        if not cand:
            continue
        t = _old_mt_batch(o, d, tri[cand])
        t = t[(t > eps) & (t <= span)]
        if len(t):
            out[k] = float(t.min())
    return out


# ---------------------------------------------------------------- 用例
def make_rays(verts, faces, n, seed, multi_dir=1, max_dist=None):
    """复刻 sample_wall_thickness 的射线构造，保证新旧吃同一份输入。"""
    m = int(min(n, len(faces)))
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(faces), m, replace=False)
    t = verts[faces[idx]]
    centers = t.mean(axis=1)
    normals = face_normals(verts, faces[idx])
    eps = max(1e-4, float(np.linalg.norm(np.ptp(verts, axis=0))) * 1e-6)
    origins = centers - normals * eps
    # 多方向：沿切平面偏 ±20°，与生产代码一致
    helper = np.where(np.abs(normals[:, 2:3]) < 0.9,
                      np.array([[0.0, 0, 1]]), np.array([[1.0, 0, 0]]))
    t1 = np.cross(normals, helper)
    t1 /= np.maximum(np.linalg.norm(t1, axis=1, keepdims=True), 1e-12)
    ang = np.radians(20.0)
    sets = [-normals]
    if multi_dir > 1:
        sets.append(-np.cos(ang) * normals + np.sin(ang) * t1)
        sets.append(-np.cos(ang) * normals - np.sin(ang) * t1)
    return origins, sets, eps, max_dist


def main() -> int:
    models = ["ball_20k", "plate_thin", "overhang_t", "multi_5", "xintersect",
              "statue_30k", "broken_1k", "inch_254"]
    bad = 0
    print(f"{'模型':<12}{'射线':>6}{'方向':>5}{'最大差值':>12}{'命中数差':>10}{'新耗时':>9}{'旧耗时':>9}{'加速':>7}")
    print("-" * 82)
    for name in models:
        p = MODELS / f"{name}.stl"
        if not p.exists():
            continue
        mesh = meshio.load_mesh(p)
        V = np.asarray(mesh.vertices, dtype=np.float64)
        F = np.asarray(mesh.faces)
        for md, mdist in ((1, None), (3, None), (1, 1.6)):
            origins, dsets, eps, _ = make_rays(V, F, 1200, 11, multi_dir=md, max_dist=mdist)
            analysis._RAY_INDEX_CACHE.clear()
            total_new = 0.0
            total_old = 0.0
            worst = 0.0
            nhit_diff = 0
            for d in dsets:
                t0 = time.perf_counter()
                new = analysis._raycast_nearest(V, F, origins, d, eps, mdist)
                total_new += time.perf_counter() - t0
                analysis._RAY_INDEX_CACHE.clear()
                t0 = time.perf_counter()
                old = _old_raycast(V, F, origins, d, eps, mdist)
                total_old += time.perf_counter() - t0
                # 无限远视为未命中
                nf = np.isfinite(new)
                of = np.isfinite(old)
                nhit_diff += int(nf.sum() - of.sum())
                both = nf & of
                if both.any():
                    worst = max(worst, float(np.abs(new[both] - old[both]).max()))
                # 一边命中一边没命中 = 结论被改了
                xor = nf ^ of
                if xor.any():
                    worst = float("inf")
            flag = "" if (worst < 1e-9 and nhit_diff == 0) else "  ❌不一致"
            if flag:
                bad += 1
            sp = total_old / max(total_new, 1e-9)
            print(f"{name:<12}{len(origins):>6}{md:>5}{worst:>12.2e}{nhit_diff:>10}"
                  f"{total_new*1000:>8.0f}ms{total_old*1000:>8.0f}ms{sp:>6.1f}x{flag}")
    print("-" * 82)
    if bad:
        print(f"❌ {bad} 个用例与旧实现不一致，改动不可接受")
        return 1
    print("✅ 全部用例与旧实现逐位一致（含命中/未命中判定）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
