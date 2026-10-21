"""模型修补与补洞的回归测试。

每条用例都对应一个真实缺陷模式，断言的是**可证伪的几何量**：
开放边数、非流形边数、重复面数、有向体积、水密性，而不是「函数没抛异常」。
"""
from __future__ import annotations

import sys

import numpy as np
import pytest
import trimesh

from app import analysis, repair, voxelops


# ---------------------------------------------------------------- 夹具
def box(size=10.0) -> trimesh.Trimesh:
    return trimesh.creation.box(extents=[size, size, size])


def sphere(sub=3, radius=25.0) -> trimesh.Trimesh:
    return trimesh.creation.icosphere(subdivisions=sub, radius=radius)


def drop_faces(mesh: trimesh.Trimesh, idx) -> trimesh.Trimesh:
    keep = np.ones(len(mesh.faces), dtype=bool)
    keep[np.asarray(idx, dtype=int)] = False
    mesh.update_faces(keep)
    mesh.remove_unreferenced_vertices()
    return mesh


def concatenate(*meshes) -> trimesh.Trimesh:
    return trimesh.util.concatenate(list(meshes))


def grid_sheet(cells: int = 5, step: float = 2.0) -> trimesh.Trimesh:
    """单层三角网格片：开放边围成一圈，有向体积为 0。"""
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


def health(mesh) -> dict:
    v = np.asarray(mesh.vertices)
    f = np.asarray(mesh.faces)
    h = analysis.edge_histogram(f, len(v))
    return {
        **h,
        "duplicates": analysis.duplicate_face_count(f),
        "volume": analysis.signed_volume(np.asarray(v, dtype=np.float64), f),
        "faces": int(len(f)),
    }


def assert_watertight(mesh) -> None:
    h = health(mesh)
    assert h["boundary"] == 0, f"仍有开放边 {h['boundary']}"
    assert h["nonmanifold"] == 0, f"仍有非流形边 {h['nonmanifold']}"
    assert h["duplicates"] == 0, f"仍有重复面 {h['duplicates']}"


# ---------------------------------------------------------------- 边界环
def test_boundary_loop_simple_box():
    m = drop_faces(box(), [0, 1])
    loops = repair.boundary_loops(np.asarray(m.faces), len(m.vertices))
    assert len(loops) == 1
    assert len(loops[0]) == 4
    assert len(set(loops[0].tolist())) == 4


def test_boundary_loops_split_at_pinch_vertex():
    """两个洞只共享一个顶点时，不能返回自触的 8 字环。"""
    m = sphere()
    f = np.asarray(m.faces)
    pair = None
    for i in range(0, 60):
        for j in range(i + 1, 60):
            if len(set(f[i].tolist()) & set(f[j].tolist())) == 1:
                pair = (i, j)
                break
        if pair:
            break
    assert pair is not None, "夹具里找不到只共享一个顶点的两个面"
    m = drop_faces(m, pair)
    loops = repair.boundary_loops(np.asarray(m.faces), len(m.vertices))
    assert len(loops) == 2
    for ring in loops:
        assert len(ring) == 3
        assert len(set(ring.tolist())) == len(ring), f"环自触: {ring.tolist()}"


def test_boundary_loop_does_not_lose_edges():
    m = sphere()
    m = drop_faces(m, np.arange(0, len(m.faces), 40))
    h = health(m)
    loops = repair.boundary_loops(np.asarray(m.faces), len(m.vertices))
    edges = set()
    for ring in loops:
        for i in range(len(ring)):
            a, b = int(ring[i]), int(ring[(i + 1) % len(ring)])
            edges.add((min(a, b), max(a, b)))
    assert len(edges) == h["boundary"]


# ---------------------------------------------------------------- 补洞
def test_fill_open_box_is_watertight():
    # 只删一个三角面，洞是平面的；补回去体积必须精确复原
    m = drop_faces(box(20.0), [0])
    out, info = repair.act_fill_holes(m, {})
    assert info["filled"] is True
    assert_watertight(out)
    assert out.is_watertight
    assert abs(float(out.volume) - 8000.0) < 1e-6


def test_fill_nonplanar_hole_is_watertight():
    # 删掉两个相邻面（跨一条棱），补出来的是一个非平面开口；
    # 不要求体积复原，但必须闭合且没有引入非流形边
    m = drop_faces(box(20.0), [0, 1])
    out, info = repair.act_fill_holes(m, {})
    assert info["filled"] is True
    assert_watertight(out)
    assert float(out.volume) > 0


