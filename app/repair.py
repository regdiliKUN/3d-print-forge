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


def _bounded_int(params: dict, key: str, default: int, lo: int, hi: int) -> int:
    """外部参数转 int 并夹到安全区间；非法值退回默认值。"""
    try:
        v = int(params.get(key, default))
    except (TypeError, ValueError, OverflowError):
        v = int(default)
    return max(lo, min(hi, v))


# ---------------------------------------------------------------- 动作
def act_merge_vertices(mesh, params):
    before = len(mesh.vertices)
    mesh.merge_vertices(merge_tex=True, merge_norm=True)
    return mesh, {"vertices_before": before, "vertices_after": len(mesh.vertices)}


def _split_closed_walk(walk: list[int]) -> list[np.ndarray]:
    """把一个「可能自触」的闭合走线拆成若干**简单环**。

    边界图在捏合点（pinch vertex）会分叉：两个洞只共享一个顶点时，沿边走访会
    走出 8 字形，例如 [272,21,467,510,21,256]。把这种环直接投影耳切，
    会在捏合处生成重叠三角形，补完出现 4 个面共享同一条边。

    做法是用栈做环分解：遇到重复顶点就把中间那段切出来单独成环，剩下的前缀
    继续接边。上面那个 8 字会拆成 [21,467,510] 和 [272,21,256] 两个简单环，
    两者只共享顶点、不共享边，补出来的面自然不会有非流形边。
    """
    cycles: list[list[int]] = []
    stack: list[int] = []
    pos: dict[int, int] = {}
    for v in walk:
        if v in pos:
            i = pos[v]
            cyc = stack[i:]
            if len(cyc) >= 3:
                cycles.append(list(cyc))
            for x in stack[i:]:
                pos.pop(x, None)
            stack = stack[:i]
        pos[v] = len(stack)
        stack.append(v)
    if len(stack) >= 3:
        cycles.append(stack)
    return [np.asarray(c, dtype=np.int64) for c in cycles]


def boundary_loops(faces: np.ndarray, nverts: int) -> list[np.ndarray]:
    """把开放边串成**简单环**（每个顶点在环内只出现一次）。

    沿面片的有向边走（原面片 a→b，补洞走 b→a），这样补出来的面和周围面绕序一致。
    分叉处不能直接放弃——破损模型的洞常有不规则边界，一个顶点连着多条边界边，
    按「出度必须为 1」来找环会一个都找不到。允许分叉，靠已用边集合防止重复，
    再用 `_split_closed_walk` 把自触环拆开。
    """
    if len(faces) == 0:
        return []
    nverts = max(int(nverts), int(np.max(faces)) + 1, 1)
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

    def _key(a: int, b: int) -> tuple[int, int]:
        return (a, b) if a < b else (b, a)

    def free_next(cur: int) -> int | None:
        for a in nxt.get(cur, ()):
            if _key(cur, a) not in used:
                return a
        return None

    loops: list[np.ndarray] = []
    for start in list(nxt.keys()):
        while True:
            first = free_next(start)
            if first is None:
                break
            walk = [start]
            used.add(_key(start, first))
            cur: int | None = first
            while cur != start and cur is not None:
                walk.append(cur)
                if len(walk) > len(bd) + 2:  # 防御：数据异常时不要死循环
                    cur = None
                    break
                nv = free_next(cur)
                if nv is None:
                    cur = None
                    break
                used.add(_key(cur, nv))
                cur = nv
            if cur == start:
                loops.extend(_split_closed_walk(walk))
    return loops


def _tri_normal(verts: np.ndarray, tri) -> np.ndarray:
    t = verts[np.asarray(tri, dtype=np.int64)]
    n = np.cross(t[1] - t[0], t[2] - t[0])
    ln = np.linalg.norm(n)
    return n / ln if ln > 1e-12 else np.zeros(3)


def _point_in_tri_2d(p, a, b, c, eps: float = 1e-12) -> bool:
    """点是否落在三角形内（含边界）。"""
    def cross(u, v, w):
        return (v[0] - u[0]) * (w[1] - u[1]) - (v[1] - u[1]) * (w[0] - u[0])

    d1 = cross(a, b, p)
    d2 = cross(b, c, p)
    d3 = cross(c, a, p)
    has_neg = d1 < -eps or d2 < -eps or d3 < -eps
    has_pos = d1 > eps or d2 > eps or d3 > eps
    return not (has_neg and has_pos)


