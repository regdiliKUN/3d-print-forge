"""模型检测引擎：水密性、法线、壁厚、非流形边、自相交、孤立面片、单位尺寸。

每个检测项返回统一结构，附严重程度与可一键执行的修复动作。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np

from . import config, meshio, store

SEV_ORDER = {"ok": 0, "info": 1, "warning": 2, "error": 3}


# ---------------------------------------------------------------- 派生数据记忆化
# 七项检测里有四项要的是同一批派生量：边直方图（水密/非流形）、重复面
# （水密/非流形）、连通分量（法线/孤立面片）。同一张网格上它们被重复算了
# 2~3 遍，在 18 万面的模型上约占总耗时的三成。
#
# 缓存键用 (id(faces), len(faces), 附加参数)，并且**持有数组引用**：
# 只要缓存还在，id 就不会被别的对象复用，键不会串。条数上限很小，
# 只覆盖「同一次检测里的同几项」，不会无界增长。
_MEMO: "OrderedDict[tuple, tuple]" = OrderedDict()
_MEMO_LOCK = threading.RLock()
_MEMO_MAX = 12


def _memo(key: tuple, arr: np.ndarray, fn):
    k = key + (id(arr), len(arr))
    with _MEMO_LOCK:
        hit = _MEMO.get(k)
        if hit is not None:
            _MEMO.move_to_end(k)
            return hit[1]
        val = fn()
        _MEMO[k] = (arr, val)
        while len(_MEMO) > _MEMO_MAX:
            _MEMO.popitem(last=False)
        return val


def clear_memo() -> None:
    with _MEMO_LOCK:
        _MEMO.clear()


def _mesh_memo(mesh, key: str, fn):
    """把派生量挂在网格对象上共享。

    不能按 `id(faces)` 缓存：trimesh 的 `faces` 属性每次访问都可能返回**新的**
    数组对象（实测两次 `np.asarray(mesh.faces)` 不是同一个对象），
    按数组身份做键永远命中不了。网格对象本身在整轮检测里是同一个，
    挂属性既不会串键，也随网格一起被回收。
    """
    cache = getattr(mesh, "_wb_cache", None)
    if cache is None:
        cache = {}
        try:
            mesh._wb_cache = cache
        except Exception:  # noqa: BLE001
            return fn()
    with _MEMO_LOCK:
        if key in cache:
            return cache[key]
        val = fn()
        cache[key] = val
        return val


# ---------------------------------------------------------------- 几何工具
def edge_keys(faces: np.ndarray, nverts: int) -> np.ndarray:
    """把每条无向边编码成单个 int64，便于快速计数。

    nverts 必须大于最大顶点索引，否则两个不同的边可能编码成同一个 key，
    把开放边误判成流形边。外部文件索引越界时这里自行收紧，而不是相信调用方。
    """
    if len(faces) == 0:
        return np.zeros(0, dtype=np.int64)
    nverts = max(int(nverts), int(np.max(faces)) + 1, 1)
    e = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    e = np.sort(e, axis=1)
    return e[:, 0].astype(np.int64) * int(nverts) + e[:, 1].astype(np.int64)


def edge_histogram(faces: np.ndarray, nverts: int) -> dict:
    """边直方图。同一张网格上水密性/非流形两项都要用，记忆化避免重复扫描。"""
    faces = np.asarray(faces)
    if len(faces) == 0:
        return {"boundary": 0, "nonmanifold": 0, "total": 0, "max_share": 0}

    def _calc() -> dict:
        keys = edge_keys(faces, nverts)
        _, counts = np.unique(keys, return_counts=True)
        return {
            "boundary": int((counts == 1).sum()),
            "nonmanifold": int((counts > 2).sum()),
            "total": int(len(counts)),
            "max_share": int(counts.max()) if len(counts) else 0,
        }

    return _memo(("edges", int(nverts)), faces, _calc)


def duplicate_face_count(faces: np.ndarray) -> int:
    """顶点集合完全相同的多余面数量（每组保留一个，其余计数）。

    只数开放边/非流形边发现不了「整张面被复制一遍」：每条边照样有两个面，
    但实际是 4 个面共享，切片器会得到两张重合的皮。必须按面去重后才能判断。
    排序 + unique 是 O(n log n)，水密性和非流形两项都要用，走记忆化。
    """
    faces = np.asarray(faces)
    if len(faces) < 2:
        return 0

    def _calc() -> int:
        return int(len(faces) - len(np.unique(np.sort(faces, axis=1), axis=0)))

    return _memo(("dup",), faces, _calc)


def tri_areas(verts: np.ndarray, faces: np.ndarray) -> np.ndarray:
    if len(faces) == 0:
        return np.zeros(0, np.float64)
    t = verts[faces]
    return 0.5 * np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1)


def signed_volume(verts: np.ndarray, faces: np.ndarray) -> float:
    if len(faces) == 0:
        return 0.0
    t = verts[faces].astype(np.float64)
    return float(np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6.0)


def winding_conflict_count(faces: np.ndarray) -> int:
    """统计「两个面沿同一条边绕序相同」的边数。

    这是比 `mesh.is_winding_consistent` 更可解释的指标：一条边上的两个面
    本应一正一反，若方向相同，说明其中一个面被翻过。补洞/镜像/布尔残渣
    都会留下这种边。
    """
    if len(faces) < 2:
        return 0
    e = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    key = np.sort(e, axis=1)
    direction = np.where(e[:, 0] < e[:, 1], 1, -1)
    _uniq, inv = np.unique(key, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    pos = np.bincount(inv[direction > 0], minlength=len(_uniq))
    neg = np.bincount(inv[direction < 0], minlength=len(_uniq))
    return int(((pos > 1) | (neg > 1)).sum())


def component_volumes(verts: np.ndarray, faces: np.ndarray) -> tuple[int, np.ndarray, np.ndarray]:
    """按边连通分量计算有向体积，返回 (分量数, 每个面的标签, 每个分量的体积)。"""
    n, labels = union_components(faces, len(verts))
    vols = np.zeros(n, dtype=np.float64)
    if n == 0:
        return 0, labels, vols
    for c in range(n):
        vols[c] = signed_volume(verts, faces[labels == c])
    return n, labels, vols


def sanitize_small_ratio(value, default: float = 0.01) -> float:
    """碎屑阈值只允许 [0, 0.5]：这是「碎屑面积 / 最大部件面积」的比例。

    检测项与修复动作必须用同一套收敛规则，否则会出现「报告说两个部件都是碎屑、
    动作只删掉一个」这种自相矛盾。旧实现里 `check_components` 直接用
    `float(params["small_ratio"])`：传 5 会把主件也判成碎屑，传 "abc" 直接抛异常。
    """
    try:
        r = float(value)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(r):
        return default
    return float(min(max(r, 0.0), 0.5))


def classify_components(verts: np.ndarray, faces: np.ndarray,
                        small_ratio: float = 0.01, labels=None) -> dict:
    """把连通分量分成 实体 / 内腔 / 碎屑 三类。

    判据不是「体积为负就是内腔」：一块翻面的小碎屑也是负体积。负体积分量
    必须位于某个正体积实体内部才算内腔；在外面的负体积分量是碎屑。
    返回的 sizes 按面积降序，供检测报告与移除碎屑动作共用，避免两处各写一套。
    """
    small_ratio = sanitize_small_ratio(small_ratio)
    verts = np.asarray(verts, dtype=np.float64)
    faces = np.asarray(faces)
    if labels is None:
        n, labels = union_components(faces, len(verts))
    else:
        labels = np.asarray(labels)
        n = int(labels.max()) + 1 if len(labels) else 0
    if n == 0:
        return {"components": 0, "sizes": [], "tiny": 0, "cavities": 0, "labels": labels}
    areas = tri_areas(verts, faces)
    sizes = []
    for c in range(n):
        m = labels == c
        sizes.append({
            "id": c,
            "faces": int(m.sum()),
            "area": float(areas[m].sum()),
            "volume": float(signed_volume(verts, faces[m])),
        })
    ext = float(max(np.ptp(verts, axis=0))) if len(verts) else 1.0
    vol_tol = max(1e-12, ext ** 3 * 1e-12)
    positive = [s for s in sizes if s["volume"] >= -vol_tol]
    base = max((s["area"] for s in positive), default=max(s["area"] for s in sizes))
    solid = None
    negatives = [s for s in sizes if s["volume"] < -vol_tol]
    if negatives and positive:
        try:
            import trimesh

            pos_ids = [s["id"] for s in positive]
            solid = trimesh.Trimesh(
                vertices=verts,
                faces=faces[np.isin(labels, pos_ids)],
                process=False,
            )
        except Exception:  # noqa: BLE001
            solid = None

    cavities = 0
    tiny = 0
    rejected_cavities = 0
    for s in sizes:
        if s["volume"] < -vol_tol:
            inside = True
            if solid is not None and len(solid.faces):
                pts = verts[faces[labels == s["id"]]].reshape(-1, 3)
                sample = np.vstack([pts.mean(axis=0), pts[:: max(1, len(pts) // 16)][:16]])
                try:
                    inside = bool(np.mean(solid.contains(sample)) >= 0.5)
                except Exception:  # noqa: BLE001
                    inside = True
            s["kind"] = "cavity" if inside else "debris"
            cavities += int(inside)
            rejected_cavities += int(not inside)
            tiny += int(not inside)
        else:
            s["kind"] = "debris" if s["area"] < base * small_ratio else "solid"
            tiny += int(s["kind"] == "debris")
    sizes.sort(key=lambda x: -x["area"])
    return {"components": n, "sizes": sizes, "tiny": tiny, "cavities": cavities,
            "rejected_cavities": rejected_cavities, "labels": labels}


def face_normals(verts: np.ndarray, faces: np.ndarray) -> np.ndarray:
    t = verts[faces]
    n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    ln = np.linalg.norm(n, axis=1)
    ln[ln == 0] = 1.0
    return n / ln[:, None]


def union_components(faces: np.ndarray, nverts: int) -> tuple[int, np.ndarray]:
    """按共享顶点把面分组，返回 (分量数, 每个面的分量标签)。

    与 `edge_keys` 一样不信任调用方传入的 nverts：外部文件里出现越界面索引时，
    邻接矩阵会按 nverts 建形状，coo_matrix 直接抛
    「axis 1 index N exceeds matrix dimension M」，整项检测连带整个报告失败。
    这里按实际最大索引收紧，越界索引只会变成孤立顶点，不会让检测崩掉。
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    if len(faces) == 0:
        return 0, np.zeros(0, np.int32)
    faces = np.asarray(faces, dtype=np.int64)
    nverts = max(int(nverts), int(np.max(faces)) + 1, 1)

    def _calc():
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import connected_components

        e = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
        e = np.unique(np.sort(e, axis=1), axis=0)
        adj = coo_matrix(
            (np.ones(len(e), np.int8), (e[:, 0], e[:, 1])), shape=(nverts, nverts)
        )
        n, labels = connected_components(adj, directed=False)
        return int(n), labels[faces[:, 0]].astype(np.int32)

    # 法线检测与孤立面片检测都要分量标签，连通分量的构建在稠密网格上是百毫秒级
    return _memo(("comp", nverts), faces, _calc)