def test_fill_open_sheet_rejected_as_zero_thickness():
    """单层网格片有开放边，但补洞只会得到两层重合的零厚度壳。

    它的有向体积原本就是 0，旧验收只在「修复前体积非零」时才检查塌缩，
    因此会把这种「补完水密、实际没有厚度」的结果判为成功。
    """
    m = grid_sheet(cells=5)
    assert abs(analysis.signed_volume(
        np.asarray(m.vertices, dtype=np.float64), np.asarray(m.faces))) < 1e-12
    out, info = repair.act_fill_holes(m, {})
    assert info["filled"] is False
    assert info["reverted"] is True
    assert "零厚度" in info["revert_reason"]
    assert len(out.faces) == len(m.faces), "回退后应保留原始网格"


def test_fill_figure_eight_holes_no_nonmanifold():
    m = sphere()
    f = np.asarray(m.faces)
    pair = None
    for i in range(0, 80):
        for j in range(i + 1, 80):
            if len(set(f[i].tolist()) & set(f[j].tolist())) == 1:
                pair = (i, j)
                break
        if pair:
            break
    m = drop_faces(m, pair)
    out, info = repair.act_fill_holes(m, {})
    assert info["filled"] is True
    assert_watertight(out)
    assert info["nonmanifold_after"] == 0


def test_fill_broken_sphere_is_watertight():
    m = sphere()
    m = drop_faces(m, np.arange(0, len(m.faces), 40))
    before = health(m)["boundary"]
    out, info = repair.act_fill_holes(m, {})
    assert info["boundary_before"] == before
    assert info["boundary_after"] == 0
    assert_watertight(out)
    assert out.is_watertight


def test_fill_removes_sliver_instead_of_duplicating_it():
    body = box()
    v = np.array([[20.0, 0, 0], [22.0, 0, 0], [21.0, 2, 0]])
    sliver = trimesh.Trimesh(vertices=v, faces=[[0, 1, 2]], process=False)
    m = concatenate(body, sliver)
    out, info = repair.act_fill_holes(m, {})
    assert info["removed_slivers"] == 1
    assert len(out.faces) == len(body.faces)
    assert_watertight(out)
    assert abs(float(out.volume)) == pytest.approx(1000.0, abs=1e-6)


def test_fill_dedupes_double_faces():
    m = box()
    doubled = trimesh.Trimesh(vertices=m.vertices.copy(),
                              faces=np.vstack([m.faces, m.faces]), process=False)
    assert analysis.duplicate_face_count(np.asarray(doubled.faces)) == 12
    out, info = repair.act_fill_holes(doubled, {})
    assert info["duplicates_after"] == 0
    assert info["nonmanifold_after"] == 0
    assert len(out.faces) == 12
    assert_watertight(out)


def test_fill_rejects_when_all_holes_are_too_large():
    m = drop_faces(box(20.0), [0, 1])  # 一个 4 边洞
    out, info = repair.act_fill_holes(m, {"max_loop": 3})
    assert info["filled"] is False
    assert info["reverted"] is True
    assert info["remaining_loops"] == 1
    assert health(out)["boundary"] == 4


def test_fill_never_increases_nonmanifold():
    m = sphere()
    m = drop_faces(m, np.arange(0, len(m.faces), 40))
    out, info = repair.act_fill_holes(m, {})
    assert info["nonmanifold_after"] <= info["nonmanifold_before"]
    assert health(out)["nonmanifold"] == 0


# ---------------------------------------------------------------- 法线
def test_check_normals_detects_inverted_component():
    a = box()
    a.apply_translation([-20, 0, 0])
    b = box()
    b.apply_translation([20, 0, 0])
    b.faces = np.asarray(b.faces)[:, ::-1]
    m = concatenate(a, b)
    assert abs(analysis.signed_volume(np.asarray(m.vertices, float),
                                      np.asarray(m.faces))) < 1e-9
    it = analysis.check_normals(m, {})
    assert it["severity"] == "error"
    assert it["value"]["inverted_components"] == 1
    assert it["fix"]["action"] == "fix_normals"


def test_check_normals_detects_winding_conflict():
    m = box()
    f = np.asarray(m.faces).copy()
    f[0] = f[0][::-1]
    m = trimesh.Trimesh(vertices=m.vertices.copy(), faces=f, process=False)
    it = analysis.check_normals(m, {})
    assert it["severity"] in ("warning", "error")
    assert it["value"]["winding_conflicts"] > 0


