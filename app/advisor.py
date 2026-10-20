"""打印参数建议引擎。

原则：不给「经验值」，只给「从这个模型的这个数字推出来的值」。
每条建议都带 reason，前端直接把 reason 展示给用户，
用户不同意时可以顺着依据去改，而不是盲从一个数字。
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from . import analysis, meshio

PRINTERS = {
    "sla_small": {"name": "光固化 · 小型", "process": "SLA", "build": [120, 68, 150], "xy_res": 0.035},
    "sla_mid": {"name": "光固化 · 中型", "process": "SLA", "build": [192, 120, 245], "xy_res": 0.05},
    "sla_large": {"name": "光固化 · 大型", "process": "SLA", "build": [298, 165, 300], "xy_res": 0.06},
    "fdm_220": {"name": "FDM · 220 平台", "process": "FDM", "build": [220, 220, 250], "nozzle": 0.4},
    "fdm_350": {"name": "FDM · 350 平台", "process": "FDM", "build": [350, 350, 350], "nozzle": 0.4},
}

SLA_MATERIALS = {
    "standard": {"name": "标准刚性树脂", "layer": [0.03, 0.1], "exposure": 2.5, "note": "通用，细节表现好"},
    "tough": {"name": "韧性树脂", "layer": [0.03, 0.08], "exposure": 3.5, "note": "细长件、卡扣件首选"},
    "water_wash": {"name": "水洗树脂", "layer": [0.05, 0.1], "exposure": 2.0, "note": "后处理省事，强度略低"},
    "castable": {"name": "铸造树脂", "layer": [0.025, 0.05], "exposure": 4.0, "note": "首饰倒模用"},
}

FDM_MATERIALS = {
    "PLA": {"name": "PLA", "nozzle": 210, "bed": 60, "fan": 100, "note": "易打印，耐温低（约 55℃）"},
    "PETG": {"name": "PETG", "nozzle": 240, "bed": 80, "fan": 50, "note": "韧性好，耐温约 75℃"},
    "ABS": {"name": "ABS", "nozzle": 250, "bed": 100, "fan": 0, "note": "耐温 100℃，需要封闭仓"},
    "TPU": {"name": "TPU", "nozzle": 225, "bed": 50, "fan": 60, "note": "弹性体，需直驱挤出"},
}


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _round_to(x: float, step: float) -> float:
    return round(round(x / step) * step, 4)


def _overhang_ratio(mesh, deg: float = 45.0) -> float:
    """悬垂面积占比。

    把模型最低点当作平台：切片软件会把模型落到台面，所以贴着最低点的
    底面不需要支撑。只有高出最低点、下方又是空腔的面才算悬垂。
    """
    v, f = np.asarray(mesh.vertices), np.asarray(mesh.faces)
    if len(f) == 0:
        return 0.0
    n = analysis.face_normals(v, f)
    a = analysis.tri_areas(v, f)
    total = float(a.sum())
    if total <= 0:
        return 0.0
    zmin = float(v[:, 2].min())
    centers_z = v[f].mean(axis=1)[:, 2]
    mask = (n[:, 2] < -math.cos(math.radians(deg))) & (centers_z > zmin + 0.5)
    return float(a[mask].sum() / total)


def _geometry(project: dict) -> dict:
    """汇总所有用于决策的几何量，并保证每项都有值。"""
    report = project.get("analysis") or {}
    stats = report.get("stats") or {}
    items = {i.get("id"): i for i in (report.get("items") or [])}
    path = Path(project.get("current") or project.get("source") or "")
    overhang = 0.0
    if path.exists():
        try:
            mesh = meshio.load_mesh(path)
            if not stats:
                stats = meshio.mesh_stats(mesh)
            overhang = _overhang_ratio(mesh)
        except Exception:
            pass
    thick = (items.get("thickness") or {}).get("value") or {}
    ext = stats.get("extents") or [0, 0, 0]
    return {
        "extents": ext,
        "max_dim": float(max(ext)) if ext else 0.0,
        "height": float(ext[2]) if len(ext) > 2 else 0.0,
        "volume_mm3": float(stats.get("volume_mm3") or 0.0),
        "area_mm2": float(stats.get("area_mm2") or 0.0),
        "faces": int(stats.get("faces") or 0),
        "min_wall": thick.get("min"),
        "median_wall": thick.get("median"),
        "overhang_ratio": round(overhang, 4),
        "watertight": bool(stats.get("is_watertight")),
        "components": ((items.get("components") or {}).get("value") or {}).get("components", 1),
    }


def _estimate_cost(g: dict, process: str, layer_h: float, exposure: float | None,
                   infill: float | None) -> dict:
    """耗材与时长估算。数字是经验值，但推导过程可查，不是拍脑袋。"""
    vol_cm3 = max(0.0, g["volume_mm3"]) / 1000.0
    height = max(0.0, g["height"])
    if process == "SLA":
        grams = vol_cm3 * 1.1  # 光敏树脂密度约 1.1 g/cm³
        layers = int(height / max(layer_h, 0.01)) if height else 0
        per_layer = (exposure or 3.0) + 6.0  # 曝光 + 抬升回落
        minutes = (layers * per_layer + 10 * 30.0) / 60.0
        return {
            "grams": round(grams, 1),
            "hours": round(minutes / 60.0, 2),
            "layers": layers,
            "basis": f"体积 {vol_cm3:.1f}cm³ × 1.1g/cm³；{layers} 层 × ({exposure or 3.0:.1f}s 曝光 + 6s 抬升) + 底层 10×30s",
        }
    grams = vol_cm3 * 1.24 * (0.35 + (infill or 15) / 100.0 * 0.45)
    minutes = g["volume_mm3"] / 6.0 / 60.0  # 约 6mm³/s 有效挤出
    return {
        "grams": round(grams, 1),
        "hours": round(minutes / 60.0, 2),
        "layers": int(height / max(layer_h, 0.01)) if height else 0,
        "basis": f"体积 {vol_cm3:.1f}cm³ × 1.24g/cm³ × 填充系数；按 6mm³/s 有效挤出速率折算",
    }


def recommend(project: dict, overrides: dict | None = None) -> dict:
    ov = overrides or {}
    g = _geometry(project)
    printer_key = ov.get("printer") or "sla_mid"
    printer = PRINTERS.get(printer_key, PRINTERS["sla_mid"])
    process = ov.get("process") or printer["process"]
    material_key = ov.get("material") or ("standard" if process == "SLA" else "PLA")

    params: list[dict] = []
    warnings: list[str] = []
    bx, by, bz = printer["build"]

    # ---- 工艺与材料
    if process == "SLA":
        mat = SLA_MATERIALS.get(material_key, SLA_MATERIALS["standard"])
        params.append({
            "key": "material", "label": "材料", "value": mat["name"], "unit": "",
            "reason": f"{printer['name']} 机型 + 模型最大尺寸 {g['max_dim']:.0f}mm。{mat['note']}。",
        })
        # 层高：细节分辨率与最小特征挂钩
        if g["min_wall"] and g["min_wall"] < 0.6:
            lh = 0.03
            lh_reason = (
                f"最薄处仅 {g['min_wall']:.2f}mm，必须降到 0.03mm 层高，"
                f"否则薄壁处每层曝光面积过小，固化不充分会直接缺料。"
            )
        elif g["min_wall"] and g["min_wall"] < 1.2:
            lh = 0.04
            lh_reason = (
                f"最薄处 {g['min_wall']:.2f}mm，用 0.04mm 层高在薄壁段多分几层，"
                f"成型后壁面更连续；常规 0.05mm 在此厚度上容易出现层间台阶。"
            )
        elif g["max_dim"] > 150:
            lh = 0.08
            lh_reason = f"模型最大尺寸 {g['max_dim']:.0f}mm，属大件。0.08mm 层高在 {g['height']:.0f}mm 高度上约需 {g['height'] / 0.08:.0f} 层，兼顾表面与耗时。"
        else:
            lh = 0.05
            lh_reason = f"模型最大尺寸 {g['max_dim']:.0f}mm，最薄处 {g['min_wall'] if g['min_wall'] else '未测'}mm，0.05mm 是细节与速度的常规平衡点。"
        lh = _round_to(_clamp(lh, mat["layer"][0], mat["layer"][1]), 0.005)
        params.append({
            "key": "layer_height", "label": "层高", "value": lh, "unit": "mm", "reason": lh_reason,
        })
        # 曝光
        exp = mat["exposure"] * (0.85 if lh < 0.04 else 1.0) * (1.0 + (0.1 if g["max_dim"] > 200 else 0))
        params.append({
            "key": "exposure", "label": "单层曝光", "value": round(exp, 1), "unit": "s",
            "reason": f"{mat['name']} 基准 {mat['exposure']}s，层高 {lh}mm 按比例微调。首次打印建议先跑曝光测试片。",
        })
        # 壁厚 / 镂空
        if g["volume_mm3"] > 50_000:
            params.append({
                "key": "hollow", "label": "镂空", "value": True, "unit": "",
                "reason": f"体积 {g['volume_mm3'] / 1000:.1f}cm³，实心打印耗树脂多、收缩应力大。建议壳厚 2.0mm 并开 2 个 φ3mm 排液孔。",
            })
        else:
            params.append({
                "key": "hollow", "label": "镂空", "value": False, "unit": "",
                "reason": f"体积仅 {g['volume_mm3'] / 1000:.1f}cm³，镂空省不下多少树脂，反而增加排液和清洗负担。",
            })
    else:
        mat = FDM_MATERIALS.get(material_key, FDM_MATERIALS["PLA"])
        nozzle = printer.get("nozzle", 0.4)
        params.append({
            "key": "material", "label": "材料", "value": mat["name"], "unit": "",
            "reason": f"{mat['note']}。模型最大尺寸 {g['max_dim']:.0f}mm，{printer['name']} 可容纳。",
        })
        lh = 0.12 if (g["min_wall"] and g["min_wall"] < 1.5) else 0.2
        params.append({
            "key": "layer_height", "label": "层高", "value": lh, "unit": "mm",
            "reason": (
                f"喷嘴 {nozzle}mm，层高取 1/2~1/3 喷嘴直径。"
                + (f"模型最薄 {g['min_wall']:.2f}mm 偏薄，降到 0.12mm 减少层间空隙。" if g["min_wall"] and g["min_wall"] < 1.5
                   else "0.2mm 是速度与强度的平衡点。")
            ),
        })
        wall_lines = max(2, math.ceil(1.2 / nozzle))
        params.append({
            "key": "wall_thickness", "label": "壁厚", "value": round(wall_lines * nozzle, 2), "unit": "mm",
            "reason": f"{wall_lines} 圈外墙 × {nozzle}mm 线宽。低于 2 圈时强度和密封性都不够。",
        })
        infill = 15 if g["volume_mm3"] < 100_000 else 20
        params.append({
            "key": "infill", "label": "填充率", "value": infill, "unit": "%",
            "reason": (
                f"体积 {g['volume_mm3'] / 1000:.1f}cm³。"
                + ("装饰件 15% 足够，强度靠外墙。" if g["volume_mm3"] < 100_000
                   else "体积偏大，20% 陀螺填充在强度和耗材间更均衡。")
            ),
        })
        params.append({
            "key": "nozzle_temp", "label": "喷头温度", "value": mat["nozzle"], "unit": "℃",
            "reason": f"{mat['name']} 的常规区间中值。首层可 +5℃ 改善附着。",
        })
        params.append({
            "key": "bed_temp", "label": "热床温度", "value": mat["bed"], "unit": "℃",
            "reason": f"{mat['name']} 推荐热床温度，低于此值首层易翘边。",
        })

    # ---- 支撑（两种工艺共用）
    oratio = g["overhang_ratio"]
    if oratio > 0.02:
        params.append({
            "key": "supports", "label": "支撑", "value": "需要", "unit": "",
            "reason": (
                f"超过 45° 的悬垂面占总表面积 {oratio * 100:.1f}%。"
                f"这些面下方没有材料承接，不打支撑会塌陷或拉丝。"
            ),
        })
    else:
        params.append({
            "key": "supports", "label": "支撑", "value": "可选", "unit": "",
            "reason": f"悬垂面仅占 {oratio * 100:.1f}%，模型自支撑性良好。",
        })

    # ---- 分件判断
    if g["max_dim"] > max(bx, by, bz):
        warnings.append(
            f"模型最大尺寸 {g['max_dim']:.0f}mm 超出 {printer['name']} 的成型空间 "
            f"{bx}×{by}×{bz}mm，必须分件或更换机型。"
        )
    if g["height"] > bz * 0.9:
        warnings.append(f"模型高度 {g['height']:.0f}mm 接近成型高度上限 {bz}mm，支撑和取件都会很勉强。")

    # ---- 质检前置条件
    if not g["watertight"]:
        warnings.append("模型当前不水密。切片软件可能拒绝切片或生成错误截面，请先执行补洞。")
    if g["min_wall"] is not None and g["min_wall"] < 0.4:
        warnings.append(f"最薄处 {g['min_wall']:.2f}mm 低于 0.4mm，无论什么工艺都打不出来，需先加厚。")
    if g["components"] and g["components"] > 1:
        warnings.append(f"模型含 {g['components']} 个互不连通的部件，切片时会各自生成支撑，注意检查是否需要合并。")

    lh_val = next((p["value"] for p in params if p["key"] == "layer_height"), 0.05)
    exp_val = next((p["value"] for p in params if p["key"] == "exposure"), None)
    inf_val = next((p["value"] for p in params if p["key"] == "infill"), None)
    cost = _estimate_cost(g, process, float(lh_val), exp_val, inf_val)
    params.append({
        "key": "cost", "label": "预估耗材", "unit": "",
        "value": f"{cost['grams']} g / {cost['hours']} h",
        "reason": cost["basis"] + "。实际会受摆放密度、支撑量和机器效率影响。",
    })

    summary = (
        f"{printer['name']} / {params[0]['value']}，层高 {lh_val}mm，"
        f"预估 {cost['grams']}g / {cost['hours']}h。"
        + (f" {len(warnings)} 条风险提示。" if warnings else " 未发现阻断性风险。")
    )
    return {
        "process": process,
        "printer": printer_key,
        "printer_name": printer["name"],
        "geometry": g,
        "params": params,
        "warnings": warnings,
        "cost": cost,
        "summary": summary,
    }
