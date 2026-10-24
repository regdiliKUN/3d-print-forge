"""体素工具：把网格栅格化后做形态学操作，再重建回网格。

镂空、加厚、消解自交都走这条路——比布尔运算稳，代价是锐边会被轻微圆化。
坐标换算依赖 VoxelGrid.transform（体素索引 → 世界坐标）。
"""
from __future__ import annotations

import types

import numpy as np
import scipy.ndimage as ndi
import trimesh
from skimage import measure


MAX_AXIS_VOXELS = 256


def choose_pitch(mesh, target_voxels: int = 120, floor: float = 0.2) -> float:
    """按最长边切成 target_voxels 份，避免大模型把内存撑爆。

    旧实现固定 floor=0.2mm，0.5mm 的珠宝件会被切成 2~3 格，marching cubes
    基本重建不出形状。floor 只应约束「大模型不要切太细」，不能大于模型本身
    的尺度，所以这里按最长边再收一次。
    """
    ext = np.asarray(mesh.extents, dtype=np.float64) if mesh.extents is not None else np.zeros(0)
    scale = float(np.max(ext)) if ext.size else 0.0
    if not np.isfinite(scale) or scale <= 0:
        return max(float(floor), 1e-6)
    floor = min(float(floor), scale / 20.0)
    return max(floor, scale / max(20, int(target_voxels)))


def sanitize_pitch(mesh, pitch: float | None = None, max_axis: int = MAX_AXIS_VOXELS) -> float:
    """把外部传入的 pitch 收敛到可栅格化的范围。

    API 参数没有可信来源：0、负数、NaN、1e-9 都可能出现。过小的 pitch 会让
    grid 维度爆炸（1000mm 模型用 0.01mm 就是 100k³），过大的 pitch 又会让
    模型不足一个体素、重建为空。这里同时做上下界，并返回实际生效的 pitch。
    """
    ext = np.asarray(mesh.extents, dtype=np.float64) if mesh.extents is not None else np.zeros(0)
    scale = float(np.max(ext)) if ext.size else 0.0
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("模型包围盒为空，无法体素化")
    if pitch is None:
        p = choose_pitch(mesh)
    else:
        try:
            p = float(pitch)
        except (TypeError, ValueError):
            p = choose_pitch(mesh)
        if not np.isfinite(p) or p <= 0:
            p = choose_pitch(mesh)
    # 每个轴最多 max_axis 格，防止 pitch 过小把内存打爆
    lower = scale / float(max(1, int(max_axis)))
    # 最长边至少 8 格；最薄的非零边至少 1 格，否则模型会整个消失
    positive = ext[ext > max(scale * 1e-9, 1e-12)]
    thinnest = float(np.min(positive)) if positive.size else scale
    upper = min(scale / 8.0, thinnest)
    # 极薄片在 256 格上限内本来就无法保留厚度：优先保证内存有界
    p = max(min(p, upper), lower)
    return float(p)


def to_grid(mesh, pitch: float | None = None):
    """栅格化并四周补一圈空体素。

    补边是必须的：当模型正好填满自己的包围盒（薄板、立方体都是），
    grid 会全为 True，marching cubes 找不到 0/1 交界面而直接报错。
    """
    p = sanitize_pitch(mesh, pitch)
    try:
        vox = mesh.voxelized(pitch=p).fill()
    except Exception as exc:
        raise ValueError(f"体素化失败（pitch={p:.4g}mm）：{exc}") from exc
    grid = np.pad(np.asarray(vox.matrix, dtype=bool), 1, constant_values=False)
    return grid, vox, float(vox.pitch[0])


def grid_to_mesh(grid: np.ndarray, vox, pad: int = 1, positive: bool = True) -> trimesh.Trimesh | None:
    if not grid.any() or grid.all():
        return None
    verts, faces, _, _ = measure.marching_cubes(grid.astype(np.float32), level=0.5)
    verts = verts - float(pad)  # 抵消补边偏移，回到原始体素索引空间
    homog = np.column_stack([verts, np.ones(len(verts))])
    world = (vox.transform @ homog.T).T[:, :3]
    m = trimesh.Trimesh(vertices=world, faces=faces, process=True)
    # marching cubes 的朝向取决于体素索引顺序，不保证朝外；实体一律修正为正体积
    if positive and float(m.volume) < 0:
        m.invert()
    return m


def _mesh_edges(faces: np.ndarray) -> np.ndarray:
    e = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    return np.unique(np.sort(e, axis=1), axis=0)


