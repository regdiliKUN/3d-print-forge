"""模型检测引擎：水密性、法线、壁厚、非流形边、自相交、孤立面片、单位尺寸。

每个检测项返回统一结构，附严重程度与可一键执行的修复动作。
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from . import config, meshio, store

SEV_ORDER = {"ok": 0, "info": 1, "warning": 2, "error": 3}


# ---------------------------------------------------------------- 几何工具
def edge_keys(faces: np.ndarray, nverts: int) -> np.ndarray:
    """把每条无向边编码成单个 int64，便于快速计数。"""
    e = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    e.sort(axis=1)
    return e[:, 0].astype(np.int64) * int(nverts) + e[:, 1].astype(np.int64)


def edge_histogram(faces: np.ndarray, nverts: int) -> dict:
    if len(faces) == 0:
        return {"boundary": 0, "nonmanifold": 0, "total": 0}
    keys = edge_keys(faces, nverts)
    _, counts = np.unique(keys, return_counts=True)
    return {
        "boundary": int((counts == 1).sum()),
        "nonmanifold": int((counts > 2).sum()),
        "total": int(len(counts)),
        "max_share": int(counts.max()) if len(counts) else 0,
    }


def tri_areas(verts: np.ndarray, faces: np.ndarray) -> np.ndarray:
    if len(faces) == 0:
        return np.zeros(0, np.float64)
    t = verts[faces]
    return 0.5 * np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1)


def signed_volume(verts: np.ndarray, faces: np.ndarray) -> float:
    if len(faces) == 0:
        return 0.0
    t = verts[faces].astype(np.float64)
    return float(np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6.0)


def face_normals(verts: np.ndarray, faces: np.ndarray) -> np.ndarray:
    t = verts[faces]
    n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    ln = np.linalg.norm(n, axis=1)
    ln[ln == 0] = 1.0
    return n / ln[:, None]


def union_components(faces: np.ndarray, nverts: int) -> tuple[int, np.ndarray]:
    """按共享顶点把面分组，返回 (分量数, 每个面的分量标签)。"""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    if len(faces) == 0:
        return 0, np.zeros(0, np.int32)
    e = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    e = np.unique(np.sort(e, axis=1), axis=0)
    adj = coo_matrix(
        (np.ones(len(e), np.int8), (e[:, 0], e[:, 1])), shape=(nverts, nverts)
    )
    n, labels = connected_components(adj, directed=False)
    return int(n), labels[faces[:, 0]].astype(np.int32)


# ---------------------------------------------------------------- 自相交
def sat_overlap(T1: np.ndarray, T2: np.ndarray, tol: float = 1e-9, parallel_tol: float = 0.05) -> np.ndarray:
    """分离轴定理判定三角形对是否相交。T1/T2 形状 (K,3,3)。

    标准 11 轴 SAT（两法线 + 9 个边叉积），但额外排除近共面的对：
    三角形是零厚度面片，共面相邻时法线方向投影必然重叠，SAT 会把
    体素重网格出来的大量相邻面误判成自交。近共面的直接判为不相交。
    """
    e1 = np.stack([T1[:, 1] - T1[:, 0], T1[:, 2] - T1[:, 1], T1[:, 0] - T1[:, 2]], axis=1)
    e2 = np.stack([T2[:, 1] - T2[:, 0], T2[:, 2] - T2[:, 1], T2[:, 0] - T2[:, 2]], axis=1)
    raw1 = np.cross(e1[:, 0], e1[:, 1])
    raw2 = np.cross(e2[:, 0], e2[:, 1])
    l1 = np.linalg.norm(raw1, axis=1)
    l2 = np.linalg.norm(raw2, axis=1)
    n1 = raw1 / np.where(l1 > 1e-12, l1, 1.0)[:, None]
    n2 = raw2 / np.where(l2 > 1e-12, l2, 1.0)[:, None]
    parallel = np.linalg.norm(np.cross(n1, n2), axis=1) < parallel_tol

    axes = [raw1, raw2]
    for i in range(3):
        for j in range(3):
            axes.append(np.cross(e1[:, i], e2[:, j]))
    overlap = np.ones(len(T1), dtype=bool)
    for a in axes:
        na = np.linalg.norm(a, axis=1)
        valid = na > 1e-12
        aa = a / np.where(valid, na, 1.0)[:, None]
        p1 = np.einsum("kij,kj->ki", T1, aa)
        p2 = np.einsum("kij,kj->ki", T2, aa)
        sep = (p1.max(1) < p2.min(1) - tol) | (p2.max(1) < p1.min(1) - tol)
        overlap &= ~(sep & valid)
    return overlap & ~parallel


def find_self_intersections(verts: np.ndarray, faces: np.ndarray, max_pairs: int = 400_000) -> dict:
    """KD 树取局部候选对，再用 SAT 精判。自交在物理上都是局部的，这个近似足够。"""
    from scipy.spatial import cKDTree

    if len(faces) < 2:
        return {"count": 0, "pairs": [], "sampled": False}
    t = verts[faces].astype(np.float64)
    centers = t.mean(axis=1)
    radii = np.linalg.norm(t - centers[:, None, :], axis=2).max(axis=1)
    # 用 99 分位而不是 90 分位：模型里混着细长三角形时，90 分位会让搜索半径偏小，
    # 远端的穿插直接漏检。同时用包围盒对角线封顶，避免候选对爆炸。
    diag = float(np.linalg.norm(np.ptp(verts, axis=0))) or 1.0
    r = min(float(np.percentile(radii, 99)) * 2.2, diag * 0.25)
    r = max(r, 1e-6)
    tree = cKDTree(centers)
    pairs = tree.query_pairs(r=r, output_type="ndarray")
    if len(pairs) == 0:
        return {"count": 0, "pairs": [], "sampled": False}
    sampled = False
    if len(pairs) > max_pairs:
        idx = np.random.default_rng(7).choice(len(pairs), max_pairs, replace=False)
        pairs = pairs[idx]
        sampled = True
    # 排除共享顶点的相邻面
    f1, f2 = faces[pairs[:, 0]], faces[pairs[:, 1]]
    share = (
        (f1[:, 0] == f2[:, 0]) | (f1[:, 0] == f2[:, 1]) | (f1[:, 0] == f2[:, 2])
        | (f1[:, 1] == f2[:, 0]) | (f1[:, 1] == f2[:, 1]) | (f1[:, 1] == f2[:, 2])
        | (f1[:, 2] == f2[:, 0]) | (f1[:, 2] == f2[:, 1]) | (f1[:, 2] == f2[:, 2])
    )
    pairs = pairs[~share]
    if len(pairs) == 0:
        return {"count": 0, "pairs": [], "sampled": sampled}
    # AABB 粗筛
    amin = t[pairs[:, 0]].min(axis=1)
    amax = t[pairs[:, 0]].max(axis=1)
    bmin = t[pairs[:, 1]].min(axis=1)
    bmax = t[pairs[:, 1]].max(axis=1)
    box_ok = np.all(amin <= bmax + 1e-9, axis=1) & np.all(bmin <= amax + 1e-9, axis=1)
    pairs = pairs[box_ok]
    if len(pairs) == 0:
        return {"count": 0, "pairs": [], "sampled": sampled}
    scale = float(max(np.ptp(verts, axis=0))) or 1.0
    hit = sat_overlap(t[pairs[:, 0]], t[pairs[:, 1]], tol=max(1e-9, scale * 1e-6))
    bad = pairs[hit]
    return {
        "count": int(len(bad)),
        "pairs": bad[:500].tolist(),
        "sampled": sampled,
        "candidate_pairs": int(len(pairs)),
    }


# ---------------------------------------------------------------- 壁厚
def _mt_batch(origin: np.ndarray, direction: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Möller–Trumbore：单条射线对一批三角形求交，返回命中参数 t。"""
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


