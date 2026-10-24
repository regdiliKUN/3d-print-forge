"""第 41–61 轮迭代的回归用例。

每个用例对应一轮的**可证伪目标**：先能复现旧行为（错误结论/漏报/崩溃），
改动后断言新行为成立。命名前缀 Rnn 与 ROUNDS.md 的轮次编号一一对应。
"""
from __future__ import annotations

import numpy as np
import pytest
import trimesh
from shapely.geometry import Polygon

from app import analysis, batch, compare, gpu, pack, process, repair, split, store, voxelops


# ---------------------------------------------------------------- 夹具
class FakeCtx:
    """最小任务上下文：够 run_job 用即可（tick/log/断点）。"""

    class _Job:
        def __init__(self, pid):
            self.project_id = pid
            self.steps: dict = {}

    def __init__(self, pid):
        self.job = self._Job(pid)
        self.logs: list[str] = []

    def tick(self, *a, **k):
        pass

    def log(self, msg):
        self.logs.append(msg)

    def step_done(self, name):
        rec = self.job.steps.get(name)
        if rec and rec.get("status") == "done":
            return rec.get("data") or {}
        return None

    def complete_step(self, name, data=None):
        self.job.steps[name] = {"status": "done", "data": data or {}}

    def fail_step(self, name, reason):
        self.job.steps[name] = {"status": "failed", "reason": reason}


def u_section_mesh() -> trimesh.Trimesh:
    """U 形截面拉伸件：顶点均值落在缺口里（不在实体内）。"""
    outer = np.array([[0, 0], [30, 0], [30, 30], [20, 30],
                      [20, 10], [10, 10], [10, 30], [0, 30]], float)
    return trimesh.creation.extrude_polygon(Polygon(outer), height=20)


def ring_section_mesh() -> trimesh.Trimesh:
    outer = np.array([[0, 0], [40, 0], [40, 40], [0, 40]], float)
    hole = np.array([[10, 10], [30, 10], [30, 30], [10, 30]], float)
    return trimesh.creation.extrude_polygon(Polygon(outer, [hole]), height=10)


def sheet_only() -> trimesh.Trimesh:
    v = np.array([[0, 0, 0], [20, 0, 0], [20, 20, 0], [0, 20, 0],
                  [0, 40, 0], [20, 40, 0]], float)
    f = np.array([[0, 1, 2], [0, 2, 3], [0, 3, 4], [0, 4, 5]])
    return trimesh.Trimesh(vertices=v, faces=f, process=False)


def overhang_part() -> trimesh.Trimesh:
    """两端立柱 + 中间悬空的桥形件。"""
    m = trimesh.creation.box(extents=[40, 10, 20])
    m.apply_translation([0, 0, 10])
    cut = trimesh.creation.box(extents=[20, 12, 12])
    cut.apply_translation([0, 0, 4])
    return trimesh.boolean.difference([m, cut], engine="manifold")


# ---------------------------------------------------------------- R41
def test_r41_unresolved_boundary_edges_are_reported():
    """三面共边的开放片：3 条开放边走不成环，必须报出来而不是静默丢弃。"""
    v = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]], float)
    f = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3]])
    loops, st = repair.boundary_loops_ex(f, 4)
    assert loops == []
    assert st["boundary_edges"] == 3
    assert st["unresolved_edges"] == 3
    assert st["open_walks"] >= 1

    m = trimesh.Trimesh(v, f, process=False)
    _, info = repair.act_fill_holes(m, {})
    assert info.get("unresolved_boundary_edges") == 3
    assert "无法串成闭合环" in (info.get("unresolved_note") or "")


def test_r41_normal_hole_reports_zero_unresolved():
    m = trimesh.creation.box(extents=[20, 20, 20])
    keep = np.ones(len(m.faces), bool)
    keep[0] = False
    m.update_faces(keep)
    m.remove_unreferenced_vertices()
    loops, st = repair.boundary_loops_ex(np.asarray(m.faces), len(m.vertices))
    assert len(loops) == 1 and st["unresolved_edges"] == 0


