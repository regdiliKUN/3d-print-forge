"""修复动作：把不合法的网格变成可布尔、可切片的实体。

每个动作按顺序执行，中间结果落盘到任务临时目录，
所以任务中断后重跑会从断点加载，而不是从头再算一遍。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import numpy as np
import trimesh

from . import analysis, config, gpu, meshio, store, voxelops

LABELS = {
    "merge_vertices": "合并重复顶点",
    "fill_holes": "补洞",
    "fix_normals": "统一法线",
    "remove_small": "移除碎屑",
    "resolve_self_intersection": "体素重网格消解自交",
    "rescale": "单位换算",
    "thicken": "加厚",
}

ORDER_HINT = ["merge_vertices", "fill_holes", "fix_normals", "remove_small", "resolve_self_intersection"]


# ---------------------------------------------------------------- 动作
def act_merge_vertices(mesh, params):
    before = len(mesh.vertices)
    mesh.merge_vertices(merge_tex=True, merge_norm=True)
    return mesh, {"vertices_before": before, "vertices_after": len(mesh.vertices)}


def boundary_loops(faces: np.ndarray, nverts: int) -> list[np.ndarray]:
    """把开放边串成环。

    沿面片的有向边走（原面片 a→b，补洞走 b→a），这样补出来的面和周围面绕序一致。
    分叉处不能直接放弃——破损模型的洞常有不规则边界，一个顶点连着多条边界边，
    按「出度必须为 1」来找环会一个都找不到。允许分叉，靠已用边集合防止重复。
    """
    if len(faces) == 0:
        return []
    e = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    es = np.sort(e, axis=1)
    key = es[:, 0].astype(np.int64) * int(nverts) + es[:, 1].astype(np.int64)
    uniq, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    bd = e[cnt[inv] == 1]
    if len(bd) == 0:
        return []

    nxt: dict[int, list[int]] = {}
    for a, b in bd:
        nxt.setdefault(int(b), []).append(int(a))

    used: set[tuple[int, int]] = set()

    def free(cur: int) -> list[int]:
        return [a for a in nxt.get(cur, []) if (min(cur, a), max(cur, a)) not in used]

    loops: list[np.ndarray] = []
    for start in list(nxt.keys()):
        if not free(start):
            continue
        loop: list[int] = []
        cur = start
        while True:
            cands = free(cur)
            if not cands:
                break
            nxtv = cands[0]
            used.add((min(cur, nxtv), max(cur, nxtv)))
            loop.append(cur)
            cur = nxtv
            if cur == start:
                break
            if len(loop) > len(bd) + 2:
                break
        if len(loop) >= 3 and cur == start:
            loops.append(np.asarray(loop, dtype=np.int64))
    return loops


def _tri_normal(verts: np.ndarray, tri) -> np.ndarray:
    t = verts[np.asarray(tri, dtype=np.int64)]
    n = np.cross(t[1] - t[0], t[2] - t[0])
    ln = np.linalg.norm(n)
    return n / ln if ln > 1e-12 else np.zeros(3)


def triangulate_loop(verts: np.ndarray, ring: np.ndarray) -> np.ndarray:
    """把空间环投影到最佳拟合平面后做耳切三角化。"""
    pts = verts[ring]
    n = np.zeros(3)
    for i in range(len(pts)):
        a, b = pts[i], pts[(i + 1) % len(pts)]
        n[0] += (a[1] - b[1]) * (a[2] + b[2])
        n[1] += (a[2] - b[2]) * (a[0] + b[0])
        n[2] += (a[0] - b[0]) * (a[1] + b[1])
    ln = np.linalg.norm(n)
    if ln < 1e-12:
        n = np.array([0.0, 0.0, 1.0])
    else:
        n /= ln
    helper = np.array([1.0, 0, 0]) if abs(n[0]) < 0.9 else np.array([0, 1.0, 0])
    u = np.cross(n, helper)
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    coords = np.column_stack([pts @ u, pts @ v]).astype(np.float64)

    try:
        import mapbox_earcut as earcut

        idx = earcut.triangulate_float64(coords.reshape(-1), np.array([len(pts)], dtype=np.uint32))
        tris = np.asarray(idx, dtype=np.int64).reshape(-1, 3)
    except Exception:
        tris = np.array([[0, i, i + 1] for i in range(1, len(pts) - 1)], dtype=np.int64)

    out = ring[tris]
    if len(out):
        fn = _tri_normal(verts, out[0])
        if np.dot(fn, n) < 0:
            out = out[:, ::-1]
    return out


def fill_holes_manual(mesh, max_loop: int = 10000) -> tuple[trimesh.Trimesh, dict]:
    """自己补洞。trimesh.repair.fill_holes 在多数破损模型上静默返回 False，不能依赖。

    要区分两种「三边环」：
    - 真正的三角洞 → 补上
    - 孤立薄片三角形的边界（该三角形本身还在网格里，只是邻居被删光了）
      → 这种补上去会得到两个完全重合的面，制造非流形边，必须把薄片删掉。
    """
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    loops = boundary_loops(faces, len(verts))
    if not loops:
        return mesh, {"loops": 0, "added_faces": 0}

    face_key = {tuple(sorted(t.tolist())): i for i, t in enumerate(faces)}
    # 边 → 面，用于把补洞面的朝向对齐到周围面
    e2f: dict[tuple[int, int], int] = {}
    for fi, tri in enumerate(faces):
        for i in range(3):
            a, b = int(tri[i]), int(tri[(i + 1) % 3])
            e2f[(min(a, b), max(a, b))] = fi

    drop: set[int] = set()
    new_tris: list[np.ndarray] = []
    skipped = 0

    for ring in loops:
        if len(ring) == 3:
            k = tuple(sorted(ring.tolist()))
            if k in face_key:
                drop.add(face_key[k])
                continue
        if len(ring) > max_loop:
            skipped += 1
            continue
        tris = triangulate_loop(verts, ring)
        if len(tris) == 0:
            continue
        a, b = int(ring[0]), int(ring[1])
        ref = e2f.get((min(a, b), max(a, b)))
        if ref is not None:
            nb = _tri_normal(verts, faces[ref])
            fn = _tri_normal(verts, tris[0])
            if np.dot(fn, nb) < 0:
                tris = tris[:, ::-1]
        new_tris.append(tris)

    if not new_tris and not drop:
        return mesh, {"loops": len(loops), "added_faces": 0, "skipped": skipped}

    keep = np.ones(len(faces), dtype=bool)
    if drop:
        keep[list(drop)] = False
    blocks = [faces[keep]] + new_tris if new_tris else [faces[keep]]
    out = trimesh.Trimesh(vertices=verts, faces=np.vstack(blocks), process=True)
    # 补洞面可能方向与周围不一致，BFS 统一一次
    if not out.is_winding_consistent:
        f2, n_flip = orient_consistent(np.asarray(out.faces))
        out = trimesh.Trimesh(vertices=np.asarray(out.vertices), faces=f2, process=True)
        info_extra = {"winding_flipped_faces": n_flip}
    else:
        info_extra = {}
    return out, {
        "loops": len(loops),
        "added_faces": int(sum(len(t) for t in new_tris)),
        "removed_slivers": len(drop),
        "skipped": skipped,
        **info_extra,
    }


def _vertex_adjacency(faces: np.ndarray, nverts: int) -> list[np.ndarray]:
    """每个顶点的一环邻居（去重），用于给二次曲面拟合采样。"""
    adj: list[set[int]] = [set() for _ in range(nverts)]
    for tri in faces:
        a, b, c = int(tri[0]), int(tri[1]), int(tri[2])
        adj[a].update((b, c))
        adj[b].update((a, c))
        adj[c].update((a, b))
    return [np.fromiter(s, dtype=np.int64) for s in adj]


def _loop_normal(pts: np.ndarray) -> np.ndarray:
    """环的平均法向（Newell 法）。"""
    n = np.zeros(3)
    for i in range(len(pts)):
        a = pts[i]
        b = pts[(i + 1) % len(pts)]
        n[0] += (a[1] - b[1]) * (a[2] + b[2])
        n[1] += (a[2] - b[2]) * (a[0] + b[0])
        n[2] += (a[0] - b[0]) * (a[1] + b[1])
    ln = np.linalg.norm(n)
    return n / ln if ln > 1e-12 else np.array([0.0, 0.0, 1.0])


def _subdivide_interior(verts, faces, boundary_edges, levels):
    """只细分内部边、绝不动边界边 —— 否则会在补丁与实体之间留下 T 形接缝。

    每次细分把每个三角形按「有几条内部边被打点」拆成 1/2/3/4 个子三角形，
    边界边保持原样，补丁仍能与原网格严丝合缝地缝合。
    """
    verts = np.asarray(verts, dtype=np.float64).copy()
    faces = np.asarray(faces, dtype=np.int64).copy()
    for _ in range(max(0, int(levels))):
        edge_mid: dict[tuple[int, int], int] = {}
        for tri in faces:
            for k in range(3):
                a, b = int(tri[k]), int(tri[(k + 1) % 3])
                key = (min(a, b), max(a, b))
                if key in boundary_edges or key in edge_mid:
                    continue
                edge_mid[key] = len(verts)
                verts = np.vstack([verts, (verts[a] + verts[b]) * 0.5])
        new_faces: list[list[int]] = []
        for tri in faces:
            a, b, c = int(tri[0]), int(tri[1]), int(tri[2])
            mab = edge_mid.get((min(a, b), max(a, b)))
            mbc = edge_mid.get((min(b, c), max(b, c)))
            mca = edge_mid.get((min(c, a), max(c, a)))
            cnt = (mab is not None) + (mbc is not None) + (mca is not None)
            if cnt == 0:
                new_faces.append([a, b, c])
            elif cnt == 1:
                if mab is not None:
                    new_faces += [[a, mab, c], [mab, b, c]]
                elif mbc is not None:
                    new_faces += [[b, mbc, a], [mbc, c, a]]
                else:
                    new_faces += [[c, mca, b], [mca, a, b]]
            elif cnt == 2:
                if mab is not None and mbc is not None:
                    new_faces += [[mab, b, mbc], [a, mab, mbc], [a, mbc, c]]
                elif mbc is not None and mca is not None:
                    new_faces += [[mbc, c, mca], [b, mbc, mca], [b, mca, a]]
                else:
                    new_faces += [[mca, a, mab], [c, mca, mab], [c, mab, b]]
            else:
                new_faces += [[a, mab, mca], [mab, b, mbc], [mca, mbc, c], [mab, mbc, mca]]
        faces = np.asarray(new_faces, dtype=np.int64)
    return verts, faces


def _fit_quadric(pts: np.ndarray, origin: np.ndarray, u: np.ndarray, v: np.ndarray, n: np.ndarray):
    """在局部标架下拟合 z = a u² + b uv + c v² + d u + e v + f。"""
    p = pts - origin
    uu = p @ u
    vv = p @ v
    ww = p @ n
    A = np.column_stack([uu * uu, uu * vv, vv * vv, uu, vv, np.ones_like(uu)])
    coef, *_ = np.linalg.lstsq(A, ww, rcond=None)
    return coef


def _eval_quadric(coef: np.ndarray, uu: np.ndarray, vv: np.ndarray) -> np.ndarray:
    return (coef[0] * uu * uu + coef[1] * uu * vv + coef[2] * vv * vv
            + coef[3] * uu + coef[4] * vv + coef[5])


def fill_holes_smooth(mesh, max_loop: int = 10000, levels: int = 2, smooth_iters: int = 12):
    """曲率感知补洞：耳切定拓扑 → 内部细分加点 → 二次曲面拟合投影 → 拉普拉斯平滑（GPU）。

    旧做法「平面耳切 + 直接缝合」在曲面上会补出一块平板，既凹进去又有明显棱线，
    还会把洞口附近的网格拽变形。这里给补丁加内部顶点，按洞周围拟合出的二次曲面
    把它们抬到正确高度，再用固定边界的拉普拉斯平滑收顺，补出来的面顺着原模型曲率走。
    """
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    loops = boundary_loops(faces, len(verts))
    if not loops:
        return mesh, {"loops": 0, "added_faces": 0, "method": "smooth"}

    adj = _vertex_adjacency(faces, len(verts))
    V = verts.copy()
    patches: list[np.ndarray] = []
    added = 0
    skipped = 0
    for ring in loops:
        if len(ring) < 3:
            continue
        if len(ring) > max_loop:
            skipped += 1
            continue
        base = triangulate_loop(V, ring)  # 全局索引，且只用到环上顶点
        if len(base) == 0:
            continue
        local_idx = {int(v): i for i, v in enumerate(ring)}
        boundary_edges = set()
        for i in range(len(ring)):
            a = local_idx[int(ring[i])]
            b = local_idx[int(ring[(i + 1) % len(ring)])]
            boundary_edges.add((min(a, b), max(a, b)))
        pv = V[ring].copy()
        pf = np.asarray([[local_idx[int(x)] for x in tri] for tri in base], dtype=np.int64)
        # 洞越大越需要细分（小洞平面补就够，细分反而增加无谓开销）
        if len(ring) < 5:
            lv = 0
        elif len(ring) < 10:
            lv = 1
        elif len(ring) < 48:
            lv = 2
        else:
            lv = 3
        pv, pf = _subdivide_interior(pv, pf, boundary_edges, lv)
        nr = len(ring)

        # 拟合二次曲面：环点 + 一环邻居
        ring_pts = V[ring]
        neigh = []
        for rv in ring:
            for nb in adj[int(rv)][:8]:
                if int(nb) not in local_idx:
                    neigh.append(V[int(nb)])
        sample = np.vstack([ring_pts] + ([np.asarray(neigh)] if neigh else []))
        origin = ring_pts.mean(axis=0)
        nrm = _loop_normal(ring_pts)
        helper = np.array([1.0, 0, 0]) if abs(nrm[0]) < 0.9 else np.array([0, 1.0, 0])
        u = np.cross(nrm, helper)
        u /= np.linalg.norm(u)
        v = np.cross(nrm, u)
        coef = _fit_quadric(sample, origin, u, v, nrm)
        # 防止病态拟合把补丁顶飞：z 限制在洞口尺度的 2 倍以内
        span = float(np.linalg.norm(ring_pts - origin, axis=1).max()) or 1.0

        interior = np.arange(nr, len(pv))
        if len(interior):
            p = pv[interior] - origin
            uu = p @ u
            vv = p @ v
            zz = np.clip(_eval_quadric(coef, uu, vv), -2 * span, 2 * span)
            pv[interior] = origin + np.outer(uu, u) + np.outer(vv, v) + np.outer(zz, nrm)

        # 拉普拉斯平滑（边界固定）+ 每轮投影回曲面：只平滑不投影会把补丁拉塌，
        # 只投影不平滑又会留下细分带来的锯齿，两者交替才既顺又贴合曲率。
        if len(interior) and smooth_iters > 0:
            edges = np.vstack([pf[:, [0, 1]], pf[:, [1, 2]], pf[:, [2, 0]]])
            edges = np.unique(np.sort(edges, axis=1), axis=0)
            fmask = np.zeros(len(pv), dtype=bool)
            fmask[:nr] = True
            rounds = max(1, int(smooth_iters) // 4)
            for _ in range(rounds):
                pv = gpu.smooth_laplacian(pv, edges, fmask, iters=4, lam=0.5)
                p = pv[interior] - origin
                uu = p @ u
                vv = p @ v
                zz = np.clip(_eval_quadric(coef, uu, vv), -2 * span, 2 * span)
                pv[interior] = origin + np.outer(uu, u) + np.outer(vv, v) + np.outer(zz, nrm)

        start = len(V)
        V = np.vstack([V, pv[nr:]])
        remap = np.empty(len(pv), dtype=np.int64)
        remap[:nr] = ring
        remap[nr:] = start + np.arange(len(pv) - nr)
        patches.append(remap[pf])
        added += len(pv) - nr

    if not patches:
        return mesh, {"loops": len(loops), "added_faces": 0, "skipped": skipped, "method": "smooth"}

    all_faces = np.vstack([faces] + patches)
    out = trimesh.Trimesh(vertices=V, faces=all_faces, process=True)
    n_flip = 0
    if not out.is_winding_consistent:
        f2, n_flip = orient_consistent(np.asarray(out.faces))
        out = trimesh.Trimesh(vertices=np.asarray(out.vertices), faces=f2, process=True)
    return out, {
        "loops": len(loops),
        "added_faces": int(sum(len(p) for p in patches)),
        "added_verts": added,
        "skipped": skipped,
        "winding_flipped_faces": n_flip,
        "method": "smooth",
    }


def orient_consistent(faces: np.ndarray) -> tuple[np.ndarray, int]:
    """BFS 传播定向：相邻面共享边必须方向相反，否则翻转该面。

    补洞后经常出现「法线朝外但 winding 不一致」——非平面洞上这两件事不等价。
    winding 不一致会让 trimesh 的 is_volume 判 False，后续 manifold 布尔直接拒绝。
    """
    from collections import defaultdict

    faces = np.array(faces, copy=True)
    n_f = len(faces)
    if n_f == 0:
        return faces, 0
    edge_map: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for fi in range(n_f):
        t = faces[fi]
        for k in range(3):
            a, b = int(t[k]), int(t[(k + 1) % 3])
            edge_map[(min(a, b), max(a, b))].append((fi, 1 if a < b else -1))

    visited = np.zeros(n_f, dtype=bool)
    flip = np.zeros(n_f, dtype=bool)
    for seed in range(n_f):
        if visited[seed]:
            continue
        visited[seed] = True
        stack = [seed]
        while stack:
            fi = stack.pop()
            t = faces[fi] if not flip[fi] else faces[fi][::-1]
            for k in range(3):
                a, b = int(t[k]), int(t[(k + 1) % 3])
                key = (min(a, b), max(a, b))
                dir_fi = 1 if a < b else -1
                for fj, d0 in edge_map[key]:
                    if fj == fi or visited[fj]:
                        continue
                    actual = -d0 if flip[fj] else d0
                    if actual == dir_fi:
                        flip[fj] = not flip[fj]
                    visited[fj] = True
                    stack.append(fj)

    n_flip = int(flip.sum())
    if n_flip:
        faces[flip] = faces[flip][:, ::-1]
    return faces, n_flip


def act_fill_holes(mesh, params):
    verts, faces = np.asarray(mesh.vertices), np.asarray(mesh.faces)
    h0 = analysis.edge_histogram(faces, len(verts))
    before = h0["boundary"]
    if before == 0:
        return mesh, {"boundary_before": 0, "boundary_after": 0, "filled": False, "method": "none"}
    max_loop = int(params.get("max_loop", 10000))

    # 优先用曲率感知的平滑补洞；失败再退回旧的平面补丁
    try:
        smooth_mesh, info = fill_holes_smooth(
            mesh, max_loop,
            levels=int(params.get("fill_levels", 2)),
            smooth_iters=int(params.get("fill_smooth", 12)),
        )
    except Exception as e:  # noqa: BLE001
        smooth_mesh, info = mesh, {"method": "smooth_failed", "error": str(e)[:160]}

    def _judge(m):
        v = np.asarray(m.vertices)
        f = np.asarray(m.faces)
        h = analysis.edge_histogram(f, len(v))
        return h

    h1 = _judge(smooth_mesh)
    ok = h1["boundary"] < before and h1["nonmanifold"] <= h0["nonmanifold"] + 2
    if ok:
        info.update({"boundary_before": before, "boundary_after": h1["boundary"],
                     "filled": True, "nonmanifold_after": h1["nonmanifold"],
                     "gpu": gpu.info()})
        return smooth_mesh, info

    # 平滑补洞没改善 → 退回平面耳切
    planar_mesh, pinfo = fill_holes_manual(mesh, max_loop)
    h2 = _judge(planar_mesh)
    if h2["boundary"] < before:
        pinfo.update({"boundary_before": before, "boundary_after": h2["boundary"],
                      "filled": True, "nonmanifold_after": h2["nonmanifold"],
                      "fallback": "planar", "gpu": gpu.info()})
        return planar_mesh, pinfo

    info.update({"boundary_before": before, "boundary_after": before,
                 "filled": False, "reverted": True,
                 "revert_reason": f"平滑/平面补洞后开放边仍为 {h1['boundary']}/{h2['boundary']}，未改善，已回退",
                 "gpu": gpu.info()})
    return mesh, info


def act_fix_normals(mesh, params):
    """按连通体分别定向，而不是整体翻转。

    整体翻转只能处理「全部面法线都反了」这一种情况。真实破损模型里常见
    一个实体朝外、另一个实体朝内，或者镂空件的外壳朝外内壳朝内——
    这些必须逐个体用有向体积判断。
    """
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    vol_before = analysis.signed_volume(verts, faces)
    n, labels = analysis.union_components(faces, len(verts))
    if n <= 1:
        trimesh.repair.fix_normals(mesh, multibody=True)
        vol_after = analysis.signed_volume(np.asarray(mesh.vertices), np.asarray(mesh.faces))
        return mesh, {"signed_volume_before": round(vol_before, 3),
                      "signed_volume_after": round(vol_after, 3), "components": 1}

    new_faces = faces.copy()
    flipped = 0
    for c in range(n):
        mask = labels == c
        vol_c = analysis.signed_volume(verts, faces[mask])
        if vol_c < 0:
            new_faces[mask] = faces[mask][:, ::-1]
            flipped += 1
    out = trimesh.Trimesh(vertices=verts, faces=new_faces, process=True)
    vol_after = analysis.signed_volume(np.asarray(out.vertices), np.asarray(out.faces))
    return out, {
        "signed_volume_before": round(vol_before, 3),
        "signed_volume_after": round(vol_after, 3),
        "components": n,
        "flipped_components": flipped,
    }


def act_remove_small(mesh, params):
    """删碎屑，但保留内腔。

    镂空件的内壳是有向体积为负的独立连通体，按面积比例删会把它当碎屑清掉，
    结果就是镂空白做了。负体积的分量一律保留。
    """
    ratio = float(params.get("small_ratio", 0.01))
    verts, faces = np.asarray(mesh.vertices), np.asarray(mesh.faces)
    n, labels = analysis.union_components(faces, len(verts))
    if n <= 1:
        return mesh, {"components": n, "removed": 0}
    areas = analysis.tri_areas(verts, faces)
    comp_area = np.array([areas[labels == c].sum() for c in range(n)])
    keep = np.zeros(n, dtype=bool)
    cavities = 0
    for c in range(n):
        vol_c = analysis.signed_volume(verts.astype(np.float64), faces[labels == c])
        if vol_c < 0:
            keep[c] = True  # 内腔
            cavities += 1
        elif comp_area[c] >= comp_area.max() * ratio:
            keep[c] = True
    removed = int(n - keep.sum())
    if removed == 0:
        return mesh, {"components": n, "removed": 0, "cavities_kept": cavities}
    new = trimesh.Trimesh(vertices=verts, faces=faces[np.isin(labels, np.where(keep)[0])], process=True)
    return new, {"components_before": n, "components_after": int(keep.sum()),
                 "removed": removed, "cavities_kept": cavities}


def act_resolve_self_intersection(mesh, params):
    pitch = float(params.get("remesh_pitch") or 0)
    if pitch <= 0:
        pitch = max(0.2, float(max(mesh.extents)) / 220.0)
    faces_before = len(mesh.faces)
    out = voxelops.remesh(mesh, pitch)
    return out, {"pitch": round(pitch, 3), "faces_before": faces_before, "faces_after": len(out.faces)}


def act_rescale(mesh, params):
    factor = float(params.get("scale_factor", 1.0))
    if abs(factor - 1.0) < 1e-9:
        return mesh, {"applied": False}
    before = [round(float(x), 3) for x in mesh.extents]
    mesh.apply_scale(factor)
    return mesh, {"factor": factor, "extents_before": before, "extents_after": [round(float(x), 3) for x in mesh.extents]}


def act_thicken(mesh, params):
    amount = float(params.get("thicken_mm", 0.5))
    if amount <= 0:
        return mesh, {"applied": False}
    before = analysis.sample_wall_thickness(mesh, 800)
    out = voxelops.thicken(mesh, amount)
    after = analysis.sample_wall_thickness(out, 800)
    return out, {
        "delta_mm": amount,
        "min_before": before["min"],
        "min_after": after["min"],
        "note": "体素法加厚，锐边会轻微圆化",
    }


ACTIONS = {
    "merge_vertices": act_merge_vertices,
    "fill_holes": act_fill_holes,
    "fix_normals": act_fix_normals,
    "remove_small": act_remove_small,
    "resolve_self_intersection": act_resolve_self_intersection,
    "rescale": act_rescale,
    "thicken": act_thicken,
}


def normalize_order(actions: list[str]) -> list[str]:
    """按拓扑依赖排序：合并顶点→补洞→法线→碎屑→自交，其余保持原序。"""
    known = [a for a in ORDER_HINT if a in actions]
    rest = [a for a in actions if a not in ORDER_HINT]
    return known + rest


# ---------------------------------------------------------------- 任务
def run_job(ctx, params):
    pid = ctx.job.project_id
    proj = store.get(pid)
    if not proj:
        raise ValueError("项目不存在")
    actions = normalize_order(list(params.get("actions") or []))
    if not actions:
        raise ValueError("没有选择任何修复动作")

    tmpdir = config.STATE / "tmp" / ctx.job.id
    tmpdir.mkdir(parents=True, exist_ok=True)

    src = Path(proj.get("current") or proj["source"])
    ctx.tick(0.03, "加载模型", "load")
    mesh = meshio.load_mesh(src)
    before = meshio.mesh_stats(mesh)

    applied: list[dict] = []
    for i, name in enumerate(actions):
        fn = ACTIONS.get(name)
        if fn is None:
            ctx.log(f"跳过未知动作 {name}")
            continue
        base = 0.05 + 0.85 * i / len(actions)
        ctx.tick(base, f"{LABELS.get(name, name)}（{i + 1}/{len(actions)}）", name)
        step_file = tmpdir / f"{i:02d}_{name}.stl"
        if step_file.exists():
            # 断点续跑：直接加载上次算好的中间结果
            mesh = trimesh.load(str(step_file), force="mesh")
            info = ctx.job.steps.get(name, {}).get("data", {})
            ctx.log(f"断点续跑：复用 {name} 的中间结果")
        else:
            t0 = time.time()
            try:
                mesh, info = fn(mesh, params)
            except Exception as e:
                ctx.fail_step(name, str(e))
                raise
            info["elapsed"] = round(time.time() - t0, 2)
            mesh.export(str(step_file))
        applied.append({"action": name, "label": LABELS.get(name, name), "info": info})
        ctx.complete_step(name, info)

    ctx.tick(0.90, "写入结果", "save")
    out = config.OUTPUTS / f"{pid}_repaired_{int(time.time())}.stl"
    mesh.export(str(out))
    after = meshio.mesh_stats(mesh)

    # 修复后立刻复检，并把旧建议作废 —— 否则建议页会拿着修复前的报告继续喊「不水密」
    ctx.tick(0.94, "修复后复检", "analyze")
    try:
        report = analysis.analyze_mesh(mesh, out)
    except Exception as e:  # noqa: BLE001
        report = None
        ctx.log(f"修复后复检失败（不影响修复结果）：{e}")

    store.add_history(
        pid, "repair", {"actions": actions},
        out, note="；".join(f"{a['label']}" for a in applied),
    )
    if report is not None:
        store.update(pid, analysis=report, advice=None)
    else:
        store.update(pid, advice=None)
    shutil.rmtree(tmpdir, ignore_errors=True)
    ctx.tick(1.0, "修复完成", "done")
    return {"before": before, "after": after, "actions": applied,
            "output": str(out), "analysis": report}
