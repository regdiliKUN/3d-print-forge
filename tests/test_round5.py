"""第五轮：切片截面、旋转微调、切片配置导出、批量 CSV、分件避细节。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config, split, store  # noqa: E402


@pytest.fixture()
def live(tmp_path):
    src = tmp_path / "r5.stl"
    trimesh.creation.box(extents=(40, 40, 40)).export(str(src))
    proj = store.create("r5.stl", src)
    yield proj
    store.delete(proj["id"])


# ---------------------------------------------------------------- 切片截面
def test_section_area_matches_box(live):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        d = c.get(f"/api/projects/{live['id']}/section?z=0").json()
        assert d["count"] == 1
        assert d["area"] == pytest.approx(1600.0, rel=0.01)
        assert len(d["paths"][0]) >= 3


def test_section_empty_outside_model(live):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        d = c.get(f"/api/projects/{live['id']}/section?z=500").json()
        assert d["count"] == 0
        assert d["area"] == 0


def test_section_reports_bounds(live):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        d = c.get(f"/api/projects/{live['id']}/section?z=0").json()
        assert d["zmin"] == pytest.approx(-20.0, abs=0.1)
        assert d["zmax"] == pytest.approx(20.0, abs=0.1)


# ---------------------------------------------------------------- 旋转
def test_rotate_swaps_extents(live):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post(f"/api/projects/{live['id']}/rotate", json={"axis": "x", "degrees": 90})
        assert r.status_code == 200
        assert r.json()["extents"] == pytest.approx([40.0, 40.0, 40.0], abs=0.2)


def test_rotate_rejects_bad_axis(live):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post(f"/api/projects/{live['id']}/rotate", json={"axis": "w", "degrees": 90})
        assert r.status_code == 400


def test_rotate_lands_on_platform(live):
    from fastapi.testclient import TestClient

    from app.main import app
    from app import meshio

    with TestClient(app) as c:
        c.post(f"/api/projects/{live['id']}/rotate", json={"axis": "y", "degrees": 45})
        p = c.get(f"/api/projects/{live['id']}").json()
        m = meshio.load_mesh(Path(p["current"]))
        assert abs(float(m.bounds[0][2])) < 0.05


# ---------------------------------------------------------------- 导出
def test_slicer_config_export(live):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        c.post(f"/api/projects/{live['id']}/analyze", json={})
        r = c.get(f"/api/projects/{live['id']}/download?kind=slicer")
        assert r.status_code == 200
        d = r.json()
        assert d["process"] in ("SLA", "FDM")
        assert "params" in d and "layer_height_mm" in d["params"]
        assert d["model"]["watertight"] is True


def test_batch_csv_export(live):
    from fastapi.testclient import TestClient

    from app.main import app

    # 先造一份批量报告
    config.STATE.mkdir(parents=True, exist_ok=True)
    (config.STATE / "batch_report.json").write_text(
        '{"total":1,"ok":1,"failed":0,"items":[{"project_id":"x","name":"a.stl",'
        '"status":"ok","faces":12,"output":"/tmp/a.stl","warnings":["薄壁"]}]}',
        encoding="utf-8",
    )
    with TestClient(app) as c:
        r = c.get("/api/batch/report.csv")
        assert r.status_code == 200
        text = r.text
        assert text.startswith("\ufeff"), "缺 BOM，Excel 打开会乱码"
        assert "a.stl" in text and "薄壁" in text


# ---------------------------------------------------------------- 分件避细节
def test_split_avoids_dense_region():
    """细节密集区应该被绕开：切位要偏离等分点。"""
    body = trimesh.creation.cylinder(radius=20, height=120, sections=64)
    body.apply_translation([0, 0, 60])
    detail = trimesh.creation.icosphere(subdivisions=3, radius=12)
    detail.apply_translation([0, 0, 110])
    m = trimesh.util.concatenate([body, detail])
    p = split.plan(m, 50, "z")
    even = [120 * k / 3 for k in (1, 2)]
    assert len(p["cuts"]) == 2
    # 至少有一刀明显偏离等分点
    assert any(abs(a - b) > 2 for a, b in zip(p["cuts"], even))


def test_split_still_volume_conserving_after_refine():
    body = trimesh.creation.cylinder(radius=20, height=120, sections=64)
    body.apply_translation([0, 0, 60])
    detail = trimesh.creation.icosphere(subdivisions=3, radius=12)
    detail.apply_translation([0, 0, 110])
    m = trimesh.util.concatenate([body, detail])
    p = split.plan(m, 50, "z")
    pieces = split.split_mesh(m, p["cuts"], p["axis_index"])
    assert len(pieces) == p["pieces"]
    assert sum(float(x.volume) for x in pieces) == pytest.approx(float(m.volume), rel=0.005)


# ---------------------------------------------------------------- 预设一致性
def test_preset_params_match_backend_defaults():
    """前端预设里的键必须在后端 DEFAULTS 里存在，否则静默失效。"""
    from app import process

    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    start = js.index("const PRESETS")
    end = js.index("const SEV")
    block = js[start:end]
    import re

    keys = set(re.findall(r"([a-z_]+):\s*(?:[\d.\"]|true|false)", block))
    known = set(process.DEFAULTS.keys())
    unknown = {k for k in keys if k not in known and k not in {"label", "hint", "proc"}}
    assert not unknown, f"预设里有后端不认识的参数：{unknown}"
