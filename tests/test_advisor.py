"""参数建议与批处理测试：建议必须可追溯到几何量，失败必须归因到阶段。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import advisor, analysis, batch, meshio, store  # noqa: E402


def _project(tmp_path, mesh, name="m.stl", with_analysis=True):
    p = tmp_path / name
    mesh.export(str(p))
    proj = {"id": "test", "name": name, "source": str(p), "current": str(p), "analysis": None}
    if with_analysis:
        items = [fn(mesh, {"sample_faces": 400}) for fn in analysis.CHECKS]
        proj["analysis"] = {
            "stats": meshio.mesh_stats(mesh),
            "items": items,
            "summary": analysis.summarize(items),
        }
    return proj


# ---------------------------------------------------------------- 结构
def test_recommend_has_params_and_reasons(tmp_path):
    proj = _project(tmp_path, trimesh.creation.icosphere(subdivisions=2, radius=20))
    r = advisor.recommend(proj)
    assert r["process"] == "SLA"
    assert len(r["params"]) >= 4
    for p in r["params"]:
        assert p["key"] and p["label"]
        assert isinstance(p["reason"], str) and len(p["reason"]) > 8
    keys = {p["key"] for p in r["params"]}
    assert {"material", "layer_height", "exposure"} <= keys


def test_thin_wall_lowers_layer_height(tmp_path):
    thin = _project(tmp_path, trimesh.creation.box(extents=(30, 30, 0.8)), "thin.stl")
    thick = _project(tmp_path, trimesh.creation.box(extents=(30, 30, 10)), "thick.stl")
    lh_thin = next(p["value"] for p in advisor.recommend(thin)["params"] if p["key"] == "layer_height")
    lh_thick = next(p["value"] for p in advisor.recommend(thick)["params"] if p["key"] == "layer_height")
    assert lh_thin < lh_thick


def test_large_volume_suggests_hollow(tmp_path):
    big = _project(tmp_path, trimesh.creation.icosphere(subdivisions=3, radius=45), "big.stl")
    r = advisor.recommend(big)
    hollow = next(p for p in r["params"] if p["key"] == "hollow")
    assert hollow["value"] is True
    assert "cm³" in hollow["reason"]


def test_small_volume_skips_hollow(tmp_path):
    small = _project(tmp_path, trimesh.creation.icosphere(subdivisions=2, radius=10), "s.stl")
    hollow = next(p for p in advisor.recommend(small)["params"] if p["key"] == "hollow")
    assert hollow["value"] is False


def test_overhang_triggers_support(tmp_path):
    # T 形：立柱撑住顶板中心，顶板外沿大面积悬空，必须打支撑
    post = trimesh.creation.box(extents=(6, 6, 30))
    post.apply_translation([0, 0, 15])
    cap = trimesh.creation.box(extents=(40, 40, 3))
    cap.apply_translation([0, 0, 31.5])
    proj = _project(tmp_path, trimesh.util.concatenate([post, cap]), "tee.stl")
    sup = next(p for p in advisor.recommend(proj)["params"] if p["key"] == "supports")
    assert sup["value"] == "需要"


def test_flat_box_needs_no_support(tmp_path):
    proj = _project(tmp_path, trimesh.creation.box(extents=(20, 20, 20)), "box.stl")
    sup = next(p for p in advisor.recommend(proj)["params"] if p["key"] == "supports")
    assert sup["value"] == "可选"


def test_oversize_triggers_warning(tmp_path):
    huge = trimesh.creation.box(extents=(400, 100, 100))
    proj = _project(tmp_path, huge, "huge.stl")
    r = advisor.recommend(proj, {"printer": "sla_small"})
    assert any("成型空间" in w for w in r["warnings"])


def test_non_watertight_warns(tmp_path):
    m = trimesh.creation.box(extents=(20, 20, 20))
    m.update_faces(np.arange(len(m.faces)) != 0)
    proj = _project(tmp_path, m, "hole.stl")
    r = advisor.recommend(proj)
    assert any("不水密" in w for w in r["warnings"])


def test_fdm_branch_has_temperatures(tmp_path):
    proj = _project(tmp_path, trimesh.creation.box(extents=(40, 40, 20)), "f.stl")
    r = advisor.recommend(proj, {"process": "FDM", "printer": "fdm_220", "material": "PETG"})
    keys = {p["key"] for p in r["params"]}
    assert {"nozzle_temp", "bed_temp", "infill", "wall_thickness"} <= keys
    nozzle = next(p["value"] for p in r["params"] if p["key"] == "nozzle_temp")
    assert nozzle == 240


def test_works_without_analysis(tmp_path):
    proj = _project(tmp_path, trimesh.creation.icosphere(subdivisions=2, radius=15),
                    "noana.stl", with_analysis=False)
    r = advisor.recommend(proj)
    assert r["params"]
    assert r["geometry"]["max_dim"] > 0


# ---------------------------------------------------------------- 批处理
@pytest.fixture()
def live_projects(tmp_path):
    created = []
    for i, m in enumerate([
        trimesh.creation.box(extents=(20, 20, 20)),
        trimesh.creation.icosphere(subdivisions=2, radius=12),
    ]):
        p = tmp_path / f"live{i}.stl"
        m.export(str(p))
        created.append(store.create(f"live{i}.stl", p))
    yield created
    for proj in created:
        store.delete(proj["id"])


def test_batch_processes_all(live_projects):
    ids = [p["id"] for p in live_projects]
    res = [batch.process_one(pid, {"process": {"decimate_ratio": 0.8}}) for pid in ids]
    assert all(r["status"] == "ok" for r in res)
    for r in res:
        assert Path(r["output"]).exists()
        assert r["analysis"]["total"] == 7


def test_batch_reports_missing_project():
    with pytest.raises(batch.BatchError) as ei:
        batch.process_one("does-not-exist", {})
    assert ei.value.stage == "load"
    assert "不存在" in ei.value.message


def test_batch_repairs_then_processes(live_projects):
    pid = live_projects[0]["id"]
    r = batch.process_one(pid, {
        "repair_actions": ["merge_vertices", "fill_nothing", "fix_normals"],
        "process": {"hollow": False},
    })
    assert r["status"] == "ok"
    assert "merge_vertices" in r["repaired"]
    assert "fill_nothing" not in r["repaired"]


def test_batch_hollow_failure_is_attributed(live_projects, tmp_path):
    # 不水密的模型强行镂空，应归因到 process 阶段
    pid = live_projects[0]["id"]
    proj = store.get(pid)
    broken = tmp_path / "broken.stl"
    m = trimesh.creation.box(extents=(20, 20, 20))
    m.update_faces(np.arange(len(m.faces)) != 0)
    m.export(str(broken))
    store.update(pid, current=str(broken), source=str(broken))
    with pytest.raises(batch.BatchError) as ei:
        batch.process_one(pid, {"process": {"hollow": True, "wall_thickness": 2.0}})
    assert ei.value.stage == "process"
    assert "水密" in ei.value.message