# ---------------------------------------------------------------- R42
def _quality_items(thickness_value: dict) -> list[dict]:
    return [
        {"id": "watertight", "name": "水密性", "severity": "ok",
         "value": {"boundary_edges": 0, "nonmanifold_edges": 0, "duplicate_faces": 0}},
        {"id": "normals", "name": "法线", "severity": "ok",
         "value": {"signed_volume": 8000, "degenerate_faces": 0,
                   "inverted_components": 0, "winding_conflicts": 0}},
        {"id": "thickness", "name": "壁厚", "severity": "ok", "value": thickness_value},
        {"id": "nonmanifold", "name": "非流形边", "severity": "ok",
         "value": {"count": 0, "duplicate_faces": 0}},
        {"id": "self_intersection", "name": "自相交", "severity": "ok", "value": {"count": 0}},
        {"id": "components", "name": "孤立面片", "severity": "ok", "value": {"components": 1}},
        {"id": "units", "name": "单位", "severity": "ok", "value": {"unit": "mm"}},
    ]


def test_r42_coarse_thickness_score_scales_with_coverage():
    low = analysis.quality_score({"items": _quality_items(
        {"min": 2.4, "samples": 0, "sample_faces": 600, "lower_bound": True, "coverage": 0.003}),
        "stats": {"faces": 200000}})
    high = analysis.quality_score({"items": _quality_items(
        {"min": 2.4, "samples": 0, "sample_faces": 600, "lower_bound": True, "coverage": 0.05}),
        "stats": {"faces": 200000}})
    part = lambda q: [p for p in q["parts"] if p["name"] == "壁厚"][0]  # noqa: E731
    assert part(low)["score"] < part(high)["score"]
    assert low["caveats"] and "粗筛" in low["caveats"][0]
    assert not high["caveats"]


def test_r42_coarse_message_states_actual_coverage():
    m = trimesh.creation.box(extents=[60, 60, 60])
    item = analysis.check_thickness(m, {"sample_faces": 200})
    v = item["value"]
    assert v.get("coverage") is not None
    assert "采样" in item["message"]


# ---------------------------------------------------------------- R43
def test_r43_fill_fixes_inside_out_body():
    """整体反法线的模型补完洞仍有向体积为负，必须纠正，否则切片当空腔。"""
    m = trimesh.creation.box(extents=[20, 20, 20])
    m.faces = m.faces[:, ::-1]
    keep = np.ones(len(m.faces), bool)
    keep[0] = False
    m.update_faces(keep)
    m.remove_unreferenced_vertices()

    out, info = repair.act_fill_holes(m, {})
    v = np.asarray(out.vertices, float)
    f = np.asarray(out.faces)
    assert info["filled"] is True
    assert info.get("orientation_fixed") is True
    assert analysis.signed_volume(v, f) > 0


def test_r43_normal_body_is_not_inverted():
    m = trimesh.creation.box(extents=[20, 20, 20])
    keep = np.ones(len(m.faces), bool)
    keep[0] = False
    m.update_faces(keep)
    m.remove_unreferenced_vertices()
    out, info = repair.act_fill_holes(m, {})
    assert info.get("orientation_fixed") is False
    assert analysis.signed_volume(np.asarray(out.vertices, float), np.asarray(out.faces)) > 0


# ---------------------------------------------------------------- R44
def test_r44_drill_thin_wall_filter_is_active():
    """镂空件打孔要按实测壳厚过滤，而不是恒传 0（等于不过滤）。"""
    s = trimesh.creation.icosphere(subdivisions=3, radius=20)
    s.apply_translation([0, 0, 20])
    shell, _ = voxelops.hollow_shell(s, 1.6, pitch=0.8)
    out, info = process.drill_holes(shell, 2, 3.0, 1.6)
    assert info["min_wall_checked"] is not None and info["min_wall_checked"] > 0
    assert info["holes"] >= 1
    assert out.is_watertight


