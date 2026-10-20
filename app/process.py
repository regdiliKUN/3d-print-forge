"""几何处理：减面、镂空打孔、支撑生成、自动摆放、缩放。

处理顺序经过刻意安排：
减面 → 镂空 → 摆正 → 打排液孔（在最终姿态的最低点）→ 加支撑 → 缩放。
先摆正再打孔，否则孔会跟着旋转跑到侧面去。
"""
from __future__ import annotations

import shutil
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


def drill_holes(mesh, count: int, diameter: float, wall: float, points: list | None = None) -> tuple[trimesh.Trimesh, dict]:
    if count <= 0 and not points:
        return mesh, {"holes": 0}
    if points:
        xy = np.asarray([[p[0], p[1]] for p in points], dtype=np.float64)
    else:
        xy = _bottom_sample_points(mesh, count)
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
        want = int(total_area / max(0.25, float(spacing) ** 2))
        want = max(1, min(want, max_count))
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

    # 判断「悬垂点下方 10mm 内是不是实体」。
    # 三个坑：trimesh 的 intersects_location 起点贴近表面时返回 0 命中；
    # mesh.contains 在表面附近也不稳。所以直接用自己实现的 Möller–Trumbore，
    # 它只看几何交点、不区分内外，正好适合这个判断。
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
                probe, down, 1e-4, max_dist=3.0,
            )
            solid_below = np.isfinite(dist)
        except Exception:
            solid_below = hit_model & (np.asarray(anchors[:, 2] - land_z) < 10.0)

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

    def step(frac, msg):
        if progress:
            progress(frac, msg)

    step(0.1, "减面")
    if float(p["decimate_ratio"]) < 0.999:
        mesh = decimate_mesh(mesh, float(p["decimate_ratio"]))
        report["decimate"] = {"ratio": p["decimate_ratio"], "faces": len(mesh.faces)}

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
        saved = voxelops.cavity_volume(mesh, wall)
        mesh, _ = voxelops.hollow_shell(mesh, wall)
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
            float(p["wall_thickness"]), p.get("drain_hole_points") or None,
        )
        report["drill"] = dinfo

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
    return mesh, sup, report


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
    mesh = meshio.load_mesh(src)
    before = meshio.mesh_stats(mesh)

    def progress(frac: float, msg: str) -> None:
        ctx.tick(0.05 + 0.85 * frac, msg, "pipeline")

    mesh, sup_mesh, report = apply_pipeline(mesh, p, progress)

    ctx.tick(0.92, "导出", "save")
    final = mesh if sup_mesh is None else trimesh.util.concatenate([mesh, sup_mesh])
    final.apply_translation([0, 0, -float(final.bounds[0][2])])
    out = config.OUTPUTS / f"{pid}_processed_{int(time.time())}.stl"
    final.export(str(out))
    after = meshio.mesh_stats(mesh)
    after["with_supports"] = sup_mesh is not None

    store.add_history(pid, "process", p, out,
                      note=f"减面×{p['decimate_ratio']} 镂空={p['hollow']} 支撑={p['supports']}")
    ctx.tick(1.0, "处理完成", "done")
    return {"before": before, "after": after, "report": report, "output": str(out)}
