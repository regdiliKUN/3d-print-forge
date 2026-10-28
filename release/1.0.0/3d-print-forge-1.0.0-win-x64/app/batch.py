"""批量处理：多个模型套用同一套参数模板，失败项单独归因。

失败归因到具体阶段（加载/检测/修复/处理/建议/导出），
这样批量里挂掉的那几个不用逐个手动排查。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import trimesh

from . import advisor, analysis, config, meshio, process, repair, store

REPORT_FILE = config.STATE / "batch_report.json"


class BatchError(Exception):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.message = message


def process_one(pid: str, tpl: dict, on_progress=None) -> dict:
    proj = store.get(pid)
    if not proj:
        raise BatchError("load", "项目不存在")
    path = Path(proj.get("current") or proj.get("source") or "")
    if not path.exists():
        raise BatchError("load", f"模型文件缺失：{path.name}")

    try:
        mesh = meshio.load_mesh(path, mutable=True)
    except Exception as e:
        raise BatchError("load", f"无法解析网格：{e}") from e

    # ---- 检测
    try:
        # 必须走和单件检测同一套异常隔离（_safe_check）：一项抛异常不能让整个
        # 批处理项失败，否则「某个模型的自交检测崩了」会连累其余六项结论。
        items = [
            analysis._safe_check(
                fn, mesh,
                {"sample_faces": int(tpl.get("sample_faces", 3000)),
                 "check_self_intersection": bool(tpl.get("check_self_intersection", True))},
            )
            for fn in analysis.CHECKS
        ]
        summary = analysis.summarize(items)
        stats = meshio.mesh_stats(mesh)
        report = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "file": path.name,
            "stats": stats,
            "items": items,
            "summary": summary,
            # 批量报告以前没有 quality：批完打开项目详情，质量评分是空的
            "quality": analysis.quality_score({"items": items, "stats": stats}),
            "failed_checks": [i.get("id") for i in items if (i.get("value") or {}).get("error")],
        }
        store.update(pid, analysis=report)
    except Exception as e:
        raise BatchError("analyze", f"检测失败：{e}") from e

    # ---- 修复
    repaired: list[str] = []
    reverted: list[dict] = []
    actions = repair.normalize_order(list(tpl.get("repair_actions") or []))
    for name in actions:
        fn = repair.ACTIONS.get(name)
        if fn is None:
            continue
        try:
            mesh, info = fn(mesh, tpl)
            # 回退的动作不能记成「已修复」：批量报告是用户唯一的依据，
            # 写着「补洞已完成」而实际原样返回，等于把失败伪装成成功。
            if isinstance(info, dict) and info.get("reverted"):
                reverted.append({"action": name, "reason": info.get("revert_reason", "")[:200]})
            else:
                repaired.append(name)
        except Exception as e:
            raise BatchError("repair", f"修复动作 {name} 失败：{e}") from e

    # ---- 处理
    try:
        p = dict(process.DEFAULTS)
        p.update(tpl.get("process") or {})
        mesh, sup, prep = process.apply_pipeline(mesh, p, on_progress)
    except Exception as e:
        raise BatchError("process", str(e)) from e

    # ---- 参数建议（基于处理前的检测报告）
    try:
        adv = advisor.recommend(store.get(pid) or proj, tpl.get("advise") or {})
        store.update(pid, advice=adv)
    except Exception as e:
        raise BatchError("advise", f"参数建议生成失败：{e}") from e

    # ---- 导出
    try:
        final = mesh if sup is None else trimesh.util.concatenate([mesh, sup])
        final.apply_translation([0, 0, -float(final.bounds[0][2])])
        out = config.OUTPUTS / f"{pid}_batch_{int(time.time())}.stl"
        final.export(str(out))
        store.add_history(pid, "batch", tpl, out, note="批量处理")
    except Exception as e:
        raise BatchError("export", f"导出失败：{e}") from e

    return {
        "status": "ok",
        "output": str(out),
        "repaired": repaired,
        "reverted": reverted,
        "analysis": summary,
        "quality": report["quality"],
        "process": prep,
        "advice_summary": adv.get("summary"),
        "warnings": adv.get("warnings", []),
        "faces": int(len(mesh.faces)),
    }


def run_job(ctx, params):
    ids = list(params.get("project_ids") or [])
    if not ids:
        raise ValueError("未选择任何项目")
    tpl = dict(params.get("params") or {})
    tpl.setdefault("repair_actions", ["merge_vertices", "fill_holes", "fix_normals", "remove_small"])

    results: list[dict] = []
    n = len(ids)
    for i, pid in enumerate(ids):
        proj = store.get(pid) or {}
        name = proj.get("name", pid)
        base = 0.02 + 0.95 * i / n
        # 断点续跑：已成功的项目直接复用结果，重跑时不再浪费时间
        done_rec = ctx.step_done(f"proj:{pid}")
        if done_rec is not None and done_rec.get("result"):
            results.append(done_rec["result"])
            ctx.tick(base, f"跳过已完成 {i + 1}/{n}", "batch")
            continue
        ctx.tick(base, f"处理 {i + 1}/{n}：{name}", "batch")

        def on_progress(frac: float, msg: str, _base=base, _n=n, _name=name) -> None:
            ctx.tick(_base + (0.95 / _n) * frac, f"{_name} · {msg}", "batch")

        try:
            r = process_one(pid, tpl, on_progress)
            item = {"project_id": pid, "name": name, **r}
            results.append(item)
            ctx.complete_step(f"proj:{pid}", {"result": item})
            ctx.log(f"✔ {name} 完成")
        except BatchError as e:
            item = {"project_id": pid, "name": name, "status": "failed",
                    "stage": e.stage, "error": e.message}
            results.append(item)
            ctx.complete_step(f"proj:{pid}", {"result": item})
            ctx.log(f"✖ {name} 失败于 {e.stage}：{e.message}")
        except Exception as e:
            item = {"project_id": pid, "name": name, "status": "failed",
                    "stage": "unknown", "error": f"{type(e).__name__}: {e}"}
            results.append(item)
            ctx.complete_step(f"proj:{pid}", {"result": item})
            ctx.log(f"✖ {name} 未预期失败：{e}")

    ok = sum(1 for r in results if r["status"] == "ok")
    report = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total": n,
        "ok": ok,
        "failed": n - ok,
        "template": tpl,
        "items": results,
    }
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.tick(1.0, f"批量完成：{ok}/{n} 成功", "done")
    return report