def _ear_clip_2d(coords: np.ndarray) -> np.ndarray:
    """纯 Python 耳切，作为 earcut 缺失/失败时的兜底。

    旧兜底是从 0 号点拉扇形，凹多边形会补到轮廓外面，形成自相交面片；
    耳切只裁凸耳，遇到凹角会跳过，因此任意简单多边形都能正确铺满。
    返回的是局部索引；无法裁完（自交/退化）时返回空数组，由调用方决定是否回退。
    """
    n = len(coords)
    if n < 3:
        return np.zeros((0, 3), dtype=np.int64)
    x, y = coords[:, 0], coords[:, 1]
    area2 = float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
    order = list(range(n))
    if area2 < 0:  # 统一成 CCW，凸性判据才成立
        order.reverse()

    def cross(i0, i1, i2):
        a, b, c = coords[i0], coords[i1], coords[i2]
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    tris: list[tuple[int, int, int]] = []
    eps = max(1e-14, float(np.max(np.abs(coords))) * 1e-12)
    while len(order) > 3:
        m = len(order)
        clipped = False
        for k in range(m):
            i0, i1, i2 = order[(k - 1) % m], order[k], order[(k + 1) % m]
            cr = cross(i0, i1, i2)
            if cr <= eps:
                continue  # 凹角或共线，不能裁
            a, b, c = coords[i0], coords[i1], coords[i2]
            blocked = False
            for j in order:
                if j in (i0, i1, i2):
                    continue
                if _point_in_tri_2d(coords[j], a, b, c, eps):
                    blocked = True
                    break
            if blocked:
                continue
            tris.append((i0, i1, i2))
            order.pop(k)
            clipped = True
            break
        if not clipped:
            # 只剩共线点：裁掉零面积耳，保证边界边都有面
            for k in range(m):
                i0, i1, i2 = order[(k - 1) % m], order[k], order[(k + 1) % m]
                if abs(cross(i0, i1, i2)) <= eps:
                    tris.append((i0, i1, i2))
                    order.pop(k)
                    clipped = True
                    break
        if not clipped:
            return np.zeros((0, 3), dtype=np.int64)
    if len(order) == 3:
        tris.append(tuple(order))
    return np.asarray(tris, dtype=np.int64)


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
        if len(tris) != max(0, len(pts) - 2):
            raise ValueError(f"earcut 返回 {len(tris)} 个三角形，期望 {len(pts) - 2}")
    except Exception:
        tris = _ear_clip_2d(coords)
        if len(tris) == 0:
            # 自交/极端退化时耳切也会失败；扇形至少保证边全部有面，
            # 后续 act_fill_holes 的水密/体积验收会把不合格结果回退。
            tris = np.array([[0, i, i + 1] for i in range(1, len(pts) - 1)], dtype=np.int64)


    out = ring[tris]
    if len(out):
        fn = _tri_normal(verts, out[0])
        if np.dot(fn, n) < 0:
            out = out[:, ::-1]

    # 面积校验：自交/退化环的扇形三角化会重叠，三角形面积和明显大于
    # 多边形面积。宁可返回空让上层回退，也不要把重叠面片缝进模型。
    if len(out) and len(coords) >= 3:
        x, y = coords[:, 0], coords[:, 1]
        poly_area = abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) * 0.5
        tp = coords[tris]
        tri_area = 0.5 * float(np.abs(
            (tp[:, 1, 0] - tp[:, 0, 0]) * (tp[:, 2, 1] - tp[:, 0, 1])
            - (tp[:, 1, 1] - tp[:, 0, 1]) * (tp[:, 2, 0] - tp[:, 0, 0])
        ).sum())
        tol = max(1e-12, poly_area * 1e-6)
        if poly_area <= tol:
            if tri_area > tol:
                return np.zeros((0, 3), dtype=np.int64)
        elif abs(tri_area - poly_area) > max(tol, poly_area * 1e-3):
            return np.zeros((0, 3), dtype=np.int64)
    return out


def _face_key(tri) -> tuple[int, int, int]:
    a, b, c = (int(x) for x in tri)
    return tuple(sorted((a, b, c)))


