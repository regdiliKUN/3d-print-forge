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
    """切面上**实心区域内**的一个点，用于提示加桩位置。

    不能用切面顶点的均值：U 形、环形、双腔件的顶点均值会落在空腔里。
    实测 U 形截面（30×30 去掉中间 10×20 的槽）顶点均值是 (15, 17.5)，
    而该点不在实体内——按这个提示打桩，桩会悬在空气里。
    正确做法是取「最大轮廓」的面积质心，若该点不在实体内，再退到轮廓上
    最靠内的点，并用 `mesh.contains` 逐个验证。
    """
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
    except Exception:
        return None

    def _pt2d(p) -> list[float]:
        c = [float(x) for x in p]
        c[axis_index] = float(coord)
        return [round(x, 2) for x in c]

    def _inside(p) -> bool:
        try:
            return bool(mesh.contains([_pt2d(p)])[0])
        except Exception:  # noqa: BLE001
            return True  # 判不了就不要否定，交给用户

    # 1) 优先用最大轮廓的面积质心（带洞时质心可能落在洞里，所以要验证）
    try:
        planar, to_3d = (sec.to_2D() if hasattr(sec, "to_2D") else sec.to_planar())
        T = np.asarray(to_3d, dtype=np.float64)  # 2D 齐次坐标 → 3D
        polys = [p for p in planar.polygons_full if p is not None and not p.is_empty]
        if polys:
            big = max(polys, key=lambda p: p.area)

            def _lift(pt2) -> np.ndarray:
                hom = T @ np.array([pt2.x, pt2.y, 0.0, 1.0])
                w = hom[3] if abs(hom[3]) > 1e-12 else 1.0
                return hom[:3] / w

            cand = big.centroid
            if _inside(_lift(cand)):
                return _pt2d(_lift(cand))
            # 质心落在洞里 → 先试轮廓内的代表点，再向内收缩一圈
            for probe in (big.representative_point(),
                          (big.buffer(-max(0.5, float(np.sqrt(big.area)) * 0.02)).representative_point()
                           if not big.buffer(-max(0.5, float(np.sqrt(big.area)) * 0.02)).is_empty
                           else None)):
                if probe is not None and _inside(_lift(probe)):
                    return _pt2d(_lift(probe))
    except Exception:  # noqa: BLE001
        pass

    # 2) 兜底：顶点均值，若不在实体内就取离均值最近的、确实在实体内的顶点
    mean = pts.mean(axis=0)
    if _inside(mean):
        return _pt2d(mean)
    order = np.argsort(np.linalg.norm(pts - mean, axis=1))
    for i in order[: min(24, len(order))]:
        if _inside(pts[i]):
            return _pt2d(pts[i])
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
    # 每块必须自己也是闭合实体，否则切片器会把切面当开口。
    # 封盖失败在细碎轮廓上并不罕见（shapely 三角化退化），
    # 以前只是把 watertight 写进 meta 就打包了，用户打开才发现某块是破的。
    broken = [i + 1 for i, pc in enumerate(pieces) if not pc.is_watertight]
    if broken:
        ctx.log(f"警告：第 {broken} 块封盖失败，不是闭合实体")
    if not pieces:
        raise ValueError("切割后没有得到任何实体，请检查切面位置是否落在模型外")

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
        # 桩位提示必须取「切面略微往内」的截面：直接切在切面上时，截面与封盖重合，
        # contains 判内外会失败（实测圆柱件全部返回 None）。
        # 往内让 0.5mm（最多 2% 高度），截面形状几乎不变，但一定落在实体内部。
        peg_hint = None
        if i < len(pieces) - 1:
            _top = float(piece.bounds[1][p["axis_index"]])
            _h = float(piece.extents[p["axis_index"]])
            _probe = _top - min(0.5, max(1e-3, _h * 0.02))
            peg_hint = section_centroid(piece, _probe, p["axis_index"])
            if peg_hint:
                # XY 取内截面（保证在实体里），Z 回到真正的切面上
                peg_hint[p["axis_index"]] = round(_top, 2)
        meta.append({
            "index": i + 1,
            "file": f.name,
            "faces": st["faces"],
            "volume_mm3": st["volume_mm3"],
            "extents": st["extents"],
            "watertight": st["is_watertight"],
            "assembly_offset": orig_center,
            "peg_hint": peg_hint,
        })
        ctx.tick(0.3 + 0.55 * (i + 1) / len(pieces), f"导出第 {i + 1}/{len(pieces)} 块", "export")

    zip_path = config.OUTPUTS / f"{pid}_split_{int(time.time())}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for m in meta:
            z.write(out_dir / m["file"], m["file"])

    store.update(pid, assembly={
        "files": [str(out_dir / m["file"]) for m in meta],
        "offsets": [m["assembly_offset"] for m in meta],
        "names": [m["file"] for m in meta],
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    store.add_history(pid, "split", {"max_size": max_size, "axis": p["axis"]}, zip_path,
                      note=f"切成 {len(pieces)} 块")
    ctx.tick(1.0, "分件完成", "done")
    return {
        "plan": p,
        "pieces": meta,
        "output": str(zip_path),
        "dir": str(out_dir),
        "not_watertight": broken,
        "note": "切面质心已给出，建议在该位置加 φ3mm 定位桩再装配"
        + (f"；第 {broken} 块封盖失败（不是闭合实体），请换切面位置重切" if broken else ""),
    }