_RAY_INDEX_CACHE: dict = {}


def _raycast_nearest(verts: np.ndarray, faces: np.ndarray, origins: np.ndarray,
                     dirs: np.ndarray, eps: float,
                     max_dist: float | None = None) -> np.ndarray:
    """带 AABB 索引的最近命中距离。

    不用 trimesh 的 ray：它会对「射线数 × 三角形数」做全量 AABB 矩阵，
    6000 条射线 × 2 万面就是 1.2 亿个 float64，内存直接冲到 4.7GB、耗时 46 秒。
    这里按射线的 AABB 从索引里取候选，只对候选做求交，量级降到千分之一。

    max_dist 用来截断射线：壁厚判定只关心「有没有比阈值更薄的地方」，
    射线飞 200mm 才命中的位置记为 200 和记为 12 对结论没有区别，
    但搜索盒小一个数量级，候选对跟着少一个数量级。
    """
    from rtree import index as rtree_index

    key = (id(verts), len(faces))
    cached = _RAY_INDEX_CACHE.get(key)
    tri = verts[faces]
    if cached is None:
        lo = tri.min(axis=1)
        hi = tri.max(axis=1)
        prop = rtree_index.Property()
        prop.dimension = 3
        # 用生成器批量建索引，逐条 insert 在两万面上要好几秒
        def _stream():
            for i in range(len(tri)):
                yield (i, (lo[i, 0], lo[i, 1], lo[i, 2], hi[i, 0], hi[i, 1], hi[i, 2]), None)

        idx = rtree_index.Index(_stream(), properties=prop)
        if len(_RAY_INDEX_CACHE) > 2:
            _RAY_INDEX_CACHE.clear()
        _RAY_INDEX_CACHE[key] = idx
    else:
        idx = cached

    out = np.full(len(origins), np.inf)
    span = float(np.linalg.norm(np.ptp(verts, axis=0))) * 1.2 + 1e-6
    if max_dist is not None:
        span = min(span, float(max_dist))
    for k in range(len(origins)):
        o = origins[k]
        d = dirs[k]
        end = o + d * span
        box = (
            min(o[0], end[0]), min(o[1], end[1]), min(o[2], end[2]),
            max(o[0], end[0]), max(o[1], end[1]), max(o[2], end[2]),
        )
        cand = list(idx.intersection(box))
        if not cand:
            continue
        t = _mt_batch(o, d, tri[cand])
        t = t[(t > eps) & (t <= span)]
        if len(t):
            out[k] = float(t.min())
    return out


