"""打印物理模型层：把网格几何换算成「悬不悬」「要多久」「用多少料」。

这一层刻意和 advisor 分开：advisor 负责「给什么参数」，这里负责
「这些参数背后的物理量是多少」。所有的量都要能说清是怎么算出来的，
不接受「一般来说是这么多」这种来源。

两类核心计算：

1. support_analysis —— 真实悬垂判别
   不能只看面法线朝不朝下：帽底压在粗柱顶上时法线朝下，但下面有料接着，
   根本不用支撑。也不能只向下打射线看落点：柱内起点会一路穿到柱底，
   把 0 间隙误判成整根柱高。

   这里改用「上一层截面」判定：打印是一层层堆的，第 i 层只需要第 i−1 层托着。
   取该点所在高度下方的截面轮廓，向外扩 bridge_mm（可架桥距离），
   点落在这个范围里就是有支撑。这对重合面几何天然稳健，
   也是切片器生成支撑时的真实判据。

2. cross_section_profile —— 截面轮廓积分
   等间距取若干水平截面，梯形法积分出 ∫面积dz（≈体积，可拿来自检）
   和 ∫周长dz。周长积分直接决定外墙挤出路径长度，是 FDM 时长的骨架。
"""
from __future__ import annotations

import math

import numpy as np

from . import analysis, gpu

# 采样点数：悬垂判别按面积均匀撒点，点数决定「最近支撑点」的分辨率。
# 2000 点在一个 300mm² 的朝下区域上点间距约 0.4mm，对 2mm 的跨距阈值够用。
SUPPORT_SAMPLES = 2000

# 默认可架桥跨距（mm）：不靠支撑也能打出来的悬空宽度。
# 光固化取 2mm：层间是化学粘接，悬空段靠上一层拉住，2mm 以内基本不塌。
# FDM 取 5mm：挤出丝本身有刚度，常见桥接测试能过 10mm 以上，5mm 留足余量。
BRIDGE_MM_SLA = 2.0
BRIDGE_MM_FDM = 5.0

# 少于这个面积的悬空区不值得单独加支撑（mm²）。
# 支撑本身有接触疤，为几平方毫米的悬空加支撑，往往比不打还糟。
MIN_SUPPORT_AREA_MM2 = 25.0

# 贴着平台的底面由台面承接，这个厚度内不算悬空（mm）
PLATE_GAP_MM = 0.5


def _eps_for(verts: np.ndarray) -> float:
    """射线起点偏移量。按模型对角线取相对值，避免大模型精度丢失。"""
    diag = float(np.linalg.norm(np.ptp(verts, axis=0)))
    return max(1e-4, diag * 1e-6)


def _sample_points_on_faces(verts: np.ndarray, faces: np.ndarray,
                            tri_idx: np.ndarray, areas: np.ndarray,
                            n: int, rng) -> tuple[np.ndarray, np.ndarray]:
    """在指定三角形集合上按面积均匀撒点。

    不能用三角形重心代替：一个 40×40 的平面只有两个三角形，
    两个重心都落在中间，边缘的悬空区会被整个漏掉。

    返回 (采样点, 每个点所属三角形在 tri_idx 中的下标)。
    """
    w = np.maximum(areas, 0.0)
    total = float(w.sum())
    if total <= 0:
        return np.zeros((0, 3)), np.zeros((0,), dtype=int)
    cdf = np.cumsum(w)
    pick = np.searchsorted(cdf, rng.random(n) * total)
    pick = np.clip(pick, 0, len(tri_idx) - 1)
    tri = faces[tri_idx[pick]]

    u = rng.random(n)
    v = rng.random(n)
    flip = u + v > 1.0
    u = np.where(flip, 1.0 - u, u)
    v = np.where(flip, 1.0 - v, v)
    w0 = (1.0 - u - v)[:, None]
    pts = (verts[tri[:, 0]] * w0
           + verts[tri[:, 1]] * u[:, None]
           + verts[tri[:, 2]] * v[:, None])
    return pts, pick


def cross_section_polygons(mesh, samples: int = 20,
                           max_faces: int = 400_000) -> tuple[list[float], list]:
    """等间距水平截面的多边形序列（shapely 几何，带洞）。

    返回 (高度列表, 几何列表)，几何可能是 Polygon / MultiPolygon / None。
    """
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces)
    if len(faces) == 0 or len(faces) > max_faces:
        return [], []
    lo, hi = float(verts[:, 2].min()), float(verts[:, 2].max())
    span = max(hi - lo, 1e-6)
    n = int(max(4, min(samples, 64)))
    zs: list[float] = []
    geoms: list = []
    for i in range(n):
        z = lo + span * (i + 0.5) / n
        g = None
        try:
            sec = mesh.section(plane_origin=[0.0, 0.0, float(z)],
                               plane_normal=[0.0, 0.0, 1.0])
            if sec is not None:
                # trimesh 5.x 把 to_planar 改名为 to_2D，老版本没有新方法
                planar, _ = (sec.to_2D() if hasattr(sec, "to_2D")
                             else sec.to_planar())
                polys = [p for p in planar.polygons_full
                         if p is not None and not p.is_empty]
                if polys:
                    from shapely.ops import unary_union

                    g = unary_union(polys) if len(polys) > 1 else polys[0]
        except Exception:
            g = None
        zs.append(float(z))
        geoms.append(g)
    return zs, geoms