# ---------------------------------------------------------------- 自相交
def sat_overlap(T1: np.ndarray, T2: np.ndarray, tol: float = 1e-9, parallel_tol: float = 0.05) -> np.ndarray:
    """分离轴定理判定三角形对是否相交。T1/T2 形状 (K,3,3)。

    标准 11 轴 SAT（两法线 + 9 个边叉积），但额外排除近共面的对：
    三角形是零厚度面片，共面相邻时法线方向投影必然重叠，SAT 会把
    体素重网格出来的大量相邻面误判成自交。近共面的直接判为不相交。
    """
    e1 = np.stack([T1[:, 1] - T1[:, 0], T1[:, 2] - T1[:, 1], T1[:, 0] - T1[:, 2]], axis=1)
    e2 = np.stack([T2[:, 1] - T2[:, 0], T2[:, 2] - T2[:, 1], T2[:, 0] - T2[:, 2]], axis=1)
    raw1 = np.cross(e1[:, 0], e1[:, 1])
    raw2 = np.cross(e2[:, 0], e2[:, 1])
    l1 = np.linalg.norm(raw1, axis=1)
    l2 = np.linalg.norm(raw2, axis=1)
    n1 = raw1 / np.where(l1 > 1e-12, l1, 1.0)[:, None]
    n2 = raw2 / np.where(l2 > 1e-12, l2, 1.0)[:, None]
    parallel = np.linalg.norm(np.cross(n1, n2), axis=1) < parallel_tol

    axes = [raw1, raw2]
    for i in range(3):
        for j in range(3):
            axes.append(np.cross(e1[:, i], e2[:, j]))
    overlap = np.ones(len(T1), dtype=bool)
    for a in axes:
        na = np.linalg.norm(a, axis=1)
        valid = na > 1e-12
        aa = a / np.where(valid, na, 1.0)[:, None]
        p1 = np.einsum("kij,kj->ki", T1, aa)
        p2 = np.einsum("kij,kj->ki", T2, aa)
        sep = (p1.max(1) < p2.min(1) - tol) | (p2.max(1) < p1.min(1) - tol)
        overlap &= ~(sep & valid)
    return overlap & ~parallel


