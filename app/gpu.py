"""GPU 加速层：能上卡就上卡，不能就退回 numpy。

只封装真正被用到的算子（拉普拉斯平滑、批量点投影），不为了「用了 GPU」而引入
庞大框架。优先级：cupy（与 numpy API 最接近）> torch（CUDA）> CPU。

设计原则：
- 所有函数都接收/返回 numpy 数组，调用方不需要知道后端。
- 设备不可用或显存不足时**静默退回 CPU**，绝不让修复因为 GPU 挂掉。
"""
from __future__ import annotations

import numpy as np

_BACKEND: str | None = None
_NAME: str | None = None

# 规模阈值：低于这些量级，GPU 的调度/拷贝开销盖过并行收益，强行上卡反而更慢。
# 这两个数是实测出来的（见 tools/bench_gpu.py），不是拍脑袋——
# 5 千顶点的拉普拉斯平滑 GPU 只有 CPU 的 0.6 倍，2 万顶点才 4 倍。
SMOOTH_GPU_MIN_VERTS = 15000
PARITY_GPU_MIN_PAIRS = 2_000_000

# 最近一次算子实际跑在哪个后端上，供界面显示「这次用了什么」
_LAST: dict[str, str] = {}


def last_backend() -> dict:
    return dict(_LAST)


def _mark(op: str, where: str) -> None:
    _LAST[op] = where


def _probe_cupy() -> bool:
    """真的编译并跑一次内核——只探测到设备但缺 CUDA 头文件时，这里会失败，
    从而诚实退回 CPU，而不是「自称有 GPU 但每次调用都抛异常再回退」。"""
    try:
        import cupy as cp  # type: ignore

        if cp.cuda.runtime.getDeviceCount() <= 0:
            return False
        a = cp.array([1.0, 2.0, 3.0], dtype=cp.float64)
        float((a * 2.0).sum())  # 触发一次 JIT 编译
        return True
    except Exception:
        return False


def _probe_torch() -> bool:
    try:
        import torch  # type: ignore

        if not torch.cuda.is_available():
            return False
        x = torch.ones(3, device="cuda")
        float((x * 2).sum())
        return True
    except Exception:
        return False


def backend() -> str:
    """返回 'cupy' | 'torch' | 'cpu'（带缓存，探测真实可用性）。"""
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    if _probe_cupy():
        _BACKEND = "cupy"
        return _BACKEND
    if _probe_torch():
        _BACKEND = "torch"
        return _BACKEND
    _BACKEND = "cpu"
    return _BACKEND


def device_name() -> str:
    """给日志/界面看的设备名。"""
    global _NAME
    if _NAME is not None:
        return _NAME
    b = backend()
    try:
        if b == "cupy":
            import cupy

            p = cupy.cuda.runtime.getDeviceProperties(0)
            n = p["name"]
            _NAME = n.decode() if isinstance(n, bytes) else str(n)
            return _NAME
        if b == "torch":
            import torch

            _NAME = torch.cuda.get_device_name(0)
            return _NAME
    except Exception:
        pass
    _NAME = "CPU"
    return _NAME


def available() -> bool:
    return backend() != "cpu"


def info() -> dict:
    return {"available": available(), "backend": backend(), "name": device_name()}


# ---------------------------------------------------------------- 算子
def smooth_laplacian(
    verts: np.ndarray,
    edges: np.ndarray,
    fixed_mask: np.ndarray,
    iters: int = 10,
    lam: float = 0.5,
) -> np.ndarray:
    """拉普拉斯平滑（Umbrella 算子）。

    verts   (N,3) float64
    edges   (E,2) 无向边，int64
    fixed   (N,)  bool，True 的点保持不动（补洞边界）
    返回平滑后的 (N,3) numpy 数组。GPU 可用时整段在显存里迭代。
    """
    verts = np.asarray(verts, dtype=np.float64)
    edges = np.asarray(edges, dtype=np.int64)
    fixed = np.asarray(fixed_mask, dtype=bool)
    if iters <= 0 or len(edges) == 0 or len(verts) == 0:
        return verts

    b = backend()
    # 小网格上 GPU 反而更慢（内核启动 + 显存往返），按规模分派
    if b != "cpu" and len(verts) >= SMOOTH_GPU_MIN_VERTS:
        try:
            if b == "cupy":
                out = _smooth_cupy(verts, edges, fixed, iters, lam)
            else:
                out = _smooth_torch(verts, edges, fixed, iters, lam)
            _mark("smooth", b)
            return out
        except Exception:
            pass  # 显存/后端异常一律退回 CPU
    _mark("smooth", "cpu")
    return _smooth_numpy(verts, edges, fixed, iters, lam)