def test_r44_all_rejected_positions_fall_back_instead_of_failing(monkeypatch):
    s = trimesh.creation.icosphere(subdivisions=2, radius=10)
    # 让所有候选都被判成「太薄」
    monkeypatch.setattr(process, "_filter_thin_spots",
                        lambda mesh, xy, min_wall: (xy[:0], len(xy)))
    out, info = process.drill_holes(s, 2, 2.0, 1.0)
    assert info["thin_fallback"] is True
    assert info["holes"] >= 1


# ---------------------------------------------------------------- R45
def test_r45_support_density_follows_spacing():
    m = overhang_part()
    counts = []
    for sp in (2.0, 8.0):
        _sup, info = process.build_supports(m, 45.0, 1.0, sp, 400)
        counts.append(info["count"])
    assert counts[0] > counts[1], f"间距变大支撑应变稀，实测 {counts}"


# ---------------------------------------------------------------- R46
def test_r46_support_diameter_reported_after_scaling():
    m = overhang_part()
    p = dict(process.DEFAULTS)
    p.update({"supports": "auto", "support_diameter": 1.0, "scale": 2.0, "orient": False})
    _mesh, sup, rep = process.apply_pipeline(m.copy(), p)
    assert sup is not None
    assert rep["supports"]["diameter"] == 1.0
    assert rep["supports"]["diameter_effective"] == 2.0


def test_r46_run_job_stats_include_supports(tmp_path, monkeypatch):
    from app import config, meshio

    monkeypatch.setattr(config, "OUTPUTS", tmp_path)
    src = tmp_path / "part.stl"
    overhang_part().export(str(src))
    proj = store.create("part.stl", src)
    try:
        ctx = FakeCtx(proj["id"])
        res = process.run_job(ctx, {"supports": "auto", "orient": False, "support_diameter": 1.0})
        after = res["after"]
        assert after["with_supports"] is True
        assert after["faces"] > after["mesh_only_faces"]
        # 导出文件的面数必须和报告一致
        saved = meshio.load_mesh(res["output"])
        assert len(saved.faces) == after["faces"]
    finally:
        (config.PROJECTS / f"{proj['id']}.json").unlink(missing_ok=True)


# ---------------------------------------------------------------- R47
def test_r47_decimate_breaking_watertight_is_reverted():
    m = overhang_part()
    p = dict(process.DEFAULTS)
    p.update({"decimate_ratio": 0.02, "orient": False})
    mesh, _sup, rep = process.apply_pipeline(m.copy(), p)
    d = rep["decimate"]
    assert d.get("reverted") is True
    assert d.get("faces_kept") == len(m.faces)
    assert mesh.is_watertight, "回退后必须是原网格，仍然水密"


def test_r47_mild_decimate_is_kept():
    m = trimesh.creation.icosphere(subdivisions=3, radius=20)
    p = dict(process.DEFAULTS)
    p.update({"decimate_ratio": 0.6, "orient": False})
    mesh, _sup, rep = process.apply_pipeline(m.copy(), p)
    assert not rep["decimate"].get("reverted")
    assert len(mesh.faces) < len(m.faces)


# ---------------------------------------------------------------- R48
def test_r48_support_threshold_is_reported_and_consistent():
    _sup, info = process.build_supports(overhang_part(), 45.0, 1.0, 3.0, 400)
    assert info["min_support_h_mm"] == 3.0
    assert info["spacing"] == 3.0


# ---------------------------------------------------------------- R49
def test_r49_peg_hint_lands_inside_material():
    for m, coord in ((u_section_mesh(), 10.0), (ring_section_mesh(), 5.0)):
        c = split.section_centroid(m, coord, 2)
        assert c is not None
        assert bool(m.contains([[c[0], c[1], coord]])[0]), f"桩位 {c} 不在实体内"