def sample_wall_thickness(mesh, sample_n: int = 6000, multi_dir: int = 3,
                          max_dist: float | None = None) -> dict:
    """沿面法线反向打射线，量到对侧表面的距离。

    单方向会高估：曲面和楔形处，法线方向的穿行距离大于真实最小壁厚。
    所以再沿切平面偏转若干角度补几发射线，取最小值——偏保守，但不会漏掉薄点。
    """
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces)
    if len(faces) == 0:
        return {"samples": 0, "min": None, "median": None, "p5": None, "thin_ratio": 0.0}
    n = min(sample_n, len(faces))
    rng = np.random.default_rng(11)
    idx = rng.choice(len(faces), n, replace=False)
    t = verts[faces[idx]]
    centers = t.mean(axis=1)
    normals = face_normals(verts, faces[idx])
    eps = max(1e-4, float(np.linalg.norm(mesh.extents)) * 1e-6)
    origins = centers - normals * eps

    dirs = [-normals]
    if multi_dir > 1:
        helper = np.where(np.abs(normals[:, 2:3]) < 0.9,
                          np.array([[0.0, 0, 1]]), np.array([[1.0, 0, 0]]))
        t1 = np.cross(normals, helper)
        t1 /= np.maximum(np.linalg.norm(t1, axis=1, keepdims=True), 1e-12)
        ang = np.radians(20.0)
        dirs.append(-np.cos(ang) * normals + np.sin(ang) * t1)
        dirs.append(-np.cos(ang) * normals - np.sin(ang) * t1)
        if multi_dir > 3:
            t2 = np.cross(normals, t1)
            dirs.append(-np.cos(ang) * normals + np.sin(ang) * t2)

    thick = np.full(n, np.inf)
    try:
        for d in dirs:
            dist = _raycast_nearest(verts, faces, origins, d, eps * 10, max_dist)
            thick = np.minimum(thick, dist)
    except Exception:
        from scipy.spatial import cKDTree

        tree = cKDTree(verts)
        d2, _ = tree.query(centers, k=2)
        thick = d2[:, 1] * 2.0
    ok = thick[np.isfinite(thick) & (thick > 0)]
    if len(ok) == 0:
        return {"samples": 0, "min": None, "median": None, "p5": None, "thin_ratio": 0.0}
    th = config.get().thresholds
    return {
        "samples": int(len(ok)),
        "min": round(float(ok.min()), 3),
        "median": round(float(np.median(ok)), 3),
        "p5": round(float(np.percentile(ok, 5)), 3),
        "max": round(float(ok.max()), 3),
        "thin_ratio": round(float((ok < th.wall_warn).mean()), 4),
        "fail_ratio": round(float((ok < th.wall_fail).mean()), 4),
        "directions": len(dirs),
    }


