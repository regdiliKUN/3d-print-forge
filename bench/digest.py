"""把任意函数输出压成可比较的「结果指纹」，用于回归判定。

性能改动最常见的翻车方式不是变慢，而是**悄悄算错**：换个更快的算法、改个缓存键、
把截断距离调小，时间下来了但壁厚/体积/支撑数量跟着变了。所以基准不能只记时间，
必须同时记录结果的数值指纹，优化前后逐项比对。

指纹是通用的：递归遍历返回值，把标量、小数组、短字符串都收进来，
大数组只记长度与统计量（避免指纹本身比被测数据还大）。
"""
from __future__ import annotations

import math
from typing import Any

_MAX_ENTRIES = 240
_SIG = 6


def _r(x: float) -> float:
    """按 6 位有效数字取整，抹掉纯浮点噪声。"""
    if x == 0 or not math.isfinite(x):
        return float(x)
    return float(f"%.{_SIG}g" % x)


def collect(obj: Any, path: str = "", out: dict | None = None, depth: int = 0) -> dict:
    if out is None:
        out = {}
    if len(out) >= _MAX_ENTRIES or depth > 5:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).endswith(("elapsed", "at", "trace", "id", "file")):
                continue  # 时间戳/耗时不参与正确性判定
            collect(v, f"{path}.{k}" if path else str(k), out, depth + 1)
    elif isinstance(obj, (list, tuple)):
        if len(obj) <= 24:
            for i, v in enumerate(obj):
                collect(v, f"{path}[{i}]", out, depth + 1)
        else:
            out[f"{path}.len"] = len(obj)
    elif isinstance(obj, (str, bool)) or obj is None:
        out[path] = obj
    elif isinstance(obj, (int, float)):
        out[path] = _r(float(obj))
    else:
        arr = getattr(obj, "shape", None)
        if arr is not None:  # numpy 数组
            try:
                import numpy as np

                a = np.asarray(obj)
                if a.size == 0:
                    out[f"{path}.size"] = 0
                elif a.size <= 8:
                    for i, v in enumerate(a.ravel().tolist()):
                        out[f"{path}[{i}]"] = _r(float(v))
                else:
                    out[f"{path}.size"] = int(a.size)
                    if a.dtype.kind in "fiu":
                        flat = a.ravel().astype("float64")
                        out[f"{path}.mean"] = _r(float(flat.mean()))
                        out[f"{path}.min"] = _r(float(flat.min()))
                        out[f"{path}.max"] = _r(float(flat.max()))
            except Exception:
                out[path] = str(obj)[:80]
        else:
            out[path] = str(obj)[:80]
    return out


def diff(a: dict, b: dict, rel: float = 1e-4, abs_: float = 1e-6) -> list[str]:
    """比对两份指纹，返回人类可读的差异列表。空列表 = 结果一致。"""
    problems: list[str] = []
    for k in sorted(set(a) | set(b)):
        if k not in a:
            problems.append(f"+{k} = {b[k]!r}（新增）")
            continue
        if k not in b:
            problems.append(f"-{k} = {a[k]!r}（消失）")
            continue
        va, vb = a[k], b[k]
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            if not math.isclose(float(va), float(vb), rel_tol=rel, abs_tol=abs_):
                problems.append(f"{k}: {va} → {vb}")
        elif va != vb:
            problems.append(f"{k}: {va!r} → {vb!r}")
    return problems