def _unit(v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    ln = np.linalg.norm(v, axis=1)
    return v / np.where(ln > 1e-12, ln, 1.0)[:, None], ln


def _plane_cut_interval(tri: np.ndarray, dist: np.ndarray, axis: np.ndarray):
    """三角形与「过另一三角形平面」的交线区间在 axis 上的投影。

    tri (K,3,3)、dist (K,3) 是三个顶点到另一平面的有向距离。
    只有真正穿过平面的边（两端异号）才贡献交点；用 3 条边的交点投影
    取 min/max 就是凸三角形与该交线相交的那一段。
    """
    qs = []
    for i, j in ((0, 1), (1, 2), (2, 0)):
        si, sj = dist[:, i], dist[:, j]
        denom = si - sj
        # 顶点正好落在另一平面上时，该端点是交段的一部分，也要计入
        good = (si * sj <= 0) & (np.abs(denom) > 1e-15)
        t = np.divide(si, denom, out=np.zeros_like(si), where=np.abs(denom) > 1e-15)
        p = tri[:, i] + t[:, None] * (tri[:, j] - tri[:, i])
        q = np.einsum("ij,ij->i", p, axis)
        qs.append(np.where(good, q, np.nan))
    q = np.stack(qs, axis=1)
    cnt = np.sum(~np.isnan(q), axis=1)
    lo = np.nanmin(q, axis=1, initial=np.inf)
    hi = np.nanmax(q, axis=1, initial=-np.inf)
    return lo, hi, cnt >= 2


def _coplanar_overlap_2d(T1: np.ndarray, T2: np.ndarray, n1: np.ndarray, tol: float) -> np.ndarray:
    """共面三角形的 2D SAT：6 条边的法向都要有正长度重叠才算真重叠。

    相邻三角形只共享一条边时，沿该边法向的投影刚好接触（重叠长度为 0），
    会被排除；整张面被复制/两张面同向叠在一起时，6 个轴向都有正重叠。
    """
    e0 = T1[:, 1] - T1[:, 0]
    u, ln = _unit(e0)
    # 退化边时退回任意与法向不平行的方向
    fallback = np.tile(np.array([1.0, 0.0, 0.0]), (len(T1), 1))
    u = np.where((ln > 1e-12)[:, None], u, fallback)
    v, _ = _unit(np.cross(n1, u))
    P = np.concatenate([
        np.stack([np.einsum("kij,kj->ki", T1, u),
                  np.einsum("kij,kj->ki", T1, v)], axis=2),
        np.stack([np.einsum("kij,kj->ki", T2, u),
                  np.einsum("kij,kj->ki", T2, v)], axis=2),
    ], axis=1)  # (K,6,2)
    edges = np.stack([P[:, (i + 1) % 3] - P[:, i] for i in range(3)]
                     + [P[:, 3 + (i + 1) % 3] - P[:, 3 + i] for i in range(3)], axis=1)  # (K,6,2)
    axes = np.stack([-edges[:, :, 1], edges[:, :, 0]], axis=2)
    alen = np.linalg.norm(axes, axis=2)
    valid = alen > 1e-12
    axes = axes / np.where(valid, alen, 1.0)[:, :, None]
    # 每个点投影到每条候选轴：K × 6 点 × 6 轴
    proj = np.einsum("kij,klj->kil", P, axes)
    lo1, hi1 = proj[:, :3, :].min(axis=1), proj[:, :3, :].max(axis=1)
    lo2, hi2 = proj[:, 3:, :].min(axis=1), proj[:, 3:, :].max(axis=1)
    overlap = (np.minimum(hi1, hi2) - np.maximum(lo1, lo2)) > tol
    return np.all(overlap | ~valid, axis=1)


def tri_tri_overlap(T1: np.ndarray, T2: np.ndarray, tol: float = 1e-7) -> tuple[np.ndarray, np.ndarray]:
    """三角形对是否真正相交，返回 (hit, coplanar)。

    与旧 SAT 的差别：
    - 共面不再一律视为「相邻」而跳过。共面重叠（复制面、双层皮）是真实缺陷，
      用 2D SAT 判，只有正长度重叠才算。
    - 非共面用「两平面交线区间重叠」判，共享一个顶点的折叠面也能检出；
      仅共享一条边的邻居只接触不重叠，不会被误报。
    - 退化三角形直接跳过。
    """
    K = len(T1)
    if K == 0:
        return np.zeros(0, dtype=bool), np.zeros(0, dtype=bool)
    raw1 = np.cross(T1[:, 1] - T1[:, 0], T1[:, 2] - T1[:, 0])
    raw2 = np.cross(T2[:, 1] - T2[:, 0], T2[:, 2] - T2[:, 0])
    n1, l1 = _unit(raw1)
    n2, l2 = _unit(raw2)
    degenerate = (l1 <= 1e-12) | (l2 <= 1e-12)

    d1 = np.einsum("ij,ij->i", n1, T1[:, 0])
    d2 = np.einsum("ij,ij->i", n2, T2[:, 0])
    s2 = np.einsum("kj,kij->ki", n2, T1) - d2[:, None]   # T1 顶点到平面2
    s1 = np.einsum("kj,kij->ki", n1, T2) - d1[:, None]   # T2 顶点到平面1

    sep = ((s1 > tol).all(1) | (s1 < -tol).all(1)
           | (s2 > tol).all(1) | (s2 < -tol).all(1))
    coplanar = (np.abs(s1).max(1) <= tol) & (np.abs(s2).max(1) <= tol)

    D = np.cross(n1, n2)
    Dl = np.linalg.norm(D, axis=1)
    usable = ~(sep | degenerate)
    hit = np.zeros(K, dtype=bool)

    # 共面：2D 多边形重叠
    cop_ok = usable & coplanar
    if cop_ok.any():
        hit[cop_ok] = _coplanar_overlap_2d(T1[cop_ok], T2[cop_ok], n1[cop_ok], tol)

    # 非共面：交线方向必须存在，且两三角形与交线的交段有正长度重叠
    non = usable & ~coplanar & (Dl > 1e-12)
    if non.any():
        Dn = D[non] / Dl[non, None]
        lo1, hi1, ok1 = _plane_cut_interval(T1[non], s2[non], Dn)
        lo2, hi2, ok2 = _plane_cut_interval(T2[non], s1[non], Dn)
        over = (np.minimum(hi1, hi2) - np.maximum(lo1, lo2)) > tol
        hit[non] = ok1 & ok2 & over

    # 平行但相距超过容差：sep 已排除；平行且距离都在容差内即共面分支
    hit[degenerate] = False
    return hit, coplanar & hit


def _candidate_pairs(centers: np.ndarray, radii: np.ndarray, diag: float,
                     tree=None) -> tuple[np.ndarray, int]:
    """生成「可能相交」的三角形对，且**保证不漏**。

    判据是充分条件：两个三角形若相交，交点同时落在两者内部，于是
    |c_i - c_j| <= r_i + r_j <= 2·max(r_i, r_j)。

    旧实现用「99 分位半径 × 2.2，再用包围盒对角线 × 0.25 封顶」当统一搜索半径，
    只要有一对「大三角形 + 远处小三角形」的圆心距超过封顶值就直接漏检
    （实测：200mm 细长三角形与 60mm 外的小三角形真实穿插，检出数 0）。

    这里改成两层，代价仍接近原来的单次查询：
    - 批量层：半径 p（= min(99.9 分位, 上限/2)）内的所有对一次查出，
      覆盖「两个半径都 ≤ p」的全部对；
    - 补查层：半径 > p 的少数大三角形逐个按 2·r_i 查询，覆盖「至少一个半径 > p」的对。
    两层并集就是全部可能相交的对，没有分位数近似。
    """
    from scipy.spatial import cKDTree

    if tree is None:
        tree = cKDTree(centers)
    # 上限：超过对角线 1/4 的搜索半径在稠密网格上会让候选对爆炸，
    # 但被截掉的那部分由补查层负责，不牺牲完整性。
    cap = max(diag * 0.25, 1e-9)
    p = min(float(np.percentile(radii, 99.9)) if len(radii) else 0.0, cap * 0.5)
    p = max(p, 1e-12)
    pairs = tree.query_pairs(r=2.0 * p, output_type="ndarray")
    large = np.flatnonzero(radii > p)
    if len(large):
        extra = []
        for i in large:
            nb = tree.query_ball_point(centers[i], 2.0 * float(radii[i]))
            if not nb:
                continue
            nb = np.asarray(nb, dtype=np.int64)
            nb = nb[nb != i]
            if len(nb):
                extra.append(np.column_stack([np.full(len(nb), int(i), np.int64), nb]))
        if extra:
            e = np.sort(np.vstack(extra), axis=1)
            pairs = np.vstack([pairs, e]) if len(pairs) else e
            pairs = np.unique(pairs, axis=0)
    return pairs, int(len(large))


def find_self_intersections(verts: np.ndarray, faces: np.ndarray, max_pairs: int = 400_000) -> dict:
    """KD 树取局部候选对，再做精确三角-三角相交判定。自交在物理上都是局部的。"""
    from scipy.spatial import cKDTree

    if len(faces) < 2:
        return {"count": 0, "pairs": [], "sampled": False}
    t = verts[faces].astype(np.float64)
    centers = t.mean(axis=1)
    radii = np.linalg.norm(t - centers[:, None, :], axis=2).max(axis=1)
    diag = float(np.linalg.norm(np.ptp(verts, axis=0))) or 1.0
    tree = cKDTree(centers)
    pairs, n_large = _candidate_pairs(centers, radii, diag, tree)
    if len(pairs) == 0:
        return {"count": 0, "pairs": [], "sampled": False, "large_triangles": n_large}
    sampled = False
    candidate_pairs = int(len(pairs))

    # AABB 粗筛（先做，便宜且不需要物化 share 的 (K,3,3) 布尔矩阵）
    amin = t[pairs[:, 0]].min(axis=1)
    amax = t[pairs[:, 0]].max(axis=1)
    bmin = t[pairs[:, 1]].min(axis=1)
    bmax = t[pairs[:, 1]].max(axis=1)
    box_ok = np.all(amin <= bmax + 1e-9, axis=1) & np.all(bmin <= amax + 1e-9, axis=1)
    pairs = pairs[box_ok]
    if len(pairs) == 0:
        return {"count": 0, "pairs": [], "sampled": sampled,
                "candidate_pairs": candidate_pairs, "large_triangles": n_large}

    # 共享顶点数：3=完全重复的面；2=共边的邻居，只沿边接触不算穿插；
    # 1=共顶点（折叠面可能真穿插，要判）；0=普通候选对。
    f1, f2 = faces[pairs[:, 0]], faces[pairs[:, 1]]
    share = (f1[:, :, None] == f2[:, None, :]).any(axis=2).sum(axis=1)

    dup_pairs = pairs[share >= 3]
    # 共边邻居只沿共享边接触，是合法拓扑；共顶点（share=1）和不共顶点（share=0）才做精确判定
    test_pairs = pairs[share < 2]
    # 采样上限只作用于「真正要精确判定」的对：旧实现先按全部候选对（含大量
    # 合法共边邻居）截断，等于把采样预算浪费在必然被排除的对上，
    # 稠密网格上 40 万对里可能只有一小部分进入判定。
    if len(test_pairs) > max_pairs:
        idx = np.random.default_rng(7).choice(len(test_pairs), max_pairs, replace=False)
        test_pairs = test_pairs[idx]
        sampled = True
    scale = float(max(np.ptp(verts, axis=0))) or 1.0
    tol = max(1e-9, scale * 1e-6)
    if len(test_pairs):
        hit, cop = tri_tri_overlap(t[test_pairs[:, 0]], t[test_pairs[:, 1]], tol=tol)
    else:
        hit, cop = np.zeros(0, bool), np.zeros(0, bool)
    bad = np.concatenate([dup_pairs, test_pairs[hit]]) if len(test_pairs) else dup_pairs
    return {
        "count": int(len(bad)),
        "pairs": bad[:500].tolist(),
        "duplicates": int(len(dup_pairs)),
        "coplanar_overlaps": int(len(dup_pairs) + int(cop.sum())),
        "sampled": sampled,
        "candidate_pairs": candidate_pairs,
        "tested_pairs": int(len(test_pairs)),
        "large_triangles": n_large,
        "tolerance": tol,
        # 采样意味着 count 是下限而不是精确值，报告层必须据此调整措辞
        "count_is_lower_bound": bool(sampled),
    }


# ---------------------------------------------------------------- 壁厚
# 一次送进批量求交的射线数。太小则 numpy 调度开销占比高，太大则候选矩阵占内存。
_RAY_CHUNK = 48
# 分段查询的段数上限。射线是一条「线」，但拿去查询的却是它的「包围盒」——
# 一条斜穿模型的射线，包围盒的选择度和整个模型差不多，于是近四分之一的三角形
# 都成了候选。切成 K 段后每段盒子体积按 1/K³ 缩、候选总数按 1/K² 降，
# 代价是 K 倍的查询调用开销（实测每次约几十微秒），所以 K 必须随三角形密度走：
# 稀疏的小模型分段只会白付调用开销，密集的大模型才划得来。
_RAY_SUBSPLIT_MAX = 6


def _mt_pairs(origins: np.ndarray, dirs: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """批量 Möller–Trumbore：一次性算 P 个 (射线, 三角形) 对的交点参数 t。

    与旧的逐条版本区别：未命中返回 +inf 而不是丢弃行，这样才能按射线号
    分段归约取最小值。每条算式的操作数与逐条调用时逐一相同，
    所以命中判定与 t 的数值是逐位一致的，不会改变任何几何结论。
    """
    v0 = tris[:, 0, :]
    e1 = tris[:, 1, :] - v0
    e2 = tris[:, 2, :] - v0
    pvec = np.cross(dirs, e2)
    det = np.einsum("ij,ij->i", e1, pvec)
    ok = np.abs(det) > 1e-12
    safe = np.where(ok, det, 1.0)
    inv = np.where(ok, 1.0 / safe, 0.0)
    tvec = origins - v0
    u = np.einsum("ij,ij->i", tvec, pvec) * inv
    qvec = np.cross(tvec, e1)
    v = np.einsum("ij,ij->i", dirs, qvec) * inv
    t = np.einsum("ij,ij->i", e2, qvec) * inv
    hit = ok & (u >= -1e-9) & (u <= 1 + 1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9) & (t > 0)
    return np.where(hit, t, np.inf)


_RAY_INDEX_CACHE: dict = {}


def _raycast_nearest(verts: np.ndarray, faces: np.ndarray, origins: np.ndarray,
                     dirs: np.ndarray, eps: float,
                     max_dist: float | None = None) -> np.ndarray:
    """带 AABB 索引的最近命中距离。

    不用 trimesh 的 ray：它会对「射线数 × 三角形数」做全量 AABB 矩阵，
    6000 条射线 × 2 万面就是 1.2 亿个 float64，内存直接冲到 4.7GB、耗时 46 秒。
    这里按射线的 AABB 从索引里取候选，只对候选做求交。

    max_dist 用来截断射线：壁厚判定只关心「有没有比阈值更薄的地方」，
    射线飞 200mm 才命中的位置记为 200 和记为 12 对结论没有区别，
    但搜索盒小一个数量级，候选对跟着少一个数量级。
    """
    from rtree import index as rtree_index

    key = (id(verts), id(faces), len(faces))
    tri = verts[faces]
    cached = _RAY_INDEX_CACHE.get(key)
    # 必须同时校验两个数组的对象身份：id() 是内存地址，前一个数组被回收后地址会被复用；
    # 只校验 verts 会漏掉「verts 没变、faces 被替换」的情况，拿到旧索引后射线全落空。
    if cached is not None and cached[1] is verts and cached[2] is faces:
        idx = cached[0]
        mlo, mhi = cached[3], cached[4]
    else:
        lo = tri.min(axis=1)
        hi = tri.max(axis=1)
        mlo, mhi = lo.min(axis=0), hi.max(axis=0)
        prop = rtree_index.Property()
        prop.dimension = 3
        # 用生成器批量建索引，逐条 insert 在两万面上要好几秒
        def _stream():
            for i in range(len(tri)):
                yield (i, (lo[i, 0], lo[i, 1], lo[i, 2], hi[i, 0], hi[i, 1], hi[i, 2]), None)

        idx = rtree_index.Index(_stream(), properties=prop)
        if len(_RAY_INDEX_CACHE) > 2:
            _RAY_INDEX_CACHE.clear()
        # 存住两个数组的引用，避免它们被回收后 id 被别的数组复用
        _RAY_INDEX_CACHE[key] = (idx, verts, faces, mlo, mhi)

    n = len(origins)
    out = np.full(n, np.inf)
    if n == 0 or len(tri) == 0:
        return out

    span = float(np.linalg.norm(mhi - mlo)) * 1.2 + 1e-6
    if max_dist is not None:
        span = min(span, float(max_dist))

    # 把射线段裁到模型包围盒内。三角形整体落在包围盒内，所以命中点不可能
    # 在射线穿出包围盒之后——裁剪是**精确**的，不会漏命中，只是把查询盒
    # 从「包围盒对角线 × 1.2」缩到射线真正穿行模型那一段。
    # 逐轴求进出参数（slab 法）。方向分量可能为 0，除以 0 得 ±inf 或 nan，
    # 这里用极小值代替 0，语义上等价于「该轴上与这对平面平行且身在其中」。
    dd = np.where(np.abs(dirs) < 1e-12, 1e-12, dirs)
    ta = (mlo[None, :] - origins) / dd
    tb = (mhi[None, :] - origins) / dd
    tnear = np.minimum(ta, tb).max(axis=1)
    tfar = np.maximum(ta, tb).min(axis=1)
    # 整条射线都在盒外时 tnear > tfar，此时不可能命中任何三角形。
    # 查询范围额外留一点余量：贴在盒壁上的三角形，算出来的 tnear/tfar 会与
    # 求交算出的 t 差几个 ulp，不留余量会把正好命中盒壁的那次判成盒外。
    slack = span * 1e-6 + 1e-9
    eff = np.where(tfar >= tnear, np.minimum(span, np.maximum(tfar, 0.0) + slack), 0.0)


    # 分段数按三角形密度选：同样的模型尺寸，面数越多单个三角形越小、
    # 一段盒子里的候选越多，才值得细分。
    if len(tri) < 4000:
        k_splits = 1
    elif len(tri) > 60000:
        k_splits = _RAY_SUBSPLIT_MAX
    else:
        k_splits = 4
    # 平均三角形边长的量级，用来判断射线够不够长、值不值得分段
    tri_side = float(np.max(mhi - mlo)) / max(1.0, len(tri) ** (1.0 / 3.0))

    # 成批取候选、成批求交：逐条射线各调一次 _mt_batch 时，每次都要过一遍
    # numpy 调度开销，3000 条射线就是 3000 次；成批后算的是同一批 (射线,三角形) 对。
    for a in range(0, n, _RAY_CHUNK):
        b = min(a + _RAY_CHUNK, n)
        parts_t: list[np.ndarray] = []
        parts_r: list[np.ndarray] = []
        for k in range(a, b):
            L = eff[k]
            if L <= 0:
                continue
            o = origins[k]
            dv = dirs[k]
            # 沿射线把 [0, L] 均分成 K 段，逐段取候选。
            # 命中点必然落在其中某一段上，而三角形的包围盒包含该命中点、
            # 该段的包围盒也包含该点，两者必然相交——所以分段不会漏命中。
            # 被 max_dist 截断过的短射线，再分段纯属浪费
            ns = 1 if L < tri_side * 8 else k_splits
            ts = np.arange(ns + 1) * (L / ns)
            pts = o + dv * ts[:, None]
            blo = np.minimum(pts[:-1], pts[1:])
            bhi = np.maximum(pts[:-1], pts[1:])
            got: list[np.ndarray] = []
            for s in range(ns):
                box = (blo[s, 0], blo[s, 1], blo[s, 2], bhi[s, 0], bhi[s, 1], bhi[s, 2])
                c = np.fromiter(idx.intersection(box), dtype=np.int64, count=-1)
                if c.size:
                    got.append(c)
            if not got:
                continue
            cand = got[0] if len(got) == 1 else np.unique(np.concatenate(got))
            parts_t.append(cand)
            parts_r.append(np.full(cand.size, k, dtype=np.int64))
        if not parts_t:
            continue
        tall = np.concatenate(parts_t)
        rall = np.concatenate(parts_r)
        t = _mt_pairs(origins[rall], dirs[rall], tri[tall])
        t = np.where((t > eps) & (t <= span), t, np.inf)
        # rall 是按射线号升序拼出来的，可以直接分段归约，比 np.minimum.at 快得多
        first = np.flatnonzero(np.r_[True, rall[1:] != rall[:-1]])
        sel = rall[first]
        out[sel] = np.minimum(out[sel], np.minimum.reduceat(t, first))
    return out


def sample_wall_thickness(mesh, sample_n: int = 6000, multi_dir: int = 3,
                          max_dist: float | None = None) -> dict:
    """沿面法线反向打射线，量到对侧表面的距离。

    单方向会高估：曲面和楔形处，法线方向的穿行距离大于真实最小壁厚。
    所以再沿切平面偏转若干角度补几发射线，取最小值——偏保守，但不会漏掉薄点。
    """
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces)
    if len(faces) == 0:
        return {"samples": 0, "min": None, "median": None, "p5": None, "thin_ratio": 0.0}
    # 采样数必须收敛成合法正整数：负数会让 rng.choice 抛
    # 「negative dimensions are not allowed」，0 则什么都测不到
    try:
        sample_n = int(sample_n)
    except (TypeError, ValueError):
        sample_n = 6000
    n = min(max(1, sample_n), len(faces))
    rng = np.random.default_rng(11)
    idx = rng.choice(len(faces), n, replace=False)
    t = verts[faces[idx]]
    centers = t.mean(axis=1)
    normals = face_normals(verts, faces[idx])
    eps = max(1e-4, float(np.linalg.norm(mesh.extents)) * 1e-6)
    origins = centers - normals * eps

    dirs = [-normals]
    if multi_dir > 1:
        helper = np.where(np.abs(normals[:, 2:3]) < 0.9,
                          np.array([[0.0, 0, 1]]), np.array([[1.0, 0, 0]]))
        t1 = np.cross(normals, helper)
        t1 /= np.maximum(np.linalg.norm(t1, axis=1, keepdims=True), 1e-12)
        ang = np.radians(20.0)
        dirs.append(-np.cos(ang) * normals + np.sin(ang) * t1)
        dirs.append(-np.cos(ang) * normals - np.sin(ang) * t1)
        if multi_dir > 3:
            t2 = np.cross(normals, t1)
            dirs.append(-np.cos(ang) * normals + np.sin(ang) * t2)

    thick = np.full(n, np.inf)
    try:
        for d in dirs:
            dist = _raycast_nearest(verts, faces, origins, d, eps * 10, max_dist)
            thick = np.minimum(thick, dist)
    except Exception as exc:
        # 旧兜底用 KDTree 最近顶点距离 ×2 冒充壁厚：最近的顶点通常在同一个面上，
        # 量出来接近 0，会让健康模型误报薄壁；反过来也可能漏掉真正的薄点。
        # 这里不再编造数值，明确标记 degraded，由上层给「无法可靠测量」而不是「安全」。
        return {
            "samples": 0, "min": None, "median": None, "p5": None,
            "thin_ratio": None, "fail_ratio": None, "directions": len(dirs),
            "degraded": True, "method": "raycast",
            "error": f"{type(exc).__name__}: {exc}",
        }
    ok = thick[np.isfinite(thick) & (thick > 0)]
    if len(ok) == 0:
        # 带上「采样了多少个面」：调用方要区分「采样少所以没测到」和「确实都很厚」
        return {"samples": 0, "min": None, "median": None, "p5": None,
                "thin_ratio": 0.0, "sample_faces": int(n)}
    th = config.get().thresholds
    return {
        "samples": int(len(ok)),
        "sample_faces": int(n),
        "min": round(float(ok.min()), 3),
        "median": round(float(np.median(ok)), 3),
        "p5": round(float(np.percentile(ok, 5)), 3),
        "max": round(float(ok.max()), 3),
        "thin_ratio": round(float((ok < th.wall_warn).mean()), 4),
        "fail_ratio": round(float((ok < th.wall_fail).mean()), 4),
        "directions": len(dirs),
    }


# ---------------------------------------------------------------- 单位推断
def infer_units(extents) -> dict:
    mx = float(max(extents)) if extents is not None and len(extents) else 0.0
    if mx <= 0:
        return {"unit": "unknown", "max_dim": 0, "scale": 1.0, "message": "尺寸为零，无法推断"}
    if mx < 1.0:
        # 「最大尺寸 0.5」有两种同样合理的解释：0.5 米（按米建模的家具）和
        # 0.5 毫米（微缩件 / 珠宝件）。旧实现一律判米并建议 ×1000，
        # 微缩件会被放大成半米——比不提示更糟。这里按量级区分：
        # 小于 0.2 基本不可能是毫米件（0.2mm 以下几乎没有可打印件），判米；
        # 0.2~1.0 明确标为二义，给出两种结论，让用户自己选。
        as_mm = mx
        if as_mm >= 0.2:
            return {
                "unit": "m?",
                "max_dim": round(mx, 4),
                "scale": 1000.0,
                "ambiguous": True,
                "alternatives": [
                    {"unit": "m", "scale": 1000.0,
                     "label": f"按米建模：实际 {mx * 1000:.0f}mm"},
                    {"unit": "mm", "scale": 1.0,
                     "label": f"按毫米建模：实际 {mx:.2f}mm（微缩件）"},
                ],
                "message": (
                    f"最大尺寸 {mx:.4f} 有两种解释：按米建模是 {mx * 1000:.0f}mm，"
                    f"按毫米建模是 {mx:.2f}mm（微缩件 / 珠宝件）。"
                    "先确认建模单位再换算，不要盲目放大 1000 倍。"
                ),
            }
        return {
            "unit": "m",
            "max_dim": round(mx, 4),
            "scale": 1000.0,
            "ambiguous": False,
            "message": f"最大尺寸 {mx:.4f}，量级对应米。建议放大 1000 倍转为毫米。",
        }
    if mx <= 400.0:
        # 254mm = 10 英寸、203.2mm = 8 英寸这类整数值很常见（从英制 CAD 转过来），
        # 数值已经是毫米但比例可能不对，给个提示而不是直接判「没问题」
        as_in = mx / 25.4
        nearest = round(as_in)
        if 1 <= nearest <= 15 and abs(as_in - nearest) / as_in < 0.002:
            return {
                "unit": "mm?",
                "max_dim": round(mx, 2),
                "scale": 1.0,
                "ambiguous": True,
                "message": (
                    f"最大尺寸 {mx:.1f}mm，恰好等于 {nearest} 英寸（{as_in:.2f}）。"
                    f"数值本身是毫米无需换算，但如果原模型是按英寸设计的等比件，实际尺寸应为 {mx * 25.4:.0f}mm。"
                ),
            }
        return {
            "unit": "mm",
            "max_dim": round(mx, 2),
            "scale": 1.0,
            "ambiguous": False,
            "message": f"最大尺寸 {mx:.1f}mm，在绝大多数桌面机成型范围内，无需换算。",
        }
    if mx <= 1000.0:
        return {
            "unit": "mm",
            "max_dim": round(mx, 2),
            "scale": 1.0,
            "ambiguous": False,
            "message": f"最大尺寸 {mx:.1f}mm，偏大。请确认是否超出你的成型空间。",
        }
    # 超过 1 米：要么是 cm/inch 误存，要么真的是超大件
    as_in = mx / 25.4
    nearest = round(as_in)
    if nearest >= 1 and abs(as_in - nearest) / as_in < 0.002:
        return {
            "unit": "inch",
            "max_dim": round(mx, 2),
            "scale": 1 / 25.4,
            "ambiguous": False,
            "message": f"最大尺寸 {mx:.1f} 恰好是 {nearest} 英寸（{as_in:.2f}），判定为英寸建模，应缩小 25.4 倍。",
        }
    if mx <= 5000.0:
        return {
            "unit": "cm?",
            "max_dim": round(mx, 2),
            "scale": 0.1,
            "ambiguous": True,
            "message": f"最大尺寸 {mx:.0f}，超出常规毫米件范围。若原模型按厘米设计（约 {mx / 10:.0f}cm），应缩小 10 倍。",
        }
    return {
        "unit": "oversize",
        "max_dim": round(mx, 2),
        "scale": 0.001,
        "ambiguous": False,
        "message": f"最大尺寸 {mx:.0f}，远超打印范围，可能是微米单位或数值异常。请核对原始 CAD 单位。",
    }


# ---------------------------------------------------------------- 检测项
def _item(id_, name, severity, message, value=None, fix=None, details=None) -> dict:
    return {
        "id": id_,
        "name": name,
        "severity": severity,
        "message": message,
        "value": value or {},
        "fix": fix,
        "details": details or {},
    }


def check_watertight(mesh, params, ctx=None) -> dict:
    verts, faces = np.asarray(mesh.vertices), np.asarray(mesh.faces)
    h = _mesh_memo(mesh, "edges", lambda: edge_histogram(faces, len(verts)))
    dup = _mesh_memo(mesh, "dup", lambda: duplicate_face_count(faces))
    if h["boundary"] == 0 and h["nonmanifold"] == 0 and dup == 0:
        return _item("watertight", "水密性", "ok", "所有边都被恰好两个面共享，模型闭合，可直接布尔运算与切片。",
                     {"boundary_edges": 0})
    if h["boundary"] > 0:
        msg = f"存在 {h['boundary']} 条开放边，模型不闭合。切片软件无法判断内外，可能打出空壳或直接报错。"
        if h["nonmanifold"]:
            msg += f" 另有 {h['nonmanifold']} 条非流形边。"
    elif h["nonmanifold"] > 0:
        msg = (f"没有开放边，但有 {h['nonmanifold']} 条边被 3 个及以上面共享。"
               f"模型看似闭合，实际是重叠面/非流形拓扑，切片和布尔运算会在这里产生歧义。")
    else:
        msg = f"检测到 {dup} 个重复面（同一组顶点被多次使用）。模型看似闭合，实际有两层重合的皮。"
    return _item(
        "watertight", "水密性", "error", msg,
        {"boundary_edges": h["boundary"], "nonmanifold_edges": h["nonmanifold"],
         "duplicate_faces": dup},
        fix={"action": "fill_holes", "label": "一键补洞并清理重叠面"},
    )


def check_normals(mesh, params, ctx=None) -> dict:
    """法线检测：逐连通体看有向体积，不能只看总和。

    一个实体朝外、另一个整体朝内时，两者有向体积正负抵消，总和可能接近 0
    甚至为正，旧逻辑会判「正常」。这类模型切片后其中一件会变成空腔。
    """
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces)
    vol = signed_volume(verts, faces)
    areas = tri_areas(verts, faces)
    degenerate = int((areas < 1e-12).sum())
    ext = float(max(np.ptp(verts, axis=0))) if len(verts) else 1.0
    vol_tol = max(1e-9, ext ** 3 * 1e-9)
    n_comp, labels, vols = _mesh_memo(
        mesh, "component_volumes", lambda: component_volumes(verts, faces))
    _mesh_memo(mesh, "labels", lambda: (n_comp, labels))
    inverted = [c for c in range(n_comp) if vols[c] < -vol_tol]
    conflicts = winding_conflict_count(faces)
    msgs, sev, fixes = [], "ok", None
    if inverted:
        sev = "error"
        msgs.append(f"{len(inverted)} 个部件的法线整体朝内（有向体积为负）——切片软件会把它们当成空腔。")
        fixes = {"action": "fix_normals", "label": "一键统一法线"}
    if conflicts:
        if sev == "ok":
            sev = "error" if conflicts > max(3, len(faces) * 0.01) else "warning"
        msgs.append(f"有 {conflicts} 条边两侧的面绕序相同，法线方向不一致。")
        fixes = {"action": "fix_normals", "label": "一键统一法线"}
    if degenerate:
        if sev == "ok":
            sev = "warning"
        msgs.append(f"有 {degenerate} 个零面积退化面，会产生无效法线。")
        fixes = fixes or {"action": "merge_vertices", "label": "清理退化面"}
    if not msgs:
        msgs.append("法线朝外且方向一致，有向体积为正。")
    return _item(
        "normals", "法线方向", sev, " ".join(msgs),
        {"signed_volume": round(vol, 3), "degenerate_faces": degenerate,
         "components": int(n_comp), "inverted_components": len(inverted),
         "winding_conflicts": int(conflicts),
         "winding_consistent": bool(getattr(mesh, "is_winding_consistent", False))},
        fix=fixes,
    )