def test_fix_normals_makes_multi_body_positive():
    a = box()
    a.apply_translation([-20, 0, 0])
    b = box()
    b.apply_translation([20, 0, 0])
    b.faces = np.asarray(b.faces)[:, ::-1]
    m = concatenate(a, b)
    out, info = repair.act_fix_normals(m, {})
    assert info["flipped_components"] == 1
    assert float(out.volume) == pytest.approx(2000.0, rel=1e-6)
    assert analysis.winding_conflict_count(np.asarray(out.faces)) == 0


def test_fix_normals_repairs_winding_only():
    m = box()
    f = np.asarray(m.faces).copy()
    f[0] = f[0][::-1]
    m = trimesh.Trimesh(vertices=m.vertices.copy(), faces=f, process=False)
    out, info = repair.act_fix_normals(m, {})
    assert info["winding_conflicts_after"] == 0
    assert float(out.volume) == pytest.approx(1000.0, rel=1e-6)


# ---------------------------------------------------------------- 自相交
@pytest.mark.parametrize("name,t1,t2,expected", [
    ("identical", [[0, 0, 0], [10, 0, 0], [0, 10, 0]],
     [[0, 0, 0], [10, 0, 0], [0, 10, 0]], True),
    ("opposite", [[0, 0, 0], [10, 0, 0], [0, 10, 0]],
     [[0, 0, 0], [0, 10, 0], [10, 0, 0]], True),
    ("coplanar_overlap", [[0, 0, 0], [10, 0, 0], [0, 10, 0]],
     [[1, 1, 0], [11, 1, 0], [1, 11, 0]], True),
    ("coplanar_adjacent", [[0, 0, 0], [10, 0, 0], [0, 10, 0]],
     [[10, 0, 0], [0, 0, 0], [-10, 0, 0]], False),
    ("parallel_offset", [[0, 0, 0], [10, 0, 0], [0, 10, 0]],
     [[0, 0, 5], [10, 0, 5], [0, 10, 5]], False),
    ("crossing", [[0, 0, 0], [10, 0, 0], [0, 10, 0]],
     [[2, 2, -5], [2, 2, 5], [8, 2, 0]], True),
    ("apart", [[0, 0, 0], [10, 0, 0], [0, 10, 0]],
     [[20, 20, 0], [30, 20, 0], [20, 30, 0]], False),
    ("shared_vertex_cross", [[0, 0, 0], [10, 0, 0], [0, 10, 0]],
     [[0, 0, 0], [5, -5, 3], [5, 5, -3]], True),
    ("shared_vertex_touch", [[0, 0, 0], [10, 0, 0], [0, 10, 0]],
     [[0, 0, 0], [-10, 0, 0], [0, -10, 0]], False),
    ("degenerate", [[0, 0, 0], [1, 0, 0], [2, 0, 0]],
     [[0, 0, 0], [0, 1, 0], [0, 0, 1]], False),
])
def test_tri_tri_overlap(name, t1, t2, expected):
    hit, _cop = analysis.tri_tri_overlap(
        np.asarray([t1], dtype=float), np.asarray([t2], dtype=float), tol=1e-7)
    assert bool(hit[0]) is expected, name


def test_self_intersection_detects_duplicate_faces():
    m = box()
    doubled = trimesh.Trimesh(vertices=m.vertices.copy(),
                              faces=np.vstack([m.faces, m.faces]), process=False)
    res = analysis.find_self_intersections(np.asarray(doubled.vertices, float),
                                           np.asarray(doubled.faces))
    assert res["duplicates"] == 12
    assert res["count"] == 12


def test_self_intersection_detects_shared_vertex_fold():
    v = np.array([[0, 0, 0], [10, 0, 0], [0, 10, 0],
                  [5, -5, 3], [5, 5, -3]], dtype=float)
    f = np.array([[0, 1, 2], [0, 3, 4]], dtype=int)
    res = analysis.find_self_intersections(v, f)
    assert res["count"] >= 1


def test_self_intersection_clean_sphere_has_no_false_positive():
    m = sphere()
    res = analysis.find_self_intersections(np.asarray(m.vertices, float),
                                           np.asarray(m.faces))
    assert res["count"] == 0


def test_self_intersection_two_crossing_spheres():
    a = sphere(3, 15.0)
    b = sphere(3, 15.0)
    b.apply_translation([10, 10, 10])
    m = concatenate(a, b)
    res = analysis.find_self_intersections(np.asarray(m.vertices, float),
                                           np.asarray(m.faces))
    assert res["count"] > 0


# ---------------------------------------------------------------- 碎屑/内腔
def test_remove_small_keeps_true_cavity():
    from app import voxelops

    shell, _cavity = voxelops.hollow_shell(sphere(3, 20.0), 2.0)
    info_before = analysis.classify_components(np.asarray(shell.vertices, float),
                                               np.asarray(shell.faces))
    assert info_before["cavities"] == 1
    out, info = repair.act_remove_small(shell, {})
    assert info["removed"] == 0
    assert info["cavities_kept"] == 1
    assert len(out.faces) == len(shell.faces)