def cross_section_profile(mesh, samples: int = 20, max_faces: int = 400_000,
                          sections: tuple[list[float], list] | None = None) -> dict:
    """等间距水平截面的面积/周长序列与积分。

    ∫A(z)dz 应该等于体积，拿这个当自检：差得远说明模型有破面或者采样太稀。

    ∫P(z)dz 是侧面周长沿高度的积分，乘线宽再乘圈数就是外墙耗材体积，
    也是外墙挤出路径长度，FDM 时长主要就靠它。
    """
    blank = {
        "ok": False, "samples": 0, "zs": [], "areas": [], "perimeters": [],
        "max_area_mm2": 0.0, "volume_integral_mm3": 0.0,
        "perimeter_integral_mm2": 0.0, "correction": 1.0,
    }
    # sections 允许调用方复用已经切好的截面——同一张网格没必要切两遍
    zs, geoms = sections if sections is not None else cross_section_polygons(
        mesh, samples=samples, max_faces=max_faces)
    if not zs:
        return blank

    areas: list[float] = []
    perims: list[float] = []
    for g in geoms:
        a = 0.0
        p = 0.0
        if g is not None:
            try:
                a = float(g.area)
                p = float(g.length)
            except Exception:
                a = p = 0.0
        areas.append(a)
        perims.append(p)

    areas_np = np.asarray(areas, dtype=np.float64)
    perims_np = np.asarray(perims, dtype=np.float64)
    int_a = float(np.trapezoid(areas_np, zs))
    int_p = float(np.trapezoid(perims_np, zs))

    # 用体积校验采样精度：截面太稀会低估，按同比例修正周长积分
    try:
        vol = abs(float(mesh.volume))
    except Exception:
        vol = 0.0
    correction = (vol / int_a) if (int_a > 0 and vol > 0) else 1.0
    correction = float(np.clip(correction, 0.5, 2.0))

    return {
        "ok": True,
        "samples": len(zs),
        "zs": [round(float(z), 3) for z in zs],
        "areas": [round(float(x), 2) for x in areas_np],
        "perimeters": [round(float(x), 2) for x in perims_np],
        "max_area_mm2": round(float(areas_np.max()) if len(areas_np) else 0.0, 1),
        "volume_integral_mm3": round(int_a, 1),
        "perimeter_integral_mm2": round(int_p * correction, 1),
        "correction": round(correction, 3),
    }


def _geom_rings(g) -> list:
    """把 shapely 几何拆成环数组（外环 + 各洞），供批量奇偶判定用。"""
    if g is None or g.is_empty:
        return []
    parts = [g] if g.geom_type == "Polygon" else list(getattr(g, "geoms", []))
    rings = []
    for poly in parts:
        if poly.geom_type != "Polygon" or poly.is_empty:
            continue
        rings.append(np.asarray(poly.exterior.coords, dtype=np.float64))
        for hole in poly.interiors:
            rings.append(np.asarray(hole.coords, dtype=np.float64))
    return rings


def _dist_to_rings(pts_xy: np.ndarray, rings: list) -> np.ndarray:
    """点到多边形边界的最短距离，向量化版（点到线段距离取最小）。"""
    out = np.full(len(pts_xy), np.inf, dtype=np.float64)
    if len(pts_xy) == 0:
        return out
    for ring in rings:
        if ring is None or len(ring) < 2:
            continue
        a = ring
        c = np.roll(ring, -1, axis=0)
        ab = c - a
        l2 = (ab ** 2).sum(axis=1)
        l2 = np.where(l2 < 1e-12, 1e-12, l2)
        ap = pts_xy[:, None, :] - a[None, :, :]
        t = np.clip((ap * ab[None, :, :]).sum(axis=2) / l2[None, :], 0.0, 1.0)
        proj = a[None, :, :] + t[:, :, None] * ab[None, :, :]
        d = np.sqrt(((pts_xy[:, None, :] - proj) ** 2).sum(axis=2)).min(axis=1)
        out = np.minimum(out, d)
    return out


