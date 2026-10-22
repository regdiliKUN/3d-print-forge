"""基准测试的各个被测阶段。

每个阶段约定：
    fn(mesh, path, rec) -> (raw_result, summary)

- `raw_result` 只用于保持对象存活（防止被提前 GC 影响内存测量）
- `summary` 是**紧凑的领域摘要**，会被 digest.collect 压成指纹用于回归判定

摘要刻意只留「会随算法正确性改变的量」（体积、面数、壁厚最小值、支撑根数…），
剔除时间戳、耗时、trace id 这类每次都在变的噪声。
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

import numpy as np


# ---------------------------------------------------------------- 导入/导出
def st_load(mesh, path: Path, rec):
    from app import meshio

    v, f = meshio.load_vertices_faces(path)
    return (v, f), {"vertices": int(len(v)), "faces": int(len(f))}


def st_preview_payload(mesh, path: Path, rec):
    """复刻 /api/projects/{pid}/mesh 的载荷构造：base64 顶点 + 索引。"""
    from app import meshio

    m = meshio.load_mesh(path, preview_faces=60_000)
    v = np.ascontiguousarray(m.vertices, dtype=np.float32)
    f = np.ascontiguousarray(m.faces, dtype=np.uint32)
    payload = {
        "vertices": base64.b64encode(v.tobytes()).decode(),
        "indices": base64.b64encode(f.tobytes()).decode(),
    }
    n = len(payload["vertices"]) + len(payload["indices"])
    return payload, {
        "vertex_count": int(len(v)),
        "face_count": int(len(f)),
        "b64_chars": n,
        "raw_bytes": int(v.nbytes + f.nbytes),
    }


def _export(mesh, path, rec, kind: str):
    buf = io.BytesIO()
    mesh.export(buf, file_type=kind)
    data = buf.getvalue()
    return data, {"bytes": len(data)}


def _make_export(kind: str):
    def _fn(mesh, path: Path, rec):
        return _export(mesh, path, rec, kind)

    _fn.__name__ = f"export_{kind}"
    return _fn


# ---------------------------------------------------------------- 检测
def st_analyze(mesh, path: Path, rec):
    from app import analysis

    rep = analysis.analyze_mesh(mesh, path, {})
    items = {}
    for it in rep["items"]:
        items[it["id"]] = {
            "sev": it["severity"],
            "val": {k: v for k, v in (it.get("value") or {}).items()
                    if isinstance(v, (int, float, str, bool))},
        }
    return rep, {"quality": rep["quality"]["score"], "grade": rep["quality"]["grade"],
                 "verdict": rep["quality"].get("verdict", ""),
                 "items": items}


def st_thick_field(mesh, path: Path, rec):
    from app import analysis

    r = analysis.thickness_field(mesh)
    vals = np.asarray(r.get("values") or [], dtype=np.float64)
    return r, {"n": int(vals.size), "min": r.get("min"), "p5": r.get("p5"),
               "max": r.get("max"), "mean": (float(vals.mean()) if vals.size else 0.0)}


def st_overhang_field(mesh, path: Path, rec):
    from app import analysis

    r = analysis.overhang_field(mesh)
    vals = np.asarray(r.get("values") or [], dtype=np.float64)
    return r, {"n": int(vals.size), "min": r.get("min"), "max": r.get("max"),
               "mean": (float(vals.mean()) if vals.size else 0.0)}


def st_comp_field(mesh, path: Path, rec):
    from app import analysis

    r = analysis.component_field(mesh)
    return r, {"components": r.get("components"),
               "sizes": [s["area"] for s in (r.get("sizes") or [])]}


def st_section_profile(mesh, path: Path, rec):
    from app import printmodel

    r = printmodel.cross_section_profile(mesh, samples=40)
    return r, r


# ---------------------------------------------------------------- 修复
def st_fill_holes(mesh, path: Path, rec):
    from app import analysis, repair

    out, info = repair.act_fill_holes(mesh, {})
    v, f = np.asarray(out.vertices), np.asarray(out.faces)
    h = analysis.edge_histogram(f, len(v))
    return out, {"method": info.get("method"), "boundary_before": info.get("boundary_before"),
                 "boundary_after": info.get("boundary_after"),
                 "nonmanifold_after": int(h["nonmanifold"]),
                 "faces": int(len(f)), "vertices": int(len(v))}


def st_fix_normals(mesh, path: Path, rec):
    from app import repair

    out, info = repair.act_fix_normals(mesh, {})
    return out, {"vol_before": info.get("signed_volume_before"),
                 "vol_after": info.get("signed_volume_after"),
                 "components": info.get("components"),
                 "flipped": info.get("flipped_components"),
                 "faces": int(len(np.asarray(out.faces)))}


# ---------------------------------------------------------------- 处理
def st_build_supports(mesh, path: Path, rec):
    from app import process

    sup, info = process.build_supports(mesh, 45.0, 1.0, 3.0, 400)
    segs = info.get("segments") or []
    return (sup, info), {"count": int(info.get("count") or 0),
                         "segments": int(len(segs)),
                         "faces": int(len(sup.faces)) if sup is not None else 0,
                         "note": info.get("note", "")}


def st_support_density(mesh, path: Path, rec):
    from app.main import _support_density_field

    r = _support_density_field(mesh)
    vals = np.asarray(r.get("values") or [], dtype=np.float64)
    return r, {"count": r.get("count"), "max": r.get("max"),
               "n": int(vals.size),
               "mean": (float(vals.mean()) if vals.size else 0.0),
               "sum": (float(vals.sum()) if vals.size else 0.0)}


def st_hollow(mesh, path: Path, rec):
    from app import voxelops

    shell, cavity = voxelops.hollow_shell(mesh, 2.0)
    return (shell, cavity), {
        "shell_faces": int(len(shell.faces)),
        "cavity_faces": int(len(cavity.faces)),
        "shell_volume": round(float(abs(shell.volume)), 3),
        "cavity_volume": round(float(abs(cavity.volume)), 3),
    }


def st_cavity(mesh, path: Path, rec):
    from app import voxelops

    v = voxelops.cavity_volume(mesh, 2.0)
    return v, {"volume": round(float(v), 3)}


STAGES = {
    "load": st_load,
    "preview_payload": st_preview_payload,
    "export_stl": _make_export("stl"),
    "export_3mf": _make_export("3mf"),
    "export_obj": _make_export("obj"),
    "export_ply": _make_export("ply"),
    "analyze": st_analyze,
    "thick_field": st_thick_field,
    "overhang_field": st_overhang_field,
    "comp_field": st_comp_field,
    "section_profile": st_section_profile,
    "fill_holes": st_fill_holes,
    "fix_normals": st_fix_normals,
    "build_supports": st_build_supports,
    "support_density": st_support_density,
    "hollow": st_hollow,
    "cavity": st_cavity,
}

# 哪些阶段不需要预先加载网格（它们测的就是「加载」本身）
NO_MESH = {"load", "preview_payload"}

# 每个模型跑哪些阶段。不是每个阶段对每个模型都有意义——
# 刻意只跑有代表性的组合，否则全矩阵会把单次基准时间拖到不可用的长度。
MATRIX: dict[str, list[str]] = {
    # 极端形态在最前：先证明不会崩，再谈快慢
    "empty": ["analyze"],
    "needle": ["analyze", "thick_field"],
    "ball_20k": ["load", "preview_payload", "analyze", "thick_field", "overhang_field",
                 "comp_field", "section_profile", "build_supports", "support_density",
                 "hollow", "cavity", "export_stl", "export_3mf", "export_obj", "export_ply"],
    "ball_82k": ["load", "preview_payload", "analyze", "thick_field",
                 "support_density", "hollow", "export_stl"],
    "plate_thin": ["analyze", "thick_field"],
    "broken_1k": ["analyze", "fill_holes", "fix_normals"],
    "multi_5": ["analyze", "comp_field"],
    "xintersect": ["analyze"],
    "overhang_t": ["analyze", "overhang_field", "build_supports", "support_density"],
    "inch_254": ["analyze"],
    "statue_30k": ["hollow", "cavity", "export_stl", "export_3mf"],
    "degenerate": ["analyze"],
    "ascii_20k": ["load"],
}
