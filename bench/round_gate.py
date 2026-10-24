"""每一轮修补迭代的快速几何闸门。

不依赖 pytest，直接对确定性缺陷夹具执行修复动作，并断言可证伪的几何量：
开放边、非流形边、重复面、有向体积、水密性、自交对数。

用法：
    .venv\\Scripts\\python.exe bench\\round_gate.py
    .venv\\Scripts\\python.exe bench\\round_gate.py --json

退出码 0 表示全部通过；任何一项失败返回 1，并打印差异。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import analysis, compare, gpu, pack, process, repair, split, voxelops  # noqa: E402

MODELS = Path(__file__).resolve().parent / "models"


# ---------------------------------------------------------------- 夹具
def open_box(size: float = 20.0) -> trimesh.Trimesh:
    m = trimesh.creation.box(extents=[size, size, size])
    keep = np.ones(len(m.faces), bool)
    keep[0] = False
    m.update_faces(keep)
    m.remove_unreferenced_vertices()
    return m


def broken_ball() -> trimesh.Trimesh:
    m = trimesh.creation.icosphere(subdivisions=3, radius=25.0)
    keep = np.ones(len(m.faces), bool)
    keep[np.arange(0, len(m.faces), 40)] = False
    m.update_faces(keep)
    m.remove_unreferenced_vertices()
    m.faces = m.faces[:, ::-1]
    scrap = trimesh.creation.box(extents=[3, 3, 3])
    scrap.apply_translation([60, 0, 0])
    return trimesh.util.concatenate([m, scrap])


def sliver_with_body() -> trimesh.Trimesh:
    body = trimesh.creation.box(extents=[10, 10, 10])
    v = np.array([[20, 0, 0], [22, 0, 0], [21, 2, 0]], dtype=float)
    f = np.array([[0, 1, 2]])
    return trimesh.util.concatenate(
        [body, trimesh.Trimesh(vertices=v, faces=f, process=False)])


def duplicate_faces() -> trimesh.Trimesh:
    m = trimesh.creation.box(extents=[10, 10, 10])
    return trimesh.Trimesh(vertices=m.vertices.copy(),
                           faces=np.vstack([m.faces, m.faces]), process=False)


def multi_one_inverted() -> trimesh.Trimesh:
    a = trimesh.creation.box(extents=[10, 10, 10])
    a.apply_translation([-20, 0, 0])
    b = trimesh.creation.box(extents=[10, 10, 10])
    b.apply_translation([20, 0, 0])
    b.faces = b.faces[:, ::-1]
    return trimesh.util.concatenate([a, b])


def grid_sheet(cells: int = 5, step: float = 2.0) -> trimesh.Trimesh:
    verts, faces = [], []

    def vid(i, j):
        return j * (cells + 1) + i

    for j in range(cells + 1):
        for i in range(cells + 1):
            verts.append([i * step, j * step, 0.0])
    for j in range(cells):
        for i in range(cells):
            a, b, c, d = vid(i, j), vid(i + 1, j), vid(i + 1, j + 1), vid(i, j + 1)
            faces += [[a, b, c], [a, c, d]]
    return trimesh.Trimesh(vertices=np.asarray(verts, float),
                           faces=np.asarray(faces, int), process=False)


def two_spheres() -> trimesh.Trimesh:
    a = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
    b = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
    b.apply_translation([12.0, 0.0, 0.0])
    return trimesh.util.concatenate([a, b])


def sheet_with_body() -> trimesh.Trimesh:
    """立方体 + 4 个三角形拼成的零厚度薄片（旧实现识别不到，会补成双层皮）。"""
    body = trimesh.creation.box(extents=[10, 10, 10])
    v = np.array([[20, 0, 0], [30, 0, 0], [30, 10, 0], [20, 10, 0],
                  [20, 20, 0], [30, 20, 0]], dtype=float)
    f = np.array([[0, 1, 2], [0, 2, 3], [0, 3, 4], [0, 4, 5]])
    return trimesh.util.concatenate(
        [body, trimesh.Trimesh(vertices=v, faces=f, process=False)])


def hole_with_wall() -> trimesh.Trimesh:
    """顶面破洞，洞口正后方立着一片穿出顶面的墙：补丁会切进墙里。"""
    m = trimesh.creation.box(extents=[20, 20, 20])
    top = np.flatnonzero(m.face_normals[:, 2] > 0.9)
    keep = np.ones(len(m.faces), bool)
    keep[top[0]] = False
    m.update_faces(keep)
    m.remove_unreferenced_vertices()
    plate = trimesh.creation.box(extents=[2, 26, 10])
    plate.apply_translation([0, 0, 10])
    return trimesh.util.concatenate([m, plate])


# ---------------------------------------------------------------- 工具
def health(mesh) -> dict:
    v = np.asarray(mesh.vertices)
    f = np.asarray(mesh.faces)
    h = analysis.edge_histogram(f, len(v))
    return {
        "boundary": h["boundary"],
        "nonmanifold": h["nonmanifold"],
        "duplicates": analysis.duplicate_face_count(f),
        "volume": analysis.signed_volume(np.asarray(v, dtype=np.float64), f),
        "faces": int(len(f)),
        "watertight": bool(mesh.is_watertight),
    }


class Gate:
    def __init__(self):
        self.checks: list[dict] = []

    def check(self, name: str, cond: bool, detail: dict):
        self.checks.append({"name": name, "ok": bool(cond), "detail": detail})
        return bool(cond)


def run() -> Gate:
    g = Gate()
    t0 = time.time()

    # 1. 平面洞：体积必须精确复原
    m = open_box()
    out, info = repair.act_fill_holes(m, {})
    h = health(out)
    g.check("fill_planar_hole", info.get("filled") and h["boundary"] == 0
            and h["nonmanifold"] == 0 and h["duplicates"] == 0
            and abs(h["volume"] - 8000.0) < 1e-6,
            {"info_filled": info.get("filled"), **h})

    # 2. 破碎球 + 碎屑：96 开放边全部补上，且不引入非流形/重复面
    out, info = repair.act_fill_holes(broken_ball(), {})
    h = health(out)
    g.check("fill_broken_ball", info.get("filled") and h["boundary"] == 0
            and h["nonmanifold"] == 0 and h["duplicates"] == 0 and h["watertight"],
            {"boundary_before": info.get("boundary_before"), **h})

    # 3. 孤立薄片：删掉薄片而不是补成双面
    out, info = repair.act_fill_holes(sliver_with_body(), {})
    h = health(out)
    g.check("remove_sliver", h["faces"] == 12 and abs(h["volume"] - 1000.0) < 1e-6
            and h["watertight"],
            {"removed_slivers": info.get("removed_slivers"), **h})

    # 4. 重复面：去重后恢复成 12 面立方体
    out, info = repair.act_fill_holes(duplicate_faces(), {})
    h = health(out)
    g.check("dedupe_faces", h["faces"] == 12 and h["duplicates"] == 0
            and abs(h["volume"] - 1000.0) < 1e-6 and h["watertight"],
            {"deduped": info.get("deduped_faces"), **h})

    # 5. 一正一反两个实体：逐分量翻正后体积 0 -> 2000
    out, info = repair.act_fix_normals(multi_one_inverted(), {})
    h = health(out)
    g.check("fix_component_normals", abs(h["volume"] - 2000.0) < 1e-6
            and h["watertight"] and info["flipped_components"] == 1,
            {"flipped_components": info["flipped_components"], **h})

    # 6. 两球穿插：必须检出；干净球体不得误报
    hit = analysis.find_self_intersections(
        np.asarray(two_spheres().vertices), np.asarray(two_spheres().faces))
    clean = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
    clean_hit = analysis.find_self_intersections(
        np.asarray(clean.vertices), np.asarray(clean.faces))
    g.check("self_intersection", hit["count"] >= 50 and clean_hit["count"] == 0,
            {"two_spheres": hit["count"], "clean_sphere": clean_hit["count"]})

    # 7. 单层网格片：补洞必须回退，不能补成零厚度壳
    sheet = grid_sheet()
    out, info = repair.act_fill_holes(sheet, {})
    g.check("reject_zero_thickness_shell",
            info.get("reverted") is True and len(out.faces) == len(sheet.faces),
            {"reverted": info.get("reverted"),
             "reason": info.get("revert_reason", "")[:80]})

    # 8. 体素 pitch：过小/过大都必须收敛，微小模型仍能栅格化
    p_small = voxelops.sanitize_pitch(open_box(20.0), 1e-9)
    p_big = voxelops.sanitize_pitch(open_box(20.0), 1e9)
    tiny_grid, _vox, tiny_pitch = voxelops.to_grid(open_box(0.5))
    g.check("voxel_pitch_bounds",
            p_small >= 20.0 / voxelops.MAX_AXIS_VOXELS - 1e-12
            and p_big <= 20.0 / 8.0 + 1e-12
            and tiny_grid.any() and not tiny_grid.all()
            and tiny_pitch <= 0.5 / 20.0 + 1e-12,
            {"pitch_tiny_request": p_small, "pitch_huge_request": p_big,
             "tiny_effective": tiny_pitch, "tiny_shape": list(tiny_grid.shape)})

    # 9. 凹多边形耳切兜底：面积必须等于 L 形面积 3
    coords = np.array([[0, 0], [2, 0], [2, 1], [1, 1], [1, 2], [0, 2]], float)
    tris = repair._ear_clip_2d(coords)
    t = coords[tris]
    area = 0.5 * float(np.abs(
        (t[:, 1, 0] - t[:, 0, 0]) * (t[:, 2, 1] - t[:, 0, 1])
        - (t[:, 1, 1] - t[:, 0, 1]) * (t[:, 2, 0] - t[:, 0, 0])).sum())
    g.check("ear_clip_concave", len(tris) == 4 and abs(area - 3.0) < 1e-9,
            {"triangles": int(len(tris)), "area": area})

    # 10. 自交候选完整性：细长大三角形 + 远处小三角形，真实穿插必须检出
    big = np.array([[-100, -1, 0], [100, -1, 0], [0, 1, 0]], dtype=float)
    small = np.array([[60, -0.9, -5], [61, -0.9, 5], [60, -0.5, 5]], dtype=float)
    vv = np.vstack([big, small])
    ff = np.array([[0, 1, 2], [3, 4, 5]])
    hit_big = analysis.find_self_intersections(vv, ff)
    g.check("candidate_completeness", hit_big["count"] >= 1,
            {"count": hit_big["count"], "large_triangles": hit_big.get("large_triangles"),
             "candidate_pairs": hit_big.get("candidate_pairs")})

    # 11. 补丁穿插：洞口后立着墙时必须回退
    out, info = repair.act_fill_holes(hole_with_wall(), {})
    g.check("reject_patch_through_wall",
            info.get("reverted") is True and info.get("patch_hits", 0) > 0,
            {"reverted": info.get("reverted"), "patch_hits": info.get("patch_hits"),
             "reason": (info.get("revert_reason") or "")[:70]})

    # 12. 多三角薄片：删掉薄片、保留实体
    out, info = repair.act_fill_holes(sheet_with_body(), {})
    h = health(out)
    g.check("remove_multi_tri_sheet",
            h["faces"] == 12 and abs(h["volume"] - 1000.0) < 1e-6 and h["watertight"]
            and info.get("removed_slivers") == 4,
            {"removed_slivers": info.get("removed_slivers"), **h})

    # 13. 重网格复检：自交必须真的归零，并给出前后计数
    out, info = repair.act_resolve_self_intersection(two_spheres(), {"remesh_pitch": 0.6})
    g.check("remesh_verified",
            info.get("self_intersections_before", 0) > 0
            and info.get("self_intersections_after") == 0
            and info.get("watertight_after") is True
            and not info.get("degraded"),
            {k: info.get(k) for k in ("self_intersections_before", "self_intersections_after",
                                      "watertight_after", "volume_change_pct")})

    # 14. 未测出的壁厚不得评 A
    items = [
        {"id": "watertight", "name": "水密性", "severity": "ok",
         "value": {"boundary_edges": 0, "nonmanifold_edges": 0, "duplicate_faces": 0}},
        {"id": "normals", "name": "法线", "severity": "ok",
         "value": {"signed_volume": 8000, "degenerate_faces": 0,
                   "inverted_components": 0, "winding_conflicts": 0}},
        {"id": "thickness", "name": "壁厚", "severity": "info",
         "value": {"degraded": True, "samples": 0, "min": None}},
        {"id": "nonmanifold", "name": "非流形边", "severity": "ok",
         "value": {"count": 0, "duplicate_faces": 0}},
        {"id": "self_intersection", "name": "自相交", "severity": "ok", "value": {"count": 0}},
        {"id": "components", "name": "孤立面片", "severity": "ok", "value": {"components": 1}},
        {"id": "units", "name": "单位", "severity": "ok", "value": {"unit": "mm"}},
    ]
    q = analysis.quality_score({"items": items, "stats": {"faces": 12}})
    g.check("no_grade_a_when_unmeasured", q["grade"] != "A" and q["confidence"] == "low",
            {"grade": q["grade"], "score": q["score"], "degraded": q["degraded"]})

    # 15. 碎屑阈值：检测与动作必须同口径，主件不能被判碎屑
    body = trimesh.creation.box(extents=[20, 20, 20])
    deb = trimesh.creation.box(extents=[1, 1, 1])
    deb.apply_translation([30, 0, 0])
    mixed = trimesh.util.concatenate([body, deb])
    item = analysis.check_components(mixed, {"small_ratio": 5.0})
    _, ainfo = repair.act_remove_small(mixed, {"small_ratio": 5.0})
    g.check("small_ratio_consistent",
            item["value"]["tiny"] == ainfo["removed"] == 1
            and ainfo["components_after"] == 1,
            {"tiny": item["value"]["tiny"], "removed": ainfo["removed"],
             "components_after": ainfo["components_after"]})

    # 16. 动作顺序：单位换算必须早于加厚
    g.check("action_order", repair.normalize_order(["thicken", "rescale"]) == ["rescale", "thicken"],
            {"order": repair.normalize_order(["thicken", "rescale"])})

    # 17. 缓存隔离：调用方原地缩放不得污染缓存
    p = MODELS / "ball_20k.stl"
    from app import meshio
    first = meshio.load_mesh(p, mutable=True)
    before_ext = [round(float(x), 3) for x in first.extents]
    first.apply_scale(2.0)
    again = meshio.load_mesh(p)
    g.check("cache_not_mutated",
            [round(float(x), 3) for x in again.extents] == before_ext,
            {"cached_extents": [round(float(x), 3) for x in again.extents],
             "expected": before_ext})

    # 18. 检测项异常隔离：一项失败不能让整份报告消失
    real = analysis.CHECKS

    def boom(mesh, params, ctx=None):
        raise RuntimeError("gate-probe")

    analysis.CHECKS = list(real[:3]) + [boom] + list(real[4:])
    try:
        rep = analysis.analyze_mesh(trimesh.creation.box(extents=[20, 20, 20]), "cube.stl")
    finally:
        analysis.CHECKS = real
    g.check("check_failure_isolated",
            len(rep["items"]) == 7 and len(rep["failed_checks"]) == 1
            and rep["summary"]["confidence"] == "low",
            {"items": len(rep["items"]), "failed": rep["failed_checks"],
             "grade": rep["quality"]["grade"]})

    # 19. 边界环分解失败的边必须报出来（三面共边：3 条边、0 个环）
    v4 = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]], float)
    f4 = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3]])
    _l, st = repair.boundary_loops_ex(f4, 4)
    g.check("unresolved_loops_reported",
            len(_l) == 0 and st["unresolved_edges"] == 3 and st["boundary_edges"] == 3,
            {"loops": len(_l), **st})

    # 20. 壁厚粗筛分数随覆盖率下降（20 万面只测 600 面不给满分）
    def _items(cov):
        return [
            {"id": "watertight", "name": "水密性", "severity": "ok",
             "value": {"boundary_edges": 0, "nonmanifold_edges": 0, "duplicate_faces": 0}},
            {"id": "normals", "name": "法线", "severity": "ok",
             "value": {"signed_volume": 8000, "degenerate_faces": 0,
                       "inverted_components": 0, "winding_conflicts": 0}},
            {"id": "thickness", "name": "壁厚", "severity": "ok",
             "value": {"min": 2.4, "samples": 0, "sample_faces": 600,
                       "lower_bound": True, "coverage": cov}},
            {"id": "nonmanifold", "name": "非流形边", "severity": "ok",
             "value": {"count": 0, "duplicate_faces": 0}},
            {"id": "self_intersection", "name": "自相交", "severity": "ok", "value": {"count": 0}},
            {"id": "components", "name": "孤立面片", "severity": "ok", "value": {"components": 1}},
            {"id": "units", "name": "单位", "severity": "ok", "value": {"unit": "mm"}},
        ]

    q_low = analysis.quality_score({"items": _items(0.003), "stats": {"faces": 200000}})
    q_high = analysis.quality_score({"items": _items(0.05), "stats": {"faces": 200000}})
    low_part = [p for p in q_low["parts"] if p["name"] == "壁厚"][0]["score"]
    high_part = [p for p in q_high["parts"] if p["name"] == "壁厚"][0]["score"]
    g.check("thickness_coverage_scoring",
            low_part < high_part and bool(q_low["caveats"]) and not q_high["caveats"],
            {"low": low_part, "high": high_part, "caveats": q_low["caveats"]})

    # 21. 整体反法线的模型补完洞必须转正
    inv = trimesh.creation.box(extents=[20, 20, 20])
    inv.faces = inv.faces[:, ::-1]
    k = np.ones(len(inv.faces), bool)
    k[0] = False
    inv.update_faces(k)
    inv.remove_unreferenced_vertices()
    out, info = repair.act_fill_holes(inv, {})
    vol = analysis.signed_volume(np.asarray(out.vertices, float), np.asarray(out.faces))
    g.check("fill_fixes_orientation",
            info.get("orientation_fixed") is True and vol > 0,
            {"orientation_fixed": info.get("orientation_fixed"), "volume": round(vol, 2)})

    # 22. 镂空件打孔必须真的做薄壁过滤（阈值 > 0）
    sph = trimesh.creation.icosphere(subdivisions=3, radius=20)
    sph.apply_translation([0, 0, 20])
    shell, _cav = voxelops.hollow_shell(sph, 1.6, pitch=0.8)
    _dr, dinfo = process.drill_holes(shell, 2, 3.0, 1.6)
    g.check("drill_thin_filter_active",
            (dinfo.get("min_wall_checked") or 0) > 0 and dinfo["holes"] >= 1,
            {"min_wall_checked": dinfo.get("min_wall_checked"), "holes": dinfo["holes"],
             "skipped_thin": dinfo.get("skipped_thin")})

    # 23. 支撑密度必须跟 spacing 走
    def _bridge():
        b = trimesh.creation.box(extents=[40, 10, 20])
        b.apply_translation([0, 0, 10])
        c = trimesh.creation.box(extents=[20, 12, 12])
        c.apply_translation([0, 0, 4])
        return trimesh.boolean.difference([b, c], engine="manifold")

    br = _bridge()
    n_dense = process.build_supports(br, 45.0, 1.0, 2.0, 400)[1]["count"]
    n_sparse = process.build_supports(br, 45.0, 1.0, 8.0, 400)[1]["count"]
    g.check("support_spacing_respected", n_dense > n_sparse,
            {"spacing_2": n_dense, "spacing_8": n_sparse})

    # 24. 减面破坏水密必须回退
    _m, _s, prep = process.apply_pipeline(
        br.copy(), {**process.DEFAULTS, "decimate_ratio": 0.02, "orient": False})
    g.check("decimate_reverted",
            prep["decimate"].get("reverted") is True and _m.is_watertight,
            {"faces": prep["decimate"].get("faces"),
             "faces_kept": prep["decimate"].get("faces_kept"),
             "watertight": bool(_m.is_watertight)})

    # 25. 分件桩位必须落在实体内（U 形截面的顶点均值会落空）
    from shapely.geometry import Polygon as _Poly

    _u = _Poly(np.array([[0, 0], [30, 0], [30, 30], [20, 30],
                         [20, 10], [10, 10], [10, 30], [0, 30]], float))
    um = trimesh.creation.extrude_polygon(_u, height=20)
    pc = split.section_centroid(um, 10.0, 2)
    g.check("peg_hint_inside",
            pc is not None and bool(um.contains([[pc[0], pc[1], 10.0]])[0]),
            {"peg_hint": pc})

    # 26. 版本对比必须用点到面距离（同一平面不同密度 → 位移 0）
    def _grid(n):
        xs = np.linspace(0, 10, n + 1)
        X, Y = np.meshgrid(xs, xs)
        vv = np.column_stack([X.ravel(), Y.ravel(), np.zeros((n + 1) ** 2)])
        ii = np.arange((n + 1) ** 2).reshape(n + 1, n + 1)
        ff = []
        for i in range(n):
            for j in range(n):
                a, b, c2, d = ii[i, j], ii[i + 1, j], ii[i + 1, j + 1], ii[i, j + 1]
                ff += [[a, b, c2], [a, c2, d]]
        return trimesh.Trimesh(vv, np.array(ff), process=False)

    cmp_res = compare.compare(_grid(4), _grid(8), tol=0.05)
    g.check("compare_surface_metric",
            cmp_res["distance_metric"] == "surface"
            and cmp_res["vertices"]["moved_ratio"] == 0.0
            and cmp_res["identical"] is True,
            {"metric": cmp_res["distance_metric"],
             "moved_ratio": cmp_res["vertices"]["moved_ratio"],
             "max_disp": cmp_res["vertices"]["max_displacement"]})

    # 27. 孤立顶点不能被平滑拉向原点
    sv = np.array([[0, 0, 0], [10, 0, 0], [0, 10, 0], [999, 999, 999]], float)
    se = np.array([[0, 1], [1, 2], [0, 2]])
    sm = gpu.smooth_laplacian(sv, se, np.zeros(4, bool), iters=10, lam=0.5)
    g.check("isolated_vertex_kept",
            bool(np.allclose(sm[3], [999, 999, 999])) and not bool(np.allclose(sm[0], sv[0])),
            {"isolated": [round(float(x), 2) for x in sm[3]]})

    # 28. 拼盘：放不下时要说清「转 90° 能放下」
    wide = trimesh.creation.box(extents=[100, 150, 10])
    tiny = trimesh.creation.box(extents=[50, 50, 10])
    pl, sk = pack.pack([wide, tiny], (192, 120), 4.0)
    g.check("pack_rotation_hint",
            len(pl) == 1 and len(sk) == 1 and sk[0].get("rotate_fits") is True,
            {"placed": len(pl), "skipped": sk})

    g.elapsed = round(time.time() - t0, 3)
    return g


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()
    g = run()
    passed = all(c["ok"] for c in g.checks)
    if args.json:
        print(json.dumps({"passed": passed, "elapsed_sec": g.elapsed,
                          "checks": g.checks}, ensure_ascii=False, indent=2))
    else:
        for c in g.checks:
            mark = "PASS" if c["ok"] else "FAIL"
            print(f"[{mark}] {c['name']}")
            if not c["ok"]:
                print(f"       {json.dumps(c['detail'], ensure_ascii=False)}")
        print(f"\n{sum(c['ok'] for c in g.checks)}/{len(g.checks)} 项通过，"
              f"{g.elapsed:.2f}s")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
