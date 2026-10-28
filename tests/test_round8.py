"""第八轮：项目复制、模板导入导出、孔位避薄壁、镂空成本估算。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import advisor, process, store  # noqa: E402


# ---------------------------------------------------------------- 项目复制
def test_duplicate_project(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    src = tmp_path / "orig.stl"
    trimesh.creation.box(extents=(10, 10, 10)).export(str(src))
    proj = store.create("orig.stl", src)
    store.update(proj["id"], tags=["手办"], favorite=True)
    dup_id = None
    try:
        with TestClient(app) as c:
            r = c.post(f"/api/projects/{proj['id']}/duplicate")
            assert r.status_code == 200
            d = r.json()
            dup_id = d["id"]
            assert dup_id != proj["id"]
            assert "副本" in d["name"]
            assert d["tags"] == ["手办"]
            assert d["favorite"] is True
            # 副本有独立历史
            assert d["history"] == []
    finally:
        store.delete(proj["id"])
        if dup_id:
            store.delete(dup_id)


def test_duplicate_missing_project():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        assert c.post("/api/projects/nope/duplicate").status_code == 404


# ---------------------------------------------------------------- 模板导入导出
def test_template_export_import_roundtrip(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        before = c.get("/api/templates").json()["templates"]
        c.post("/api/templates", json={"name": "往返测试", "params": {"process": {"wall_thickness": 2.5}}})
        exp = c.get("/api/templates/export")
        assert exp.status_code == 200
        payload = exp.json()
        assert payload["version"] == 1
        assert any(t["name"] == "往返测试" for t in payload["templates"])

        # 再导回去，同名应加后缀而不是覆盖
        r = c.post("/api/templates/import", json={"templates": payload["templates"]})
        assert r.status_code == 200
        assert r.json()["added"] == len(payload["templates"])
        names = [t["name"] for t in c.get("/api/templates").json()["templates"]]
        assert any(n.endswith("(导入)") for n in names)

        # 清理本次创建的
        for t in c.get("/api/templates").json()["templates"]:
            if t["name"] not in {x["name"] for x in before}:
                c.delete(f"/api/templates/{t['id']}")


def test_template_import_rejects_bad_payload():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        assert c.post("/api/templates/import", json={"templates": "not-a-list"}).status_code == 400
        assert c.post("/api/templates/import", json={}).status_code == 400


# ---------------------------------------------------------------- 孔位避薄壁
def test_drill_rejects_too_thin_plate():
    """0.5mm 板低于开孔最低壁厚，应明确拒绝而不是打出豁口。"""
    thin = trimesh.creation.box(extents=(40, 40, 0.5))
    with pytest.raises(ValueError) as ei:
        process.drill_holes(thin, 2, 3.0, 2.0)
    assert "加厚" in str(ei.value) or "孔径" in str(ei.value)


def test_drill_works_on_thick_plate():
    plate = trimesh.creation.box(extents=(40, 40, 3.0))
    out, info = process.drill_holes(plate, 2, 3.0, 2.0)
    assert info["holes"] == 2
    assert out.is_watertight
    assert "skipped_thin" in info


def test_drill_reports_skipped_thin_count():
    """过滤逻辑要报告跳过了多少位置，方便判断底面够不够用。"""
    plate = trimesh.creation.box(extents=(60, 60, 1.2))
    _, info = process.drill_holes(plate, 2, 3.0, 1.2)
    assert isinstance(info.get("skipped_thin"), int)


# ---------------------------------------------------------------- 镂空成本
def test_cost_uses_shell_volume_when_hollow():
    """镂空件的耗材应按壳体积算，不是整件实心体积。"""
    proj = {
        "id": "t", "name": "t", "source": "", "current": "",
        "analysis": {"stats": {"extents": [40, 40, 80], "volume_mm3": 100000,
                               "faces": 1000, "area_mm2": 20000, "is_watertight": True},
                     "items": []},
    }
    adv = advisor.recommend(proj)
    hollow = next(p for p in adv["params"] if p["key"] == "hollow")
    assert hollow["value"] is True, "这个体积应该建议镂空"
    eff = adv["cost"]["effective_volume_cm3"]
    assert eff < 100.0, f"镂空后仍按实心算：{eff}cm³"
    assert "表面积" in adv["cost"]["basis"]


def test_cost_uses_solid_volume_when_not_hollow():
    proj = {
        "id": "t", "name": "t", "source": "", "current": "",
        "analysis": {"stats": {"extents": [20, 20, 20], "volume_mm3": 8000,
                               "faces": 100, "area_mm2": 2400, "is_watertight": True},
                     "items": []},
    }
    adv = advisor.recommend(proj)
    hollow = next(p for p in adv["params"] if p["key"] == "hollow")
    assert hollow["value"] is False
    assert adv["cost"]["effective_volume_cm3"] == pytest.approx(8.0, abs=0.2)
    assert "实心体积" in adv["cost"]["basis"]


def test_effective_volume_never_exceeds_solid():
    """壳体积估算不能超过实心体积（小件镂空反而更费料）。"""
    g = {"volume_mm3": 1000, "area_mm2": 5000}
    eff, _ = advisor._effective_volume(g, hollow=True, wall=2.0)
    assert eff <= 1000


# ---------------------------------------------------------------- 压测脚本存在
def test_stress_script_exists():
    p = ROOT / "tests" / "stress.py"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "jobs.submit" in text
    assert "内存增长可控" in text
