"""分件：把超出成型尺寸的模型沿主轴切成可独立打印的多块。

切完每块都重新封盖，保证是闭合实体——不封盖的话切片软件会把切面当成开口。
"""
from __future__ import annotations

import time
import zipfile
from pathlib import Path

import numpy as np
import trimesh

from . import config, meshio, store


def plan(mesh, max_size: float, axis: str = "auto") -> dict:
    """算切几刀、切在哪。"""
    ext = np.asarray(mesh.extents, dtype=np.float64)
    if axis == "auto":
        idx = int(np.argmax(ext))
    else:
        idx = "xyz".index(axis)
    if ext[idx] <= max_size or max_size <= 0:
        return {"axis": "xyz"[idx], "axis_index": idx, "cuts": [], "pieces": 1,
                "extent": round(float(ext[idx]), 2), "needed": False}
    n = int(np.ceil(ext[idx] / max_size))
    lo, hi = float(mesh.bounds[0][idx]), float(mesh.bounds[1][idx])
    step = (hi - lo) / n
    cuts = [lo + step * k for k in range(1, n)]

    # 在等分位置附近找「顶点最稀疏」的高度再切。
    # 等分点很容易正好落在手指、五官这种细节密集区，切下去既要补一堆小洞，
    # 又会把特征切成两半。顶点稀疏的地方通常是大片平整面，切在这里最省事。
    if len(cuts) and len(mesh.vertices) > 100:
        vals = np.asarray(mesh.vertices, dtype=np.float64)[:, idx]
        window = step * 0.2
        refined = []
        for c in cuts:
            cands = np.linspace(max(lo + 1e-3, c - window), min(hi - 1e-3, c + window), 9)
            dens = [int(np.sum(np.abs(vals - z) < 0.5)) for z in cands]
            refined.append(float(cands[int(np.argmin(dens))]))
        cuts = refined

    return {
        "axis": "xyz"[idx], "axis_index": idx,
        "cuts": [round(float(c), 3) for c in cuts],
        "pieces": n,
        "extent": round(float(ext[idx]), 2),
        "step": round(float(step), 2),
        "needed": True,
    }


def split_mesh(mesh, cuts: list[float], axis_index: int) -> list[trimesh.Trimesh]:
    """按坐标切分。每一段先切下界再切上界，两次都带封盖。"""
    normal = np.zeros(3)
    normal[axis_index] = 1.0
    lo = float(mesh.bounds[0][axis_index])
    hi = float(mesh.bounds[1][axis_index])
    bounds = [lo] + list(cuts) + [hi]
    pieces: list[trimesh.Trimesh] = []
    last = len(bounds) - 2
    for k in range(len(bounds) - 1):
        a, b = float(bounds[k]), float(bounds[k + 1])
        m = mesh
        if k > 0:
            origin = np.zeros(3)
            origin[axis_index] = a
            m = m.slice_plane(origin, normal, cap=True)  # 保留 z > a
        if k < last:
            origin = np.zeros(3)
            origin[axis_index] = b
            m = m.slice_plane(origin, -normal, cap=True)  # 保留 z < b
        if m is None or len(m.faces) == 0:
            continue
        pieces.append(m)
    return pieces


def section_centroid(mesh, coord: float, axis_index: int) -> list[float] | None:
    """切面质心，用于提示加桩位置。"""
    origin = np.zeros(3)
    origin[axis_index] = coord
    normal = np.zeros(3)
    normal[axis_index] = 1.0
    try:
        sec = mesh.section(plane_origin=origin, plane_normal=normal)
        if sec is None:
            return None
        pts = np.asarray(sec.vertices, dtype=np.float64)
        if len(pts) == 0:
            return None
        # 用切面顶点均值，比走 to_planar + shapely 质心稳（后者对多段轮廓会抛）
        c = pts.mean(axis=0)
        c[axis_index] = coord
        return [round(float(x), 2) for x in c]
    except Exception:
        return None


def run_job(ctx, params):
    pid = ctx.job.project_id
    proj = store.get(pid)
    if not proj:
        raise ValueError("项目不存在")
    path = Path(proj.get("current") or proj["source"])
    ctx.tick(0.05, "加载模型", "load")
    mesh = meshio.load_mesh(path)

    max_size = float(params.get("max_size") or 0)
    axis = params.get("axis") or "auto"
    if max_size <= 0:
        raise ValueError("未指定成型尺寸上限")
    if not mesh.is_watertight:
        raise ValueError("模型不水密，切分后切面无法正确封盖。请先修复。")

    p = plan(mesh, max_size, axis)
    if not p["needed"]:
        return {"plan": p, "pieces": [], "output": None,
                "note": f"最长边 {p['extent']}mm 未超过 {max_size}mm，不需要分件"}

    ctx.tick(0.25, f"沿 {p['axis'].upper()} 轴切 {len(p['cuts'])} 刀", "cut")
    pieces = split_mesh(mesh, p["cuts"], p["axis_index"])
    if len(pieces) != p["pieces"]:
        ctx.log(f"警告：预期 {p['pieces']} 块，实际得到 {len(pieces)} 块")

    out_dir = config.OUTPUTS / f"{pid}_split_{int(time.time())}"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(proj["name"]).stem
    meta = []
    for i, piece in enumerate(pieces):
        # 记下切割时的原始中心，装配预览要按这个位置摆回去
        c0 = piece.bounds.mean(axis=0)
        orig_center = [round(float(c0[0]), 2), round(float(c0[1]), 2), round(float(c0[2]), 2)]
        piece.apply_translation([0, 0, -float(piece.bounds[0][2])])
        f = out_dir / f"{stem}_part{i + 1:02d}.stl"
        piece.export(str(f))
        st = meshio.mesh_stats(piece)
        meta.append({
            "index": i + 1,
            "file": f.name,
            "faces": st["faces"],
            "volume_mm3": st["volume_mm3"],
            "extents": st["extents"],
            "watertight": st["is_watertight"],
            "assembly_offset": orig_center,
            "peg_hint": section_centroid(piece, float(piece.bounds[1][p["axis_index"]]), p["axis_index"])
            if i < len(pieces) - 1 else None,
        })
        ctx.tick(0.3 + 0.55 * (i + 1) / len(pieces), f"导出第 {i + 1}/{len(pieces)} 块", "export")

    zip_path = config.OUTPUTS / f"{pid}_split_{int(time.time())}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for m in meta:
            z.write(out_dir / m["file"], m["file"])

    store.add_history(pid, "split", {"max_size": max_size, "axis": p["axis"]}, zip_path,
                      note=f"切成 {len(pieces)} 块")
    ctx.tick(1.0, "分件完成", "done")
    return {
        "plan": p,
        "pieces": meta,
        "output": str(zip_path),
        "dir": str(out_dir),
        "note": "切面质心已给出，建议在该位置加 φ3mm 定位桩再装配",
    }