def test_remove_small_removes_inverted_debris():
    body = box(20.0)
    debris = box(1.0)
    debris.apply_translation([30, 0, 0])
    debris.faces = np.asarray(debris.faces)[:, ::-1]
    m = concatenate(body, debris)
    out, info = repair.act_remove_small(m, {})
    assert info["removed"] == 1
    assert info["rejected_cavities"] == 1
    assert float(out.volume) == pytest.approx(8000.0, rel=1e-6)


def test_component_check_uses_area_ratio_not_wrong_units():
    body = box(20.0)
    scrap = box(1.0)  # 1mm 立方，体积 1mm³，但面积 6mm²
    scrap.apply_translation([30, 0, 0])
    m = concatenate(body, scrap)
    it = analysis.check_components(m, {})
    assert it["value"]["tiny"] == 1
    assert it["severity"] == "warning"


# ---------------------------------------------------------------- 检测项
def test_check_watertight_duplicate_faces_is_error():
    m = box()
    doubled = trimesh.Trimesh(vertices=m.vertices.copy(),
                              faces=np.vstack([m.faces, m.faces]), process=False)
    it = analysis.check_watertight(doubled, {})
    assert it["severity"] == "error"
    assert "重复面" in it["message"] or "非流形" in it["message"]


def test_quality_score_penalises_nonmanifold():
    m = box()
    doubled = trimesh.Trimesh(vertices=m.vertices.copy(),
                              faces=np.vstack([m.faces, m.faces]), process=False)
    rep = analysis.analyze_mesh(doubled, "dup.stl", {"sample_faces": 100})
    assert rep["quality"]["grade"] in ("C", "D")
    assert rep["quality"]["score"] < 75


def test_edge_keys_are_collision_free_when_nverts_too_small():
    # 最大索引 6，但调用方误传 3；旧编码在 nverts=3 时让
    # (0,5) 与 (1,2) 撞成 key=5、(0,6) 与 (1,3) 撞成 key=6。
    # edge_keys 必须按实际最大索引收紧 nverts，6 条边得到 6 个不同 key。
    f = np.array([[0, 5, 6], [1, 2, 3]], dtype=np.int64)
    keys = analysis.edge_keys(f, 3)
    assert len(np.unique(keys)) == len(keys) == 6