# ---------------------------------------------------------------- 单位推断
def infer_units(extents) -> dict:
    mx = float(max(extents)) if extents is not None and len(extents) else 0.0
    if mx <= 0:
        return {"unit": "unknown", "max_dim": 0, "scale": 1.0, "message": "尺寸为零，无法推断"}
    if mx < 1.0:
        return {
            "unit": "m",
            "max_dim": round(mx, 4),
            "scale": 1000.0,
            "message": f"最大尺寸 {mx:.4f}，量级对应米。建议放大 1000 倍转为毫米。",
        }
    if mx <= 400.0:
        return {
            "unit": "mm",
            "max_dim": round(mx, 2),
            "scale": 1.0,
            "message": f"最大尺寸 {mx:.1f}mm，在绝大多数桌面机成型范围内，无需换算。",
        }
    if mx <= 1000.0:
        return {
            "unit": "mm",
            "max_dim": round(mx, 2),
            "scale": 1.0,
            "message": f"最大尺寸 {mx:.1f}mm，偏大。请确认是否超出你的成型空间。",
        }
    # 超过 1 米：要么是 cm/inch 误存，要么真的是超大件
    as_in = mx / 25.4
    nearest = round(as_in)
    if nearest >= 1 and abs(as_in - nearest) / as_in < 0.002:
        return {
            "unit": "inch",
            "max_dim": round(mx, 2),
            "scale": 1 / 25.4,
            "message": f"最大尺寸 {mx:.1f} 恰好是 {nearest} 英寸（{as_in:.2f}），判定为英寸建模，应缩小 25.4 倍。",
        }
    if mx <= 5000.0:
        return {
            "unit": "cm?",
            "max_dim": round(mx, 2),
            "scale": 0.1,
            "message": f"最大尺寸 {mx:.0f}，超出常规毫米件范围。若原模型按厘米设计（约 {mx / 10:.0f}cm），应缩小 10 倍。",
        }
    return {
        "unit": "oversize",
        "max_dim": round(mx, 2),
        "scale": 0.001,
        "message": f"最大尺寸 {mx:.0f}，远超打印范围，可能是微米单位或数值异常。请核对原始 CAD 单位。",
    }


# ---------------------------------------------------------------- 检测项
def _item(id_, name, severity, message, value=None, fix=None, details=None) -> dict:
    return {
        "id": id_,
        "name": name,
        "severity": severity,
        "message": message,
        "value": value or {},
        "fix": fix,
        "details": details or {},
    }


def check_watertight(mesh, params, ctx=None) -> dict:
    verts, faces = np.asarray(mesh.vertices), np.asarray(mesh.faces)
    h = edge_histogram(faces, len(verts))
    if h["boundary"] == 0 and h["nonmanifold"] == 0:
        return _item("watertight", "水密性", "ok", "所有边都被恰好两个面共享，模型闭合，可直接布尔运算与切片。",
                     {"boundary_edges": 0})
    sev = "error" if h["boundary"] > 0 else "warning"
    return _item(
        "watertight", "水密性", sev,
        f"存在 {h['boundary']} 条开放边，模型不闭合。切片软件无法判断内外，可能打出空壳或直接报错。",
        {"boundary_edges": h["boundary"], "nonmanifold_edges": h["nonmanifold"]},
        fix={"action": "fill_holes", "label": "一键补洞"},
    )


def check_normals(mesh, params, ctx=None) -> dict:
    verts, faces = np.asarray(mesh.vertices), np.asarray(mesh.faces)
    vol = signed_volume(verts, faces)
    areas = tri_areas(verts, faces)
    degenerate = int((areas < 1e-12).sum())
    flipped = vol < 0
    msgs, sev, fixes = [], "ok", None
    if flipped:
        sev = "error"
        msgs.append("有向体积为负，整体法线朝内——切片软件会把实体当成空腔。")
        fixes = {"action": "fix_normals", "label": "一键翻转法线"}
    if degenerate:
        if sev == "ok":
            sev = "warning"
        msgs.append(f"有 {degenerate} 个零面积退化面，会产生无效法线。")
    if not msgs:
        msgs.append("法线朝外且方向一致，有向体积为正。")
    return _item(
        "normals", "法线方向", sev, " ".join(msgs),
        {"signed_volume": round(vol, 3), "degenerate_faces": degenerate,
         "winding_consistent": bool(getattr(mesh, "is_winding_consistent", False))},
        fix=fixes or ({"action": "merge_vertices", "label": "清理退化面"} if degenerate else None),
    )