def support_analysis(mesh, threshold_deg: float = 45.0, bridge_mm: float = 2.0,
                     sample_n: int = SUPPORT_SAMPLES, samples: int = 48,
                     seed: int = 29,
                     sections: tuple[list[float], list] | None = None) -> dict:
    """判定哪些朝下的面是真的悬空、要多少支撑。

    两步：
    1) 角度筛选：朝下超过阈值（默认 45°）的面才可能是悬垂；
       贴着平台的底面由台面承接，排除。
    2) 遮挡判定：取该点下方那一层的截面轮廓，向外扩 bridge_mm，
       点还落在外面就说明下方没有材料、跨距也超过自架桥能力，必须加支撑。

    返回：
      overhang_ratio      需要支撑的面积占比（用于「要不要支撑」）
      downward_ratio      朝下面的面积占比
      bridgeable_ratio    朝下但能自行架桥的面积占比
      support_contact_mm2 需要支撑的绝对面积（用于耗材估算）
      max_span_mm         最大悬空跨距
    """
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces)
    empty = {
        "overhang_ratio": 0.0, "downward_ratio": 0.0, "bridgeable_ratio": 0.0,
        "support_contact_mm2": 0.0, "max_span_mm": 0.0, "mean_span_mm": 0.0,
        "samples": 0, "ok": False,
    }
    if len(faces) == 0 or len(verts) == 0:
        return empty

    normals = analysis.face_normals(verts, faces)
    areas = analysis.tri_areas(verts, faces)
    total_area = float(areas.sum())
    if total_area <= 0:
        return empty

    # 法线朝下超过阈值的面：n_z < -cos(阈值)
    limit = -math.cos(math.radians(threshold_deg))
    down_idx = np.flatnonzero(normals[:, 2] < limit)
    if len(down_idx) == 0:
        empty["ok"] = True
        return empty

    rng = np.random.default_rng(seed)
    n = int(max(200, min(sample_n, 8000)))
    pts, _pick = _sample_points_on_faces(verts, faces, down_idx, areas[down_idx], n, rng)
    if len(pts) == 0:
        empty["ok"] = True
        return empty

    zmin = float(verts[:, 2].min())
    zmax = float(verts[:, 2].max())
    height = max(zmax - zmin, 1e-6)

    # 下方那一层的截面轮廓，向外扩 bridge_mm 就是「能被架住」的范围。
    # sections 可由调用方复用，避免同一张网格被切两遍。
    zs, geoms = sections if sections is not None else cross_section_polygons(
        mesh, samples=samples)
    if not zs:
        empty["ok"] = True
        return empty
    zones: list = []
    for g in geoms:
        if g is None or g.is_empty:
            zones.append(None)
            continue
        try:
            zones.append(g.buffer(bridge_mm))
        except Exception:
            zones.append(None)

    z_arr = np.asarray(zs, dtype=np.float64)
    n_pts = len(pts)
    supported = np.zeros(n_pts, dtype=bool)
    span = np.zeros(n_pts, dtype=np.float64)

    # 1) 先给每个采样点分配「下方那一层」的截面下标（向量化，避免 Python 循环）
    j_of = np.searchsorted(z_arr, pts[:, 2], side="left") - 1
    # 贴着平台的底面：台面直接承接
    supported[(pts[:, 2] - zmin) <= PLATE_GAP_MM] = True
    # 落在第一个采样间隔内的，下方就是平台
    supported[j_of < 0] = True

    # 2) 剩下的点按所属层分组，一批算完「点在多边形内」。
    #    原来是逐点调 shapely.contains，2000 个点要 100ms 上下；
    #    改成「点 × 边」矩阵运算后是毫秒级，数据量大时还能落到 GPU 上。
    todo = np.flatnonzero(~supported)
    if len(todo):
        js = j_of[todo]
        order = np.argsort(js, kind="stable")
        todo = todo[order]
        js = js[order]
        cut = np.flatnonzero(np.diff(js)) + 1
        for gi in np.split(np.arange(len(todo)), cut):
            if not len(gi):
                continue
            j = int(js[gi[0]])
            sel = todo[gi]
            zone = zones[j]
            if zone is None:
                span[sel] = height
                continue
            inside = gpu.points_in_rings(pts[sel][:, :2], _geom_rings(zone))
            if inside.any():
                supported[sel[inside]] = True
            out_sel = sel[~inside]
            if len(out_sel):
                d = _dist_to_rings(pts[out_sel][:, :2], _geom_rings(geoms[j]))
                span[out_sel] = np.where(np.isfinite(d), d, height)

    down_area = float(areas[down_idx].sum())
    unsup_frac = float((~supported).mean())
    overhang_ratio = float(unsup_frac * down_area / total_area) if total_area > 0 else 0.0
    bridgeable = float(supported.mean() * down_area / total_area) if total_area > 0 else 0.0
    contact = float(down_area * unsup_frac)

    return {
        "overhang_ratio": round(overhang_ratio, 4),
        "downward_ratio": round(float(down_area / total_area), 4) if total_area > 0 else 0.0,
        "bridgeable_ratio": round(bridgeable, 4),
        "support_contact_mm2": round(contact, 1),
        "max_span_mm": round(float(span.max()) if len(span) else 0.0, 2),
        "mean_span_mm": round(float(span[~supported].mean()) if (~supported).any() else 0.0, 2),
        "samples": int(len(pts)),
        "ok": True,
    }
