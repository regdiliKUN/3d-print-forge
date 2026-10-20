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
    try:
        if b == "cupy":
            return _smooth_cupy(verts, edges, fixed, iters, lam)
        if b == "torch":
            return _smooth_torch(verts, edges, fixed, iters, lam)
    except Exception:
        pass  # 显存/后端异常一律退回 CPU
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