def _dedupe_faces(faces: np.ndarray) -> tuple[np.ndarray, int]:
    """删掉顶点集合完全相同的重复面，每组只留第一个。

    重复面是 STL 拼接/布尔残渣里最常见的非流形来源：同一条边被 4 个面共享，
    但每条边照样「有面」，只数开放边的检测会判它水密。必须按面去重。
    """
    if len(faces) == 0:
        return faces, 0
    _uniq, first, _inv, cnt = np.unique(
        np.sort(np.asarray(faces, dtype=np.int64), axis=1),
        axis=0, return_index=True, return_inverse=True, return_counts=True,
    )
    if len(first) == len(faces):
        return faces, 0
    keep = np.zeros(len(faces), dtype=bool)
    keep[first] = True
    return faces[keep], int(len(faces) - len(first))


def _sliver_face_indices(faces: np.ndarray, loops: list[np.ndarray]) -> set[int]:
    """找出「三边都是开放边、但三角形本身还在」的孤立薄片。

    这种环不是洞，而是失去所有邻居的薄片。补上它会得到两个完全重合的面；
    旧逻辑在平滑补洞路径里正是这么干的，补完边计数变成 2/2，看似水密，
    实际是零厚度壳，后续布尔和切片都会出问题。
    """
    by_key: dict[tuple[int, int, int], list[int]] = {}
    for i, tri in enumerate(faces):
        by_key.setdefault(_face_key(tri), []).append(i)
    drop: set[int] = set()
    for ring in loops:
        if len(ring) == 3:
            drop.update(by_key.get(_face_key(ring), ()))
    return drop


def fill_holes_manual(mesh, max_loop: int = 10000) -> tuple[trimesh.Trimesh, dict]:
    """自己补洞。trimesh.repair.fill_holes 在多数破损模型上静默返回 False，不能依赖。

    要区分两种「三边环」：
    - 真正的三角洞 → 补上
    - 孤立薄片三角形的边界（该三角形本身还在网格里，只是邻居被删光了）
      → 这种补上去会得到两个完全重合的面，制造非流形边，必须把薄片删掉。
    """
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    faces, deduped = _dedupe_faces(faces)
    loops = boundary_loops(faces, len(verts))
    if not loops and deduped == 0:
        return mesh, {"loops": 0, "added_faces": 0}

    # 边 → 面，用于把补洞面的朝向对齐到周围面
    e2f: dict[tuple[int, int], int] = {}
    for fi, tri in enumerate(faces):
        for i in range(3):
            a, b = int(tri[i]), int(tri[(i + 1) % 3])
            e2f[(min(a, b), max(a, b))] = fi

    drop: set[int] = _sliver_face_indices(faces, loops)
    new_tris: list[np.ndarray] = []
    skipped = 0

    for ring in loops:
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

    if not new_tris and not drop and deduped == 0:
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
        "deduped_faces": deduped,
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

    # 先清掉重复面和孤立薄片。这两类缺陷的共同点是「每条边都有面」，
    # 只按开放边找洞会完全看不见它们；但补洞/布尔/切片都会在这里出错。
    cleaned = mesh
    deduped = 0
    removed_slivers = 0
    faces, deduped = _dedupe_faces(faces)
    if deduped:
        cleaned = trimesh.Trimesh(vertices=verts, faces=faces, process=True)
        verts = np.asarray(cleaned.vertices, dtype=np.float64)
        faces = np.asarray(cleaned.faces, dtype=np.int64)
    loops = boundary_loops(faces, len(verts))
    slivers = _sliver_face_indices(faces, loops)
    if slivers:
        removed_slivers = len(slivers)
        keep = np.ones(len(faces), dtype=bool)
        keep[list(slivers)] = False
        cleaned = trimesh.Trimesh(vertices=verts, faces=faces[keep], process=True)
        verts = np.asarray(cleaned.vertices, dtype=np.float64)
        faces = np.asarray(cleaned.faces, dtype=np.int64)
        loops = boundary_loops(faces, len(verts))
    if not loops:
        return cleaned, {"loops": 0, "added_faces": 0, "method": "smooth",
                         "deduped_faces": deduped, "removed_slivers": removed_slivers}

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
        return cleaned, {"loops": len(loops), "added_faces": 0, "skipped": skipped,
                         "deduped_faces": deduped, "removed_slivers": removed_slivers,
                         "method": "smooth"}

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
        "deduped_faces": deduped,
        "removed_slivers": removed_slivers,
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