def test_r49_peg_hint_convex_case_is_centroid():
    m = trimesh.creation.box(extents=[20, 10, 30])
    c = split.section_centroid(m, 0.0, 2)
    assert c is not None
    assert abs(c[0]) < 0.6 and abs(c[1]) < 0.6


# ---------------------------------------------------------------- R50
def test_r50_split_reports_not_watertight_pieces(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "OUTPUTS", tmp_path)
    m = trimesh.creation.cylinder(radius=15, height=120, sections=48)
    src = tmp_path / "tall.stl"
    m.export(str(src))
    proj = store.create("tall.stl", src)
    try:
        ctx = FakeCtx(proj["id"])
        res = split.run_job(ctx, {"max_size": 50.0})
        assert res["plan"]["pieces"] == 3
        assert res["not_watertight"] == []
        assert all(p["watertight"] for p in res["pieces"])
        # 桩位必须给出来，且落在切面上
        assert res["pieces"][0]["peg_hint"] is not None
    finally:
        (config.PROJECTS / f"{proj['id']}.json").unlink(missing_ok=True)


# ---------------------------------------------------------------- R51
def test_r51_pack_hints_rotation_when_it_would_fit():
    wide = trimesh.creation.box(extents=[100, 150, 10])
    small = trimesh.creation.box(extents=[50, 50, 10])
    placed, skipped = pack.pack([wide, small], (192, 120), 4.0)
    assert len(placed) == 1 and len(skipped) == 1
    assert skipped[0]["rotate_fits"] is True
    assert "转 90°" in skipped[0]["reason"]


def test_r51_pack_rejects_bad_platform_params(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "OUTPUTS", tmp_path)
    src = tmp_path / "p.stl"
    trimesh.creation.box(extents=[10, 10, 10]).export(str(src))
    proj = store.create("p.stl", src)
    try:
        ctx = FakeCtx(proj["id"])
        for bad in ({"platform_w": float("nan")}, {"platform_d": 0}, {"gap": -1}):
            with pytest.raises(ValueError):
                pack.run_job(ctx, bad)
    finally:
        (config.PROJECTS / f"{proj['id']}.json").unlink(missing_ok=True)


# ---------------------------------------------------------------- R52
def test_r52_compare_uses_point_to_surface():
    """同一张平面、不同密度：表面完全相同，旧口径会把 56/81 个顶点判成移动。"""

    def grid(n):
        xs = np.linspace(0, 10, n + 1)
        X, Y = np.meshgrid(xs, xs)
        v = np.column_stack([X.ravel(), Y.ravel(), np.zeros((n + 1) ** 2)])
        idx = np.arange((n + 1) ** 2).reshape(n + 1, n + 1)
        f = []
        for i in range(n):
            for j in range(n):
                a, b, c, d = idx[i, j], idx[i + 1, j], idx[i + 1, j + 1], idx[i, j + 1]
                f += [[a, b, c], [a, c, d]]
        return trimesh.Trimesh(v, np.array(f), process=False)

    r = compare.compare(grid(4), grid(8), tol=0.05)
    assert r["distance_metric"] == "surface"
    assert r["vertices"]["moved_ratio"] == 0.0
    assert r["vertices"]["max_displacement"] == 0.0
    assert r["identical"] is True


# ---------------------------------------------------------------- R53
def test_r53_compare_separates_scale_from_deformation():
    a = trimesh.creation.icosphere(subdivisions=2, radius=10)
    b = trimesh.creation.icosphere(subdivisions=2, radius=10)
    b.apply_scale(1.5)
    r = compare.compare(a, b, tol=0.05)
    assert r["size"]["uniform"] is True
    assert abs(r["size"]["uniform_scale"] - 1.5) < 1e-3
    assert r["size"]["changed"] is True
    assert "缩放" in r["note"]

    c = trimesh.creation.icosphere(subdivisions=2, radius=10)
    r2 = compare.compare(a, c, tol=0.05)
    assert r2["size"]["changed"] is False
    assert r2["identical"] is True