def check_thickness(mesh, params, ctx=None) -> dict:
    """壁厚检测，厚模型走快速通道。

    壁厚采样是全流程最慢的一项（每条射线都要遍历候选三角形）。
    先抽 600 面粗测，如果最薄处都在告警线两倍以上，就没必要再跑全量——
    实测这一步让厚模型省掉约 90% 的检测时间。
    """
    try:
        n = int(params.get("sample_faces", 1500))
    except (TypeError, ValueError):
        n = 1500
    n = max(1, min(n, 200_000))
    th = config.get().thresholds
    limit = th.wall_warn * 2
    # 粗筛只需要回答「有没有比告警线两倍更薄的地方」，所以射线直接截断到该距离。
    # 截断后每条射线的候选三角形骤减，32 万面模型从 15.8s 降到 0.6s。
    quick = sample_wall_thickness(mesh, min(600, n), multi_dir=1, max_dist=limit)
    if quick.get("degraded"):
        return _item("thickness", "壁厚", "info",
                     f"壁厚检测降级：射线求交失败（{quick.get('error')}），无法给出可靠厚度。"
                     "请先修复网格缺陷再复检。", quick)
    if quick["samples"] == 0:
        quick["mode"] = "coarse"
        quick["min"] = round(limit, 2)
        quick["lower_bound"] = True
        total_faces = max(1, len(mesh.faces))
        sampled = int(quick.get("sample_faces") or min(600, n))
        # 空网格（0 面）时 sampled/1 会算出 600 这种荒谬覆盖率，封顶到 1
        coverage = min(1.0, sampled / total_faces)
        quick["coverage"] = round(coverage, 5)
        quick["sampled_faces"] = sampled
        # 覆盖率高就说「安全」，覆盖率低只能说「粗筛没发现」——
        # 20 万面模型只测 600 面（0.3%）时，一块 0.1% 面积的局部薄壁有一半概率被漏掉，
        # 这时候把结论写成「整件厚度安全」就是在替用户下不该下的判断。
        if coverage >= 0.02:
            tail = "整件厚度安全，未做全量扫描。"
        elif coverage >= 0.005:
            tail = f"未做全量扫描；本次只覆盖 {coverage * 100:.1f}% 的表面，局部薄壁仍可能漏检。"
        else:
            tail = (f"仅覆盖 {coverage * 100:.2f}% 的表面，局部薄壁漏检风险较高；"
                    "需要确切结论请把壁厚采样面数调大后重测。")
        return _item("thickness", "壁厚", "ok",
                     f"粗筛未发现低于 {limit:.1f}mm 的薄壁（{sampled} 面采样）。{tail}",
                     quick)
    # 全量阶段截断射线：只要判定「比告警线厚」就够了，不必扫穿整个模型
    # 全量阶段同样截断：超过 2mm 都算安全，没必要扫穿整个模型
    res = sample_wall_thickness(mesh, n, multi_dir=3, max_dist=max(2.0, th.wall_warn * 2.5))
    res["mode"] = "full"
    if res.get("degraded"):
        return _item("thickness", "壁厚", "info",
                     f"壁厚全量检测降级：射线求交失败（{res.get('error')}），无法给出可靠厚度。"
                     "请先修复网格缺陷再复检。", res)
    if res["samples"] == 0:
        # 全量采样一条射线都没命中，但粗筛可能已经量到薄点。旧逻辑无条件判 ok，
        # 会把「粗筛 600 面里 0.3mm」的薄壁直接放过。以粗筛实测值为准，并标注来源。
        quick["mode"] = "coarse_confirm"
        mn_q = quick.get("min")
        if mn_q is not None and not quick.get("lower_bound"):
            quick["lower_bound"] = False
            if mn_q < th.wall_fail:
                return _item("thickness", "壁厚", "error",
                             f"全量采样未命中，但粗筛测得最薄 {mn_q:.2f}mm（600 面），低于 {th.wall_fail}mm 打印下限。",
                             quick, fix={"action": "thicken", "label": "一键加厚到安全值"})
            if mn_q < th.wall_warn:
                return _item("thickness", "壁厚", "warning",
                             f"全量采样未命中，但粗筛测得最薄 {mn_q:.2f}mm（600 面），偏薄。",
                             quick, fix={"action": "thicken", "label": "一键加厚到安全值"})
        # 粗筛也没命中：只能确定「采样范围内没有低于截断距离的薄壁」
        quick["lower_bound"] = True
        return _item("thickness", "壁厚", "ok",
                     f"最薄不小于 {limit:.1f}mm（粗筛 600 面未发现更薄处），整件厚度安全，未做全量扫描。",
                     quick)
    mn = res["min"]
    if mn < th.wall_fail:
        sev, msg = "error", f"最薄处仅 {mn:.2f}mm（中位 {res['median']:.2f}mm），低于 {th.wall_fail}mm 的打印下限，成型后会断裂或缺失。"
    elif mn < th.wall_warn:
        sev, msg = "warning", f"最薄处 {mn:.2f}mm（中位 {res['median']:.2f}mm），偏薄，清洗和二次固化时容易压塌。"
    else:
        sev, msg = "ok", f"最薄 {mn:.2f}mm，中位 {res['median']:.2f}mm，厚度安全。"
    return _item("thickness", "壁厚", sev, msg, res,
                 fix={"action": "thicken", "label": "一键加厚到安全值"} if sev != "ok" else None)


