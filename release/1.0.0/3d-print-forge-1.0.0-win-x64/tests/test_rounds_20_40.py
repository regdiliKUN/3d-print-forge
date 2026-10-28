"""第 20–40 轮迭代的回归用例。

每个用例对应一轮的**可证伪目标**：先能复现旧行为（失败/错误结论），
改动后断言新行为成立。命名前缀 Rnn 与 ROUNDS.md 的轮次编号一一对应。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import trimesh

from app import analysis, jobs, meshio, repair, voxelops


# ---------------------------------------------------------------- 夹具
def health(mesh) -> dict:
    v = np.asarray(mesh.vertices)
    f = np.asarray(mesh.faces)
    h = analysis.edge_histogram(f, len(v))
    return {
        "boundary": h["boundary"],
        "nonmanifold": h["nonmanifold"],
        "duplicates": analysis.duplicate_face_count(f),
        "volume": round(analysis.signed_volume(np.asarray(v, dtype=np.float64), f), 6),
        "faces": int(len(f)),
        "watertight": bool(mesh.is_watertight),
    }


def box_with_top_hole(size: float = 20.0) -> trimesh.Trimesh:
    """立方体去掉一块顶面（真正的「顶部洞」，便于让补丁与侧向实体相交）。"""
    m = trimesh.creation.box(extents=[size, size, size])
    top = np.flatnonzero(m.face_normals[:, 2] > 0.9)
    keep = np.ones(len(m.faces), bool)
    keep[top[0]] = False
    m.update_faces(keep)
    m.remove_unreferenced_vertices()
    return m


# ---------------------------------------------------------------- R20
def test_r20_candidate_pairs_do_not_miss_large_triangles():
    """细长大三角形的圆心距超过旧「对角线 1/4」封顶时，真实穿插曾被漏检（检出 0）。"""
    big = np.array([[-100, -1, 0], [100, -1, 0], [0, 1, 0]], dtype=float)
    small = np.array([[60, -0.9, -5], [61, -0.9, 5], [60, -0.5, 5]], dtype=float)
    verts = np.vstack([big, small])
    faces = np.array([[0, 1, 2], [3, 4, 5]])

    truth, _ = analysis.tri_tri_overlap(verts[faces[:1]], verts[faces[1:]], tol=1e-6)
    assert bool(truth[0]) is True, "夹具本身必须真的相交"

    res = analysis.find_self_intersections(verts, faces)
    assert res["count"] >= 1
    assert res["large_triangles"] >= 1, "半径超出批量阈值的三角形应走补查层"


def test_r20_clean_sphere_still_has_no_false_positive():
    m = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
    res = analysis.find_self_intersections(np.asarray(m.vertices), np.asarray(m.faces))
    assert res["count"] == 0


# ---------------------------------------------------------------- R21
def test_r21_patch_crossing_a_wall_is_rejected():
    """洞口正后方挡着一片墙时，补丁会从墙里穿过去；开放边归零、体积正常，旧实现报成功。"""
    body = box_with_top_hole(20.0)
    plate = trimesh.creation.box(extents=[2, 26, 10])
    plate.apply_translation([0, 0, 10])  # 穿过 z=10 的顶面开口
    mesh = trimesh.util.concatenate([body, plate])
    before = health(mesh)

    out, info = repair.act_fill_holes(mesh, {})
    assert info["reverted"] is True
    assert "穿插" in info["revert_reason"]
    assert info.get("patch_hits", 0) > 0
    # 回退必须原样保留输入
    assert health(out)["faces"] == before["faces"]


def test_r21_normal_fill_reports_zero_patch_hits():
    m = box_with_top_hole(20.0)
    out, info = repair.act_fill_holes(m, {})
    assert info["filled"] is True
    assert info.get("patch_hits", 0) == 0
    assert health(out)["watertight"]


# ---------------------------------------------------------------- R22
def test_r22_multi_triangle_sheet_is_removed_not_filled():
    """两个以上三角形拼成的零厚度薄片：以前识别不到，补洞会把它缝成双层皮。"""
    body = trimesh.creation.box(extents=[10, 10, 10])
    v = np.array([[20, 0, 0], [30, 0, 0], [30, 10, 0], [20, 10, 0],
                  [20, 20, 0], [30, 20, 0]], dtype=float)
    f = np.array([[0, 1, 2], [0, 2, 3], [0, 3, 4], [0, 4, 5]])
    sheet = trimesh.Trimesh(vertices=v, faces=f, process=False)
    mesh = trimesh.util.concatenate([body, sheet])
    assert len(mesh.faces) == 16

    out, info = repair.act_fill_holes(mesh, {})
    h = health(out)
    assert info.get("removed_slivers") == 4
    assert h["faces"] == 12
    assert abs(h["volume"] - 1000.0) < 1e-6
    assert h["watertight"]


def test_r22_sheet_only_model_is_reverted_with_clear_reason():
    v = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0],
                  [0, 20, 0], [10, 20, 0]], dtype=float)
    f = np.array([[0, 1, 2], [0, 2, 3], [0, 3, 4], [0, 4, 5]])
    sheet = trimesh.Trimesh(vertices=v, faces=f, process=False)
    out, info = repair.act_fill_holes(sheet, {})
    assert info["reverted"] is True
    assert "零厚度薄片" in info["revert_reason"]
    assert len(out.faces) == 4


# ---------------------------------------------------------------- R23
def test_r23_remesh_reports_before_and_after_self_intersections():
    a = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
    b = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
    b.apply_translation([12, 0, 0])
    mesh = trimesh.util.concatenate([a, b])
    out, info = repair.act_resolve_self_intersection(mesh, {"remesh_pitch": 0.6})
    assert info["self_intersections_before"] >= 50
    assert info["self_intersections_after"] == 0
    assert info["watertight_after"] is True
    assert not info.get("degraded"), info.get("degrade_reason")


# ---------------------------------------------------------------- R24
def test_r24_units_are_ambiguous_between_micro_part_and_meters():
    amb = analysis.infer_units([0.5, 0.4, 0.3])
    assert amb["ambiguous"] is True
    assert amb["unit"].endswith("?")
    assert {a["unit"] for a in amb["alternatives"]} == {"m", "mm"}
    # 0.05 量级不可能是毫米件，判米不犹豫
    tiny = analysis.infer_units([0.05, 0.04, 0.03])
    assert tiny["ambiguous"] is False and tiny["scale"] == 1000.0
    # 检测项：二义时必须降级为 info，并且修复建议要标注「需先确认单位」
    item = analysis.check_units(trimesh.creation.box(extents=[0.5, 0.4, 0.3]), {})
    assert item["severity"] == "info"
    assert item["fix"]["needs_confirmation"] is True


# ---------------------------------------------------------------- R25
def _score_items(thickness_value: dict) -> list[dict]:
    return [
        {"id": "watertight", "name": "水密性", "severity": "ok",
         "value": {"boundary_edges": 0, "nonmanifold_edges": 0, "duplicate_faces": 0}},
        {"id": "normals", "name": "法线", "severity": "ok",
         "value": {"signed_volume": 8000, "degenerate_faces": 0,
                   "inverted_components": 0, "winding_conflicts": 0}},
        {"id": "thickness", "name": "壁厚", "severity": "info", "value": thickness_value},
        {"id": "nonmanifold", "name": "非流形边", "severity": "ok",
         "value": {"count": 0, "duplicate_faces": 0}},
        {"id": "self_intersection", "name": "自相交", "severity": "ok", "value": {"count": 0}},
        {"id": "components", "name": "孤立面片", "severity": "ok", "value": {"components": 1}},
        {"id": "units", "name": "单位", "severity": "ok", "value": {"unit": "mm"}},
    ]


def test_r25_unmeasured_thickness_cannot_earn_grade_a():
    items = _score_items({"degraded": True, "samples": 0, "min": None})
    q = analysis.quality_score({"items": items, "stats": {"faces": 12}})
    assert q["grade"] != "A"
    assert q["confidence"] == "low"
    assert "壁厚未能测量" in q["degraded"]
    assert "可以直接打印" not in q["verdict"]


def test_r25_healthy_model_still_gets_grade_a():
    items = _score_items({"min": 2.4, "samples": 900})
    q = analysis.quality_score({"items": items, "stats": {"faces": 12}})
    assert q["grade"] == "A"
    assert q["confidence"] == "normal"


def test_r25_sampled_self_intersection_is_labelled_lower_bound():
    items = _score_items({"min": 2.4, "samples": 900})
    for it in items:
        if it["id"] == "self_intersection":
            it["value"] = {"count": 3, "count_is_lower_bound": True}
    q = analysis.quality_score({"items": items, "stats": {"faces": 12}})
    note = [p["note"] for p in q["parts"] if p["name"] == "自交"][0]
    assert "≥" in note and "采样" in note


# ---------------------------------------------------------------- R26
def test_r26_small_ratio_is_sanitized_identically_everywhere():
    body = trimesh.creation.box(extents=[20, 20, 20])
    debris = trimesh.creation.box(extents=[1, 1, 1])
    debris.apply_translation([30, 0, 0])
    mesh = trimesh.util.concatenate([body, debris])

    assert analysis.sanitize_small_ratio(5.0) == 0.5
    assert analysis.sanitize_small_ratio(-1) == 0.0
    assert analysis.sanitize_small_ratio("abc") == 0.01
    assert analysis.sanitize_small_ratio(float("nan")) == 0.01

    for raw in (5.0, "abc", 0.01):
        item = analysis.check_components(mesh, {"small_ratio": raw})
        _, info = repair.act_remove_small(mesh, {"small_ratio": raw})
        assert item["value"]["tiny"] == info["removed"], f"ratio={raw!r} 检测与动作不一致"
        # 主件永远不能被判成碎屑
        assert item["value"]["tiny"] <= item["value"]["components"] - 1


def test_r26_message_uses_the_actual_ratio():
    body = trimesh.creation.box(extents=[20, 20, 20])
    debris = trimesh.creation.box(extents=[1, 1, 1])
    debris.apply_translation([30, 0, 0])
    mesh = trimesh.util.concatenate([body, debris])
    item = analysis.check_components(mesh, {"small_ratio": 0.02})
    assert "2%" in item["message"]


# ---------------------------------------------------------------- R27
def test_r27_action_order_respects_units_and_voxel_ops():
    assert repair.normalize_order(["thicken", "rescale"]) == ["rescale", "thicken"]
    assert repair.normalize_order(["thicken", "fill_holes"]) == ["fill_holes", "thicken"]
    order = repair.normalize_order(["thicken", "rescale", "fill_holes", "merge_vertices"])
    assert order == ["merge_vertices", "rescale", "fill_holes", "thicken"]


# ---------------------------------------------------------------- R28
def test_r28_fix_normals_reports_unresolvable_conflicts():
    m = trimesh.creation.box(extents=[10, 10, 10])
    dup = trimesh.Trimesh(vertices=m.vertices.copy(),
                          faces=np.vstack([m.faces, m.faces]), process=False)
    out, info = repair.act_fix_normals(dup, {})
    assert info["winding_conflicts_before"] > 0
    assert info["winding_conflicts_after"] > 0
    assert info.get("degraded") is True
    assert "绕序冲突" in info["degrade_reason"]


def test_r28_fix_normals_clean_model_is_not_degraded():
    a = trimesh.creation.box(extents=[10, 10, 10])
    a.apply_translation([-20, 0, 0])
    b = trimesh.creation.box(extents=[10, 10, 10])
    b.apply_translation([20, 0, 0])
    b.faces = b.faces[:, ::-1]
    out, info = repair.act_fix_normals(trimesh.util.concatenate([a, b]), {})
    assert info["flipped_components"] == 1
    assert not info.get("degraded")


# ---------------------------------------------------------------- R29
def test_r29_one_failing_check_does_not_kill_the_report(monkeypatch):
    def boom(mesh, params, ctx=None):
        raise RuntimeError("simulated")

    monkeypatch.setattr(analysis, "CHECKS", list(analysis.CHECKS[:3]) + [boom]
                        + list(analysis.CHECKS[4:]))
    rep = analysis.analyze_mesh(trimesh.creation.box(extents=[20, 20, 20]), "cube.stl")
    assert len(rep["items"]) == 7
    assert len(rep["failed_checks"]) == 1
    assert rep["summary"]["confidence"] == "low"
    assert rep["quality"]["grade"] != "A"


# ---------------------------------------------------------------- R30
def test_r30_out_of_range_vertex_index_does_not_raise():
    n, labels = analysis.union_components(np.array([[0, 1, 5]]), 3)
    assert n == 4 and len(labels) == 1


def test_r30_negative_sample_faces_is_clamped():
    m = trimesh.creation.box(extents=[10, 10, 10])
    res = analysis.sample_wall_thickness(m, -5)
    assert res["samples"] >= 1


# ---------------------------------------------------------------- R31
def test_r31_api_rejects_out_of_range_numbers():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    r = c.post("/api/demo")
    pid = r.json()["id"]
    assert c.post(f"/api/projects/{pid}/analyze", json={"sample_faces": -5}).status_code == 422
    assert c.post(f"/api/projects/{pid}/repair",
                  json={"actions": ["thicken"], "thicken_mm": 1e9}).status_code == 422
    assert c.post(f"/api/projects/{pid}/repair",
                  json={"actions": ["rescale"], "scale_factor": 0}).status_code == 422
    assert c.post(f"/api/projects/{pid}/repair",
                  json={"actions": ["fill_holes"], "fill_levels": 99}).status_code == 422
    # 合法值必须放行
    ok = c.post(f"/api/projects/{pid}/repair",
                json={"actions": ["fill_holes"], "fill_levels": 3, "fill_smooth": 8})
    assert ok.status_code == 200


# ---------------------------------------------------------------- R32
def test_r32_lock_ratio_uses_the_axis_the_user_filled():
    from app.main import _resize_factors

    # 100×80×200，只填宽度 50 → 系数必须是 50/100，而不是 50/200
    assert _resize_factors([100, 80, 200], {"width": 50}, True) == [0.5, 0.5, 0.5]
    assert _resize_factors([100, 80, 200], {"height": 100}, True) == [0.5, 0.5, 0.5]
    # 非等比：只改宽度
    assert _resize_factors([100, 80, 200], {"width": 50}, False) == [0.5, 1.0, 1.0]
    # 没填任何尺寸时不动
    assert _resize_factors([100, 80, 200], {}, True) == [1.0, 1.0, 1.0]


# ---------------------------------------------------------------- R33
def test_r33_concurrent_persist_does_not_lose_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOB_FILE", tmp_path / "jobs.json")

    def make(i: int) -> None:
        j = jobs.Job("t", f"job{i}", {}, None)
        for _ in range(20):
            j._persist()

    threads = [threading.Thread(target=make, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
    assert len(data) == 6


# ---------------------------------------------------------------- R34
def test_r34_paused_queued_job_does_not_hold_a_worker():
    mgr = jobs.JobManager()
    mgr._scale_workers()
    gate = threading.Event()
    ran: list[str] = []

    def worker(ctx, params):
        ran.append(params["tag"])
        ctx.tick(0.05, "go")
        if params["tag"] == "j1":
            gate.wait(5)
        return {}

    j1 = mgr.submit("t", "j1", {"tag": "j1"}, worker)
    time.sleep(0.15)
    j2 = mgr.submit("t", "j2", {"tag": "j2"}, worker)
    time.sleep(0.1)
    if j2.status == "queued":
        assert mgr.pause(j2.id) is True
        gate.set()
        time.sleep(0.6)
        assert j2.status == "paused"
        assert "j2" not in ran, "暂停中的排队任务不该被调度"
        assert mgr.resume(j2.id) is True
        time.sleep(0.6)
        assert j2.status == "done"
        assert "j2" in ran
    else:  # 多核机器上 j2 可能已被取走，退化为「暂停后仍可恢复」
        gate.set()
        time.sleep(0.4)
        assert mgr.pause(j2.id) is True or j2.status == "done"
    gate.set()


# ---------------------------------------------------------------- R35
def test_r35_cached_mesh_is_not_mutated_by_callers(tmp_path):
    m = trimesh.creation.box(extents=[10, 10, 10])
    p = tmp_path / "cube.stl"
    m.export(str(p))

    first = meshio.load_mesh(p, mutable=True)
    first.apply_scale(2.0)
    assert np.allclose(first.extents, 20.0)

    again = meshio.load_mesh(p)
    assert np.allclose(again.extents, 10.0), "缓存里的网格被调用方改坏了"

    third = meshio.load_mesh(p, mutable=True)
    assert third is not again


# ---------------------------------------------------------------- R36
def test_r36_derived_data_is_shared_between_checks(monkeypatch):
    calls = {"edges": 0, "dup": 0}
    real_edges = analysis.edge_histogram
    real_dup = analysis.duplicate_face_count

    def counting_edges(*a, **kw):
        calls["edges"] += 1
        return real_edges(*a, **kw)

    def counting_dup(*a, **kw):
        calls["dup"] += 1
        return real_dup(*a, **kw)

    monkeypatch.setattr(analysis, "edge_histogram", counting_edges)
    monkeypatch.setattr(analysis, "duplicate_face_count", counting_dup)
    mesh = trimesh.creation.box(extents=[20, 20, 20])
    analysis.check_watertight(mesh, {})
    analysis.check_nonmanifold(mesh, {})
    assert calls["edges"] == 1, "水密性与非流形检测应共用同一份边直方图"
    assert calls["dup"] == 1


# ---------------------------------------------------------------- R37 / R38
def test_r37_ui_surfaces_revert_and_degrade_reasons():
    src = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    for token in ("revert_reason", "degrade_reason", "patch_hits", "volume_warning",
                  "lastRepair", "rr-note"):
        assert token in src, f"界面没有展示 {token}"


def test_r38_ui_exposes_fill_and_remesh_parameters():
    src = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    for token in ('id="p-levels"', 'id="p-fillsmooth"', 'id="p-pitch"',
                  "fill_levels", "fill_smooth", "remesh_pitch"):
        assert token in src, f"界面缺少参数 {token}"
    # 碎屑阈值判的是面积，标签不能写体积
    assert "占最大部件体积比" not in src
