"""网格加载：二进制 STL 用 memmap 分块读取，ASCII STL 流式解析，其余交给 trimesh。

大文件不一次性进内存：二进制 STL 按 chunk_faces 分块，块间只保留顶点与索引。
"""
from __future__ import annotations

import gc
import itertools
import re
import struct
from collections import OrderedDict
from pathlib import Path
from typing import Callable

import numpy as np

from . import config

_CACHE: "OrderedDict[tuple, object]" = OrderedDict()
_CACHE_MAX = 3
_CACHE_FACES_BUDGET = 3_000_000  # 缓存里最多留多少面，超了淘汰最旧的

_STL_DTYPE = np.dtype([("n", "<f4", (3,)), ("v", "<f4", (3, 3)), ("attr", "<u2")])


# ---------------------------------------------------------------- 格式嗅探
def is_binary_stl(path: Path) -> bool:
    size = path.stat().st_size
    if size < 84:
        return False
    with path.open("rb") as f:
        head = f.read(84)
    n = struct.unpack("<I", head[80:84])[0]
    if 84 + n * 50 == size:
        return True
    if head[:5].lower() == b"solid":
        # 有些二进制 STL 也以 solid 开头，靠尺寸判定
        return 84 + n * 50 == size
    return False


def _stl_face_count(path: Path) -> int:
    with path.open("rb") as f:
        f.seek(80)
        return struct.unpack("<I", f.read(4))[0]