def taubin_smooth(mesh: trimesh.Trimesh, iters: int = 3, lam: float = 0.5, mu: float = -0.53) -> trimesh.Trimesh:
    """Taubin λ|μ 平滑：去掉 marching cubes 的台阶纹，又不像普通拉普拉斯那样整体缩水。

    λ 步做一次收缩、μ 步（负系数）做一次膨胀，两者配对后体积基本守恒。
    所有顶点都参与（体素重建出来的是闭合体，没有需要固定的边界）。
    """
    from . import gpu

    if iters <= 0:
        return mesh
    v = np.asarray(mesh.vertices, dtype=np.float64)
    f = np.asarray(mesh.faces, dtype=np.int64)
    if len(v) == 0 or len(f) == 0:
        return mesh
    e = _mesh_edges(f)
    fixed = np.zeros(len(v), dtype=bool)
    for _ in range(int(iters)):
        v = gpu.smooth_laplacian(v, e, fixed, iters=1, lam=lam)
        v = gpu.smooth_laplacian(v, e, fixed, iters=1, lam=mu)
    return trimesh.Trimesh(vertices=v, faces=f, process=True)


def remesh(mesh, pitch: float | None = None, smooth_iters: int = 3) -> trimesh.Trimesh:
    """体素重网格：能一次性消解自交与非流形，代价是细节量化到 pitch。

    marching cubes 会在斜面/曲面上留下台阶（就是肉眼看到的「层纹」），
    重建后补一遍 Taubin 平滑把它抹平；smooth_iters=0 可关闭。
    """
    grid, vox, _ = to_grid(mesh, pitch)
    out = grid_to_mesh(grid, vox)
    if out is None:
        raise ValueError("体素化结果为空，模型可能没有体积")
    if smooth_iters and smooth_iters > 0:
        out = taubin_smooth(out, int(smooth_iters))
    return out


def thicken(mesh, amount: float, pitch: float | None = None, smooth_iters: int = 2) -> trimesh.Trimesh:
    """沿法线向外补 amount 毫米。

    外扩距离场需要足够的空余空间：只补 1 层的话，包围盒外的距离会被截断，
    整块 grid 都会落进 amount 阈值内，反而变成实心。
    """
    grid, vox, p = to_grid(mesh, pitch)
    extra = int(np.ceil(amount / p)) + 2
    grid = np.pad(grid, extra, constant_values=False)
    outside = ndi.distance_transform_edt(~grid) * p
    grown = grid | (outside <= amount)
    out = grid_to_mesh(grown, vox, pad=1 + extra)
    if out is None:
        raise ValueError("加厚失败：模型体素为空或完全填满")
    if smooth_iters and smooth_iters > 0:
        out = taubin_smooth(out, int(smooth_iters))
    return out


def _coarsen_grid(grid: np.ndarray, vox, factor: int):
    """体素降采样：面数按 factor² 下降，拓扑天然保持。

    比减面安全——fast_simplification 在体素网格上做边折叠必然产生非流形边，
    内腔一旦非流形，后续打孔布尔运算就会失败。
    """
    core = grid[1:-1, 1:-1, 1:-1]
    small = core[::factor, ::factor, ::factor]
    coarse_grid = np.pad(small, 1, constant_values=False)
    t = vox.transform.copy() @ np.diag([factor, factor, factor, 1.0])
    return coarse_grid, types.SimpleNamespace(transform=t)


def hollow_shell(mesh, wall: float, pitch: float | None = None,
                 coarse: int = 2) -> tuple[trimesh.Trimesh, trimesh.Trimesh]:
    """返回 (双层壳体, 内腔网格)。内腔法线保证朝内，合并后体积 = 外壳 - 内腔。"""
    grid, vox, p = to_grid(mesh, pitch)
    inside = ndi.distance_transform_edt(grid) * p
    inner_grid = inside > wall
    if not inner_grid.any():
        raise ValueError(
            f"壳厚 {wall}mm 超过模型最薄处的一半，无法镂空。请减小壳厚或先加厚模型。"
        )
    if coarse > 1:
        inner_grid, cvox = _coarsen_grid(inner_grid, vox, coarse)
    else:
        cvox = vox
    cavity = grid_to_mesh(inner_grid, cvox)
    if cavity is None:
        raise ValueError("内腔重建失败")
    # marching cubes 的朝向取决于体素索引顺序，不保证朝内——用体积符号判定，
    # 内腔必须贡献负体积，否则合并后体积会变成"外壳+内腔"。
    if float(cavity.volume) > 0:
        cavity.invert()
    if not cavity.is_watertight:
        cavity.merge_vertices()
    shell = trimesh.util.concatenate([mesh, cavity])
    return shell, cavity


def cavity_volume(mesh, wall: float, pitch: float | None = None) -> float:
    """预估镂空后可省下的体积（mm³）。"""
    grid, vox, p = to_grid(mesh, pitch)
    inside = ndi.distance_transform_edt(grid) * p
    inner_grid = inside > wall
    return float(inner_grid.sum() * (p ** 3))
