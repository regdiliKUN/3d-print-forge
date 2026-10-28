"""几何处理：减面、镂空打孔、支撑生成、自动摆放、缩放。

处理顺序经过刻意安排：
减面 → 镂空 → 摆正 → 打排液孔（在最终姿态的最低点）→ 加支撑 → 缩放。
先摆正再打孔，否则孔会跟着旋转跑到侧面去。
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import trimesh

from . import analysis, config, meshio, store, voxelops

DEFAULTS = {
    "decimate_ratio": 1.0,
    "hollow": False,
    "wall_thickness": 2.0,
    "drain_holes": 2,
    "hole_diameter": 3.0,
    "supports": "none",
    "support_points": [],
    "support_diameter": 1.0,
    "support_spacing": 3.0,
    "overhang_deg": 45.0,
    "orient": True,
    "scale": 1.0,
    "target_height": 0.0,
    "target_max_dim": 0.0,
}


# ---------------------------------------------------------------- 减面
def decimate_mesh(mesh, ratio: float) -> trimesh.Trimesh:
    if ratio >= 0.999 or len(mesh.faces) < 16:
        return mesh
    import fast_simplification

    target = max(4, int(len(mesh.faces) * ratio))
    v, f = fast_simplification.simplify(
        np.asarray(mesh.vertices, dtype=np.float32),
        np.asarray(mesh.faces, dtype=np.int32),
        target_reduction=1.0 - target / len(mesh.faces),
    )
    return trimesh.Trimesh(vertices=v, faces=f, process=True)


# ---------------------------------------------------------------- 摆放
def _overhang_ratio(verts, faces, overhang_deg: float) -> float:
    n = analysis.face_normals(verts, faces)
    a = analysis.tri_areas(verts, faces)
    if a.sum() <= 0:
        return 1.0
    thr = -np.cos(np.radians(overhang_deg))
    return float(a[n[:, 2] < thr].sum() / a.sum())


def auto_orient(mesh, overhang_deg: float = 45.0) -> tuple[trimesh.Trimesh, dict]:
    """枚举 6 个主朝向，选「悬垂少 + 高度低 + 底部稳」的姿态。"""
    import math

    candidates = [
        ("原样", np.eye(4)),
        ("绕X+90°", trimesh.transformations.rotation_matrix(math.pi / 2, [1, 0, 0])),
        ("绕X-90°", trimesh.transformations.rotation_matrix(-math.pi / 2, [1, 0, 0])),
        ("绕X180°", trimesh.transformations.rotation_matrix(math.pi, [1, 0, 0])),
        ("绕Y+90°", trimesh.transformations.rotation_matrix(math.pi / 2, [0, 1, 0])),
        ("绕Y-90°", trimesh.transformations.rotation_matrix(-math.pi / 2, [0, 1, 0])),
    ]
    span = float(max(mesh.extents)) or 1.0
    best = None
    scores = []
    for name, T in candidates:
        m = mesh.copy()
        m.apply_transform(T)
        m.apply_translation([0, 0, -float(m.bounds[0][2])])
        v, f = np.asarray(m.vertices), np.asarray(m.faces)
        over = _overhang_ratio(v, f, overhang_deg)
        h = float(m.extents[2])
        # 底部接触面积占比：接触越大越不容易被刮刀带翻
        n = analysis.face_normals(v, f)
        a = analysis.tri_areas(v, f)
        total_a = float(a.sum()) or 1.0
        contact = float(a[(n[:, 2] < -0.9) & (v[f].mean(axis=1)[:, 2] < 0.5)].sum()) / total_a
        score = over * 2.0 + h / span - contact * 0.6
        scores.append({"pose": name, "overhang_ratio": round(over, 4), "height": round(h, 2),
                       "contact_ratio": round(contact, 4), "score": round(score, 4)})
        if best is None or score < best[0]:
            best = (score, m, name, over, contact)
    assert best is not None
    scores.sort(key=lambda x: x["score"])
    return best[1], {"chosen": best[2], "overhang_ratio": round(best[3], 4),
                     "contact_ratio": round(best[4], 4),
                     "candidates": scores, "score": round(best[0], 4)}


# ---------------------------------------------------------------- 打孔
def _frustum(r_bottom: float, r_top: float, height: float, sections: int = 10) -> trimesh.Trimesh:
    """圆台：支撑柱下粗上细，底面接触大、顶部接触点小，既抗倒又不留大疤。"""
    ang = np.linspace(0, 2 * np.pi, sections, endpoint=False)
    c, s = np.cos(ang), np.sin(ang)
    bottom = np.c_[r_bottom * c, r_bottom * s, np.zeros(sections)]
    top = np.c_[r_top * c, r_top * s, np.full(sections, height)]
    verts = np.vstack([bottom, top, [[0, 0, 0], [0, 0, height]]])
    faces = []
    for i in range(sections):
        j = (i + 1) % sections
        faces.append([i, j, sections + j])
        faces.append([i, sections + j, sections + i])
        faces.append([2 * sections, j, i])
        faces.append([2 * sections + 1, sections + i, sections + j])
    m = trimesh.Trimesh(vertices=verts, faces=np.asarray(faces), process=True)
    if float(m.volume) < 0:
        m.invert()
    return m


def _bottom_sample_points(mesh, count: int, band: float = 4.0) -> np.ndarray:
    """在模型底面挑分散的孔位。

    只考虑法线朝下的面——排液孔必须从底面打，打在侧面会变成「装饰洞」，
    树脂照样排不出来。再在候选里做最远点采样保证孔之间拉开距离。
    """
    v, f = np.asarray(mesh.vertices), np.asarray(mesh.faces)
    zmin = float(v[:, 2].min())
    centers = v[f].mean(axis=1)
    normals = analysis.face_normals(v, f)
    down = normals[:, 2] < -0.3
    low = centers[(centers[:, 2] < zmin + band) & down]
    if len(low) < count:
        # 底面太小（锥形件、球形件），放宽到所有低处面
        low = centers[centers[:, 2] < zmin + band]
    if len(low) < count:
        low = centers[np.argsort(centers[:, 2])[: max(12, count * 4)]]
    if len(low) <= count:
        return low[:, :2]
    rng = np.random.default_rng(3)
    picked = [int(rng.integers(len(low)))]
    d = np.linalg.norm(low[:, :2] - low[picked[0], :2], axis=1)
    for _ in range(count - 1):
        nxt = int(np.argmax(d))
        picked.append(nxt)
        d = np.minimum(d, np.linalg.norm(low[:, :2] - low[nxt, :2], axis=1))
    return low[picked, :2]


def _filter_thin_spots(mesh, xy: np.ndarray, min_wall: float) -> tuple[np.ndarray, int]:
    """剔除壁厚不足的孔位。沿竖直方向测该位置上下两面之间的距离。"""
    v = np.asarray(mesh.vertices, dtype=np.float64)
    f = np.asarray(mesh.faces)
    if len(xy) == 0:
        return xy, 0
    zmin, zmax = float(v[:, 2].min()), float(v[:, 2].max())
    origins = np.c_[xy[:, 0], xy[:, 1], np.full(len(xy), zmax + 1.0)]
    down = np.tile([0.0, 0.0, -1.0], (len(xy), 1))
    try:
        hits = analysis._raycast_nearest(v, f, origins, down, 1e-4, max_dist=(zmax - zmin) + 2.0)
    except Exception:
        return xy, 0
    # 只量「外表面到第一个内表面」这一段。
    # 不能继续往下量第二段：镂空件的第二段是空腔，一量就全是 0，孔全被拒。
    # 这一段对实心件就是总厚度，对镂空件就是壳厚，两种情况都正好是开孔需要的深度。
    keep = []
    skipped = 0
    for i in range(len(xy)):
        if not np.isfinite(hits[i]):
            skipped += 1
            continue
        ztop = zmax + 1.0 - float(hits[i])
        o2 = np.array([[xy[i, 0], xy[i, 1], ztop - 0.05]])
        d2 = analysis._raycast_nearest(v, f, o2, np.array([[0.0, 0, -1.0]]), 1e-4,
                                       max_dist=(ztop - zmin) + 1.0)
        if np.isfinite(d2[0]) and float(d2[0]) >= min_wall:
            keep.append(i)
        else:
            skipped += 1
    return xy[keep] if keep else xy[:0], skipped


def drill_holes(mesh, count: int, diameter: float, wall: float, points: list | None = None,
                min_wall: float | None = None) -> tuple[trimesh.Trimesh, dict]:
    if count <= 0 and not points:
        return mesh, {"holes": 0}
    skipped_thin = 0
    thin_fallback = False
    if points:
        xy = np.asarray([[p[0], p[1]] for p in points], dtype=np.float64)
    else:
        # 整块板比孔径还薄时，任何「过滤后回退」都只会打出豁口。
        # 先做一次整体厚度闸门：只在模型最薄方向确实小于孔径 1/4 时拒绝，
        # 镂空薄壳的包围盒仍然很大，不会被误伤。
        try:
            thin_dim = float(np.min(mesh.extents))
        except Exception:
            thin_dim = 0.0
        hard_min = max(0.6, float(diameter) * 0.25)
        if thin_dim and thin_dim < hard_min:
            raise ValueError(
                f"模型最薄方向只有 {thin_dim:.2f}mm，小于孔径 {diameter:g}mm 的安全下限；"
                "请加厚模型或减小孔径后再打排液孔"
            )
        xy = _bottom_sample_points(mesh, count * 3)
        # 孔必须打在够厚的地方：薄壁上一打就穿到侧面，孔变成豁口。
        # 注意这里不能用「名义壁厚」卡：体素法的实际壳厚受 pitch 限制，
        # 0.9mm 设定可能只做出 0.33mm，按名义值过滤会把所有位置都拒掉。
        # 所以阈值取「实测壳厚的 30%，且不低于 0.25mm」——只拦真正的纸片位。
        mw = min_wall if min_wall is not None else max(0.25, wall * 0.3)
        if mw > 0 and len(xy):
            filtered, skipped_thin = _filter_thin_spots(mesh, xy, min_wall=mw)
            if len(filtered) == 0:
                # 全被拒时不能直接失败：宁可在薄处开孔并说明，也不让整条处理链挂掉
                thin_fallback = True
            else:
                xy = filtered
        if len(xy) > count:
            sel = np.linspace(0, len(xy) - 1, count).astype(int)
            xy = xy[sel]
        if len(xy) == 0:
            raise ValueError("底面找不到可开孔的位置，请改用点选排液孔位置")
    v = np.asarray(mesh.vertices)
    zmin, zmax = float(v[:, 2].min()), float(v[:, 2].max())
    depth = min(max(wall * 3.0, 4.0), (zmax - zmin) * 0.6)
    cutters = []
    for x, y in xy:
        cyl = trimesh.creation.cylinder(radius=diameter / 2.0, height=depth + 2.0, sections=28)
        cyl.apply_translation([float(x), float(y), zmin - 1.0 + (depth + 2.0) / 2.0])
        cutters.append(cyl)
    try:
        out = trimesh.boolean.difference([mesh] + cutters, engine="manifold")
    except Exception as e:
        raise ValueError(f"打孔布尔运算失败：{e}。请确认模型在镂空后仍是闭合流形。") from e
    return out, {
        "holes": len(xy),
        "diameter": diameter,
        "depth": round(depth, 2),
        "positions": [[round(float(x), 2), round(float(y), 2)] for x, y in xy],
        "skipped_thin": skipped_thin,
        "min_wall_checked": None if points else round(float(min_wall if min_wall is not None
                                                          else max(0.25, wall * 0.3)), 3),
        "thin_fallback": thin_fallback,
    }


# ---------------------------------------------------------------- 支撑
def build_supports(
    mesh,
    overhang_deg: float = 45.0,
    diameter: float = 1.0,
    spacing: float = 3.0,
    max_count: int = 400,
    points: list | None = None,
    platform_z: float = 0.0,
    avoid_model: bool = True,
    min_support_h: float = 3.0,
) -> tuple[trimesh.Trimesh | None, dict]:
    v, f = np.asarray(mesh.vertices), np.asarray(mesh.faces)
    zmin = float(v[:, 2].min())
    # 支撑落脚基准是平台，不是模型最低点——悬空的件要撑到台面，不是撑到自己脚下
    base_z = min(float(platform_z), zmin)
    if points:
        anchors = np.asarray([[p[0], p[1], p[2]] for p in points], dtype=np.float64)
        anchor_normals = np.tile([0.0, 0.0, -1.0], (len(anchors), 1))
    else:
        n = analysis.face_normals(v, f)
        thr = -np.cos(np.radians(overhang_deg))
        idx = np.where(n[:, 2] < thr)[0]
        if len(idx) == 0:
            return None, {"count": 0, "note": "没有超过阈值角的悬垂面，无需支撑"}
        # 按面积均匀撒点，而不是「每个面取一个质心再去重」。
        # 后者在低面数模型上会严重偏稀：一个 40×40 的底面如果只有 2 个三角形，
        # 就只撒出 2 个支撑点，整个悬空区只撑两根柱。
        tri_idx = idx
        areas = analysis.tri_areas(v, f)[tri_idx]
        total_area = float(areas.sum())
        # 目标点数按「面积 / 间距²」算；随机撒点后还要按最小间距去重，
        # 去重会吃掉约一半样本。旧实现直接拿目标数撒点，4mm 间距最终只剩
        # 2.8mm 的稀疏支撑网。先过采样再裁回目标数，密度才和用户输入一致。
        target = int(total_area / max(0.25, float(spacing) ** 2))
        target = max(1, min(target, max_count))
        want = min(max_count * 4, max(target + 8, int(target * 2.6)))
        rng = np.random.default_rng(5)
        probs = areas / areas.sum() if total_area > 0 else None
        pick = rng.choice(len(tri_idx), size=want, p=probs, replace=True)
        picked_faces = tri_idx[pick]
        tris = v[f[picked_faces]]
        # 三角形内按重心坐标均匀取点
        r1 = rng.random((want, 1))
        r2 = rng.random((want, 1))
        sq = np.sqrt(r1)
        anchors = (1 - sq) * tris[:, 0] + sq * (1 - r2) * tris[:, 1] + sq * r2 * tris[:, 2]
        anchor_normals = analysis.face_normals(v, f)[picked_faces]

        # 面积加权采样会重复抽到同一个大面，导致同一位置叠好几根柱。
        # 用 KD 树按真实距离去重：分格法在格子边界会漏（跨格只差 0.1mm 也算不同格）。
        if len(anchors) > 1:
            from scipy.spatial import cKDTree

            # 去重半径必须跟 spacing 挂钩。旧实现只看直径（diameter×1.5），
            # 用户设 spacing=3mm 却得到 1.5mm 的柱间距——支撑比设定密一倍，
            # 拆支撑的工作量和表面疤痕都翻倍。
            min_dist = max(float(diameter) * 1.5, float(spacing) * 0.8)
            tree2d = cKDTree(anchors[:, :2])
            keep_mask = np.ones(len(anchors), dtype=bool)
            for i, j in sorted(tree2d.query_pairs(min_dist)):
                if keep_mask[i] and keep_mask[j]:
                    keep_mask[j] = False
            if keep_mask.any():
                anchors = anchors[keep_mask]
                anchor_normals = anchor_normals[keep_mask]
            # 过采样已经完成使命，裁回用户设定的密度；不裁会让 max_count=400
            # 的模型实际长出 700 根支撑，拆支撑时间翻倍。
            if len(anchors) > target:
                sel = np.sort(rng.choice(len(anchors), size=target, replace=False))
                anchors = anchors[sel]
                anchor_normals = anchor_normals[sel]
        # 只对自动识别的点过滤「贴平台」；手动点选是用户明确要的，不能悄悄丢掉
        anchors = anchors[anchors[:, 2] > base_z + 0.5]

    if len(anchors) == 0:
        return None, {"count": 0, "note": "悬垂点都贴着平台，不需要支撑"}

    origins = anchors - np.array([0, 0, 1e-3])
    dirs = np.tile([0.0, 0.0, -1.0], (len(origins), 1))
    land_z = np.full(len(origins), base_z)
    hit_model = np.zeros(len(origins), dtype=bool)
    try:
        locs, ray_idx, _ = mesh.ray.intersects_location(origins, dirs, multiple_hits=False)
        if len(ray_idx):
            for k in range(len(ray_idx)):
                ri = int(ray_idx[k])
                z = float(locs[k][2])
                if z > land_z[ri]:
                    land_z[ri] = z
                    hit_model[ri] = True
    except Exception:
        pass

    # 判断「悬垂点正下方是不是自己的实体」。是的话这根柱只有几毫米，
    # 属于台阶而不是悬垂，撑上去反而在表面留疤——按 min_support_h 统一判定。
    # 旧实现这里写死 3.0，注释却写 10mm，两者对不上；现在两处都用同一个参数，
    # 并写进报告，用户看到「跳过 N 根」时能知道是按什么尺度判的。
    solid_below = np.zeros(len(anchors), dtype=bool)
    if avoid_model and len(anchors):
        try:
            # 沿面法线朝外挪 0.1mm 再往下打。
            # 直接向下偏移会钻到实体内部，射线先打穿自己所在的面，
            # 于是「悬空在空气中的球面」也被判成「下方有实体」。
            probe = anchors + anchor_normals * 0.1
            down = np.tile([0.0, 0.0, -1.0], (len(probe), 1))
            dist = analysis._raycast_nearest(
                np.asarray(mesh.vertices, dtype=np.float64),
                np.asarray(mesh.faces),
                probe, down, 1e-4, max_dist=float(min_support_h),
            )
            solid_below = np.isfinite(dist)
        except Exception:
            solid_below = hit_model & (np.asarray(anchors[:, 2] - land_z) < float(min_support_h))

    parts = []
    heights = []
    segments = []
    skipped_solid = 0
    for i, ((x, y, z), z0) in enumerate(zip(anchors, land_z)):
        h = float(z) - float(z0)
        if h < 0.6:
            continue
        if solid_below[i]:
            skipped_solid += 1
            continue
        # 顶部按设定直径，底部放大 1.8 倍：细高柱底部面积小，取件时一碰就倒
        sup_piece = _frustum(diameter / 2.0 * 1.8, diameter / 2.0, h)
        sup_piece.apply_translation([float(x), float(y), float(z0)])
        parts.append(sup_piece)
        heights.append(h)
        segments.append([round(float(x), 2), round(float(y), 2),
                         round(float(z0), 2), round(float(z), 2)])
    if not parts:
        return None, {"count": 0, "note": "悬垂点离落点太近，未生成支撑"}
    sup = trimesh.util.concatenate(parts)
    return sup, {
        "count": len(parts),
        "diameter": diameter,
        "spacing": float(spacing),
        "min_support_h_mm": float(min_support_h),
        "platform_z": round(base_z, 3),
        "min_height": round(float(min(heights)), 2),
        "max_height": round(float(max(heights)), 2),
        "segments": segments,
        "skipped_solid": skipped_solid,
    }


# ---------------------------------------------------------------- 流水线
def apply_pipeline(mesh, p: dict, progress=None) -> tuple[trimesh.Trimesh, trimesh.Trimesh | None, dict]:
    """执行完整处理链，返回 (主模型, 支撑, 报告)。

    抽成纯函数是为了让批处理复用同一套逻辑——
    单件处理和批量处理走不同代码路径，是结果不一致的常见来源。
    """
    report: dict = {"decimate": None, "hollow": None, "drill": None,
                    "orient": None, "supports": None, "scale": None}
    total_volume = float(mesh.volume) if mesh.is_watertight else 0.0
    # 镂空时壳厚可能被自动下调；排液孔过滤必须用「实际壳厚」，
    # 否则用户设 2mm、实际只有 0.5mm，打孔仍按 2mm 判断，孔会直接打穿侧壁。
    effective_wall = float(p.get("wall_thickness") or 2.0)

    def step(frac, msg):
        if progress:
            progress(frac, msg)

    step(0.1, "减面")
    if float(p["decimate_ratio"]) < 0.999:
        was_watertight = bool(mesh.is_watertight)
        vol_before = abs(float(mesh.volume)) if was_watertight else 0.0
        dec = decimate_mesh(mesh, float(p["decimate_ratio"]))
        dinfo = {"ratio": p["decimate_ratio"], "faces": int(len(dec.faces))}
        if vol_before > 0 and float(dec.volume) > 0:
            dinfo["volume_change_pct"] = round((abs(float(dec.volume)) - vol_before) / vol_before * 100, 2)
        # 减面是边折叠，遇到非流形/极不均匀的网格会在实体上留下裂缝。
        # 以前减完直接用：镂空那步报「模型不水密」，用户根本想不到是减面干的。
        # 这里当场验收，破坏水密就退回原网格并说明。
        if was_watertight and not dec.is_watertight:
            dinfo["reverted"] = True
            dinfo["faces_kept"] = int(len(mesh.faces))
            dinfo["reason"] = "减面破坏了水密性（边折叠在细碎处留下裂缝），已保留原网格继续处理"
        else:
            mesh = dec
        report["decimate"] = dinfo

    step(0.25, "镂空")
    if p["hollow"]:
        if not mesh.is_watertight:
            raise ValueError("模型不水密，无法镂空。请先补洞并统一法线。")
        wall = float(p["wall_thickness"])
        # 壳厚超过最薄处的一半会直接把模型切穿，自动收到安全值而不是硬失败
        probe = analysis.sample_wall_thickness(mesh, 1200, multi_dir=1)
        min_wall = probe.get("min")
        if min_wall and wall > float(min_wall) * 0.45:
            safe = max(0.4, round(float(min_wall) * 0.45, 2))
            report["hollow_warning"] = (
                f"壳厚 {wall}mm 超过模型最薄处 {min_wall}mm 的 45%，已自动降到 {safe}mm，否则会把壁切穿"
            )
            wall = safe
        effective_wall = wall
        # 体积预估与镂空共用同一次体素化/EDT：旧写法先 cavity_volume 再
        # hollow_shell，同一张网格跑了两遍 O(N³)，20k 面实测多花约 0.9s、+420MB。
        hollow_stats: dict = {}
        mesh, _ = voxelops.hollow_shell(mesh, wall, stats=hollow_stats)
        saved = float(hollow_stats.get("cavity_mm3", 0.0))
        report["hollow"] = {
            "wall_mm": wall,
            "cavity_mm3": round(saved, 1),
            "solid_volume_mm3": round(total_volume, 1),
            "shell_volume_mm3": round(float(mesh.volume), 1),
            "resin_saved_pct": round(saved / max(1e-6, total_volume) * 100, 1),
        }

    step(0.42, "摆正")
    if p["orient"]:
        mesh, oinfo = auto_orient(mesh, float(p["overhang_deg"]))
        report["orient"] = oinfo

    step(0.55, "打排液孔")
    if p["hollow"] and int(p["drain_holes"]) > 0:
        mesh, dinfo = drill_holes(
            mesh, int(p["drain_holes"]), float(p["hole_diameter"]),
            effective_wall, p.get("drain_hole_points") or None,
        )
        report["drill"] = dinfo
        if dinfo.get("thin_fallback"):
            report.setdefault("warnings", []).append(
                f"底面 {dinfo.get('skipped_thin', 0)} 个候选位置壳厚不足，全部被拒；"
                "已在最薄处开孔，请打印后确认孔是否打穿而不是打成豁口。"
            )
    elif p["hollow"]:
        report.setdefault("warnings", []).append(
            "已镂空但没有排液孔：树脂/粉末会封在空腔里。建议至少开 2 个 3mm 排液孔，"
            "并让孔位于成型后的最低处。"
        )

    step(0.7, "生成支撑")
    sup = None
    if p["supports"] in ("auto", "manual"):
        pts = (p.get("support_points") or None) if p["supports"] == "manual" else None
        sup, sinfo = build_supports(
            mesh, float(p["overhang_deg"]), float(p["support_diameter"]),
            float(p["support_spacing"]), 400, pts,
            avoid_model=(p["supports"] != "manual"),
        )
        report["supports"] = sinfo

    step(0.85, "缩放")
    factor = float(p["scale"])
    if float(p.get("target_height") or 0) > 0:
        cur = float(mesh.extents[2])
        if cur > 1e-9:
            factor = float(p["target_height"]) / cur
    elif float(p.get("target_max_dim") or 0) > 0:
        # 按最长边缩放，适合「不管朝向、只要总高度不超 200」这类需求
        cur = float(max(mesh.extents))
        if cur > 1e-9:
            factor = float(p["target_max_dim"]) / cur
    if abs(factor - 1.0) > 1e-9:
        mesh.apply_scale(factor)
        if sup is not None:
            sup.apply_scale(factor)
        report["scale"] = {"factor": round(factor, 4),
                           "extents": [round(float(x), 2) for x in mesh.extents]}
        if report.get("supports") and report["supports"].get("count"):
            # 支撑直径是工艺参数（mm），但整体缩放会把它一起放大。
            # 报告里只写用户填的 1.0mm 会误导：实际打出来是 factor×1.0。
            eff = round(float(p["support_diameter"]) * factor, 3)
            report["supports"]["diameter_effective"] = eff
            report["supports"]["note"] = (
                f"支撑直径随整体缩放同比放大：设定 {p['support_diameter']}mm → 实际 {eff}mm"
            )
    return mesh, sup, report


def preview_pipeline(mesh, params: dict, build: list[float] | None = None) -> dict:
    """轻量预检：只做摆正与缩放，估算尺寸/体积/用量，不跑减面、镂空和支撑。

    完整处理会体素镂空、生成支撑，动辄数秒到数十秒；用户在改参数时需要的
    是「会不会超出成型空间、大概用多少料」，不是立刻产出 STL。这里用同一套
    摆正和缩放逻辑，保证预检尺寸与正式处理一致，其余字段明确标注为估算。
    """
    p = dict(DEFAULTS)
    # 注意：入参曾用 p 命名又立刻被 DEFAULTS 覆盖，导致预检永远按默认参数算——
    # 用户把目标高度改成 80mm，预检仍显示 20mm、还提示「可打印」。
    p.update({k: v for k, v in (params or {}).items() if k in DEFAULTS})

    m = mesh.copy()
    warnings: list[str] = []
    errors: list[str] = []
    orientation = None
    if p["orient"]:
        m, orientation = auto_orient(m, float(p["overhang_deg"]))

    pre_extents = [float(x) for x in m.extents]
    factor = float(p["scale"])
    target = "scale"
    if float(p.get("target_height") or 0) > 0:
        target = "height"
        cur = float(m.extents[2])
        if cur > 1e-9:
            factor = float(p["target_height"]) / cur
    elif float(p.get("target_max_dim") or 0) > 0:
        target = "max_dim"
        cur = float(max(m.extents))
        if cur > 1e-9:
            factor = float(p["target_max_dim"]) / cur
    if abs(factor - 1.0) > 1e-12:
        m.apply_scale(factor)

    stats = meshio.mesh_stats(m)
    volume = max(0.0, float(stats.get("volume_mm3") or 0.0))
    area = max(0.0, float(stats.get("area_mm2") or 0.0))
    extents = [float(x) for x in (stats.get("extents") or [0, 0, 0])]

    shell_volume = None
    saved_volume = None
    if p["hollow"]:
        if not stats.get("is_watertight"):
            errors.append("模型不水密，无法镂空；请先补洞并统一法线")
        else:
            wall = float(p["wall_thickness"])
            # 薄壁模型按表面积×壁厚会高估，封顶到实体体积；这是估算，不是切片结果。
            shell_volume = min(volume, area * wall)
            saved_volume = max(0.0, volume - shell_volume)
            if wall > 0 and area > 0 and volume / area * 2 < wall:
                warnings.append(
                    f"壳厚 {wall:g}mm 接近或超过平均壁厚，正式处理会自动降壳厚，预检用量仅供参考"
                )
            if int(p["drain_holes"]) <= 0:
                warnings.append("已镂空但没有排液孔：树脂/粉末会封在空腔里，建议至少开 2 个孔")
    else:
        warnings.append("未镂空：按实体估算用量；开启镂空可省料但需要排液孔")

    if p["supports"] in ("auto", "manual"):
        warnings.append("支撑体积未计入预检用量；正式处理后会额外增加支撑材料")
    if float(p["decimate_ratio"]) < 0.999:
        warnings.append(f"减面 ×{float(p['decimate_ratio']):g} 可能轻微改变尺寸与体积，预检按原网格计算")

    fit = _fit_check(extents, build)
    if fit and not fit["ok"]:
        warnings.append(
            "当前尺寸超出成型空间："
            + "、".join(f"{o['axis']} 需 {o['need']:.1f}mm > {o['have']:.1f}mm"
                        for o in fit["oversize"])
        )
    if fit and fit.get("scale_to_fit", 1.0) < 1.0:
        fit["scale_to_fit"] = round(fit["scale_to_fit"], 4)
        fit["scaled_extents"] = [round(x * fit["scale_to_fit"], 2) for x in extents]

    return {
        "before": {
            "extents": [round(x, 3) for x in mesh.extents],
            "max_dim": round(float(max(mesh.extents)), 3) if len(mesh.extents) else 0,
            "volume_mm3": round(float(mesh.volume), 3) if mesh.is_watertight else None,
        },
        "orientation": orientation,
        "pre_scale_extents": [round(x, 2) for x in pre_extents],
        "scale": {"factor": round(factor, 4), "target": target,
                  "extents": [round(x, 2) for x in extents]},
        "extents": [round(x, 2) for x in extents],
        "volume_mm3": round(volume, 1),
        "volume_cm3": round(volume / 1000.0, 2),
        "area_mm2": round(area, 1),
        "estimated_shell_volume_mm3": round(shell_volume, 1) if shell_volume is not None else None,
        "estimated_saved_volume_mm3": round(saved_volume, 1) if saved_volume is not None else None,
        "estimated_saved_pct": round(saved_volume / max(1e-9, volume) * 100, 1)
                                if saved_volume is not None else None,
        "fit": fit,
        "warnings": warnings,
        "errors": errors,
        "approximate": True,
    }


def _fit_check(extents: list[float], build: list[float] | None) -> dict | None:
    """按可旋转的姿态检查成型空间；0.5mm 是取件/夹具余量。"""
    if not build or len(build) != 3:
        return None
    try:
        build = [float(x) for x in build]
    except (TypeError, ValueError):
        return None
    if any(x <= 0 for x in build):
        return None
    margin = 0.5
    ext = [float(x) for x in extents]
    dims = sorted(ext, reverse=True)
    beds = sorted(build, reverse=True)
    over = []
    for i, (need, have) in enumerate(zip(dims, beds)):
        available = have - margin * 2
        if need > available + 1e-6:
            label = ("最长边", "次长边", "最短边")[i] if i < 3 else f"第 {i + 1} 边"
            over.append({"axis": label, "need": round(need, 2),
                         "have": round(have, 2), "over": round(need - available, 2)})
    scale_to_fit = 1.0
    if ext and any(x > 0 for x in ext):
        ratios = [(b - margin * 2) / x for b, x in zip(beds, dims) if x > 1e-9 and b > margin * 2]
        if ratios:
            scale_to_fit = min(1.0, min(ratios))
    return {
        "ok": not over,
        "build": [round(x, 2) for x in build],
        "extents": [round(x, 2) for x in ext],
        "margin_mm": margin,
        "oversize": over,
        "scale_to_fit": scale_to_fit,
    }


# ---------------------------------------------------------------- 任务
def run_job(ctx, params):
    pid = ctx.job.project_id
    proj = store.get(pid)
    if not proj:
        raise ValueError("项目不存在")
    p = dict(DEFAULTS)
    p.update({k: v for k, v in (params or {}).items() if k in DEFAULTS})

    src = Path(proj.get("current") or proj["source"])
    ctx.tick(0.03, "加载模型", "load")
    mesh = meshio.load_mesh(src, mutable=True)
    before = meshio.mesh_stats(mesh)

    def progress(frac: float, msg: str) -> None:
        ctx.tick(0.05 + 0.85 * frac, msg, "pipeline")

    mesh, sup_mesh, report = apply_pipeline(mesh, p, progress)

    ctx.tick(0.92, "导出", "save")
    final = mesh if sup_mesh is None else trimesh.util.concatenate([mesh, sup_mesh])
    final.apply_translation([0, 0, -float(final.bounds[0][2])])
    out = config.OUTPUTS / f"{pid}_processed_{int(time.time())}.stl"
    final.export(str(out))
    # 统计口径必须和导出的文件一致：以前这里统计的是不含支撑的 mesh，
    # 用户拿到的 STL 面数比界面上显示的多一大截（支撑也是实体）。
    after = meshio.mesh_stats(final)
    after["mesh_only_faces"] = int(len(mesh.faces))
    after["with_supports"] = sup_mesh is not None

    store.add_history(pid, "process", p, out,
                      note=f"减面×{p['decimate_ratio']} 镂空={p['hollow']} 支撑={p['supports']}")
    ctx.tick(1.0, "处理完成", "done")
    return {"before": before, "after": after, "report": report, "output": str(out)}