def check_nonmanifold(mesh, params, ctx=None) -> dict:
    verts, faces = np.asarray(mesh.vertices), np.asarray(mesh.faces)
    # 与水密性检测共用同一份边直方图/重复面计数
    h = _mesh_memo(mesh, "edges", lambda: edge_histogram(faces, len(verts)))
    dup = _mesh_memo(mesh, "dup", lambda: duplicate_face_count(faces))
    if h["nonmanifold"] == 0 and dup == 0:
        return _item("nonmanifold", "非流形边", "ok", "没有一条边被超过两个面共享。", {"count": 0})
    parts = []
    if h["nonmanifold"]:
        parts.append(f"{h['nonmanifold']} 条边被 3 个及以上面共享")
    if dup:
        parts.append(f"{dup} 个面与其它面顶点完全相同（重复面）")
    return _item(
        "nonmanifold", "非流形边", "error",
        "；".join(parts) + "。布尔运算和切片会在这里产生歧义，常见于两块面片叠在一起或被复制了一遍。",
        {"count": h["nonmanifold"], "duplicate_faces": dup},
        fix={"action": "fill_holes", "label": "清理非流形与重复面"},
    )


def check_self_intersection(mesh, params, ctx=None) -> dict:
    if not params.get("check_self_intersection", True):
        return _item("self_intersection", "自相交", "info", "本次已跳过自相交检测。", {"skipped": True})
    verts, faces = np.asarray(mesh.vertices), np.asarray(mesh.faces)
    t0 = time.time()
    res = find_self_intersections(verts, faces)
    res["elapsed"] = round(time.time() - t0, 2)
    if res["count"] == 0:
        return _item("self_intersection", "自相交", "ok",
                     f"未发现三角形互相穿插（检查 {res.get('candidate_pairs', 0)} 组候选对）。", res)
    return _item(
        "self_intersection", "自相交", "warning",
        f"发现 {res['count']} 处三角形穿插。切片时这些区域会生成错误的截面，表现为模型表面出现多余薄片或孔洞。",
        res,
        fix={"action": "resolve_self_intersection", "label": "体素重网格消解"},
    )


