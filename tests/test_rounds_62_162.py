"""R63–R164 的回归用例：以用户实际路径（导入→检测→修复→处理→导出→运维）为主。

每个用例都对应 ROUNDS.md 里的一个可验证行为：先能复现旧实现的错误结果，
再断言新实现给出正确结果或明确拒绝。测试只使用临时项目，结束后清理。
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config, store  # noqa: E402
from app.main import _csv_safe, _download_headers, _save_queue, app  # noqa: E402


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def cube(tmp_path):
    src = tmp_path / "cube.stl"
    trimesh.creation.box(extents=(20, 20, 20)).export(str(src))
    proj = store.create("cube.stl", src)
    yield proj
    store.delete(proj["id"])


def _out_file(pid: str, name: str, src: Path) -> Path:
    dst = config.OUTPUTS / f"{pid}_{name}.stl"
    shutil.copyfile(src, dst)
    return dst


# ---------------------------------------------------------------- 静态资源缓存
def test_static_assets_revalidate_and_api_is_not_stored(client):
    """升级后浏览器必须回源校验，不能继续跑缓存的旧 app.js。"""
    html = client.get("/")
    assert html.status_code == 200
    assert "no-cache" in html.headers.get("cache-control", "")
    # 资源 URL 带 mtime 版本号：文件一改，浏览器就会请求新地址
    assert "/static/style.css?v=" in html.text
    assert "/static/app.js?v=" in html.text

    js = client.get("/static/app.js")
    assert js.status_code == 200
    assert "no-cache" in js.headers.get("cache-control", "")
    assert "viewer.js?v=" in js.text

    health = client.get("/api/health")
    assert health.status_code == 200
    assert "no-store" in health.headers.get("cache-control", "")


def test_numeric_step_shortcut_calls_existing_goto():
    """数字键 1–6 曾调用不存在的 goStep()，按键会直接抛 ReferenceError。"""
    src = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "goStep(" not in src
    assert "goto(Number(e.key) - 1)" in src


# ---------------------------------------------------------------- 下载头
def test_download_headers_strip_crlf_and_quote():
    h = _download_headers('报告"名\r\nX-Evil: 1/模型.stl')["Content-Disposition"]
    assert "\r" not in h and "\n" not in h
    assert "X-Evil" in h  # 只清控制字符，不丢正文
    assert h.count('"') == 2  # 只有 ASCII 回退名两侧的引号
    assert "filename*=UTF-8''" in h
    assert "inline" not in h


def test_download_headers_inline_and_ascii_fallback():
    h = _download_headers("模型报告.pdf", "inline")["Content-Disposition"]
    assert h.startswith("inline;")
    assert 'filename="' in h
    assert "filename*=UTF-8''" in h


def test_download_route_utf8_name(client, tmp_path):
    src = tmp_path / "x.stl"
    trimesh.creation.box().export(str(src))
    proj = store.create('报告/恶意"名.stl', src)
    try:
        r = client.get(f"/api/projects/{proj['id']}/download?kind=report")
        assert r.status_code == 200, r.text
        cd = r.headers["content-disposition"]
        assert cd.startswith("attachment;")
        assert "filename*=UTF-8''" in cd
        assert "\r" not in cd and "\n" not in cd
        r2 = client.get(f"/api/projects/{proj['id']}/report.html")
        assert r2.status_code == 200
        assert r2.headers["content-disposition"].startswith("inline;")
    finally:
        store.delete(proj["id"])


# ---------------------------------------------------------------- 批量打包
def test_batch_zip_sanitizes_names(client, tmp_path):
    ids = []
    try:
        for name in ("../evil/模型.stl", "a/b/c.stl"):
            src = tmp_path / f"{len(ids)}.stl"
            trimesh.creation.box().export(str(src))
            ids.append(store.create(name, src)["id"])
        r = client.get(f"/api/batch/export.zip?ids={','.join(ids)}")
        assert r.status_code == 200, r.text
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = z.namelist()
        assert "manifest.json" in names
        model_names = [n for n in names if n != "manifest.json"]
        assert len(model_names) == 2
        for n in model_names:
            assert "/" not in n and "\\" not in n and ".." not in n
            assert n.endswith(".stl")
        manifest = json.loads(z.read("manifest.json"))
        assert len(manifest) == 2
        assert all(m["file"] in model_names for m in manifest)
    finally:
        for pid in ids:
            store.delete(pid)


def test_batch_zip_empty_404(client):
    r = client.get("/api/batch/export.zip?ids=not-exist")
    assert r.status_code == 404


# ---------------------------------------------------------------- 队列
def test_queue_dedupe_reorder_and_prune(client, tmp_path):
    _save_queue([])
    pid = None
    try:
        src = tmp_path / "q.stl"
        trimesh.creation.box().export(str(src))
        pid = store.create("q.stl", src)["id"]

        r = client.post("/api/queue", json={"id": pid, "note": "第一件\x00\x07"})
        assert r.status_code == 200
        assert r.json()["count"] == 1
        assert r.json()["items"][0]["note"] == "第一件"

        r = client.post("/api/queue", json={"id": pid})
        assert r.json()["count"] == 1
        assert r.json()["note"] == "已在队列中"

        # 重复 id + 未知 id 不能把列表变长或复制条目
        r = client.post("/api/queue/reorder", json={"order": [pid, pid, "ghost"]})
        assert r.status_code == 200
        assert r.json()["count"] == 1
        assert r.json()["ignored"] == ["ghost"]

        store.delete(pid)
        pid = None
        r = client.post("/api/queue/prune")
        assert r.status_code == 200
        assert r.json()["removed"] == 1
        assert r.json()["count"] == 0
        assert client.get("/api/queue").json()["items"] == []
    finally:
        _save_queue([])
        if pid:
            store.delete(pid)


def test_queue_missing_project_404(client):
    _save_queue([])
    try:
        r = client.post("/api/queue", json={"id": "nope"})
        assert r.status_code == 404
    finally:
        _save_queue([])


# ---------------------------------------------------------------- 历史清理
def test_history_cleanup_keeps_recent_and_current(cube):
    base = Path(cube["current"])
    paths = [_out_file(cube["id"], f"step{i}", base) for i in range(4)]
    for p in paths:
        store.add_history(cube["id"], "scale", {"factor": 1.1}, p, note="test")

    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post(f"/api/projects/{cube['id']}/history/cleanup?keep=2")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["removed"] == 2
        assert d["freed_bytes"] > 0
        assert d["kept"] == 2

    p = store.get(cube["id"])
    assert len(p["history"]) == 4
    assert p["history"][0]["out_removed"] is True
    assert p["history"][1]["out_removed"] is True
    assert not paths[0].exists() and not paths[1].exists()
    assert paths[2].exists() and paths[3].exists()  # 最近两版保留
    assert Path(p["current"]).exists()


def test_history_cleanup_protects_shared_files(cube, tmp_path):
    base = Path(cube["current"])
    shared = _out_file(cube["id"], "shared", base)
    other_src = tmp_path / "other.stl"
    trimesh.creation.box().export(str(other_src))
    other = store.create("other.stl", other_src)
    try:
        store.add_history(cube["id"], "scale", {}, shared)
        store.add_history(cube["id"], "scale", {}, _out_file(cube["id"], "newer", base))
        store.add_history(other["id"], "scale", {}, shared)
        r = store.cleanup_history(cube["id"], keep=1)
        assert r["removed"] == 0
        assert shared.exists()
    finally:
        store.delete(other["id"])
        # 共享文件此时无人引用，随 cube 删除一起清掉
        shared.unlink(missing_ok=True)


def test_history_cleanup_404(client):
    assert client.post("/api/projects/nope/history/cleanup").status_code == 404


# ---------------------------------------------------------------- 打印记录
def test_print_record_accepts_duration_and_normalizes_result(client, cube):
    pid = cube["id"]
    store.update(pid, analysis={"stats": {"faces": 12}})
    r = client.post(f"/api/projects/{pid}/print-record", json={
        "result": "pass", "note": "层纹\x00正常", "duration_min": "90.5",
        "material": "PLA", "printer": "A1",
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["success"] == 1 and d["total"] == 1
    assert d["total_minutes"] == 90.5
    rec = d["prints"][0]
    assert rec["result"] == "success"
    assert rec["note"] == "层纹正常"
    assert rec["material"] == "PLA"
    assert rec["faces"] == 12

    r = client.post(f"/api/projects/{pid}/print-record", json={"result": "失败"})
    assert r.status_code == 200
    assert r.json()["total"] == 2

    g = client.get(f"/api/projects/{pid}/print-record").json()
    assert g["total_minutes"] == 90.5
    assert g["rate"] == 50.0


@pytest.mark.parametrize("payload,code", [
    ({"result": "maybe"}, 400),
    ({"result": "success", "duration_min": "abc"}, 422),
    ({"result": "success", "duration_min": -1}, 422),
    ({"result": "success", "duration_min": 100001}, 422),
])
def test_print_record_rejects_bad_input(client, cube, payload, code):
    r = client.post(f"/api/projects/{cube['id']}/print-record", json=payload)
    assert r.status_code == code, r.text


# ---------------------------------------------------------------- 修复计划
def test_repair_plan_translates_all_findings(cube):
    store.update(cube["id"], analysis={
        "items": [
            {"id": "watertight", "value": {"boundary_edges": 4, "nonmanifold_edges": 2,
                                           "duplicate_faces": 3}},
            {"id": "normals", "value": {"inverted_components": 1, "winding_conflicts": 2}},
            {"id": "self_intersection", "value": {"count": 5}},
            {"id": "components", "value": {"components": 6, "tiny": 2}},
            {"id": "units", "value": {"unit": "m?", "suggested_scale": 1000,
                                      "ambiguous": True}},
            {"id": "thickness", "value": {"min": 0.5}},
        ],
        "summary": {"total": 6},
    })

    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        d = c.get(f"/api/projects/{cube['id']}/repair-plan").json()
    assert set(d["actions"]) == {
        "merge_vertices", "fill_holes", "fix_normals",
        "resolve_self_intersection", "remove_small", "thicken",
    }
    assert "rescale" not in d["actions"]  # 单位歧义时绝不自动换算
    assert d["scale_factor"] is None
    assert d["warnings"] and "单位不确定" in d["warnings"][0]
    assert "碎屑" in d["reasons"]["remove_small"]
    assert d["has_analysis"] is True
    assert d["resolved"] is False


def test_repair_plan_rescales_only_unambiguous_unit(tmp_path):
    src = tmp_path / "u.stl"
    trimesh.creation.box().export(str(src))
    proj = store.create("u.stl", src)
    try:
        store.update(proj["id"], analysis={
            "items": [{"id": "units", "value": {"unit": "m", "suggested_scale": 1000}}],
            "summary": {"total": 1},
        })
        from fastapi.testclient import TestClient

        with TestClient(app) as c:
            d = c.get(f"/api/projects/{proj['id']}/repair-plan").json()
        assert d["actions"] == ["rescale"]
        assert d["scale_factor"] == 1000
        assert d["warnings"] == []
        assert d["resolved"] is False
    finally:
        store.delete(proj["id"])


def test_repair_plan_resolved_when_clean(cube):
    store.update(cube["id"], analysis={"items": [
        {"id": "watertight", "value": {"boundary_edges": 0}},
    ], "summary": {"total": 1}})
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        d = c.get(f"/api/projects/{cube['id']}/repair-plan").json()
    assert d["actions"] == []
    assert d["resolved"] is True


# ---------------------------------------------------------------- 处理预检
def test_process_preview_fit_and_scale(client, cube):
    r = client.post(f"/api/projects/{cube['id']}/process-preview",
                    json={"build": [30, 30, 30], "orient": False})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["fit"]["ok"] is True
    assert d["extents"] == [20, 20, 20]
    assert d["approximate"] is True
    assert d["fit"]["scale_to_fit"] == 1.0

    r = client.post(f"/api/projects/{cube['id']}/process-preview",
                    json={"build": [10, 10, 10], "orient": False})
    d = r.json()
    assert d["fit"]["ok"] is False
    assert d["fit"]["oversize"]
    assert 0 < d["fit"]["scale_to_fit"] < 1
    assert d["fit"]["scaled_extents"][0] <= 9.0


def test_process_preview_rejects_bad_build(client, cube):
    for build in ([1, 2], [0, 10, 10], ["a", 1, 2], [1, True, 3]):
        r = client.post(f"/api/projects/{cube['id']}/process-preview", json={"build": build})
        assert r.status_code == 422, (build, r.text)


def test_process_preview_conflicting_targets_prefers_height(client, cube):
    r = client.post(f"/api/projects/{cube['id']}/process-preview", json={
        "build": [100, 100, 100], "target_height": 40, "target_max_dim": 80,
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["scale"]["target"] == "height"
    assert d["extents"][2] == pytest.approx(40, abs=0.1)


# ---------------------------------------------------------------- 项目包
def test_bundle_roundtrip_restores_history(client, cube):
    src = Path(cube["current"])
    hist_file = _out_file(cube["id"], "bundled", src)
    store.add_history(cube["id"], "scale", {"factor": 1.2}, hist_file, note="打包测试")

    r = client.get(f"/api/projects/{cube['id']}/bundle")
    assert r.status_code == 200, r.text
    assert "filename*=UTF-8''" in r.headers["content-disposition"]
    raw = r.content

    r = client.post("/api/projects/import-bundle",
                    files={"file": ("pkg.zip", raw, "application/zip")})
    assert r.status_code == 200, r.text
    d = r.json()
    new_id = d["id"]
    try:
        assert d["restored"] is True
        assert d["history"] == 1
        newp = store.get(new_id)
        assert len(newp["history"]) == 1
        assert newp["history"][0]["note"] == "打包测试"
        assert Path(newp["history"][0]["out"]).exists()
    finally:
        store.delete(new_id)


def test_bundle_rejects_path_traversal(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("project.json", "{}")
        z.writestr("model.stl", b"solid x")
        z.writestr("../evil.stl", b"x")
    r = client.post("/api/projects/import-bundle",
                    files={"file": ("bad.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 400
    assert "非法路径" in r.text


def test_bundle_rejects_too_many_members(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(501):
            z.writestr(f"f{i}.txt", b"")
    r = client.post("/api/projects/import-bundle",
                    files={"file": ("many.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 400
    assert "文件过多" in r.text


# ---------------------------------------------------------------- 检测历史 / CSV
def test_analyze_appends_history(client, cube):
    import time

    def run_analysis():
        r = client.post(f"/api/projects/{cube['id']}/analyze",
                        json={"check_self_intersection": False, "sample_faces": 100})
        assert r.status_code == 200, r.text
        jid = r.json()["id"]
        end = time.time() + 30
        while time.time() < end:
            d = client.get(f"/api/jobs/{jid}").json()
            if d["status"] in ("done", "failed", "cancelled"):
                assert d["status"] == "done", d
                return
            time.sleep(0.05)
        raise AssertionError("检测任务超时")

    run_analysis()
    p = store.get(cube["id"])
    hist = p.get("analysis_history") or []
    assert len(hist) == 1
    assert hist[0]["faces"] == 12
    assert "score" in hist[0]

    # 同一文件重复检测：更新时间戳而不是堆重复条目
    run_analysis()
    assert len(store.get(cube["id"])["analysis_history"]) == 1

    # 文件真的变了（mtime 前进）才追加新点
    import os
    src = Path(cube["current"])
    st = src.stat()
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    run_analysis()
    assert len(store.get(cube["id"])["analysis_history"]) == 2


def test_csv_formula_injection_is_escaped():
    assert _csv_safe("=cmd|' /C calc'!A0").startswith("'=")
    assert _csv_safe("+1").startswith("'+")
    assert _csv_safe("-1").startswith("'-")
    assert _csv_safe("@SUM(A1)").startswith("'@")
    assert _csv_safe("正常项目") == "正常项目"
    assert "\x00" not in _csv_safe("a\x00b")


def test_batch_report_csv_escapes_user_text(client):
    f = config.STATE / "batch_report.json"
    backup = f.read_bytes() if f.exists() else None
    try:
        f.write_text(json.dumps({"items": [{
            "name": "=HYPERLINK(\"http://x\")", "status": "done", "stage": "process",
            "faces": 12, "output": "", "error": "+ERR", "warnings": ["@warn"],
        }]}, ensure_ascii=False), encoding="utf-8")
        r = client.get("/api/batch/report.csv")
        assert r.status_code == 200, r.text
        body = r.content.decode("utf-8-sig")
        assert "'=HYPERLINK" in body
        assert "'+ERR" in body
        assert "'@warn" in body
        assert "filename*=UTF-8''" in r.headers["content-disposition"]
    finally:
        if backup is None:
            f.unlink(missing_ok=True)
        else:
            f.write_bytes(backup)


# ---------------------------------------------------------------- 建议可打印性
def test_advisor_printability_and_fallbacks(cube):
    from app import advisor

    src = Path(cube["current"])
    store.update(cube["id"], analysis={
        "stats": {"extents": [20, 20, 20], "volume_mm3": 8000, "area_mm2": 2400,
                  "faces": 12, "is_watertight": True},
        "items": [{"id": "thickness", "value": {"min": 2.0, "median": 2.0}}],
    })
    p = store.get(cube["id"])
    r = advisor.recommend(p, {"printer": "bambu_a1", "material": "PLA"})
    assert r["printability"]["status"] in ("ready", "caution")
    assert len(r["printability"]["checks"]) == 4
    assert all("ok" in c and "detail" in c for c in r["printability"]["checks"])
    assert r["fallbacks"]["printer"] is None

    r2 = advisor.recommend(p, {"printer": "no_such_printer"})
    assert r2["fallbacks"]["printer"] == "no_such_printer"
    assert any("没有机型预设" in w for w in r2["warnings"])


def test_advisor_stale_report_is_recomputed(cube):
    from app import advisor

    store.update(cube["id"], analysis={
        "stats": {"extents": [20, 20, 20], "volume_mm3": 8000, "area_mm2": 2400,
                  "faces": 12, "is_watertight": True},
        "items": [{"id": "thickness", "value": {"min": 2.0}}],
    })
    store.update(cube["id"], analysis_stale=True)
    r = advisor.recommend(store.get(cube["id"]), {"printer": "bambu_a1"})
    assert r["geometry"]["stale"] is True
    assert r["geometry"]["geometry_source"] == "recomputed"
    assert "已过期" in " ".join(r["confidence"]["reasons"])
    assert any("过期" in w for w in r["warnings"])
