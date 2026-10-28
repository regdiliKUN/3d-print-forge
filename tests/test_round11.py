"""第十一轮：精确尺寸缩放、平台居中、层耗时、打印队列、分件装配位置。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config, split, store  # noqa: E402


@pytest.fixture()
def box_proj(tmp_path):
    src = tmp_path / "box.stl"
    m = trimesh.creation.box(extents=(20, 30, 40))
    m.apply_translation([50, 50, 20])
    m.export(str(src))
    proj = store.create("box.stl", src)
    yield proj
    store.delete(proj["id"])


# ---------------------------------------------------------------- 缩放
def test_resize_locked(box_proj):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post(f"/api/projects/{box_proj['id']}/resize",
                   json={"height": 80, "lock_ratio": True})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["extents"] == pytest.approx([40.0, 60.0, 80.0], abs=0.2)
        assert d["factors"][0] == pytest.approx(2.0, abs=0.01)


def test_resize_unlocked(box_proj):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post(f"/api/projects/{box_proj['id']}/resize",
                   json={"width": 10, "depth": 60, "height": 20, "lock_ratio": False})
        assert r.status_code == 200
        assert r.json()["extents"] == pytest.approx([10.0, 60.0, 20.0], abs=0.2)


def test_resize_requires_target(box_proj):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        assert c.post(f"/api/projects/{box_proj['id']}/resize",
                      json={"lock_ratio": True}).status_code == 400
        assert c.post(f"/api/projects/{box_proj['id']}/resize",
                      json={"width": 0}).status_code == 422


def test_resize_lands_on_platform(box_proj):
    from fastapi.testclient import TestClient

    from app.main import app
    from app import meshio

    with TestClient(app) as c:
        c.post(f"/api/projects/{box_proj['id']}/resize", json={"height": 50})
        p = c.get(f"/api/projects/{box_proj['id']}").json()
        m = meshio.load_mesh(Path(p["current"]))
        assert abs(float(m.bounds[0][2])) < 0.05


# ---------------------------------------------------------------- 居中
def test_center_puts_xy_at_origin(box_proj):
    from fastapi.testclient import TestClient

    from app.main import app
    from app import meshio

    with TestClient(app) as c:
        r = c.post(f"/api/projects/{box_proj['id']}/center")
        assert r.status_code == 200
        assert r.json()["moved"] == pytest.approx([-50.0, -50.0], abs=0.1)
        p = c.get(f"/api/projects/{box_proj['id']}").json()
        m = meshio.load_mesh(Path(p["current"]))
        cx = float((m.bounds[0][0] + m.bounds[1][0]) / 2)
        cy = float((m.bounds[0][1] + m.bounds[1][1]) / 2)
        assert abs(cx) < 0.1 and abs(cy) < 0.1
        assert abs(float(m.bounds[0][2])) < 0.1


# ---------------------------------------------------------------- 层耗时
def test_layer_times(box_proj):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        d = c.get(f"/api/projects/{box_proj['id']}/layer-times?samples=20&layer_h=0.05").json()
        assert len(d["layers"]) == 20
        assert d["total_layers"] == pytest.approx(800, abs=2)
        assert d["avg_seconds"] > 0
        assert d["est_hours"] > 0
        # 立方体每层面积恒定，耗时应基本一致
        secs = [r["seconds"] for r in d["layers"]]
        assert max(secs) - min(secs) < 2.0


def test_layer_times_rejects_bad_params(box_proj):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        assert c.get(f"/api/projects/{box_proj['id']}/layer-times?layer_h=0").status_code == 422
        assert c.get(f"/api/projects/{box_proj['id']}/layer-times?samples=2").status_code == 422


# ---------------------------------------------------------------- 打印队列
@pytest.fixture()
def queue_clean():
    qf = config.STATE / "print_queue.json"
    backup = qf.read_text(encoding="utf-8") if qf.exists() else None
    qf.unlink(missing_ok=True)
    yield
    if backup is not None:
        qf.write_text(backup, encoding="utf-8")
    else:
        qf.unlink(missing_ok=True)


def test_queue_add_and_dedup(tmp_path, queue_clean):
    from fastapi.testclient import TestClient

    from app.main import app

    ids = []
    for i in range(2):
        f = tmp_path / f"q{i}.stl"
        trimesh.creation.box(extents=(10, 10, 10)).export(str(f))
        ids.append(store.create(f"q{i}.stl", f)["id"])
    try:
        with TestClient(app) as c:
            c.post("/api/queue", json={"id": ids[0], "note": "优先"})
            c.post("/api/queue", json={"id": ids[1]})
            d = c.get("/api/queue").json()
            assert d["count"] == 2
            # 重复加入不产生副本
            r = c.post("/api/queue", json={"id": ids[0]})
            assert r.json()["note"] == "已在队列中"
            assert c.get("/api/queue").json()["count"] == 2
    finally:
        for i in ids:
            store.delete(i)


def test_queue_reorder(tmp_path, queue_clean):
    from fastapi.testclient import TestClient

    from app.main import app

    ids = []
    for i in range(3):
        f = tmp_path / f"r{i}.stl"
        trimesh.creation.box(extents=(10, 10, 10)).export(str(f))
        ids.append(store.create(f"r{i}.stl", f)["id"])
    try:
        with TestClient(app) as c:
            for i in ids:
                c.post("/api/queue", json={"id": i})
            rev = list(reversed(ids))
            c.post("/api/queue/reorder", json={"order": rev})
            got = [x["id"] for x in c.get("/api/queue").json()["items"]]
            assert got == rev
    finally:
        for i in ids:
            store.delete(i)


def test_queue_remove(tmp_path, queue_clean):
    from fastapi.testclient import TestClient

    from app.main import app

    f = tmp_path / "d.stl"
    trimesh.creation.box(extents=(10, 10, 10)).export(str(f))
    pid = store.create("d.stl", f)["id"]
    try:
        with TestClient(app) as c:
            c.post("/api/queue", json={"id": pid})
            assert c.get("/api/queue").json()["count"] == 1
            c.delete("/api/queue/0")
            assert c.get("/api/queue").json()["count"] == 0
            # 越界删除不报错
            assert c.delete("/api/queue/9").status_code == 200
    finally:
        store.delete(pid)


def test_queue_add_missing_project(queue_clean):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        assert c.post("/api/queue", json={"id": "nope"}).status_code == 404


# ---------------------------------------------------------------- 装配位置
def test_split_records_assembly_offset():
    body = trimesh.creation.cylinder(radius=20, height=120, sections=48)
    body.apply_translation([0, 0, 60])
    p = split.plan(body, 50, "z")
    pieces = split.split_mesh(body, p["cuts"], p["axis_index"])
    assert len(pieces) == p["pieces"]
    # 各块切割时的中心应该沿 z 递增，装配预览靠这个摆回原位
    centers = [float(x.bounds.mean(axis=0)[2]) for x in pieces]
    assert centers == sorted(centers)
    assert centers[0] < centers[-1]