# ---------------------------------------------------------------- R54
def test_r54_repair_does_not_force_gpu_probe(tmp_path, monkeypatch):
    """小模型的补洞不该为了报一句「用了什么后端」而 import cupy。"""
    m = trimesh.creation.box(extents=[20, 20, 20])
    keep = np.ones(len(m.faces), bool)
    keep[0] = False
    m.update_faces(keep)
    m.remove_unreferenced_vertices()
    out, info = repair.act_fill_holes(m, {})
    # 只报「实际用了什么」，不触发探测：没跑 GPU 算子时就是空字典
    assert isinstance(info.get("gpu"), dict)
    assert "backend" not in info.get("gpu", {}), "不应为了报告而强制探测后端"


def test_r54_cuda_driver_check_exists():
    assert isinstance(gpu._cuda_driver_present(), bool)


# ---------------------------------------------------------------- R55
def test_r55_isolated_vertex_is_not_dragged_to_origin():
    v = np.array([[0, 0, 0], [10, 0, 0], [0, 10, 0], [999, 999, 999]], float)
    e = np.array([[0, 1], [1, 2], [0, 2]])
    out = gpu.smooth_laplacian(v, e, np.zeros(4, bool), iters=10, lam=0.5)
    assert np.allclose(out[3], [999, 999, 999]), "孤立顶点必须保持不动"
    assert not np.allclose(out[0], v[0]), "其余顶点仍然要被平滑"


# ---------------------------------------------------------------- R56
def test_r56_api_validates_process_split_pack_rotate():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    pid = c.post("/api/demo").json()["id"]
    assert c.post(f"/api/projects/{pid}/process", json={"supports": "automatic"}).status_code == 422
    assert c.post(f"/api/projects/{pid}/process", json={"wall_thickness": 99}).status_code == 422
    assert c.post(f"/api/projects/{pid}/process", json={"overhang_deg": 5}).status_code == 422
    assert c.post(f"/api/projects/{pid}/split", json={"max_size": 50, "axis": "xy"}).status_code == 422
    assert c.post(f"/api/projects/{pid}/split", json={"axis": "z"}).status_code == 422
    assert c.post(f"/api/projects/{pid}/pack", json={"platform_w": "nan"}).status_code == 422
    assert c.post(f"/api/projects/{pid}/rotate", json={"axis": "w"}).status_code == 422
    assert c.post(f"/api/projects/{pid}/bore",
                  json={"points": [[0, 0, 0]], "diameter": "abc"}).status_code == 422
    # 合法值放行
    assert c.post(f"/api/projects/{pid}/process", json={"supports": "auto"}).status_code == 200


# ---------------------------------------------------------------- R57 / R58
def test_r57_ui_marks_unconfirmed_checks():
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    for token in ("summary?.degraded", "unconfirmed", "ITEM_NAME", "未确认"):
        assert token in src, f"界面没有展示未确认项：{token}"


def test_r58_ui_surfaces_process_warnings():
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    for token in ("hollow_warning", "thin_fallback", "decimate?.reverted",
                  "diameter_effective", "lastProcess"):
        assert token in src, f"界面没有展示处理警告：{token}"


# ---------------------------------------------------------------- R59
def test_r59_batch_uses_safe_checks_and_marks_reverts(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "OUTPUTS", tmp_path)
    src = tmp_path / "sheet.stl"
    sheet_only().export(str(src))
    proj = store.create("sheet.stl", src)
    try:
        r = batch.process_one(proj["id"], {"repair_actions": ["merge_vertices", "fill_holes"],
                                          "process": {"orient": False}})
        assert "fill_holes" in [x["action"] for x in r["reverted"]]
        assert "fill_holes" not in r["repaired"]
        assert r["quality"]["grade"] in {"A", "B", "C", "D"}
        saved = store.get(proj["id"])
        assert (saved.get("analysis") or {}).get("quality"), "批量检测报告必须带质量评分"
    finally:
        (config.PROJECTS / f"{proj['id']}.json").unlink(missing_ok=True)