def check_thickness(mesh, params, ctx=None) -> dict:
    """壁厚检测，厚模型走快速通道。

    壁厚采样是全流程最慢的一项（每条射线都要遍历候选三角形）。
    先抽 600 面粗测，如果最薄处都在告警线两倍以上，就没必要再跑全量——
    实测这一步让厚模型省掉约 90% 的检测时间。
    """
    n = int(params.get("sample_faces", 1500))
    th = config.get().thresholds
    quick = sample_wall_thickness(mesh, min(600, n), multi_dir=1)
    if quick["samples"] and quick["min"] and quick["min"] > th.wall_warn * 2:
        quick["mode"] = "coarse"
        return _item("thickness", "壁厚", "ok",
                     f"最薄 {quick['min']:.2f}mm（粗筛，600 面采样），远高于 {th.wall_warn}mm 告警线，未做全量扫描。",
                     quick)
    # 全量阶段截断射线：只要判定「比告警线厚」就够了，不必扫穿整个模型
    res = sample_wall_thickness(mesh, n, multi_dir=3, max_dist=max(12.0, th.wall_warn * 10))
    res["mode"] = "full"
    if res["samples"] == 0:
        # 射线全被截断，说明整件都比截断距离还厚，用粗筛结论即可
        quick["mode"] = "coarse"
        return _item("thickness", "壁厚", "ok",
                     f"最薄 {quick['min']:.2f}mm，整件厚度都在截断距离以外，未发现薄壁。",
                     quick)
    mn = res["min"]
    if mn < th.wall_fail:
        sev, msg = "error", f"最薄处仅 {mn:.2f}mm（中位 {res['median']:.2f}mm），低于 {th.wall_fail}mm 的打印下限，成型后会断裂或缺失。"
    elif mn < th.wall_warn:
        sev, msg = "warning", f"最薄处 {mn:.2f}mm（中位 {res['median']:.2f}mm），偏薄，清洗和二次固化时容易压塌。"
    else:
        sev, msg = "ok", f"最薄 {mn:.2f}mm，中位 {res['median']:.2f}mm，厚度安全。"
    return _item("thickness", "壁厚", sev, msg, res,
                 fix={"action": "thicken", "label": "一键加厚到安全值"} if sev != "ok" else None)


def check_nonmanifold(mesh, params, ctx=None) -> dict:
    verts, faces = np.asarray(mesh.vertices), np.asarray(mesh.faces)
    h = edge_histogram(faces, len(verts))
    if h["nonmanifold"] == 0:
        return _item("nonmanifold", "非流形边", "ok", "没有一条边被超过两个面共享。", {"count": 0})
    return _item(
        "nonmanifold", "非流形边", "warning",
        f"{h['nonmanifold']} 条边被 3 个及以上面共享。布尔运算和切片可能在这里产生歧义，常见于两块面片叠在一起。",
        {"count": h["nonmanifold"]},
        fix={"action": "merge_vertices", "label": "合并顶点消解"},
    )


def check_self_intersection(mesh, params, ctx=None) -> dict:
    if not params.get("check_self_intersection", True):
        return _item("self_intersection", "自相交", "info", "本次已跳过自相交检测。", {"skipped": True})
    verts, faces = np.asarray(mesh.vertices), np.asarray(mesh.faces)
    t0 = time.time()
    res = find_self_intersections(verts, faces)
    res["elapsed"] = round(time.time() - t0, 2)
    if res["count"] == 0:
        return _item("self_intersection", "自相交", "ok",
                     f"未发现三角形互相穿插（检查 {res.get('candidate_pairs', 0)} 组候选对）。", res)
    return _item(
        "self_intersection", "自相交", "warning",
        f"发现 {res['count']} 处三角形穿插。切片时这些区域会生成错误的截面，表现为模型表面出现多余薄片或孔洞。",
        res,
        fix={"action": "resolve_self_intersection", "label": "体素重网格消解"},
    )


