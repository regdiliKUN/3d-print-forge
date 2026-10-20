"""第三轮：问题定位、支撑预览、拼盘、HTML 报告、英寸识别。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import analysis, pack, process, store  # noqa: E402


# ---------------------------------------------------------------- 连通体分色
def test_component_field_counts_parts():
    a = trimesh.creation.icosphere(subdivisions=2, radius=10)
    b = trimesh.creation.box(extents=(8, 8, 8))
    b.apply_translation([25, 0, 0])
    c = trimesh.creation.box(extents=(4, 4, 4))
    c.apply_translation([0, 25, 0])
    m = trimesh.util.concatenate([a, b, c])
    f = analysis.component_field(m)
    assert f["components"] == 3
    assert len(f["values"]) == len(m.vertices)
    assert len(f["sizes"]) == 3
    # 面积降序
    assert f["sizes"][0]["area"] >= f["sizes"][-1]["area"]


def test_component_field_single_part():
    m = trimesh.creation.box(extents=(10, 10, 10))
    f = analysis.component_field(m)
    assert f["components"] == 1


# ---------------------------------------------------------------- 英寸识别
@pytest.mark.parametrize("ext,expect", [
    ((254, 100, 100), "mm?"),        # 10 英寸
    ((203.2, 100, 100), "mm?"),      # 8 英寸
    ((150, 100, 80), "mm"),
    ((0.25, 0.2, 0.2), "m"),
])
def test_infer_units_inch_hint(ext, expect):
    assert analysis.infer_units(ext)["unit"] == expect


def test_check_units_inch_is_info_not_warning():
    m = trimesh.creation.box(extents=(254, 100, 100))
    r = analysis.check_units(m, {})
    assert r["severity"] == "info"
    assert "10 英寸" in r["message"]


def test_units_fix_carries_factor():
    m = trimesh.creation.box(extents=(0.25, 0.2, 0.2))
    r = analysis.check_units(m, {})
    assert r["fix"]["action"] == "rescale"
    assert r["fix"]["factor"] == pytest.approx(1000.0)


# ---------------------------------------------------------------- 支撑线段
def test_supports_return_segments():
    plate = trimesh.creation.box(extents=(20, 20, 2))
    plate.apply_translation([0, 0, 20])
    sup, info = process.build_supports(plate, 45.0, 1.0, 4.0, 50)
    assert info["count"] > 0
    segs = info["segments"]
    assert len(segs) == info["count"]
    for s in segs[:5]:
        assert len(s) == 4
        assert s[3] > s[2]  # 终点高于起点
    assert sup is not None and sup.is_watertight


def test_supports_no_segments_when_flat():
    m = trimesh.creation.box(extents=(20, 20, 20))
    _, info = process.build_supports(m, 45.0, 1.0, 4.0, 50)
    assert info["count"] == 0


# ---------------------------------------------------------------- 拼盘
def test_pack_places_all_small_parts():
    items = [trimesh.creation.box(extents=(40, 40, 10)) for _ in range(4)]
    placed, skipped = pack.pack(items, (192, 120), gap=4)
    assert len(placed) == 4
    assert not skipped
    # 不重叠
    for i in range(len(placed)):
        for j in range(i + 1, len(placed)):
            a, b = placed[i], placed[j]
            overlap_x = a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
            overlap_y = a["y"] < b["y"] + b["d"] and b["y"] < a["y"] + a["d"]
            assert not (overlap_x and overlap_y)


def test_pack_skips_oversize():
    items = [trimesh.creation.box(extents=(300, 50, 10))]
    placed, skipped = pack.pack(items, (192, 120))
    assert not placed
    assert len(skipped) == 1
    assert "超出平台" in skipped[0]["reason"]


def test_pack_respects_platform_height():
    items = [trimesh.creation.box(extents=(60, 60, 10)) for _ in range(6)]
    placed, skipped = pack.pack(items, (192, 120), gap=4)
    assert len(placed) + len(skipped) == 6
    for it in placed:
        assert it["y"] + it["d"] <= 120.5


# ---------------------------------------------------------------- 接口
@pytest.fixture()
def live(tmp_path):
    src = tmp_path / "r3.stl"
    body = trimesh.creation.icosphere(subdivisions=2, radius=15)
    body.update_faces(np.arange(len(body.faces)) > 2)
    tiny = trimesh.creation.box(extents=(0.3, 0.3, 0.3))
    tiny.apply_translation([40, 0, 0])
    trimesh.util.concatenate([body, tiny]).export(str(src))
    proj = store.create("r3.stl", src)
    yield proj
    store.delete(proj["id"])


def test_issues_endpoint_finds_holes_and_debris(live):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.get(f"/api/projects/{live['id']}/issues")
        assert r.status_code == 200
        d = r.json()
        assert d["counts"]["holes"] > 0
        assert d["counts"]["debris"] >= 1
        for p in d["holes"]:
            assert len(p) == 3


def test_preview_supports_endpoint(live):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post(f"/api/projects/{live['id']}/preview-supports",
                   json={"overhang_deg": 45, "diameter": 1.0, "spacing": 4.0})
        assert r.status_code == 200
        d = r.json()
        assert "segments" in d
        assert d["count"] == len(d["segments"])


def test_heatmap_components_kind(live):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.get(f"/api/projects/{live['id']}/heatmap?kind=components&max_faces=20000")
        assert r.status_code == 200
        d = r.json()
        assert d["kind"] == "components"
        assert d["meta"]["components"] >= 2


def test_html_report_renders(live):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        c.post(f"/api/projects/{live['id']}/analyze", json={})
        r = c.get(f"/api/projects/{live['id']}/report.html")
        assert r.status_code == 200
        html = r.text
        assert "<!DOCTYPE html>" in html
        assert "检测报告" in html
        assert "r3.stl" in html
        # 自包含：不引用外部资源
        assert "http://" not in html.split("</style>")[1]
        assert "<script" not in html
