"""方案切换 + 多来源交叉查证 + 信源汇总的测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import advisor, analysis, meshio  # noqa: E402


def _project(tmp_path, mesh, name="m.stl"):
    p = tmp_path / name
    mesh.export(str(p))
    items = [fn(mesh, {"sample_faces": 400}) for fn in analysis.CHECKS]
    return {
        "id": "test", "name": name, "source": str(p), "current": str(p),
        "analysis": {"stats": meshio.mesh_stats(mesh), "items": items, "summary": analysis.summarize(items)},
    }


def _val(r, key):
    return next(p["value"] for p in r["params"] if p["key"] == key)


def test_default_plan_is_balanced(tmp_path):
    r = advisor.recommend(_project(tmp_path, trimesh.creation.icosphere(subdivisions=2, radius=20)))
    assert r["plan"] == "balanced"
    assert set(r["plans"]) == {"aggressive", "balanced", "conservative"}
    for k in ("aggressive", "balanced", "conservative"):
        assert r["plans"][k]["label"] and r["plans"][k]["desc"]


def test_plans_change_layer_height(tmp_path):
    proj = _project(tmp_path, trimesh.creation.icosphere(subdivisions=2, radius=20))
    lh = {k: _val(advisor.recommend(proj, {"plan": k}), "layer_height")
          for k in ("aggressive", "balanced", "conservative")}
    assert lh["aggressive"] > lh["conservative"]
    assert lh["aggressive"] >= lh["balanced"] >= lh["conservative"]


def test_conservative_more_infill_fdm(tmp_path):
    proj = _project(tmp_path, trimesh.creation.box(extents=(40, 40, 20)), "b.stl")
    base = {"process": "FDM", "printer": "fdm_220", "material": "PLA"}
    agg = _val(advisor.recommend(proj, {**base, "plan": "aggressive"}), "infill")
    con = _val(advisor.recommend(proj, {**base, "plan": "conservative"}), "infill")
    assert con > agg


def test_sources_present(tmp_path):
    r = advisor.recommend(_project(tmp_path, trimesh.creation.icosphere(subdivisions=2, radius=20)))
    assert len(r["sources"]) == 3
    titles = " ".join(g["title"] for g in r["sources"])
    assert "几何量" in titles and "基准" in titles and "计算依据" in titles
    for g in r["sources"]:
        assert g["items"] and all(isinstance(x, str) and x for x in g["items"])


def test_rationale_and_cross_verification(tmp_path):
    # 薄壁件会触发「壁厚」这一路信号，层高应出现交叉查证说明
    thin = _project(tmp_path, trimesh.creation.box(extents=(30, 30, 0.8)), "thin.stl")
    r = advisor.recommend(thin)
    assert r["rationale"]
    assert any("层高交叉查证" in x for x in r["rationale"])


def test_unknown_plan_falls_back_to_balanced(tmp_path):
    proj = _project(tmp_path, trimesh.creation.icosphere(subdivisions=2, radius=20))
    assert advisor.recommend(proj, {"plan": "nonsense"})["plan"] == "balanced"