def check_components(mesh, params, ctx=None) -> dict:
    """连通分量检测。碎屑阈值按「面积占比」而不是把 mm³ 当 mm² 用。

    旧实现拿 `tiny_volume`（mm³）直接和三角形面积和（mm²）比，量纲不一致：
    一个 0.5mm³ 的碎屑只要是 1mm² 的薄片就会被判正常，而一个 3mm² 的小零件
    反而可能被当成碎屑。这里改为与主部件面积占比，并保留体积做辅助判据。
    """
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces)
    ratio = sanitize_small_ratio(params.get("small_ratio", 0.01))
    # 复用「法线」检测已经算好的连通分量标签，省一次 connected_components
    _n, labels = _mesh_memo(mesh, "labels", lambda: union_components(faces, len(verts)))
    info = classify_components(verts, faces, ratio, labels=labels)
    n = info["components"]
    if n <= 1:
        return _item("components", "孤立面片", "ok", "模型是单一连通体。", {"components": n})
    sizes = info["sizes"]
    total_area = max(1e-9, sum(s["area"] for s in sizes))
    tiny = info["tiny"]
    cavities = info["cavities"]
    sev = "warning" if tiny else "info"
    msg = (f"模型由 {n} 个互不连通的部件组成，最大部件占 "
           f"{sizes[0]['area'] / total_area * 100:.1f}% 表面积。")
    if cavities:
        msg += f" 其中 {cavities} 个是实体内部的内腔，不会被当成碎屑。"
    if tiny:
        # 阈值是可配置的，文案不能写死 1%
        msg += (f" 其中 {tiny} 个是碎屑（面积不足主件的 {ratio * 100:g}%，"
                "或负体积但不在实体内），建议清理。")
    clean_sizes = [{k: v for k, v in s.items()} for s in sizes[:8]]
    return _item("components", "孤立面片", sev, msg,
                 {"components": n, "sizes": clean_sizes, "tiny": tiny, "cavities": cavities,
                  "small_ratio": ratio},
                 fix={"action": "remove_small", "label": "移除碎屑"} if tiny else None)


