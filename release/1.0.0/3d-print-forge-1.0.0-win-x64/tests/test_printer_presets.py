"""机型预设与建议联动的回归测试。

这一组守的是「换了机型/材料/喷嘴之后，参数到底有没有真的跟着变」，
以及边界输入不会产出非法值。改动面大，没有这些测试很容易悄悄回退。
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import advisor, calibration, printers  # noqa: E402

P1MAX = "anycubic_photon_p1_max"
FDM = "bambu_x1c"


def _proj(ext=(40, 40, 50), volume=None, wall=None, faces=12, watertight=True):
    vol = volume if volume is not None else ext[0] * ext[1] * ext[2] * 0.6
    items = []
    if wall is not None:
        items = [{"id": "thickness", "value": {
            "samples": 900, "min": wall, "p5": wall * 1.1,
            "median": wall * 1.2, "max": 6.0}}]
    return {"id": "t", "name": "m", "source": "", "current": "", "analysis": {
        "stats": {"extents": list(ext), "volume_mm3": vol,
                  "area_mm2": 2 * (ext[0] * ext[1] + ext[1] * ext[2] + ext[0] * ext[2]),
                  "faces": faces, "is_watertight": watertight},
        "items": items}}


def _kv(adv, key):
    return next((p["value"] for p in adv["params"] if p["key"] == key), None)


# ---------------------------------------------------------------- 预设库
def test_p1max_required_preset_exists():
    """用户自用机型必须在库里，且关键规格不能为空。"""
    p = printers.get_preset(P1MAX)
    assert p is not None
    assert abs(p["build"][0] - 285.5) < 0.01
    assert p["process"] == "SLA"
    assert p["irradiance"] > 0
    assert 0 < p["xy_res"] < 0.1
    assert p["layer_range"][0] < p["layer_range"][1]


def test_default_sla_is_p1max():
    assert printers.DEFAULT_PRESET["SLA"] == P1MAX


def test_preset_validation_clean():
    assert printers.validate_presets() == []


@pytest.mark.parametrize("patch,frag", [
    ({"layer_range": [0.4, 0.1]}, "区间非法"),
    ({"max_flow": 0}, "max_flow"),
    ({"layer_range": [0.1, 0.9]}, "超过喷嘴"),
    ({"nozzle": 0}, "nozzle"),
])
def test_validation_catches_bad_data(patch, frag):
    base = copy.deepcopy(printers.PRESETS[FDM])
    printers.PRESETS["__bad__"] = {**base, **patch, "key": "__bad__"}
    try:
        probs = printers.validate_presets()
        assert any("__bad__" in p and frag in p for p in probs), probs
    finally:
        printers.PRESETS.pop("__bad__", None)


def test_aliases_all_resolve():
    for alias, target in printers.ALIASES.items():
        assert target in printers.PRESETS
        merged, _ = printers.resolve(alias)
        assert merged["key"] == target or merged["resolved_from"] == "preset"


def test_unknown_key_falls_back():
    merged, _ = printers.resolve("no_such_printer", "SLA")
    assert merged["resolved_from"] == "fallback"
    assert merged["process"] == "SLA"


# ---------------------------------------------------------------- 机型联动
def test_printer_changes_exposure():
    """换机型必须换曝光——不同机型照度差一倍很常见。"""
    p1 = advisor.recommend(_proj(), {"printer": P1MAX, "material": "standard"})
    mars = advisor.recommend(_proj(), {"printer": "elegoo_mars_4", "material": "standard"})
    assert _kv(p1, "exposure") != _kv(mars, "exposure")
    assert _kv(mars, "exposure") > _kv(p1, "exposure"), "Mars 4 照度更低，曝光应更长"


def test_material_changes_exposure():
    a = advisor.recommend(_proj(), {"printer": P1MAX, "material": "standard"})
    b = advisor.recommend(_proj(), {"printer": P1MAX, "material": "tough"})
    assert _kv(a, "exposure") != _kv(b, "exposure")


def test_official_exposure_anchor_used():
    """有厂家标定值的组合，应直接给到标定值（锚点），而不是纯物理外推。"""
    adv = advisor.recommend(_proj(), {"printer": P1MAX, "material": "water_wash"})
    exp = next(p for p in adv["params"] if p["key"] == "exposure")
    anchor = printers.PRESETS[P1MAX]["official_exposure"]["water_wash"]
    assert abs(exp["value"] - anchor) < 0.15, f"锚点 {anchor}s，实际给到 {exp['value']}s"
    assert "厂家" in exp["reason"]


def test_authoritative_process_from_printer():
    """选了 FDM 机型，就不该按光固化出参数。"""
    adv = advisor.recommend(_proj(), {"printer": FDM, "process": "SLA"})
    assert adv["process"] == "FDM"
    assert _kv(adv, "exposure") is None


# ---------------------------------------------------------------- 喷嘴
@pytest.mark.parametrize("nozzle,expect", [(0.4, 0.4), (0.6, 0.6), (0.8, 0.8)])
def test_nozzle_override_applied(nozzle, expect):
    adv = advisor.recommend(_proj(), {"printer": FDM, "nozzle": nozzle})
    assert _kv(adv, "nozzle") == expect


def test_nozzle_unsupported_falls_back():
    adv = advisor.recommend(_proj(), {"printer": FDM, "nozzle": 0.5})
    assert _kv(adv, "nozzle") == 0.4
    assert any("不支持" in w and "喷嘴" in w for w in adv["warnings"])


def test_nozzle_changes_wall_thickness():
    a = advisor.recommend(_proj(), {"printer": FDM, "nozzle": 0.4})
    b = advisor.recommend(_proj(), {"printer": FDM, "nozzle": 0.8})
    assert _kv(a, "wall_thickness") != _kv(b, "wall_thickness")


# ---------------------------------------------------------------- 边界
def test_layer_height_always_in_range():
    """任何机型×方案组合，层高都必须落在机型支持的区间内。"""
    for key in (P1MAX, "elegoo_mars_4", FDM, "prusa_mini"):
        pr = printers.get_preset(key)
        lo, hi = pr["layer_range"]
        for plan in ("aggressive", "balanced", "conservative"):
            adv = advisor.recommend(_proj(), {"printer": key, "plan": plan})
            lh = _kv(adv, "layer_height")
            assert lo - 1e-6 <= lh <= hi + 1e-6, f"{key}/{plan} 层高 {lh} 越界 [{lo},{hi}]"


def test_round_in_range_edge_cases():
    f = advisor._round_in_range
    assert f(0.298, 0.01, 0.05, 0.298) <= 0.298
    assert f(0.021, 0.005, 0.021, 0.10) >= 0.021
    assert f(0.9, 0.005, 0.03, 0.15) == 0.15
    assert f(0.05, 0.005, 0.10, 0.05) == 0.10      # 区间倒置时取更细一侧
    assert f(0.05, 0.0, 0.03, 0.15) > 0            # 零步长不除零


def test_oversize_model_infeasible():
    adv = advisor.recommend(_proj(ext=(400, 220, 100)), {"printer": P1MAX})
    assert adv["feasible"] is False
    assert adv["build_fit"]["fits"] is False
    assert any("成型空间" in w for w in adv["warnings"])


def test_axis_swapped_still_infeasible():
    """290 无论怎么转都进不了 285.5 的宽度。"""
    adv = advisor.recommend(_proj(ext=(290, 200, 295)), {"printer": P1MAX})
    assert adv["feasible"] is False


def test_normal_model_feasible():
    adv = advisor.recommend(_proj(ext=(120, 90, 150)), {"printer": P1MAX})
    assert adv["feasible"] is True
    assert adv["degenerate"] is None


@pytest.mark.parametrize("kw,frag", [
    ({"faces": 0, "volume": 0.0}, "没有任何三角面"),
    ({"volume": 0.0}, "空壳"),
    ({"ext": (40, 40, 0.02), "volume": 30.0}, "还矮"),
])
def test_degenerate_models_blocked(kw, frag):
    adv = advisor.recommend(_proj(**kw), {"printer": P1MAX})
    assert adv["feasible"] is False
    assert adv["degenerate"] and frag in adv["degenerate"]
    assert adv["confidence"]["score"] < 0.6


def test_thin_wall_below_pixel_warns():
    adv = advisor.recommend(_proj(wall=0.03), {"printer": P1MAX})
    assert any("像素" in w for w in adv["warnings"])


def test_isolated_spike_not_treated_as_real():
    """最小值远小于 5% 分位数 → 判伪影，改用分位数。"""
    proj = _proj()
    proj["analysis"]["items"] = [{"id": "thickness", "value": {
        "samples": 900, "min": 0.01, "p5": 1.2, "median": 1.5, "max": 6.0}}]
    adv = advisor.recommend(proj, {"printer": P1MAX})
    assert adv["geometry"]["min_wall"] == pytest.approx(1.2, abs=1e-6)


def test_broken_measurement_discarded():
    """最大壁厚都只有 0.076mm → 整组判废。"""
    proj = _proj()
    proj["analysis"]["items"] = [{"id": "thickness", "value": {
        "samples": 28, "min": 0.001, "p5": 0.003, "median": 0.019, "max": 0.076}}]
    adv = advisor.recommend(proj, {"printer": P1MAX})
    assert adv["geometry"]["min_wall"] is None
    assert adv["geometry"]["wall_note"]


# ---------------------------------------------------------------- 缓存
def test_sections_cache_keeps_geometry_identical(tmp_path):
    m = trimesh.creation.box(extents=(40, 40, 50))
    f = tmp_path / "c.stl"
    m.export(str(f))
    proj = {"id": "t", "name": "c.stl", "source": str(f), "current": str(f), "analysis": None}
    a = advisor.recommend(proj, {"printer": P1MAX, "plan": "balanced"})
    b = advisor.recommend(proj, {"printer": P1MAX, "plan": "conservative"})
    assert a["geometry"]["volume_mm3"] == b["geometry"]["volume_mm3"]
    assert a["geometry"]["overhang_ratio"] == b["geometry"]["overhang_ratio"]
    assert _kv(a, "layer_height") != _kv(b, "layer_height")


# ---------------------------------------------------------------- 接口
def test_materials_api(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        for proc, first in (("SLA", "standard"), ("FDM", "PLA")):
            d = c.get(f"/api/materials?process={proc}").json()
            assert d["process"] == proc
            assert d["materials"] and d["default"]
            assert all(m["name"] for m in d["materials"])


def test_unknown_printer_404_has_hint():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        r = c.get("/api/printers/definitely_not_here")
        assert r.status_code == 404
        detail = r.json()["detail"]
        assert "available" in detail and P1MAX in detail["available"]


def test_export_follows_selected_printer(tmp_path):
    from fastapi.testclient import TestClient

    from app import store
    from app.main import app

    m = trimesh.creation.box(extents=(40, 40, 50))
    f = tmp_path / "e.stl"
    m.export(str(f))
    saved = store.save({"id": "expt", "name": "e.stl", "source": str(f), "current": str(f)})
    try:
        with TestClient(app) as c:
            a = c.get(f"/api/projects/{saved['id']}/download?kind=slicer").json()
            b = c.get(f"/api/projects/{saved['id']}/download?kind=slicer"
                      "&printer=elegoo_mars_4&material=tough").json()
            assert a["printer_preset"]["key"] == P1MAX
            assert b["printer_preset"]["key"] == "elegoo_mars_4"
            assert a["params"]["exposure_time_s"] != b["params"]["exposure_time_s"]
            assert "feasible" in b and "printer_preset" in b
    finally:
        store.delete("expt")


@pytest.fixture()
def clean_cal():
    calibration.clear()
    yield
    calibration.clear()


def test_calibration_scoped_to_printer(clean_cal):
    """A 机器的历史不该改 B 机器的建议。"""
    for _ in range(8):
        calibration.add_record("SLA", P1MAX, "standard", {"hours": 2.0}, {"hours": 3.0})
    here = calibration.estimate("SLA", P1MAX, "standard", "hours")
    other = calibration.estimate("SLA", "elegoo_mars_4", "standard", "hours")
    assert here["factor"] > 1.0
    assert other["factor"] == 1.0