def _repair_health(mesh) -> dict:
    """修复动作的验收指标。开放边不是唯一标准：补洞同时不能新增非流形边/重复面，
    也不能把闭合体补成零体积壳。"""
    v = np.asarray(mesh.vertices)
    f = np.asarray(mesh.faces)
    h = analysis.edge_histogram(f, len(v))
    volume = analysis.signed_volume(np.asarray(v, dtype=np.float64), f)
    abs_volume = abs(volume)
    if len(f) and abs_volume <= 1e-12:
        # 有向体积可能因正负分量抵消而归零（例如实体 + 内腔）。
        # 这时再按连通分量取绝对体积和，才能区分「真零厚度」和「正负抵消」。
        try:
            _n, _labels, vols = analysis.component_volumes(
                np.asarray(v, dtype=np.float64), f)
            abs_volume = float(np.abs(vols).sum())
        except Exception:  # noqa: BLE001
            pass
    return {
        "boundary": h["boundary"],
        "nonmanifold": h["nonmanifold"],
        "duplicates": analysis.duplicate_face_count(f),
        "volume": volume,
        "abs_volume": abs_volume,
        "faces": int(len(f)),
    }


def _loop_summary(mesh, limit: int = 20) -> dict:
    try:
        loops = boundary_loops(np.asarray(mesh.faces), len(mesh.vertices))
    except Exception:  # noqa: BLE001
        return {"remaining_loops": 0, "remaining_loop_sizes": []}
    return {
        "remaining_loops": len(loops),
        "remaining_loop_sizes": [int(len(x)) for x in loops[:limit]],
    }


def act_fill_holes(mesh, params):
    """补洞。验收标准是「开放边减少，且没有引入新的非流形边/重复面」。

    旧实现允许非流形边 +2，理由是「补洞难免碰坏两条边」。实测这会把
    「两个洞在顶点处捏合」补成 4 面共享一条边，却仍然报告成功；后续布尔
    直接拒绝。现在改为零容忍，并且平滑/平面两条路径都评分后择优。
    """
    verts, faces = np.asarray(mesh.vertices), np.asarray(mesh.faces)
    h0 = _repair_health(mesh)
    before = h0["boundary"]
    if before == 0 and h0["duplicates"] == 0 and h0["nonmanifold"] == 0:
        return mesh, {"boundary_before": 0, "boundary_after": 0, "filled": False, "method": "none"}
    max_loop = _bounded_int(params, "max_loop", 10000, 3, 2_000_000)
    extent = float(max(np.ptp(np.asarray(mesh.vertices, dtype=np.float64), axis=0))) if len(verts) else 1.0
    min_volume = max(1e-9, extent ** 3 * 1e-9)

    def _judge(m) -> tuple[bool, dict, str]:
        h = _repair_health(m)
        if h["boundary"] >= before and h["duplicates"] >= h0["duplicates"]:
            return False, h, "开放边没有减少"
        if h["nonmanifold"] > h0["nonmanifold"]:
            return False, h, f"新增非流形边（{h0['nonmanifold']} → {h['nonmanifold']}）"
        if h["duplicates"] > h0["duplicates"]:
            return False, h, f"新增重复面（{h0['duplicates']} → {h['duplicates']}）"
        if h["faces"] == 0 and h0["faces"] > 0:
            return False, h, "清理后没有剩余面，模型只有薄片没有实体"
        if h["boundary"] == 0 and h["abs_volume"] <= min_volume:
            return False, h, "补洞后体积为零厚度壳"
        return True, h, ""

    candidates: list[tuple[str, object, dict, dict, str, bool]] = []
    try:
        smooth_mesh, info = fill_holes_smooth(
            mesh, max_loop,
            levels=_bounded_int(params, "fill_levels", 2, 0, 4),
            smooth_iters=_bounded_int(params, "fill_smooth", 12, 0, 50),
        )
    except Exception as e:  # noqa: BLE001
        smooth_mesh, info = mesh, {"method": "smooth_failed", "error": str(e)[:160]}
    ok1, h1, why1 = _judge(smooth_mesh)
    candidates.append(("smooth", smooth_mesh, info, h1, why1, ok1))

    if not ok1:
        planar_mesh, pinfo = fill_holes_manual(mesh, max_loop)
        ok2, h2, why2 = _judge(planar_mesh)
        candidates.append(("planar", planar_mesh, pinfo, h2, why2, ok2))

    good = [c for c in candidates if c[5]]
    if good:
        # 先看剩余开放边，再看非流形边与重复面；同样好时优先平滑补丁（曲率贴合）
        good.sort(key=lambda c: (c[3]["boundary"], c[3]["nonmanifold"], c[3]["duplicates"],
                                 0 if c[0] == "smooth" else 1))
        method, chosen, info, health, _, _ = good[0]
        info = dict(info)
        info.update({
            "boundary_before": before, "boundary_after": health["boundary"],
            "filled": health["boundary"] < before,
            "cleaned_only": health["boundary"] == before,
            "nonmanifold_before": h0["nonmanifold"], "nonmanifold_after": health["nonmanifold"],
            "duplicates_before": h0["duplicates"], "duplicates_after": health["duplicates"],
            "method": info.get("method", method),
            "gpu": gpu.info(),
        })
        if method == "planar":
            info["fallback"] = "planar"
        info.update(_loop_summary(chosen))
        return chosen, info

    # 两条路径都不合格：保留原模型，把「差在哪」写清楚，前端可直接展示
    detail = "；".join(f"{c[0]}: 开放边 {c[3]['boundary']}、非流形 {c[3]['nonmanifold']}（{c[4]}）"
                      for c in candidates)
    info = dict(candidates[0][2])
    info.update({
        "boundary_before": before, "boundary_after": before,
        "filled": False, "reverted": True,
        "revert_reason": f"补洞未达验收标准，已回退。{detail}",
        "rejected_candidates": [
            {"method": c[0], "boundary": c[3]["boundary"], "nonmanifold": c[3]["nonmanifold"],
             "duplicates": c[3]["duplicates"], "reason": c[4]} for c in candidates
        ],
        "gpu": gpu.info(),
    })
    info.update(_loop_summary(mesh))
    return mesh, info


