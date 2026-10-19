"""端到端验证：从导入缺陷模型到导出可打印成品，走完整 HTTP 链路。

直接运行：python tests/e2e.py
也兼容 pytest。
"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path

import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import jobs, store  # noqa: E402
from app.main import app  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  ✔ {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  ✖ {name}  {detail}")


def wait_job(client: TestClient, jid: str, timeout: float = 180.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{jid}").json()
        if j["status"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.25)
    return client.get(f"/api/jobs/{jid}").json()


def broken_model() -> trimesh.Trimesh:
    """一个同时具备破洞、法线错误、碎屑、薄壁的模型。"""
    body = trimesh.creation.icosphere(subdivisions=3, radius=18)
    body.update_faces(np.arange(len(body.faces)) > 3)      # 抠掉几片 → 破洞
    body.invert()                                          # 法线整体朝内
    tiny = trimesh.creation.box(extents=(0.4, 0.4, 0.4))   # 碎屑
    tiny.apply_translation([40, 0, 0])
    return trimesh.util.concatenate([body, tiny])


def upload(client: TestClient, mesh: trimesh.Trimesh, name: str) -> str:
    buf = io.BytesIO()
    mesh.export(buf, file_type="stl")
    buf.seek(0)
    r = client.post("/api/upload", files={"files": (name, buf.getvalue(), "model/stl")})
    assert r.status_code == 200, r.text
    return r.json()["projects"][0]["id"]


def run() -> int:
    print("\n=== 端到端验证 ===\n")
    with TestClient(app) as client:
        check("服务健康", client.get("/api/health").json()["ok"])

        # ---------------- 1. 导入
        mesh = broken_model()
        pid = upload(client, mesh, "broken.stl")
        proj = client.get(f"/api/projects/{pid}").json()
        check("导入成功", proj["id"] == pid, f"{proj['name']}")

        # ---------------- 2. 检测
        j = wait_job(client, client.post(f"/api/projects/{pid}/analyze", json={}).json()["id"])
        check("检测任务完成", j["status"] == "done", j.get("error") or "")
        rep = client.get(f"/api/projects/{pid}").json()["analysis"]
        items = {i["id"]: i for i in rep["items"]}
        check("检测出 7 项", len(rep["items"]) == 7)
        check("识别出破洞", items["watertight"]["severity"] == "error",
              f"开放边 {items['watertight']['value']['boundary_edges']}")
        check("识别出法线翻转", items["normals"]["value"]["signed_volume"] < 0,
              f"有向体积 {items['normals']['value']['signed_volume']}")
        check("识别出多个部件", items["components"]["value"]["components"] >= 2,
              f"{items['components']['value']['components']} 个")
        check("报告含修复动作", bool(items["watertight"].get("fix")))

        # ---------------- 3. 修复
        j = wait_job(client, client.post(
            f"/api/projects/{pid}/repair",
            json={"actions": ["merge_vertices", "fill_holes", "fix_normals", "remove_small"],
                  "small_ratio": 0.01},
        ).json()["id"])
        check("修复任务完成", j["status"] == "done", j.get("error") or "")
        after = j["result"]["after"]
        check("修复后水密", after["is_watertight"] is True)
        check("修复后体积为正", after["volume_mm3"] > 0, f"{after['volume_mm3']:.0f} mm³")

        # ---------------- 4. 复检
        j = wait_job(client, client.post(f"/api/projects/{pid}/analyze", json={}).json()["id"])
        rep2 = client.get(f"/api/projects/{pid}").json()["analysis"]
        it2 = {i["id"]: i for i in rep2["items"]}
        check("复检：水密已修复", it2["watertight"]["severity"] == "ok")
        check("复检：法线已修复", it2["normals"]["value"]["signed_volume"] > 0)
        check("复检：碎屑已清除", it2["components"]["value"]["components"] == 1)
        check("复检严重项下降",
              rep2["summary"]["counts"]["error"] < rep["summary"]["counts"]["error"],
              f"{rep['summary']['counts']['error']} → {rep2['summary']['counts']['error']}")

        # ---------------- 5. 处理
        j = wait_job(client, client.post(f"/api/projects/{pid}/process", json={
            "decimate_ratio": 0.7, "hollow": True, "wall_thickness": 2.0,
            "drain_holes": 2, "hole_diameter": 3.0, "supports": "auto",
            "overhang_deg": 45.0, "orient": True, "scale": 1.0,
        }).json()["id"], timeout=300)
        check("处理任务完成", j["status"] == "done", j.get("error") or "")
        pr = j["result"]["report"]
        check("镂空生效", (pr["hollow"] or {}).get("resin_saved_pct", 0) > 20,
              f"省料 {pr['hollow']['resin_saved_pct']}%")
        check("排液孔已打", (pr["drill"] or {}).get("holes", 0) == 2)
        check("自动摆正", (pr["orient"] or {}).get("chosen") is not None,
              (pr["orient"] or {}).get("chosen", ""))
        # 镂空会新增内壳，面数不一定下降；关键是内腔已减面，总量可控
        check("面数控制在可切片范围", j["result"]["after"]["faces"] < 60000,
              f"{j['result']['before']['faces']} → {j['result']['after']['faces']} 面")
        check("镂空后体积小于实心", j["result"]["after"]["volume_mm3"] < j["result"]["before"]["volume_mm3"],
              f"{j['result']['before']['volume_mm3']:.0f} → {j['result']['after']['volume_mm3']:.0f} mm³")

        # ---------------- 6. 预览接口
        m = client.get(f"/api/projects/{pid}/mesh?max_faces=20000").json()
        check("预览网格可读", m["face_count"] > 0, f"{m['face_count']} 面")
        import base64
        vb = base64.b64decode(m["vertices"])
        check("顶点数据对齐", len(vb) % 12 == 0, f"{len(vb) // 12} 顶点")

        # ---------------- 7. 参数建议
        adv = client.get(f"/api/projects/{pid}/advice").json()
        check("建议含参数项", len(adv.get("params", [])) >= 4, f"{len(adv.get('params', []))} 项")
        check("每条建议都有依据", all(len(p["reason"]) > 8 for p in adv["params"]))
        keys = {p["key"] for p in adv["params"]}
        check("建议覆盖材料/层高/支撑", {"material", "layer_height", "supports"} <= keys)

        # ---------------- 8. 导出
        for kind in ("stl", "config", "report", "log"):
            r = client.get(f"/api/projects/{pid}/download?kind={kind}")
            check(f"导出 {kind}", r.status_code == 200 and len(r.content) > 50, f"{len(r.content)} B")
        stl = client.get(f"/api/projects/{pid}/download?kind=stl").content
        out = ROOT / "data" / "outputs" / "_e2e_check.stl"
        out.write_bytes(stl)
        reloaded = trimesh.load(str(out), force="mesh")
        check("导出 STL 可回读", len(reloaded.faces) > 0, f"{len(reloaded.faces)} 面")
        check("导出模型落在平台", abs(float(reloaded.bounds[0][2])) < 0.01,
              f"zmin={reloaded.bounds[0][2]:.3f}")
        out.unlink(missing_ok=True)

        # ---------------- 9. 批量
        p2 = upload(client, trimesh.creation.box(extents=(25, 25, 25)), "cube.stl")
        p3 = upload(client, trimesh.creation.icosphere(subdivisions=2, radius=10), "ball.stl")
        j = wait_job(client, client.post("/api/batch", json={
            "project_ids": [p2, p3],
            "params": {"repair_actions": ["merge_vertices", "fix_normals"],
                       "process": {"hollow": False, "supports": "none"}},
        }).json()["id"], timeout=300)
        check("批量任务完成", j["status"] == "done", j.get("error") or "")
        br = j["result"]
        check("批量全部成功", br["ok"] == 2, f"{br['ok']}/{br['total']}")
        rep_file = ROOT / "data" / "state" / "batch_report.json"
        check("批量报告已落盘", rep_file.exists())

        # ---------------- 10. 失败归因
        j = wait_job(client, client.post("/api/batch", json={
            "project_ids": [p2, "nonexistent-id"],
            "params": {"process": {}},
        }).json()["id"])
        items = {x["project_id"]: x for x in j["result"]["items"]}
        check("失败项单独列出", items["nonexistent-id"]["status"] == "failed")
        check("失败项归因到阶段", items["nonexistent-id"]["stage"] == "load",
              items["nonexistent-id"]["error"])

        # ---------------- 11. 暂停 / 恢复 / 中断
        gate = {"n": 0}

        def slow(ctx, params):
            for i in range(60):
                ctx.tick(i / 60, f"第 {i} 步")
                time.sleep(0.05)
                gate["n"] = i
            return {"ok": True}

        jb = jobs.submit("test", "慢任务", {}, slow)
        time.sleep(0.6)
        r = client.post(f"/api/jobs/{jb.id}/pause")
        check("可以暂停", r.status_code == 200)
        time.sleep(0.5)
        st = client.get(f"/api/jobs/{jb.id}").json()
        check("暂停后状态正确", st["status"] == "paused")
        n1 = gate["n"]
        time.sleep(0.6)
        check("暂停后确实停止推进", gate["n"] == n1, f"仍停在 {n1}")
        client.post(f"/api/jobs/{jb.id}/resume")
        time.sleep(0.6)
        check("恢复后继续推进", gate["n"] > n1, f"{n1} → {gate['n']}")
        client.post(f"/api/jobs/{jb.id}/cancel")
        time.sleep(0.4)
        st = client.get(f"/api/jobs/{jb.id}").json()
        check("可以中断", st["status"] in ("cancelled", "done"), st["status"])

        # ---------------- 12. 断点续跑
        runs = {"count": 0, "resumed": False}

        def resumable(ctx, params):
            runs["count"] += 1
            if ctx.step_done("phase1") is None:
                ctx.tick(0.3, "第一阶段")
                ctx.complete_step("phase1", {"v": 1})
            else:
                runs["resumed"] = True
            raise RuntimeError("故意在第二阶段失败")

        jb2 = jobs.submit("test", "断点任务", {}, resumable)
        j2 = wait_job(client, jb2.id, timeout=20)
        check("首次执行失败", j2["status"] == "failed")
        client.post(f"/api/jobs/{jb2.id}/resume")
        j3 = wait_job(client, jb2.id, timeout=20)
        check("续跑复用了已完成步骤", runs["count"] == 2 and runs["resumed"],
              f"执行 {runs['count']} 次，命中断点={runs['resumed']}")

        # ---------------- 13. 配置与模板
        r = client.post("/api/config", json={"runtime": {"max_workers": 3, "memory_limit_mb": 6144}})
        check("运行配置可写", r.status_code == 200 and r.json()["runtime"]["max_workers"] == 3)
        r = client.post("/api/templates", json={"name": "手办模板", "params": {"process": {"wall_thickness": 2}}})
        tid = r.json()["template"]["id"]
        check("模板可保存", any(t["id"] == tid for t in client.get("/api/templates").json()["templates"]))
        client.delete(f"/api/templates/{tid}")
        check("模板可删除", not any(t["id"] == tid for t in client.get("/api/templates").json()["templates"]))

        # 清理
        for x in (pid, p2, p3):
            client.delete(f"/api/projects/{x}")

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 0 if not FAIL else 1


def test_e2e():
    assert run() == 0


if __name__ == "__main__":
    raise SystemExit(run())