def _smooth_numpy(verts, edges, fixed, iters, lam):
    n = len(verts)
    deg = np.zeros(n, dtype=np.float64)
    np.add.at(deg, edges[:, 0], 1.0)
    np.add.at(deg, edges[:, 1], 1.0)
    deg = np.maximum(deg, 1.0)
    v = verts.copy()
    for _ in range(iters):
        acc = np.zeros((n, 3), dtype=np.float64)
        np.add.at(acc, edges[:, 0], v[edges[:, 1]])
        np.add.at(acc, edges[:, 1], v[edges[:, 0]])
        avg = acc / deg[:, None]
        v = np.where(fixed[:, None], v, v * (1.0 - lam) + avg * lam)
    return v


def _smooth_cupy(verts, edges, fixed, iters, lam):
    import cupy as cp

    n = len(verts)
    v = cp.asarray(verts)
    e = cp.asarray(edges)
    fx = cp.asarray(fixed)
    deg = cp.zeros(n, dtype=cp.float64)
    cp.add.at(deg, e[:, 0], 1.0)
    cp.add.at(deg, e[:, 1], 1.0)
    deg = cp.maximum(deg, 1.0)
    for _ in range(iters):
        acc = cp.zeros((n, 3), dtype=cp.float64)
        cp.add.at(acc, e[:, 0], v[e[:, 1]])
        cp.add.at(acc, e[:, 1], v[e[:, 0]])
        avg = acc / deg[:, None]
        v = cp.where(fx[:, None], v, v * (1.0 - lam) + avg * lam)
    return cp.asnumpy(v)


def _smooth_torch(verts, edges, fixed, iters, lam):
    import torch

    n = len(verts)
    dev = "cuda"
    v = torch.as_tensor(verts, dtype=torch.float64, device=dev)
    e = torch.as_tensor(edges, dtype=torch.long, device=dev)
    fx = torch.as_tensor(fixed, device=dev)
    deg = torch.zeros(n, dtype=torch.float64, device=dev)
    ones = torch.ones(len(edges), dtype=torch.float64, device=dev)
    deg.index_add_(0, e[:, 0], ones)
    deg.index_add_(0, e[:, 1], ones)
    deg = deg.clamp(min=1.0)
    for _ in range(iters):
        acc = torch.zeros((n, 3), dtype=torch.float64, device=dev)
        acc.index_add_(0, e[:, 0], v[e[:, 1]])
        acc.index_add_(0, e[:, 1], v[e[:, 0]])
        avg = acc / deg.unsqueeze(1)
        v = torch.where(fx.unsqueeze(1), v, v * (1.0 - lam) + avg * lam)
    return v.cpu().numpy()


# ---------------------------------------------------------------- 批量几何算子
def points_in_rings(points_xy: np.ndarray, rings: list[np.ndarray],
                    chunk: int = 400) -> np.ndarray:
    """点在多边形内判定（射线奇偶法），批量版。

    为什么不用 shapely 的 contains 逐点判：
        2000 个点 × 逐点调用是个 Python 级循环，单次建议要 100ms 上下。
        这里改成「点 × 边」的矩阵运算，一次算完。

    为什么要 GPU：
        点边对是 P×E 的量级（2000×500 = 100 万），正好是 GPU 擅长的规整乘法。
        显存不够或没卡时按 chunk 切块，仍然走同一条代码路径。

    rings  每个环是一组 (M,2) 顶点，首尾不闭合也行（内部自动补上闭合边）
    返回   (P,) bool，落在奇数个环内即为「内部」
    """
    pts = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
    if len(pts) == 0 or not rings:
        return np.zeros(len(pts), dtype=bool)

    pairs = len(pts) * sum(len(r) for r in rings if r is not None and len(r) >= 3)
    b = backend()
    if b == "cupy" and pairs >= PARITY_GPU_MIN_PAIRS:
        try:
            out = _in_rings_cupy(pts, rings, chunk)
            _mark("parity", "cupy")
            return out
        except Exception:
            pass  # 显存不足等异常一律退回 CPU
    _mark("parity", "cpu")
    return _in_rings_numpy(pts, rings, chunk)