def act_fix_normals(mesh, params):
    """先统一绕序，再按连通体分别判定朝向。

    只按有向体积整体翻转只能处理「全部面都反了」。真实破损模型里常见：
    - 一个实体朝外、另一个实体朝内（体积正负抵消，总和看不出问题）
    - 同一连通体里有几个面被翻过（绕序不一致，体积仍为正）
    - 镂空件外壳朝外、内壳朝内（两个连通体，体积一正一负）
    所以顺序必须是：BFS 绕序一致化 → 逐分量有向体积判朝向。
    """
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    vol_before = analysis.signed_volume(verts, faces)
    conflicts_before = analysis.winding_conflict_count(faces)

    faces_fixed, winding_flipped = orient_consistent(faces)
    n, labels = analysis.union_components(faces_fixed, len(verts))
    ext = float(max(np.ptp(verts, axis=0))) if len(verts) else 1.0
    tol = max(1e-12, ext ** 3 * 1e-12)
    flipped_components = 0
    for c in range(n):
        mask = labels == c
        if analysis.signed_volume(verts, faces_fixed[mask]) < -tol:
            faces_fixed[mask] = faces_fixed[mask][:, ::-1]
            flipped_components += 1

    out = trimesh.Trimesh(vertices=verts, faces=faces_fixed, process=True)
    out_verts = np.asarray(out.vertices, dtype=np.float64)
    out_faces = np.asarray(out.faces)
    vol_after = analysis.signed_volume(out_verts, out_faces)
    conflicts_after = analysis.winding_conflict_count(out_faces)
    return out, {
        "signed_volume_before": round(vol_before, 3),
        "signed_volume_after": round(vol_after, 3),
        "components": n,
        "winding_flipped_faces": int(winding_flipped),
        "winding_conflicts_before": int(conflicts_before),
        "winding_conflicts_after": int(conflicts_after),
        "flipped_components": flipped_components,
    }


