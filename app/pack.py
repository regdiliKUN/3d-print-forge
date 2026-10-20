"""拼盘排版：把多个模型排到一块成型平台上，减少换盘次数。

行式装箱（shelf packing）：按最长边降序，逐行铺满再换行。
比二维装箱差一些，但对打印件这种尺寸差异大的场景足够，而且结果可预测——
用户能理解「为什么排成这样」比「多塞进一个」更重要。
"""
from __future__ import annotations

import time
from pathlib import Path

import trimesh

from . import config, meshio, store


def pack(items: list[trimesh.Trimesh], platform: tuple[float, float],
         gap: float = 4.0) -> tuple[list[dict], list[dict]]:
    """返回 (排布结果, 放不下的列表)。"""
    placed: list[dict] = []
    skipped: list[dict] = []
    x = y = 0.0
    row_h = 0.0
    # 大件先排，小件填空，行高浪费最小
    order = sorted(range(len(items)), key=lambda i: -float(max(items[i].extents)))
    for i in order:
        m = items[i]
        w, d = float(m.extents[0]), float(m.extents[1])
        if w > platform[0] or d > platform[1]:
            skipped.append({"index": i, "reason": f"单件 {w:.0f}×{d:.0f}mm 超出平台 {platform[0]:.0f}×{platform[1]:.0f}mm"})
            continue
        if x + w > platform[0]:
            x = 0.0
            y += row_h + gap
            row_h = 0.0
        if y + d > platform[1]:
            skipped.append({"index": i, "reason": "平台已满，放不下"})
            continue
        placed.append({
            "index": i,
            "x": round(x, 2),
            "y": round(y, 2),
            "w": round(w, 2),
            "d": round(d, 2),
            "h": round(float(m.extents[2]), 2),
        })
        x += w + gap
        row_h = max(row_h, d)
    return placed, skipped


def run_job(ctx, params):
    pid = ctx.job.project_id
    proj = store.get(pid)
    if not proj:
        raise ValueError("项目不存在")
    ids = list(params.get("project_ids") or [])
    if not ids:
        ids = [pid]
    pw = float(params.get("platform_w") or 192)
    pd = float(params.get("platform_d") or 120)
    gap = float(params.get("gap") or 4.0)

    meshes: list[trimesh.Trimesh] = []
    names: list[str] = []
    for i, x in enumerate(ids):
        ctx.tick(0.05 + 0.5 * i / max(1, len(ids)), f"加载 {i + 1}/{len(ids)}", "load")
        p = store.get(x)
        if not p:
            continue
        path = Path(p.get("current") or p.get("source") or "")
        if not path.exists():
            continue
        m = meshio.load_mesh(path)
        m.apply_translation([0, 0, -float(m.bounds[0][2])])
        meshes.append(m)
        names.append(p["name"])

    if not meshes:
        raise ValueError("没有可排版的模型")

    ctx.tick(0.6, "计算排布", "pack")
    placed, skipped = pack(meshes, (pw, pd), gap)

    ctx.tick(0.75, "合并导出", "export")
    parts = []
    for item in placed:
        m = meshes[item["index"]].copy()
        m.apply_translation([item["x"] + item["w"] / 2, item["y"] + item["d"] / 2, 0])
        parts.append(m)
    merged = trimesh.util.concatenate(parts) if parts else None
    out = None
    if merged is not None:
        out = config.OUTPUTS / f"{pid}_packed_{int(time.time())}.stl"
        merged.export(str(out))

    ctx.tick(1.0, "排版完成", "done")
    return {
        "platform": [pw, pd],
        "gap": gap,
        "placed": [
            {**it, "name": names[it["index"]] if it["index"] < len(names) else str(it["index"])}
            for it in placed
        ],
        "skipped": [
            {**it, "name": names[it["index"]] if it["index"] < len(names) else str(it["index"])}
            for it in skipped
        ],
        "count": len(placed),
        "output": str(out) if out else None,
        "note": f"排入 {len(placed)} 件" + (f"，{len(skipped)} 件放不下" if skipped else ""),
    }
