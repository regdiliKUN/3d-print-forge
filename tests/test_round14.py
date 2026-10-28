"""第十四轮：示例模型（引导用）。"""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import analysis, meshio, store  # noqa: E402


def test_demo_creates_project():
    from fastapi.testclient import TestClient

    from app.main import app

    pid = None
    try:
        with TestClient(app) as c:
            r = c.post("/api/demo")
            assert r.status_code == 200, r.text
            d = r.json()
            pid = d["id"]
            assert "示例" in d["name"]
            assert "示例" in (d.get("tags") or [])
            assert Path(d["source"]).exists()
    finally:
        if pid:
            store.delete(pid)


def test_demo_has_intentional_defects():
    """示例模型必须真的有问题，否则新用户走一遍流程看不到任何反馈。"""
    from fastapi.testclient import TestClient

    from app.main import app

    pid = None
    try:
        with TestClient(app) as c:
            d = c.post("/api/demo").json()
            pid = d["id"]
            m = meshio.load_mesh(Path(d["source"]))
            items = {i["id"]: i for i in (fn(m, {"sample_faces": 1200}) for fn in analysis.CHECKS)}

            # 破洞：必须有开放边
            assert items["watertight"]["severity"] in ("warning", "error")
            assert (items["watertight"]["value"] or {}).get("boundary_edges", 0) > 0

            # 法线反转：有向体积应为负
            assert float((items["normals"]["value"] or {}).get("signed_volume") or 0) < 0

            # 碎屑：连通体不止一个
            assert int((items["components"]["value"] or {}).get("components") or 1) >= 2
    finally:
        if pid:
            store.delete(pid)


def test_demo_quality_score_is_low():
    """带这么多缺陷，质量评分不该是 A。"""
    from fastapi.testclient import TestClient

    from app.main import app

    pid = None
    try:
        with TestClient(app) as c:
            d = c.post("/api/demo").json()
            pid = d["id"]
            m = meshio.load_mesh(Path(d["source"]))
            items = [fn(m, {"sample_faces": 1200}) for fn in analysis.CHECKS]
            q = analysis.quality_score({"items": items})
            assert q["grade"] in ("C", "D"), f"示例模型评分过高：{q['score']} {q['grade']}"
    finally:
        if pid:
            store.delete(pid)


def test_demo_is_repairable():
    """示例模型要能真的修好，不然引导到「一键修复」那步会卡住。"""
    from app import repair

    from fastapi.testclient import TestClient

    from app.main import app

    pid = None
    try:
        with TestClient(app) as c:
            d = c.post("/api/demo").json()
            pid = d["id"]
        m = meshio.load_mesh(Path(d["source"]))
        for action in ("merge_vertices", "fill_holes", "fix_normals", "remove_small"):
            m, _ = repair.ACTIONS[action](m, {"small_ratio": 0.02})
        assert m.is_watertight, "修复后应该水密"
        assert float(m.volume) > 0, "修复后体积应为正"
        # 球 + 薄板本来就是两个独立部件，只该清掉 1.2mm 的碎屑。
        # 所以修复后剩 2 个连通体是对的，不是 1 个。
        n, _ = analysis.union_components(m.faces, len(m.vertices))
        assert n == 2, f"应该只剩球和薄板两块，实际 {n} 块"
        # 碎屑体积 1.728mm³，清掉后总体积应接近球+薄板
        assert float(m.volume) > 30000
    finally:
        if pid:
            store.delete(pid)


def test_demo_appears_in_project_list():
    from fastapi.testclient import TestClient

    from app.main import app

    pid = None
    try:
        with TestClient(app) as c:
            pid = c.post("/api/demo").json()["id"]
            listed = c.get("/api/projects").json()["projects"]
            assert any(x["id"] == pid for x in listed)
    finally:
        if pid:
            store.delete(pid)
