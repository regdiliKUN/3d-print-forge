"""第七轮：布尔运算、模型合并、多格式导出、checkout 路径校验。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config, store  # noqa: E402


@pytest.fixture()
def pair(tmp_path):
    a_src = tmp_path / "box.stl"
    b_src = tmp_path / "sphere.stl"
    trimesh.creation.box(extents=(20, 20, 20)).export(str(a_src))
    trimesh.creation.icosphere(subdivisions=2, radius=12).export(str(b_src))
    pa = store.create("box.stl", a_src)
    pb = store.create("sphere.stl", b_src)
    yield pa, pb
    store.delete(pa["id"])
    store.delete(pb["id"])


# ---------------------------------------------------------------- 布尔运算
def test_boolean_union(pair):
    from fastapi.testclient import TestClient

    from app.main import app

    pa, pb = pair
    with TestClient(app) as c:
        r = c.post(f"/api/projects/{pa['id']}/boolean",
                   json={"op": "union", "other_id": pb["id"]})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["label"] == "并集"
        assert d["faces"] > 0
        # 并排布置时并集体积 = 两者之和
        assert d["volume_mm3"] == pytest.approx(8000 + 7238, rel=0.12)


def test_boolean_intersection_needs_overlap(pair):
    from fastapi.testclient import TestClient

    from app.main import app

    pa, pb = pair
    with TestClient(app) as c:
        r = c.post(f"/api/projects/{pa['id']}/boolean",
                   json={"op": "intersection", "other_id": pb["id"], "overlap": True})
        assert r.status_code == 200, r.text
        assert r.json()["volume_mm3"] > 0


def test_boolean_rejects_self(pair):
    from fastapi.testclient import TestClient

    from app.main import app

    pa, _ = pair
    with TestClient(app) as c:
        r = c.post(f"/api/projects/{pa['id']}/boolean",
                   json={"op": "union", "other_id": pa["id"]})
        assert r.status_code == 400


def test_boolean_rejects_bad_op(pair):
    from fastapi.testclient import TestClient

    from app.main import app

    pa, pb = pair
    with TestClient(app) as c:
        r = c.post(f"/api/projects/{pa['id']}/boolean",
                   json={"op": "xor", "other_id": pb["id"]})
        assert r.status_code == 400


def test_boolean_requires_watertight(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    a_src = tmp_path / "ok.stl"
    b_src = tmp_path / "broken.stl"
    trimesh.creation.box(extents=(10, 10, 10)).export(str(a_src))
    broken = trimesh.creation.box(extents=(10, 10, 10))
    import numpy as np

    broken.update_faces(np.arange(len(broken.faces)) != 0)
    broken.export(str(b_src))
    pa = store.create("ok.stl", a_src)
    pb = store.create("broken.stl", b_src)
    try:
        with TestClient(app) as c:
            r = c.post(f"/api/projects/{pa['id']}/boolean",
                       json={"op": "union", "other_id": pb["id"]})
            assert r.status_code == 400
            assert "水密" in r.json()["detail"]
    finally:
        store.delete(pa["id"])
        store.delete(pb["id"])


# ---------------------------------------------------------------- 合并
def test_merge_two_projects(pair):
    from fastapi.testclient import TestClient

    from app.main import app

    pa, pb = pair
    with TestClient(app) as c:
        r = c.post(f"/api/projects/{pa['id']}/merge", json={"project_ids": [pb["id"]]})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["count"] == 2
        assert d["faces"] == 12 + 320  # box 12 面 + icosphere(2) 320 面


def test_merge_requires_two(pair):
    from fastapi.testclient import TestClient

    from app.main import app

    pa, _ = pair
    with TestClient(app) as c:
        r = c.post(f"/api/projects/{pa['id']}/merge", json={"project_ids": []})
        assert r.status_code == 400


# ---------------------------------------------------------------- 多格式导出
@pytest.mark.parametrize("kind,marker", [("obj", b"v "), ("ply", b"ply")])
def test_export_extra_formats(pair, kind, marker):
    from fastapi.testclient import TestClient

    from app.main import app

    pa, _ = pair
    with TestClient(app) as c:
        r = c.get(f"/api/projects/{pa['id']}/download?kind={kind}")
        assert r.status_code == 200
        assert marker in r.content[:200]


def test_export_rejects_unknown_kind(pair):
    from fastapi.testclient import TestClient

    from app.main import app

    pa, _ = pair
    with TestClient(app) as c:
        assert c.get(f"/api/projects/{pa['id']}/download?kind=exe").status_code == 422


# ---------------------------------------------------------------- checkout
def test_checkout_within_outputs(pair):
    from fastapi.testclient import TestClient

    from app.main import app

    pa, _ = pair
    out = config.OUTPUTS / "_checkout_test.stl"
    trimesh.creation.box(extents=(30, 30, 30)).export(str(out))
    try:
        with TestClient(app) as c:
            r = c.post(f"/api/projects/{pa['id']}/checkout", json={"path": str(out)})
            assert r.status_code == 200
            assert c.get(f"/api/projects/{pa['id']}").json()["current"] == str(out.resolve())
    finally:
        out.unlink(missing_ok=True)


def test_checkout_rejects_outside_path(pair):
    """不能把任意系统文件设成工作副本。"""
    from fastapi.testclient import TestClient

    from app.main import app

    pa, _ = pair
    with TestClient(app) as c:
        for bad in ["C:/Windows/notepad.exe", "/etc/passwd", "../../secret.txt"]:
            r = c.post(f"/api/projects/{pa['id']}/checkout", json={"path": bad})
            assert r.status_code in (400, 404), f"{bad} 没被拦住"


def test_checkout_missing_file(pair):
    from fastapi.testclient import TestClient

    from app.main import app

    pa, _ = pair
    with TestClient(app) as c:
        r = c.post(f"/api/projects/{pa['id']}/checkout",
                   json={"path": str(config.OUTPUTS / "nope_does_not_exist.stl")})
        assert r.status_code == 404


# ---------------------------------------------------------------- 依赖清单
def test_requirements_lock_exists_and_pinned():
    lock = ROOT / "requirements.lock"
    assert lock.exists(), "缺少锁定依赖清单"
    lines = [ln.strip() for ln in lock.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) >= 20
    # 每行都必须是 name==version
    bad = [ln for ln in lines if "==" not in ln]
    assert not bad, f"未锁定版本的条目：{bad[:5]}"


def test_english_readme_exists():
    en = ROOT / "README.en.md"
    assert en.exists()
    text = en.read_text(encoding="utf-8")
    assert "3D Print Forge" in text
    assert "Quick start" in text
    assert len(text) > 3000