# ---------------------------------------------------------------- 三角化兜底
def test_triangulate_concave_loop_earcut_fallback(monkeypatch):
    """earcut 不可用时，凹多边形必须用耳切而不是从 0 号点拉扇形。

    L 形环面积 3；扇形会在凹角处补到轮廓外，面积虽然也可能对上，
    但三角形会覆盖 (1,2) 所在的右上空白区。
    """
    class BrokenEarcut:
        @staticmethod
        def triangulate_float64(*args, **kwargs):
            raise RuntimeError("earcut unavailable")

    monkeypatch.setitem(sys.modules, "mapbox_earcut", BrokenEarcut)
    verts = np.array([
        [0, 0, 0], [2, 0, 0], [2, 1, 0],
        [1, 1, 0], [1, 2, 0], [0, 2, 0],
    ], dtype=np.float64)
    tris = repair.triangulate_loop(verts, np.arange(6))
    assert len(tris) == 4

    t = verts[tris]
    area = 0.5 * np.linalg.norm(
        np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1)
    assert float(area.sum()) == pytest.approx(3.0, abs=1e-9)

    centers = t.mean(axis=1)
    inside_l = (centers[:, 1] <= 1.0 + 1e-9) | (centers[:, 0] <= 1.0 + 1e-9)
    assert np.all(inside_l), f"三角形质心落到 L 形外面: {centers[~inside_l]}"

    # 每条边界边恰好被一个三角形使用，内部对角线恰好两次
    counts: dict[tuple[int, int], int] = {}
    for tri in tris:
        for i in range(3):
            a, b = sorted((int(tri[i]), int(tri[(i + 1) % 3])))
            counts[(a, b)] = counts.get((a, b), 0) + 1
    boundary = {(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (0, 5)}
    for edge, cnt in counts.items():
        assert cnt == (1 if edge in boundary else 2), f"{edge} 被使用 {cnt} 次"


def test_triangulate_self_intersecting_loop_is_rejected(monkeypatch):
    """自交环宁可补不上，也不能缝进重叠三角形。"""
    class BrokenEarcut:
        @staticmethod
        def triangulate_float64(*args, **kwargs):
            raise RuntimeError("earcut unavailable")

    monkeypatch.setitem(sys.modules, "mapbox_earcut", BrokenEarcut)
    verts = np.array([
        [0, 0, 0], [1, 1, 0], [1, 0, 0], [0, 1, 0],
    ], dtype=np.float64)  # 蝴蝶结，两条边交叉
    tris = repair.triangulate_loop(verts, np.arange(4))
    assert len(tris) == 0, "自交环不应返回扇形重叠面"


# ---------------------------------------------------------------- 体素参数防护
def test_sanitize_pitch_bounds_and_empty_mesh():
    m = box(20.0)
    for bad in (float("nan"), float("inf"), -5.0, 0.0, 1e9):
        p = voxelops.sanitize_pitch(m, bad)
        assert np.isfinite(p) and 0 < p <= 20.0
    # 过小的 pitch 收敛到每轴最多 MAX_AXIS_VOXELS 格
    assert voxelops.sanitize_pitch(m, 1e-9) >= 20.0 / voxelops.MAX_AXIS_VOXELS - 1e-12
    # 过大的 pitch 至少保留 8 格
    assert voxelops.sanitize_pitch(m, 1e9) <= 20.0 / 8.0 + 1e-12

    with pytest.raises(ValueError):
        voxelops.sanitize_pitch(trimesh.Trimesh(), 1.0)


def test_to_grid_tiny_model_and_huge_pitch():
    # 0.5mm 珠宝件不能被固定 0.2mm floor 切成 2 格
    tiny = box(0.5)
    p = voxelops.choose_pitch(tiny)
    assert p <= 0.5 / 20.0 + 1e-12
    grid, _vox, actual = voxelops.to_grid(tiny)
    assert actual == pytest.approx(p)
    assert grid.any() and not grid.all(), "微小模型栅格化后为空或完全填满"
    assert max(grid.shape) <= voxelops.MAX_AXIS_VOXELS + 2

    # 超大 pitch 收敛后仍应得到一个有界的实心区域
    grid2, _vox2, actual2 = voxelops.to_grid(box(20.0), 1e9)
    assert actual2 <= 20.0
    assert grid2.any() and not grid2.all()


def test_resolve_self_intersection_clamps_bad_pitch():
    out, info = repair.act_resolve_self_intersection(
        box(20.0), {"remesh_pitch": 1e9, "remesh_smooth": 0})
    assert info["pitch_requested"] == 1e9
    assert np.isfinite(info["pitch"]) and info["pitch"] <= 20.0
    assert len(out.faces) > 0
    assert float(out.volume) > 0


def test_wall_thickness_marks_degraded_on_raycast_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("raycast failed")

    monkeypatch.setattr(analysis, "_raycast_nearest", boom)
    res = analysis.sample_wall_thickness(box(10.0), 20, multi_dir=1)
    assert res["degraded"] is True
    assert res["samples"] == 0
    assert "raycast failed" in res["error"]

    item = analysis.check_thickness(box(10.0), {"sample_faces": 20})
    assert item["severity"] == "info", "测量失败不能谎报 ok"
    assert item["value"]["degraded"] is True


def test_thicken_clamps_absurd_amount():
    # 2mm 立方体请求加厚 1000mm：必须收敛到模型尺度，不能按 1000mm 铺距离场
    out, info = repair.act_thicken(
        box(2.0), {"thicken_mm": 1e6, "thicken_smooth": 0})
    assert info["requested_mm"] == 1e6
    assert info["delta_mm"] <= 0.5 + 1e-9
    assert info["delta_mm"] == pytest.approx(0.5)
    assert len(out.faces) > 0
    assert float(out.volume) > 0


def test_rescale_rejects_invalid_factor():
    # 0/负数/NaN 会把模型塌成一个点或产生 inf 坐标，必须拒绝而不是照做
    for bad in (0.0, -1.0, float("nan"), float("inf"), 1e9):
        with pytest.raises(ValueError):
            repair.act_rescale(box(10.0), {"scale_factor": bad})
    out, info = repair.act_rescale(box(10.0), {"scale_factor": 25.4})
    assert info["factor"] == 25.4
    assert max(out.extents) == pytest.approx(254.0)


def test_fill_action_sanitizes_numeric_params():
    m = drop_faces(box(20.0), [0])
    out, info = repair.act_fill_holes(
        m, {"fill_levels": -99, "fill_smooth": "oops", "max_loop": "bad"})
    assert info["filled"] is True
    assert_watertight(out)
    assert float(out.volume) == pytest.approx(8000.0, abs=1e-6)