def check_units(mesh, params, ctx=None) -> dict:
    ext = getattr(mesh, "extents", None)
    res = infer_units(ext)
    sev = "ok" if res["unit"] == "mm" else ("error" if res["unit"] == "oversize" else "warning")
    if res["unit"].endswith("?") or res.get("ambiguous"):
        sev = "info"
    # 二义时不给「一键 ×1000」的强建议：微缩件被放大 1000 倍比不提示更糟。
    # 只在结论明确（或用户可从候选中选）时才带修复动作。
    fix = None
    if abs(res["scale"] - 1) > 1e-9:
        if res.get("ambiguous"):
            fix = {"action": "rescale", "label": f"按 ×{res['scale']:.4g} 换算（需先确认单位）",
                   "factor": res["scale"], "needs_confirmation": True}
        else:
            fix = {"action": "rescale", "label": f"按 ×{res['scale']:.4g} 换算", "factor": res["scale"]}
    return _item("units", "单位与尺寸", sev, res["message"], res, fix=fix)


# ---------------------------------------------------------------- 热力图场
def _face_to_vertex(verts: np.ndarray, faces: np.ndarray, face_val: np.ndarray,
                    mode: str = "mean") -> np.ndarray:
    """把逐面数值摊到顶点上，前端才能用顶点色渲染。

    壁厚必须用 min 聚合：薄板的侧面射线会横穿整个板宽（几十毫米），
    平均或距离加权会把顶面的 0.6mm 冲淡成二十几毫米，热力图就全绿了。
    """
    rep = np.repeat(face_val.astype(np.float64), 3)
    if mode == "min":
        out = np.full(len(verts), np.inf)
        np.minimum.at(out, faces.ravel(), rep)
        out[~np.isfinite(out)] = 0.0
        return out
    acc = np.zeros(len(verts), dtype=np.float64)
    cnt = np.zeros(len(verts), dtype=np.float64)
    np.add.at(acc, faces.ravel(), rep)
    np.add.at(cnt, faces.ravel(), 1.0)
    return acc / np.maximum(cnt, 1.0)


def thickness_field(mesh, samples: int = 3000, max_dist: float | None = None) -> dict:
    """逐面壁厚场：稀疏采样测厚 + KNN 插值铺满全网格。

    逐面都打射线在十万面上要几分钟，所以采 3000 个面测，
    再用距离反比插值——热力图只需要看出「哪块薄」，不需要每面精确。
    """
    from scipy.spatial import cKDTree

    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces)
    if len(faces) == 0:
        return {"values": [], "min": None, "max": None, "p5": None}
    all_centers = verts[faces].mean(axis=1)
    n = min(samples, len(faces))
    rng = np.random.default_rng(17)
    idx = rng.choice(len(faces), n, replace=False)
    centers = verts[faces[idx]].mean(axis=1)
    normals = face_normals(verts, faces[idx])
    eps = max(1e-4, float(np.linalg.norm(mesh.extents)) * 1e-6)
    # 热力图要能看出「整件都厚」，所以默认不截断射线；薄壁件射线本来就短，不会慢
    dist = _raycast_nearest(verts, faces, centers - normals * eps, -normals, eps * 10, max_dist)
    # 贴着相邻面命中的亚微米值不是真实壁厚，是射线起点偏移不足造成的数值伪影
    good = np.isfinite(dist) & (dist > 0.05)
    if not good.any():
        return {"values": [0.0] * len(verts), "min": None, "max": None, "p5": None}
    fill = float(np.median(dist[good]))
    dist = np.where(good, dist, fill)

    tree = cKDTree(centers)
    k = min(4, len(centers))
    d, ind = tree.query(all_centers, k=k)
    if k == 1:
        face_val = dist[ind]
    else:
        w = 1.0 / np.maximum(d, 1e-6)
        w /= w.sum(axis=1, keepdims=True)
        face_val = (w * dist[ind]).sum(axis=1)
    vert_val = _face_to_vertex(verts, faces, face_val, mode="min")
    return {
        "values": [round(float(x), 4) for x in vert_val],
        "min": round(float(vert_val.min()), 3),
        "max": round(float(vert_val.max()), 3),
        "p5": round(float(np.percentile(vert_val, 5)), 3),
        "unit": "mm",
        "direction": "lower_is_worse",
    }


def overhang_field(mesh, threshold_deg: float = 45.0) -> dict:
    """逐面悬垂角场：面法线与竖直向下的夹角，0° 完全朝下、90° 竖直、180° 朝上。"""
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces)
    if len(faces) == 0:
        return {"values": [], "min": None, "max": None}
    n = face_normals(verts, faces)
    ang = np.degrees(np.arccos(np.clip(-n[:, 2], -1.0, 1.0)))
    # 贴平台的底面本来就有台面承接，标成 180（安全色）避免满屏红
    centers_z = verts[faces].mean(axis=1)[:, 2]
    zmin = float(verts[:, 2].min())
    ang = np.where(centers_z < zmin + 0.5, 180.0, ang)
    # 用 min 聚合：顶点取相邻面里最危险的那个角，边缘才不会被安全面冲淡
    vert_val = _face_to_vertex(verts, faces, ang, mode="min")
    return {
        "values": [round(float(x), 2) for x in vert_val],
        "min": round(float(vert_val.min()), 2),
        "max": round(float(vert_val.max()), 2),
        "threshold": float(threshold_deg),
        "unit": "°",
        "direction": "lower_is_worse",
    }


def quality_score(report: dict) -> dict:
    """把七项检测折算成 0–100 分和等级。

    权重按「对打印成败的影响」分配：水密和壁厚最致命，单位错了整件比例都不对，
    但单位问题在切片前就能看出来，所以权重低一些。
    """
    items = {i.get("id"): i for i in (report.get("items") or [])}
    parts: list[dict] = []

    # 空模型闸门：一个 0 面的文件，七项检测项项都「没有问题」——
    # 没有边可断、没有面可反、没有自交、没有碎屑——于是被算成 A 级「可以直接打印」，
    # 打印一个根本不存在的件。这属于「给出错误的安全结论」，比直接报错危险得多，
    # 所以必须在打分之前单独拦下来。
    n_faces = int((report.get("stats") or {}).get("faces") or 0)
    if n_faces == 0:
        return {
            "score": 0.0, "grade": "D", "verdict": "模型没有任何三角面，无法打印",
            "parts": [{"name": "几何存在性", "score": 0.0, "max": 100,
                       "note": "0 个三角面，其余各项无意义"}],
            "max": 100, "empty": True,
        }

    def add(name: str, got: float, full: int, note: str) -> None:
        parts.append({"name": name, "score": round(max(0.0, min(float(full), got)), 1),
                      "max": full, "note": note})

    it = items.get("watertight") or {}
    v = it.get("value") or {}
    b = int(v.get("boundary_edges") or 0)
    nm = int(v.get("nonmanifold_edges") or 0)
    dup = int(v.get("duplicate_faces") or 0)
    add("水密性", 25 if (b == 0 and nm == 0 and dup == 0) else max(0, 25 - min(25, b * 2 + nm * 3 + dup * 3)),
        25, f"开放边 {b}，非流形 {nm}，重复面 {dup}")

    it = items.get("normals") or {}
    v = it.get("value") or {}
    vol = float(v.get("signed_volume") or 0)
    deg = int(v.get("degenerate_faces") or 0)
    inv = int(v.get("inverted_components") or 0)
    wc = int(v.get("winding_conflicts") or 0)
    n_score = 15.0
    if inv:
        n_score -= min(12, 6 * inv)
    if wc:
        n_score -= min(8, 2 + wc * 0.5)
    if vol <= 0:
        n_score -= 3
    if deg:
        n_score -= min(3, deg * 0.5)
    add("法线", max(0.0, n_score), 15,
        f"朝内部件 {inv}，绕序冲突 {wc}，退化面 {deg}")

    it = items.get("thickness") or {}
    v = it.get("value") or {}
    mn = v.get("min")
    degraded: list[str] = []
    caveats: list[str] = []
    if mn is None:
        t_score = 0.0
        note = "未能测量（射线求交失败或全部未命中）"
        degraded.append("壁厚未能测量")
    elif v.get("degraded"):
        t_score = 0.0
        note = "测量降级，数值不可信"
        degraded.append("壁厚测量降级")
    elif v.get("lower_bound"):
        # 粗筛只说明「采样到的面都不薄」，覆盖面越小这个结论越弱。
        # 旧实现无条件给满分：20 万面模型只测 600 面（0.3%）也拿 20/20。
        cov = float(v.get("coverage") or 0.0)
        if cov >= 0.02:
            t_score = 20.0
        elif cov >= 0.005:
            t_score = 16.0
        else:
            t_score = 12.0
        note = f"最薄 ≥ {float(mn):.2f}mm（粗筛，覆盖 {cov * 100:.2f}% 表面）"
        if cov < 0.005:
            caveats.append(f"壁厚只做了粗筛（覆盖 {cov * 100:.2f}% 表面），局部薄壁可能漏检")
    else:
        mn = float(mn)
        t_score = 20 if mn >= 1.5 else 14 if mn >= 0.8 else 7 if mn >= 0.4 else 0
        note = f"最薄 {mn:.2f}mm"
    add("壁厚", t_score, 20, note)

    it = items.get("nonmanifold") or {}
    v = it.get("value") or {}
    c = int(v.get("count") or 0)
    dup = int(v.get("duplicate_faces") or 0)
    add("流形性", max(0, 15 - min(15, c + dup * 3)), 15, f"非流形边 {c}，重复面 {dup}")

    it = items.get("self_intersection") or {}
    si_val = it.get("value") or {}
    c = int(si_val.get("count") or 0)
    si_note = f"穿插 {c} 处"
    if si_val.get("count_is_lower_bound") or si_val.get("sampled"):
        # 采样过就是下限，不能说「只有 N 处」
        si_note = f"穿插 ≥ {c} 处（候选对超上限，已采样）"
        degraded.append("自交检测已采样，结果为下限")
    if si_val.get("error"):
        si_note = "检测失败，结果不可信"
        degraded.append("自交检测失败")
    add("自交", 10 if c == 0 else max(0, 10 - min(10, c / 5)), 10, si_note)

    it = items.get("components") or {}
    n = int((it.get("value") or {}).get("components") or 1)
    add("连通性", 10 if n == 1 else max(0, 10 - (n - 1) * 3), 10, f"{n} 个部件")

    it = items.get("units") or {}
    u = str((it.get("value") or {}).get("unit") or "unknown")
    add("单位", 5 if u == "mm" else (3 if u.endswith("?") else 1), 5, u)

    # 任何一项检测失败/降级，都不允许给出「可以直接打印」的结论。
    # 之前壁厚测不出来只扣 8 分，总分 92 仍判 A，等于用「没测到」冒充「没问题」。
    failed = [i for i in (report.get("items") or []) if (i.get("value") or {}).get("error")]
    for i in failed:
        degraded.append(f"{i.get('name')} 检测失败")

    total = sum(p["score"] for p in parts)
    grade = "A" if total >= 90 else "B" if total >= 75 else "C" if total >= 60 else "D"
    if degraded and grade in ("A", "B"):
        # 上限降到 C：不确定就不给「可以打印」的结论，但也不假装几何有多差
        grade = "C"
    verdict = {
        "A": "可以直接打印",
        "B": "小问题，建议修一下再打",
        "C": "有明显缺陷，修完再打成功率更高" if not degraded
             else "有项目未能确认（" + "、".join(degraded[:3]) + "），建议先补测再打",
        "D": "问题严重，直接打大概率失败",
    }[grade]
    return {"score": round(total, 1), "grade": grade, "verdict": verdict,
            "parts": parts, "max": 100,
            "degraded": degraded, "caveats": caveats,
            "confidence": "low" if degraded else "normal"}