def _ring_edges(ring: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """把环转成边向量 (x1,y1)-(x2,y2)。"""
    r = np.asarray(ring, dtype=np.float64)
    if len(r) < 3:
        return np.zeros((0, 2)), np.zeros((0, 2))
    a = r
    c = np.roll(r, -1, axis=0)
    return a, c


def _in_rings_numpy(pts, rings, chunk):
    n = len(pts)
    crossings = np.zeros(n, dtype=np.int32)
    for ring in rings:
        a, c = _ring_edges(ring)
        if len(a) == 0:
            continue
        x1, y1 = a[:, 0], a[:, 1]
        x2, y2 = c[:, 0], c[:, 1]
        dy = np.where(np.abs(y2 - y1) < 1e-12, 1e-12, y2 - y1)
        for s in range(0, n, chunk):
            px = pts[s:s + chunk, 0][:, None]
            py = pts[s:s + chunk, 1][:, None]
            straddle = (y1[None, :] > py) != (y2[None, :] > py)
            xcross = (x2 - x1)[None, :] * (py - y1[None, :]) / dy[None, :] + x1[None, :]
            crossings[s:s + chunk] += (straddle & (px < xcross)).sum(axis=1)
    return (crossings % 2) == 1


def _in_rings_cupy(pts, rings, chunk):
    import cupy as cp

    n = len(pts)
    crossings = cp.zeros(n, dtype=cp.int32)
    for ring in rings:
        a, c = _ring_edges(ring)
        if len(a) == 0:
            continue
        x1 = cp.asarray(a[:, 0])
        y1 = cp.asarray(a[:, 1])
        x2 = cp.asarray(c[:, 0])
        y2 = cp.asarray(c[:, 1])
        dy = cp.where(cp.abs(y2 - y1) < 1e-12, 1e-12, y2 - y1)
        for s in range(0, n, chunk):
            px = cp.asarray(pts[s:s + chunk, 0])[:, None]
            py = cp.asarray(pts[s:s + chunk, 1])[:, None]
            straddle = (y1[None, :] > py) != (y2[None, :] > py)
            xcross = (x2 - x1)[None, :] * (py - y1[None, :]) / dy[None, :] + x1[None, :]
            crossings[s:s + chunk] += (straddle & (px < xcross)).sum(axis=1)
    return cp.asnumpy(crossings % 2 == 1)


def monte_carlo(sampler, n: int, chunk: int = 100) -> np.ndarray:
    """批量蒙特卡洛：把 n 次采样的扰动矩阵一次性算完。

    sampler 接收 (n, 4) 的倍率矩阵 [密度, 流量, 几何, 层开销]，返回 (n, 2) 的
    [耗材, 时长]。矩阵运算本身在 GPU 上做，采样的随机数仍在 CPU 生成——
    这是有意的：随机数生成器在 GPU 上按块跑反而更慢，而且不可复现。
    """
    rng = np.random.default_rng(7)
    u = np.empty((n, 4), dtype=np.float64)
    u[:, 0] = rng.normal(1.0, 0.02, n)                    # 密度
    u[:, 1] = rng.lognormal(0.0, 0.12, n)                 # 流量
    u[:, 2] = rng.lognormal(0.0, 0.05, n)                 # 几何采样
    u[:, 3] = rng.lognormal(0.0, 0.18, n)                 # 层开销
    return np.asarray(sampler(u), dtype=np.float64)
