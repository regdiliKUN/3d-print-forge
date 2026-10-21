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

from app import analysis, repair, voxelops  # noqa: E402

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
