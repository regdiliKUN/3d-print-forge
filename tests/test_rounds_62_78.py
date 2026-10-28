"""第 62~78 轮迭代的逐轮回归。

每一轮对应一个可证伪的目标：先让它在旧代码上失败，再确认修复后通过。
覆盖：状态一致性（报告过期、标定标签）、文件完整性（截断 STL、损坏记录）、
建议可信度（支撑未评估、输入回退、摘要文案）、存储并发（save 合并）。
"""
from __future__ import annotations

import json

import numpy as np
import pytest
import trimesh

from app import advisor, calibration, config, main, meshio, printmodel, store


# ---------------------------------------------------------------- 夹具
@pytest.fixture()
def tmp_projects(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS", tmp_path)
    return tmp_path


@pytest.fixture()
def box_stl(tmp_path):
    p = tmp_path / "box.stl"
    trimesh.creation.box(extents=[10, 20, 30]).export(str(p))
    return p


def _report(extents=(10, 20, 30), volume=6000.0, faces=12, min_wall=2.0, components=1):
    return {
        "stats": {"extents": list(extents), "volume_mm3": volume, "area_mm2": 2200.0,
                  "faces": faces, "is_watertight": True},
        "items": [{"id": "thickness", "value": {"min": min_wall}},
                  {"id": "components", "value": {"components": components}}],
        "summary": {"total": 7, "worst": "ok"},
        "quality": {"score": 95, "grade": "A", "verdict": "可以直接打印", "parts": []},
    }


def _project(tmp_path, name="m.stl", analysis=None, path=None):
    src = path or (tmp_path / name)
    if path is None and not src.exists():
        trimesh.creation.box(extents=[10, 20, 30]).export(str(src))
    proj = {"id": "p1", "name": name, "current": str(src), "source": str(src),
            "history": [], "advice": None, "analysis": analysis}
    return proj


# ---------------------------------------------------------------- R62 报告过期
def test_r62_geometry_change_marks_report_stale(tmp_projects, box_stl):
    proj = store.create("box.stl", box_stl)
    store.update(proj["id"], analysis=_report())
    assert store.get(proj["id"])["analysis_stale"] is False

    big = tmp_projects / "big.stl"
    m = meshio.load_mesh(box_stl, mutable=True)
    m.apply_scale(3.0)
    m.export(str(big))
    store.add_history(proj["id"], "resize", {"width": 30}, big)

    rec = store.get(proj["id"])
    assert rec["analysis_stale"] is True
    assert rec["advice"] is None


def test_r62_advisor_ignores_stale_stats(tmp_projects, box_stl):
    """缩放到 3 倍后，建议必须按新体积算，而不是沿用旧报告的 6000mm³。"""
    proj = store.create("box.stl", box_stl)
    store.update(proj["id"], analysis=_report())
    big = tmp_projects / "big.stl"
    m = meshio.load_mesh(box_stl, mutable=True)
    m.apply_scale(3.0)
    m.export(str(big))
    store.add_history(proj["id"], "resize", {}, big)

    geo = advisor._geometry(store.get(proj["id"]))
    assert geo["stale"] is True
    assert abs(geo["volume_mm3"] - 162000) < 1.0
    assert geo["extents"][0] == pytest.approx(30.0)


def test_r62_fresh_analysis_clears_stale(tmp_projects, box_stl):
    proj = store.create("box.stl", box_stl)
    store.add_history(proj["id"], "rotate", {}, box_stl)
    assert store.get(proj["id"])["analysis_stale"] is True
    store.update(proj["id"], analysis=_report())
    assert store.get(proj["id"])["analysis_stale"] is False


# ---------------------------------------------------------------- R63 截断文件
def test_r63_truncated_binary_stl_is_rejected(tmp_path, box_stl):
    raw = box_stl.read_bytes()
    half = tmp_path / "half.stl"
    half.write_bytes(raw[: len(raw) // 2])
    info = meshio.probe_stl(half)
    assert info["format"] == "binary" and info["truncated"] is True
    assert info["missing_bytes"] > 0
    with pytest.raises(ValueError, match="截断"):
        meshio.load_mesh(half)


def test_r63_trailing_bytes_still_parse(tmp_path, box_stl):
    """尾部多几个字节不能把二进制 STL 判成 ASCII —— 旧实现会得到 0 个面。"""
    tail = tmp_path / "tail.stl"
    tail.write_bytes(box_stl.read_bytes() + b"\0" * 20)
    assert meshio.probe_stl(tail)["trailing_bytes"] == 20
    assert len(meshio.load_mesh(tail).faces) == 12


def test_r63_ascii_without_endsolid_is_only_suspicious(tmp_path, box_stl):
    asc = tmp_path / "a.stl"
    trimesh.creation.box(extents=[10, 20, 30]).export(str(asc), file_type="stl_ascii")
    asc.write_text(asc.read_text().replace("endsolid", ""), encoding="utf-8")
    info = meshio.probe_stl(asc)
    assert info["format"] == "ascii"
    assert info.get("truncated_suspect") is True
    assert len(meshio.load_mesh(asc).faces) == 12  # 只标记，不拒收


def test_r63_upload_rejects_truncated(tmp_path, box_stl):
    from fastapi.testclient import TestClient

    raw = box_stl.read_bytes()
    c = TestClient(main.app)
    r = c.post("/api/upload",
               files=[("files", ("half.stl", raw[: len(raw) // 2], "model/stl"))])
    body = r.json()
    assert not body["projects"]
    assert "截断" in body["errors"][0]["error"]


# ---------------------------------------------------------------- R64 支撑未判定
def test_r64_support_unassessed_when_sections_missing(monkeypatch):
    box = trimesh.creation.box(extents=[20, 20, 20])
    monkeypatch.setattr(printmodel, "cross_section_polygons", lambda *a, **k: ([], []))
    res = printmodel.support_analysis(box)
    assert res["ok"] is False
    assert "截面" in res["reason"]


def test_r64_advisor_says_unassessed_not_safe(tmp_path):
    proj = _project(tmp_path, path=tmp_path / "missing.stl")
    proj["analysis"] = _report()
    adv = advisor.recommend(proj, {"process": "FDM", "printer": "bambu_a1"})
    sup = next(p for p in adv["params"] if p["key"] == "supports")
    assert sup["value"] == "未评估"
    assert "不要据此认为模型不需要支撑" in " ".join(adv["warnings"])
    assert adv["confidence"]["score"] < 0.5


def test_r64_printability_marks_support_unknown(tmp_path):
    proj = _project(tmp_path, path=tmp_path / "missing.stl")
    proj["analysis"] = _report()
    adv = advisor.recommend(proj, {"process": "FDM", "printer": "bambu_a1"})
    chk = next(c for c in adv["printability"]["checks"] if c["key"] == "support")
    assert chk["ok"] is None  # 未判定 → 界面画「?」而不是「✕」
    assert any(b["key"] == "support" for b in adv["printability"]["blockers"])


# ---------------------------------------------------------------- R65/R66 输入回退
def test_r65_unknown_printer_is_reported(tmp_path):
    proj = _project(tmp_path, path=tmp_path / "missing.stl")
    proj["analysis"] = _report()
    adv = advisor.recommend(proj, {"process": "FDM", "printer": "no_such_printer"})
    assert adv["fallbacks"]["printer"] == "no_such_printer"
    assert any("没有机型预设" in w for w in adv["warnings"])


def test_r66_process_material_mismatch_is_reported(tmp_path):
    proj = _project(tmp_path, path=tmp_path / "missing.stl")
    proj["analysis"] = _report()
    adv = advisor.recommend(proj, {"process": "FDM", "printer": "bambu_a1",
                                   "material": "standard"})
    assert adv["fallbacks"]["material"] == "standard"
    assert adv["params"][0]["value"] == "PLA"


def test_r66_matching_inputs_have_no_fallback(tmp_path):
    proj = _project(tmp_path, path=tmp_path / "missing.stl")
    proj["analysis"] = _report()
    adv = advisor.recommend(proj, {"process": "FDM", "printer": "bambu_a1",
                                   "material": "PETG"})
    assert adv["fallbacks"] == {"printer": None, "material": None, "plan": None}


# ---------------------------------------------------------------- R67/R68 标定
@pytest.mark.parametrize("raw,expected", [
    ("false", False), ("0", False), (0, False), (False, False),
    ("true", True), ("1", True), (1, True), (True, True),
])
def test_r67_calibrate_truthiness(raw, expected):
    assert advisor._truthy(raw, True) is expected


def test_r67_calibrate_false_disables_correction(tmp_path):
    proj = _project(tmp_path, path=tmp_path / "missing.stl")
    proj["analysis"] = _report()
    adv = advisor.recommend(proj, {"process": "FDM", "printer": "bambu_a1",
                                   "calibrate": "false"})
    assert adv["calibration"]["applied"] is False


def test_r68_calibration_source_is_honest():
    cal = calibration.estimate("FDM", "bambu_a1", "PLA", "hours")
    assert cal["source"] in ("group", "sparse", "prior")
    # 没做跨机型池化，就不该出现 "global"
    assert cal["source"] != "global"


# ---------------------------------------------------------------- R69/R70 报告
def test_r69_report_lists_unconfirmed_checks():
    p = {"id": "r", "name": "r.stl", "history": [],
         "analysis": {"stats": {"faces": 10}, "items": [], "summary": {"counts": {"error": 0}},
                      "failed_checks": ["self_intersection"],
                      "quality": {"score": 80, "grade": "C", "verdict": "x", "parts": [],
                                  "caveats": ["壁厚只采样了 0.3% 的面"]}},
         "advice": {"params": [], "warnings": [],
                    "confidence": {"grade": "中", "score": 0.6, "reasons": ["壁厚采样不足"]}}}
    html = main._build_report_html(p)
    assert "未确认项" in html
    assert "这项检测没有完成" in html
    assert "壁厚只采样了 0.3%" in html


def test_r70_report_shows_ranges_and_confidence():
    p = {"id": "r", "name": "r.stl", "history": [],
         "analysis": {"stats": {"faces": 10}, "items": [], "summary": {"counts": {}},
                      "quality": {}},
         "advice": {"params": [{"label": "层高", "value": 0.2, "unit": "mm", "reason": "x",
                                "range": {"lo": 0.16, "hi": 0.24, "unit": "mm"}}],
                    "warnings": [],
                    "confidence": {"grade": "中", "score": 0.62, "reasons": ["壁厚采样不足"]}}}
    html = main._build_report_html(p)
    assert "0.16–0.24mm" in html
    assert "置信度 中（0.62）" in html


def test_r70_report_flags_stale_analysis():
    p = {"id": "r", "name": "r.stl", "history": [], "analysis_stale": True,
         "analysis": {"stats": {"faces": 10}, "items": [], "summary": {"counts": {}},
                      "quality": {}}, "advice": {}}
    html = main._build_report_html(p)
    assert "检测之后被修改过" in html


# ---------------------------------------------------------------- R71/R72/R73 存储
def test_r71_save_does_not_clobber_concurrent_fields(tmp_projects, box_stl):
    proj = store.create("box.stl", box_stl)
    store.update(proj["id"], tags=["old"], favorite=True)
    stale = store.get(proj["id"])
    store.update(proj["id"], tags=["newer"])
    stale["name"] = "renamed.stl"
    store.save(stale)
    after = store.get(proj["id"])
    assert after["tags"] == ["newer"]      # 并发写入保留
    assert after["favorite"] is True
    assert after["name"] == "renamed.stl"  # 本次修改生效


def test_r71_save_without_snapshot_still_writes(tmp_projects, box_stl):
    proj = store.create("box.stl", box_stl)
    proj["name"] = "plain-dict.stl"
    store.save(dict(proj))
    assert store.get(proj["id"])["name"] == "plain-dict.stl"


def test_r72_damaged_record_is_listed(tmp_projects):
    (tmp_projects / "broken.json").write_text("{not json", encoding="utf-8")
    rows = {x["id"]: x for x in store.list_all()}
    assert rows["broken"]["damaged"] is True
    assert "记录损坏" in rows["broken"]["name"]
    assert store.delete("broken") is True


def test_r73_issue_count_is_not_check_count(tmp_projects, box_stl):
    proj = store.create("box.stl", box_stl)
    store.update(proj["id"], analysis={
        "items": [{"severity": "ok"}, {"severity": "warning"}, {"severity": "error"}],
        "summary": {"total": 7, "worst": "error"}})
    row = next(x for x in store.list_all() if x["id"] == proj["id"])
    assert row["issues"] == 2  # 不是 7


# ---------------------------------------------------------------- R74 中间产物回收
def test_r74_orphan_tmp_cleanup(tmp_path, monkeypatch):
    from app import jobs

    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(jobs, "JOB_FILE", tmp_path / "jobs.json")
    (tmp_path / "tmp" / "alive").mkdir(parents=True)
    (tmp_path / "tmp" / "done").mkdir()
    (tmp_path / "tmp" / "orphan").mkdir()
    (tmp_path / "jobs.json").write_text(
        json.dumps({"alive": {"status": "failed"}, "done": {"status": "done"}}),
        encoding="utf-8")
    res = jobs.cleanup_orphan_tmp()
    left = sorted(p.name for p in (tmp_path / "tmp").iterdir())
    assert res["removed"] == 2
    assert left == ["alive"]  # 失败任务留着，断点续跑要用


# ---------------------------------------------------------------- R75 界面提示
def test_r75_ui_renders_fallback_banner():
    js = (config.ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "function fallbackBanner" in js
    assert "输入被替换过" in js
    assert "a.fallbacks" in js


# ---------------------------------------------------------------- R76 接口枚举
def test_r76_advice_rejects_unknown_process_and_plan(tmp_projects, box_stl):
    from fastapi.testclient import TestClient

    proj = store.create("box.stl", box_stl)
    c = TestClient(main.app)
    assert c.get(f"/api/projects/{proj['id']}/advice?process=fdm2").status_code == 422
    assert c.get(f"/api/projects/{proj['id']}/advice?plan=fast").status_code == 422
    assert c.get(f"/api/projects/{proj['id']}/advice?process=FDM&plan=balanced").status_code == 200


# ---------------------------------------------------------------- R77/R78 文案与分量
def test_r77_degenerate_summary_leads_with_reason(tmp_path):
    proj = _project(tmp_path, path=tmp_path / "missing.stl")
    proj["analysis"] = {"stats": {"extents": [0, 0, 0], "volume_mm3": 0, "area_mm2": 0,
                                  "faces": 0, "is_watertight": False}, "items": []}
    adv = advisor.recommend(proj, {"process": "FDM", "printer": "bambu_a1"})
    assert "不可打印" in adv["summary"]
    assert "0.0g" not in adv["summary"]


def test_r78_components_recomputed_when_report_stale(tmp_path):
    a = trimesh.creation.box(extents=[10, 10, 10])
    b = trimesh.creation.box(extents=[10, 10, 10])
    b.apply_translation([30, 0, 0])
    src = tmp_path / "two.stl"
    trimesh.util.concatenate([a, b]).export(str(src))

    proj = _project(tmp_path, name="two.stl", path=src)
    proj["analysis_stale"] = True   # 报告不可用
    geo = advisor._geometry(proj)
    assert geo["components"] == 2
    assert geo["components_known"] is True