# ---------------------------------------------------------------- 加载
def load_vertices_faces(
    path: Path,
    chunk_faces: int = 0,
    progress: Callable[[float], None] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """返回 (vertices float32 Nx3, faces uint32 Mx3)，已做顶点去重。"""
    path = Path(path)
    if path.suffix.lower() == ".stl" and is_binary_stl(path):
        return _load_binary_stl(path, chunk_faces or config.get().runtime.chunk_faces, progress)
    if path.suffix.lower() == ".stl":
        return _load_ascii_stl(path, progress)
    return _load_via_trimesh(path)


def _load_binary_stl(path: Path, chunk: int, progress=None) -> tuple[np.ndarray, np.ndarray]:
    n = _stl_face_count(path)
    if n == 0:
        return np.zeros((0, 3), np.float32), np.zeros((0, 3), np.uint32)
    chunk = max(1000, min(chunk, n))
    mm = np.memmap(path, dtype=_STL_DTYPE, mode="r", offset=84, shape=(n,))
    verts_parts: list[np.ndarray] = []
    for start in range(0, n, chunk):
        stop = min(start + chunk, n)
        block = np.asarray(mm[start:stop]["v"]).reshape(-1, 3)
        verts_parts.append(block.astype(np.float32, copy=False))
        if progress:
            progress(stop / n)
    del mm
    raw = np.concatenate(verts_parts) if len(verts_parts) > 1 else verts_parts[0]
    del verts_parts
    faces = np.arange(len(raw), dtype=np.uint32).reshape(-1, 3)
    return _weld(raw, faces)


_VERTEX_RE = re.compile(rb"vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)")


def _load_ascii_stl(path: Path, progress=None) -> tuple[np.ndarray, np.ndarray]:
    """ASCII STL 解析：分块正则 + fromiter 批量转 float。

    逐行 Python 循环在几十万顶点的文件上要几十秒，这里按 8MB 分块、
    块内用编译好的正则一次扫描，块边界按换行切开避免截断半个 token。
    """
    parts: list[tuple[bytes, bytes, bytes]] = []
    total = max(1, path.stat().st_size)
    read = 0
    buf = b""
    with path.open("rb") as f:
        while True:
            chunk = f.read(8 << 20)
            if not chunk:
                break
            buf += chunk
            cut = buf.rfind(b"\n")
            if cut < 0:
                continue
            seg, buf = buf[: cut + 1], buf[cut + 1 :]
            read += len(seg)
            parts.extend(_VERTEX_RE.findall(seg))
            if progress:
                progress(min(1.0, read / total))
    if buf:
        parts.extend(_VERTEX_RE.findall(buf))

    if not parts:
        return np.zeros((0, 3), np.float32), np.zeros((0, 3), np.uint32)
    flat = itertools.chain.from_iterable(parts)
    raw = np.fromiter(map(float, flat), dtype=np.float32, count=len(parts) * 3).reshape(-1, 3)
    faces = np.arange(len(raw), dtype=np.uint32).reshape(-1, 3)
    return _weld(raw, faces)


def _load_via_trimesh(path: Path) -> tuple[np.ndarray, np.ndarray]:
    import trimesh

    scene = trimesh.load(str(path), force="scene")
    if isinstance(scene, trimesh.Trimesh):
        m = scene
    else:
        geoms = [g for g in scene.geometry.values() if isinstance(g, trimesh.Trimesh)]
        if not geoms:
            raise ValueError("文件里没有可用的三角网格")
        m = trimesh.util.concatenate(geoms)
    return (
        np.ascontiguousarray(m.vertices, dtype=np.float32),
        np.ascontiguousarray(m.faces, dtype=np.uint32),
    )


def _weld(vertices: np.ndarray, faces: np.ndarray, decimals: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """按量化坐标去重顶点。STL 每个三角形存独立顶点，不去重无法判断流形性。"""
    if len(vertices) == 0:
        return vertices, faces
    q = np.round(vertices, decimals)
    # 用结构化视图做 unique，比 lexsort 快且稳
    view = np.ascontiguousarray(q).view([("x", q.dtype), ("y", q.dtype), ("z", q.dtype)]).ravel()
    _, first_idx, inverse = np.unique(view, return_index=True, return_inverse=True)
    new_verts = vertices[first_idx]
    new_faces = inverse.astype(np.uint32)[faces]
    # 去掉退化三角形（三点重合）
    ok = (new_faces[:, 0] != new_faces[:, 1]) & (new_faces[:, 1] != new_faces[:, 2]) & (new_faces[:, 0] != new_faces[:, 2])
    return np.ascontiguousarray(new_verts), np.ascontiguousarray(new_faces[ok])


# ---------------------------------------------------------------- 缓存
def _cache_key(path: Path, preview: int) -> tuple:
    st = path.stat()
    return (str(path.resolve()), st.st_mtime_ns, st.st_size, preview)


def load_mesh(path: Path, preview_faces: int = 0, mutable: bool = False):
    """加载为 trimesh.Trimesh；preview_faces>0 时额外降采样用于前端预览。

    `mutable=True` 表示调用方会原地改这份网格（缩放、平移、旋转、合并顶点…），
    此时返回缓存对象的**副本**。缓存是按「路径 + mtime + 面数上限」命中的，
    如果直接把缓存对象交出去让调用方 `apply_scale`，下一次同路径加载拿到的
    就是已经被缩放过的模型——实测第二次加载 60mm 的球变成 120mm，
    而且这个污染会跨请求保留到进程重启。
    """
    import trimesh

    path = Path(path)
    key = _cache_key(path, preview_faces)
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
        return hit.copy() if mutable else hit

    verts, faces = load_vertices_faces(path)
    if preview_faces and len(faces) > preview_faces:
        verts, faces = decimate_arrays(verts, faces, preview_faces)
    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False, validate=False)
    mesh.metadata["source_path"] = str(path)

    _CACHE[key] = mesh
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    total = sum(len(m.faces) for m in _CACHE.values())
    while total > _CACHE_FACES_BUDGET and len(_CACHE) > 1:
        _, old = _CACHE.popitem(last=False)
        total -= len(old.faces)
    _enforce_memory_budget()
    gc.collect()
    return mesh.copy() if mutable else mesh


def _cluster_decimate(verts: np.ndarray, faces: np.ndarray, target: int) -> tuple[np.ndarray, np.ndarray]:
    """顶点聚类简化：把顶点量化到体素格，合并后去退化面。

    作为 fast_simplification 的兜底——后者在非流形或极不均匀的网格上会失败，
    失败后直接按 stride 抽面会让模型变成一堆碎三角，看着就是坏的。
    """
    if len(faces) <= target:
        return verts, faces
    lo = verts.min(axis=0)
    hi = verts.max(axis=0)
    span = np.maximum(hi - lo, 1e-9)
    n_cells = max(8, int(round((target * 2) ** (1 / 3))))
    cell = span / n_cells
    q = np.clip(np.floor((verts - lo) / cell).astype(np.int64), 0, n_cells - 1)
    key = q[:, 0] * n_cells * n_cells + q[:, 1] * n_cells + q[:, 2]
    _, inv = np.unique(key, return_inverse=True)
    inv = inv.reshape(-1)
    n_new = int(inv.max()) + 1
    counts = np.bincount(inv, minlength=n_new).astype(np.float64)
    new_verts = np.zeros((n_new, 3), dtype=np.float32)
    for d in range(3):
        new_verts[:, d] = np.bincount(inv, weights=verts[:, d], minlength=n_new) / np.maximum(counts, 1)
    nf = inv[faces].astype(np.uint32)
    ok = (nf[:, 0] != nf[:, 1]) & (nf[:, 1] != nf[:, 2]) & (nf[:, 0] != nf[:, 2])
    nf = nf[ok]
    if len(nf) == 0:
        return verts, faces
    sorted_f = np.sort(nf, axis=1)
    _, first = np.unique(sorted_f, axis=0, return_index=True)
    return np.ascontiguousarray(new_verts), np.ascontiguousarray(nf[np.sort(first)])


def decimate_arrays(verts: np.ndarray, faces: np.ndarray, target: int) -> tuple[np.ndarray, np.ndarray]:
    """把面数降到 target 附近，用于预览。"""
    if len(faces) <= target:
        return verts, faces
    try:
        import fast_simplification

        v2, f2 = fast_simplification.simplify(
            verts.astype(np.float32), faces.astype(np.int32), target_reduction=1.0 - target / len(faces)
        )
        if len(f2) > 8:
            return np.ascontiguousarray(v2, np.float32), np.ascontiguousarray(f2, np.uint32)
    except Exception:
        pass
    return _cluster_decimate(verts, faces, target)


def _enforce_memory_budget() -> None:
    """进程内存吃紧时主动丢缓存，避免为了预览把整个服务拖进 OOM。"""
    try:
        import os

        import psutil

        mb = psutil.Process(os.getpid()).memory_info().rss / 1048576
    except Exception:
        return
    limit = config.get().runtime.memory_limit_mb
    if limit and mb > limit * 0.75 and _CACHE:
        _CACHE.clear()
        gc.collect()


def clear_cache() -> None:
    _CACHE.clear()
    gc.collect()


# ---------------------------------------------------------------- 统计
def mesh_stats(mesh) -> dict:
    ext = getattr(mesh, "extents", None)
    try:
        volume = float(mesh.volume) if mesh.is_volume else float(abs(mesh.volume))
    except Exception:
        volume = 0.0
    try:
        area = float(mesh.area)
    except Exception:
        area = 0.0
    return {
        "vertices": int(len(mesh.vertices)),
        "faces": int(len(mesh.faces)),
        "extents": [round(float(x), 4) for x in ext] if ext is not None else None,
        "max_dim": round(float(max(ext)), 4) if ext is not None else None,
        "volume_mm3": round(volume, 3),
        "area_mm2": round(area, 3),
        "is_watertight": bool(getattr(mesh, "is_watertight", False)),
        "is_winding_consistent": bool(getattr(mesh, "is_winding_consistent", False)),
        "euler_number": int(getattr(mesh, "euler_number", 0)),
    }