def check_components(mesh, params, ctx=None) -> dict:
    verts, faces = np.asarray(mesh.vertices), np.asarray(mesh.faces)
    n, labels = union_components(faces, len(verts))
    if n <= 1:
        return _item("components", "孤立面片", "ok", "模型是单一连通体。", {"components": n})
    areas = tri_areas(verts, faces)
    sizes = []
    for c in range(n):
        m = labels == c
        sizes.append({"faces": int(m.sum()), "area": float(areas[m].sum())})
    sizes.sort(key=lambda x: -x["area"])
    tiny = [s for s in sizes if s["area"] < config.get().thresholds.tiny_volume]
    sev = "warning" if tiny else "info"
    msg = f"模型由 {n} 个互不连通的部件组成，最大部件占 {sizes[0]['area'] / max(1e-9, sum(s['area'] for s in sizes)) * 100:.1f}% 表面积。"
    if tiny:
        msg += f" 其中 {len(tiny)} 个是碎屑（体积过小），建议清理。"
    return _item("components", "孤立面片", sev, msg,
                 {"components": n, "sizes": sizes[:8], "tiny": len(tiny)},
                 fix={"action": "remove_small", "label": "移除碎屑"} if tiny else None)


def check_units(mesh, params, ctx=None) -> dict:
    ext = getattr(mesh, "extents", None)
    res = infer_units(ext)
    sev = "ok" if res["unit"] == "mm" else ("error" if res["unit"] == "oversize" else "warning")
    return _item("units", "单位与尺寸", sev, res["message"], res,
                 fix={"action": "rescale", "label": f"按 ×{res['scale']:.4g} 换算"} if abs(res["scale"] - 1) > 1e-9 else None)


CHECKS = [
    check_watertight,
    check_normals,
    check_thickness,
    check_nonmanifold,
    check_self_intersection,
    check_components,
    check_units,
]


# ---------------------------------------------------------------- 任务
def summarize(items: list[dict]) -> dict:
    counts = {"ok": 0, "info": 0, "warning": 0, "error": 0}
    for it in items:
        counts[it["severity"]] = counts.get(it["severity"], 0) + 1
    worst = max((it["severity"] for it in items), key=lambda s: SEV_ORDER[s], default="ok")
    actionable = [it["id"] for it in items if it.get("fix")]
    return {
        "total": len(items),
        "counts": counts,
        "worst": worst,
        "actionable": actionable,
        "blocking": counts["error"] > 0,
    }


def run_job(ctx, params):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    pid = ctx.job.project_id
    proj = store.get(pid)
    if not proj:
        raise ValueError("项目不存在")
    path = Path(proj.get("current") or proj["source"])
    mtime = path.stat().st_mtime_ns

    ctx.tick(0.02, "读取模型文件", "load")
    mesh = meshio.load_mesh(path)
    stats = meshio.mesh_stats(mesh)
    ctx.complete_step("load", {"faces": stats["faces"], "mtime": mtime})

    # 断点续跑：已算过且文件没变过的项直接复用
    items: list[dict | None] = [None] * len(CHECKS)
    todo: list[int] = []
    for i, fn in enumerate(CHECKS):
        rec = ctx.job.steps.get(fn.__name__)
        if rec and rec.get("status") == "done" and rec.get("data", {}).get("_mtime") == mtime:
            items[i] = rec["data"]["item"]
        else:
            todo.append(i)

    def work(i: int) -> tuple[int, dict]:
        t0 = time.time()
        it = CHECKS[i](mesh, params, None)
        it["elapsed"] = round(time.time() - t0, 3)
        return i, it

    # 七项检测彼此独立，线程池并行——耗时从「七项之和」压到「最慢的那项」
    done = len(CHECKS) - len(todo)
    if todo:
        workers = min(4, len(todo))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chk") as ex:
            futs = [ex.submit(work, i) for i in todo]
            for fut in as_completed(futs):
                i, it = fut.result()
                items[i] = it
                ctx.complete_step(CHECKS[i].__name__, {"_mtime": mtime, "item": it})
                done += 1
                ctx.tick(0.05 + 0.9 * done / len(CHECKS), f"完成 {done}/{len(CHECKS)} 项", "analyze")

    final_items = [it for it in items if it is not None]
    summary = summarize(final_items)
    report = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "file": path.name,
        "stats": stats,
        "items": final_items,
        "summary": summary,
    }
    store.update(pid, analysis=report)
    ctx.tick(1.0, "检测完成", "done")
    return {"report": report}