def component_field(mesh) -> dict:
    """把连通分量编号摊到顶点上，前端用调色板分色——一眼看出模型由几块拼成。"""
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces)
    if len(faces) == 0:
        return {"values": [], "components": 0}
    n, labels = union_components(faces, len(verts))
    vert_val = _face_to_vertex(verts, faces, labels.astype(np.float64), mode="mean")
    areas = tri_areas(verts, faces)
    sizes = sorted(
        [{"id": c, "area": round(float(areas[labels == c].sum()), 1)} for c in range(n)],
        key=lambda x: -x["area"],
    )
    return {
        "values": [round(float(x), 3) for x in vert_val],
        "components": int(n),
        "sizes": sizes[:12],
        "unit": "",
        "direction": "categorical",
    }


CHECKS = [
    check_watertight,
    check_normals,
    check_thickness,
    check_nonmanifold,
    check_self_intersection,
    check_components,
    check_units,
]

# 检测项 id / 显示名，供异常兜底时生成占位结论（顺序与 CHECKS 一一对应）
CHECK_IDS = {
    "check_watertight": "watertight",
    "check_normals": "normals",
    "check_thickness": "thickness",
    "check_nonmanifold": "nonmanifold",
    "check_self_intersection": "self_intersection",
    "check_components": "components",
    "check_units": "units",
}
CHECK_LABELS = {
    "check_watertight": "水密性",
    "check_normals": "法线",
    "check_thickness": "壁厚",
    "check_nonmanifold": "非流形边",
    "check_self_intersection": "自相交",
    "check_components": "孤立面片",
    "check_units": "单位与尺寸",
}


# ---------------------------------------------------------------- 任务
def summarize(items: list[dict]) -> dict:
    counts = {"ok": 0, "info": 0, "warning": 0, "error": 0}
    for it in items:
        counts[it["severity"]] = counts.get(it["severity"], 0) + 1
    worst = max((it["severity"] for it in items), key=lambda s: SEV_ORDER[s], default="ok")
    actionable = [it["id"] for it in items if it.get("fix")]
    # 检测失败/降级要和「真的没问题」区分开：前端据此显示「有项目未确认」
    degraded = [it["id"] for it in items
                if (it.get("value") or {}).get("degraded") or (it.get("value") or {}).get("error")]
    return {
        "total": len(items),
        "counts": counts,
        "worst": worst,
        "actionable": actionable,
        "blocking": counts["error"] > 0,
        "degraded": degraded,
        "confidence": "low" if degraded else "normal",
    }


def run_job(ctx, params):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    pid = ctx.job.project_id
    proj = store.get(pid)
    if not proj:
        raise ValueError("项目不存在")
    path = Path(proj.get("current") or proj["source"])
    mtime = path.stat().st_mtime_ns

    ctx.tick(0.02, "读取模型文件", "load")
    mesh = meshio.load_mesh(path)
    stats = meshio.mesh_stats(mesh)
    ctx.complete_step("load", {"faces": stats["faces"], "mtime": mtime})

    # 断点续跑：已算过且文件没变过的项直接复用
    items: list[dict | None] = [None] * len(CHECKS)
    todo: list[int] = []
    for i, fn in enumerate(CHECKS):
        rec = ctx.job.steps.get(fn.__name__)
        if rec and rec.get("status") == "done" and rec.get("data", {}).get("_mtime") == mtime:
            items[i] = rec["data"]["item"]
        else:
            todo.append(i)

    def work(i: int) -> tuple[int, dict]:
        return i, _safe_check(CHECKS[i], mesh, params)

    # 七项检测彼此独立，线程池并行——耗时从「七项之和」压到「最慢的那项」
    done = len(CHECKS) - len(todo)
    if todo:
        workers = min(4, len(todo))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chk") as ex:
            futs = [ex.submit(work, i) for i in todo]
            for fut in as_completed(futs):
                i, it = fut.result()
                items[i] = it
                ctx.complete_step(CHECKS[i].__name__, {"_mtime": mtime, "item": it})
                done += 1
                ctx.tick(0.05 + 0.9 * done / len(CHECKS), f"完成 {done}/{len(CHECKS)} 项", "analyze")

    final_items = [it for it in items if it is not None]
    summary = summarize(final_items)
    report = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "file": path.name,
        "stats": stats,
        "items": final_items,
        "summary": summary,
        "quality": quality_score({"items": final_items, "stats": stats}),
        "failed_checks": [i.get("id") for i in final_items if (i.get("value") or {}).get("error")],
    }
    store.update(pid, analysis=report)
    ctx.tick(1.0, "检测完成", "done")
    return {"report": report}


def _safe_check(fn, mesh, params: dict) -> dict:
    """单项检测异常不得拖垮整份报告。

    七项检测里任何一项抛异常（外部参数非法、几何极端、第三方库边界），
    旧实现会让整个任务 failed：用户拿不到其余六项结论，也看不到到底哪项坏了。
    这里把异常收成一条 info 级结论，并在 value 里留 error 字段，
    质量评分据此把等级封顶（见 quality_score）。
    """
    t0 = time.time()
    try:
        it = fn(mesh, params, None)
        it["elapsed"] = round(time.time() - t0, 3)
        return it
    except Exception as e:  # noqa: BLE001
        name = CHECK_LABELS.get(fn.__name__, fn.__name__)
        it = _item(
            CHECK_IDS.get(fn.__name__, fn.__name__), name, "info",
            f"这项检测没能完成（{type(e).__name__}: {str(e)[:120]}），"
            "其余检测不受影响；该项目的结论需要人工确认。",
            {"error": f"{type(e).__name__}: {str(e)[:200]}", "degraded": True},
        )
        it["elapsed"] = round(time.time() - t0, 3)
        return it


def analyze_mesh(mesh, path, params: dict | None = None) -> dict:
    """对一个内存里的网格跑全套检测，返回报告（不落盘、不依赖任务上下文）。

    修复完成后要立刻复检，用这个即可——不必再起一个检测任务，也避免
    「修完还拿着修复前的旧报告报警」这种状态不同步。
    """
    from concurrent.futures import ThreadPoolExecutor

    params = params or {}
    stats = meshio.mesh_stats(mesh)
    # 七项检测彼此独立，线程池并行，耗时压到「最慢那一项」
    workers = min(4, len(CHECKS))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rechk") as ex:
        items = list(ex.map(lambda fn: _safe_check(fn, mesh, params), CHECKS))
    summary = summarize(items)
    return {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "file": Path(path).name,
        "stats": stats,
        "items": items,
        "summary": summary,
        "quality": quality_score({"items": items, "stats": stats}),
        "failed_checks": [i.get("id") for i in items if (i.get("value") or {}).get("error")],
    }