def act_remove_small(mesh, params):
    """删碎屑，但保留真正的内腔。

    镂空件的内壳是有向体积为负的独立连通体，按面积比例删会把它当碎屑清掉。
    但反过来「负体积一律保留」也不对：一块翻面的小碎屑同样是负体积，会被
    当成内腔永久留下。正确判据是——负体积分量还必须位于某个实体分量内部。
    """
    try:
        ratio = float(params.get("small_ratio", 0.01))
    except (TypeError, ValueError):
        ratio = 0.01
    if not np.isfinite(ratio):
        ratio = 0.01
    ratio = min(max(ratio, 0.0), 0.5)
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces)
    info = analysis.classify_components(verts, faces, ratio)
    n = info["components"]
    if n <= 1:
        return mesh, {"components": n, "removed": 0}
    labels = info["labels"]
    keep_ids = [s["id"] for s in info["sizes"] if s["kind"] != "debris"]
    removed = int(n - len(keep_ids))
    if removed == 0:
        return mesh, {"components": n, "removed": 0, "cavities_kept": info["cavities"],
                      "rejected_cavities": info["rejected_cavities"]}
    new = trimesh.Trimesh(vertices=verts, faces=faces[np.isin(labels, keep_ids)], process=True)
    return new, {"components_before": n, "components_after": int(len(keep_ids)),
                 "removed": removed, "cavities_kept": info["cavities"],
                 "rejected_cavities": info["rejected_cavities"]}


def act_resolve_self_intersection(mesh, params):
    raw = params.get("remesh_pitch")
    try:
        requested = float(raw) if raw not in (None, "") else 0.0
    except (TypeError, ValueError):
        requested = 0.0
    # sanitize_pitch 统一处理 0/负数/NaN/过小/过大，并给出实际生效值
    pitch = voxelops.sanitize_pitch(mesh, requested if requested > 0 else None)
    faces_before = len(mesh.faces)
    sm = _bounded_int(params, "remesh_smooth", 3, 0, 20)
    out = voxelops.remesh(mesh, pitch, smooth_iters=sm)
    return out, {"pitch": round(pitch, 4), "pitch_requested": requested,
                 "faces_before": faces_before, "faces_after": len(out.faces),
                 "smooth_iters": sm}


def act_rescale(mesh, params):
    try:
        factor = float(params.get("scale_factor", 1.0))
    except (TypeError, ValueError):
        raise ValueError("缩放系数不是有效数字")
    if not np.isfinite(factor) or factor <= 0:
        raise ValueError("缩放系数必须为有限正数")
    if not (1e-6 <= factor <= 1e6):
        raise ValueError(f"缩放系数 {factor:g} 超出安全范围（1e-6 ~ 1e6）")
    if abs(factor - 1.0) < 1e-9:
        return mesh, {"applied": False}
    before = [round(float(x), 3) for x in mesh.extents]
    mesh.apply_scale(factor)
    return mesh, {"factor": factor, "extents_before": before, "extents_after": [round(float(x), 3) for x in mesh.extents]}


def act_thicken(mesh, params):
    try:
        requested = float(params.get("thicken_mm", 0.5))
    except (TypeError, ValueError):
        raise ValueError("加厚量不是有效数字")
    if not np.isfinite(requested):
        raise ValueError("加厚量不是有限数值")
    if requested <= 0:
        return mesh, {"applied": False}
    scale = float(max(mesh.extents)) if mesh.extents is not None else 0.0
    if scale <= 0:
        raise ValueError("模型包围盒为空，无法加厚")
    # 距离场要为 amount 预留空余体素，amount 越大 padding 越深；
    # 外部传入 1000mm 这类值会直接吃掉数 GB 内存，这里收敛到模型尺度内。
    amount = min(requested, max(0.5, scale * 0.25))
    before = analysis.sample_wall_thickness(mesh, 800)
    sm = _bounded_int(params, "thicken_smooth", 2, 0, 20)
    out = voxelops.thicken(mesh, amount, smooth_iters=sm)
    after = analysis.sample_wall_thickness(out, 800)
    info = {
        "delta_mm": round(amount, 3),
        "min_before": before["min"],
        "min_after": after["min"],
        "smooth_iters": sm,
        "note": "体素法加厚，锐边会轻微圆化",
    }
    if amount < requested:
        info["requested_mm"] = round(requested, 3)
        info["note"] += "；加厚量已收敛到模型尺度的 25%"
    if before.get("degraded") or after.get("degraded"):
        info["degraded"] = True
    return out, info


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
