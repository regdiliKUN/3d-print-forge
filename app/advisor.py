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

# 三档方案：同一模型，用户可在「更快 / 平衡 / 更精细」之间一键切换。
# 默认 balanced，保持与旧版完全一致的数值（老测试/老用户不受影响）。
PLANS = {
    "aggressive": {
        "label": "激进",
        "desc": "更快、更省料：层高偏粗、填充偏低。适合验证结构、外观要求不高的件。",
        "layer_mult": 1.4,
        "infill_delta": -5,
        "wall_delta_lines": 0,
        "exposure_mult": 0.9,
        "support_bias": 0.03,
    },
    "balanced": {
        "label": "折中",
        "desc": "质量与时间兼顾，默认方案。",
        "layer_mult": 1.0,
        "infill_delta": 0,
        "wall_delta_lines": 0,
        "exposure_mult": 1.0,
        "support_bias": 0.02,
    },
    "conservative": {
        "label": "保守",
        "desc": "更细腻、更结实：层高偏细、填充偏高、曝光略长。适合手办、承力件。",
        "layer_mult": 0.75,
        "infill_delta": 5,
        "wall_delta_lines": 1,
        "exposure_mult": 1.1,
        "support_bias": 0.01,
    },
}
DEFAULT_PLAN = "balanced"


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


def _effective_volume(g: dict, hollow: bool, wall: float) -> tuple[float, str]:
    """实际要固化的体积。

    镂空后真正消耗的不是整件体积，而是「表面积 × 壳厚」——
    直接拿实心体积算会把树脂用量高估好几倍。
    """
    solid = max(0.0, g["volume_mm3"])
    if not hollow:
        return solid, "实心体积"
    shell = max(0.0, g["area_mm2"]) * max(0.0, wall)
    # 表面积 × 壳厚 是壳体积的上界；球/柱这类件实际约为 0.75~0.9 倍
    est = min(solid, shell * 0.85)
    return est, f"表面积 {g['area_mm2']:.0f}mm² × 壳厚 {wall}mm × 0.85"


def _estimate_cost(g: dict, process: str, layer_h: float, exposure: float | None,
                   infill: float | None, hollow: bool = False,
                   wall: float = 2.0) -> dict:
    """耗材与时长估算。数字是经验值，但推导过程可查，不是拍脑袋。"""
    eff_vol, vol_basis = _effective_volume(g, hollow, wall)
    vol_cm3 = max(0.0, eff_vol) / 1000.0
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
            "effective_volume_cm3": round(vol_cm3, 2),
            "basis": (f"{vol_basis} → {vol_cm3:.1f}cm³ × 1.1g/cm³；"
                      f"{layers} 层 × ({exposure or 3.0:.1f}s 曝光 + 6s 抬升) + 底层 10×30s"),
        }
    grams = vol_cm3 * 1.24 * (0.35 + (infill or 15) / 100.0 * 0.45)
    minutes = g["volume_mm3"] / 6.0 / 60.0  # 约 6mm³/s 有效挤出
    return {
        "grams": round(grams, 1),
        "hours": round(minutes / 60.0, 2),
        "layers": int(height / max(layer_h, 0.01)) if height else 0,
        "effective_volume_cm3": round(vol_cm3, 2),
        "basis": (f"{vol_basis} → {vol_cm3:.1f}cm³ × 1.24g/cm³ × 填充系数；"
                  f"按 6mm³/s 有效挤出速率折算"),
    }


def _layer_height_sla(g: dict, mat: dict) -> tuple[float, str, list[str]]:
    """多来源交叉查证层高。

    同时看四路信号：壁厚（细节）、最大尺寸（时间成本）、悬垂占比（表面质量）、
    材料允许区间。取「更细」的那个作为下限（细节优先，保守），再按材料区间夹住。
    返回 (层高, 理由, 参与判断的信号列表)。
    """
    cands: list[tuple[float, str]] = []
    signals: list[str] = []
    mw = g.get("min_wall")
    if mw is not None:
        if mw < 0.6:
            cands.append((0.03, f"最薄处 {mw:.2f}mm，薄壁处每层曝光面积小，必须 0.03mm 才能固化充分"))
            signals.append(f"壁厚 {mw:.2f}mm→0.03mm")
        elif mw < 1.2:
            cands.append((0.04, f"最薄处 {mw:.2f}mm，0.04mm 让薄壁段多分几层，避免层间台阶"))
            signals.append(f"壁厚 {mw:.2f}mm→0.04mm")
        else:
            signals.append(f"壁厚 {mw:.2f}mm 不是瓶颈")
    if g.get("max_dim", 0) > 150:
        cands.append((0.08, f"最大尺寸 {g['max_dim']:.0f}mm 属大件，0.08mm 在 {g.get('height', 0):.0f}mm 高度上省时"))
        signals.append(f"尺寸 {g['max_dim']:.0f}mm→0.08mm")
    if g.get("overhang_ratio", 0) > 0.15:
        cands.append((0.04, f"悬垂占比 {g['overhang_ratio'] * 100:.0f}%，偏细层高能改善支撑接触面的表面质量"))
        signals.append(f"悬垂 {g['overhang_ratio'] * 100:.0f}%→0.04mm")
    lo, hi = mat["layer"]
    if cands:
        cands.sort(key=lambda c: c[0])
        val, reason = cands[0]  # 取最细（安全优先）
        if len(cands) > 1:
            finer = cands[0][0]
            coarser = cands[-1][0]
            if coarser > finer:
                reason += f"；另有来源建议 {coarser}mm（更省时），取更细的 {finer}mm 以保细节。"
    else:
        val = 0.05
        reason = f"尺寸与壁厚都不苛刻，0.05mm 是细节与速度的常规平衡点。"
    val = _round_to(_clamp(val, lo, hi), 0.005)
    return val, reason, signals


def _layer_height_fdm(g: dict, nozzle: float) -> tuple[float, str, list[str]]:
    """FDM 层高：同样多来源——薄壁要细，其余按喷嘴 1/2~1/3。"""
    signals: list[str] = []
    mw = g.get("min_wall")
    if mw is not None and mw < 1.5:
        val = 0.12
        reason = f"模型最薄 {mw:.2f}mm 偏薄，降到 0.12mm 减少层间空隙（喷嘴 {nozzle}mm）。"
        signals.append(f"壁厚 {mw:.2f}mm→0.12mm")
    else:
        val = 0.2
        reason = f"喷嘴 {nozzle}mm，层高取 1/2~1/3 喷嘴直径；0.2mm 是速度与强度的平衡点。"
        signals.append("壁厚不苛刻→0.2mm")
    return val, reason, signals


def _sources(g: dict, printer: dict, process: str, mat: dict) -> list[dict]:
    """参数信息来源汇总：数据从哪来、按什么算、参考了哪些基准值。"""
    bx, by, bz = printer["build"]
    vol_cm3 = g.get("volume_mm3", 0) / 1000.0
    area_cm2 = g.get("area_mm2", 0) / 100.0
    mw = g.get("min_wall")
    geo = [
        f"体积 {vol_cm3:.2f} cm³ —— 检测报告 stats.volume_mm3",
        f"表面积 {area_cm2:.1f} cm² —— 检测报告 stats.area_mm2",
        f"最薄处 {mw:.2f} mm —— 壁厚检测 items.thickness" if mw is not None else "最薄处未测 —— 缺少壁厚检测数据",
        f"悬垂占比 {g.get('overhang_ratio', 0) * 100:.1f}% —— 45° 法向切片统计",
        f"连通部件 {g.get('components', 1)} —— 连通分量分析",
        f"最大尺寸 {g.get('max_dim', 0):.1f} mm / 高度 {g.get('height', 0):.1f} mm —— 包围盒",
    ]
    if process == "SLA":
        lo, hi = mat["layer"]
        base = [
            f"{printer['name']} 成型空间 {bx}×{by}×{bz} mm",
            f"{mat['name']} 层高区间 {lo}–{hi} mm",
            f"{mat['name']} 曝光基准 {mat['exposure']} s",
            "光敏树脂密度约 1.1 g/cm³",
        ]
    else:
        base = [
            f"{printer['name']} 成型空间 {bx}×{by}×{bz} mm，喷嘴 {printer.get('nozzle', 0.4)} mm",
            f"{mat['name']} 喷头 {mat['nozzle']}℃ / 热床 {mat['bed']}℃",
            "PLA/PETG 等密度按 1.24 g/cm³ 估",
            "有效挤出速率按 6 mm³/s 折算",
        ]
    calc = [
        "壳体积 ≈ 表面积 × 壳厚 × 0.85（球/柱件实际约为上界的 0.75~0.9）",
        "耗材 = 有效体积 × 密度 × 填充系数",
        "层数 = 高度 ÷ 层高；SLA 时长另加底层 10×30s 与每层抬升回落 6s",
        "层高下限由壁厚/细节决定，上限由材料区间与时间成本夹住",
    ]
    return [
        {"title": "几何量（来自检测报告与模型文件）", "items": geo},
        {"title": "机型与材料基准（内置规格表）", "items": base},
        {"title": "计算依据", "items": calc},
    ]


def recommend(project: dict, overrides: dict | None = None) -> dict:
    ov = overrides or {}
    g = _geometry(project)
    printer_key = ov.get("printer") or "sla_mid"
    printer = PRINTERS.get(printer_key, PRINTERS["sla_mid"])
    process = ov.get("process") or printer["process"]
    material_key = ov.get("material") or ("standard" if process == "SLA" else "PLA")
    plan_key = ov.get("plan") if ov.get("plan") in PLANS else DEFAULT_PLAN
    plan = PLANS[plan_key]

    params: list[dict] = []
    warnings: list[str] = []
    rationale: list[str] = []
    bx, by, bz = printer["build"]

    # ---- 工艺与材料
    if process == "SLA":
        mat = SLA_MATERIALS.get(material_key, SLA_MATERIALS["standard"])
        params.append({
            "key": "material", "label": "材料", "value": mat["name"], "unit": "",
            "reason": f"{printer['name']} 机型 + 模型最大尺寸 {g['max_dim']:.0f}mm。{mat['note']}。",
        })
        # 层高：多来源交叉查证（壁厚 / 尺寸 / 悬垂 / 材料区间）
        lh, lh_reason, lh_signals = _layer_height_sla(g, mat)
        lh = _round_to(_clamp(lh * plan["layer_mult"], mat["layer"][0], mat["layer"][1]), 0.005)
        if plan["layer_mult"] != 1.0:
            lh_reason += f" 方案「{plan['label']}」再 ×{plan['layer_mult']} → {lh}mm。"
        params.append({
            "key": "layer_height", "label": "层高", "value": lh, "unit": "mm", "reason": lh_reason,
        })
        if lh_signals:
            rationale.append("层高交叉查证：" + "；".join(lh_signals) + f" → 取 {lh}mm")
        # 曝光
        exp = mat["exposure"] * (0.85 if lh < 0.04 else 1.0) * (1.0 + (0.1 if g["max_dim"] > 200 else 0)) * plan["exposure_mult"]
        exp_reason = f"{mat['name']} 基准 {mat['exposure']}s，层高 {lh}mm 按比例微调。"
        if plan["exposure_mult"] != 1.0:
            exp_reason += f" 方案「{plan['label']}」曝光 ×{plan['exposure_mult']}。"
        exp_reason += "首次打印建议先跑曝光测试片。"
        params.append({
            "key": "exposure", "label": "单层曝光", "value": round(exp, 1), "unit": "s",
            "reason": exp_reason,
        })
        # 壁厚 / 镂空
        if g["volume_mm3"] > 50_000:
            params.append({
                "key": "hollow", "label": "镂空", "value": True, "unit": "",
                "reason": f"体积 {g['volume_mm3'] / 1000:.1f}cm³，实心打印耗树脂多、收缩应力大。建议壳厚 2.0mm 并开 2 个 φ3mm 排液孔。",
            })
            rationale.append(f"体积 {g['volume_mm3'] / 1000:.1f}cm³ > 50cm³ → 建议镂空省料并降收缩应力")
        else:
            params.append({
                "key": "hollow", "label": "镂空", "value": False, "unit": "",
                "reason": f"体积仅 {g['volume_mm3'] / 1000:.1f}cm³，镂空省不下多少树脂，反而增加排液和清洗负担。",
            })
            rationale.append(f"体积仅 {g['volume_mm3'] / 1000:.1f}cm³ ≤ 50cm³ → 不镂空更省事")
    else:
        mat = FDM_MATERIALS.get(material_key, FDM_MATERIALS["PLA"])
        nozzle = printer.get("nozzle", 0.4)
        params.append({
            "key": "material", "label": "材料", "value": mat["name"], "unit": "",
            "reason": f"{mat['note']}。模型最大尺寸 {g['max_dim']:.0f}mm，{printer['name']} 可容纳。",
        })
        lh, lh_reason, lh_signals = _layer_height_fdm(g, nozzle)
        lh = _round_to(_clamp(lh * plan["layer_mult"], nozzle * 0.2, nozzle * 0.75), 0.01)
        if plan["layer_mult"] != 1.0:
            lh_reason += f" 方案「{plan['label']}」再 ×{plan['layer_mult']} → {lh}mm。"
        params.append({
            "key": "layer_height", "label": "层高", "value": lh, "unit": "mm", "reason": lh_reason,
        })
        if lh_signals:
            rationale.append("层高交叉查证：" + "；".join(lh_signals) + f" → 取 {lh}mm")
        wall_lines = max(2, math.ceil(1.2 / nozzle)) + plan["wall_delta_lines"]
        wall_reason = f"{wall_lines} 圈外墙 × {nozzle}mm 线宽。低于 2 圈时强度和密封性都不够。"
        if plan["wall_delta_lines"]:
            wall_reason += f" 方案「{plan['label']}」多 {plan['wall_delta_lines']} 圈。"
        params.append({
            "key": "wall_thickness", "label": "壁厚", "value": round(wall_lines * nozzle, 2), "unit": "mm",
            "reason": wall_reason,
        })
        infill = (15 if g["volume_mm3"] < 100_000 else 20) + plan["infill_delta"]
        infill = max(5, min(80, infill))
        infill_reason = (
            f"体积 {g['volume_mm3'] / 1000:.1f}cm³。"
            + ("装饰件 15% 足够，强度靠外墙。" if g["volume_mm3"] < 100_000
               else "体积偏大，20% 陀螺填充在强度和耗材间更均衡。")
        )
        if plan["infill_delta"]:
            infill_reason += f" 方案「{plan['label']}」{plan['infill_delta']:+d}%。"
        params.append({
            "key": "infill", "label": "填充率", "value": infill, "unit": "%",
            "reason": infill_reason,
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
    if oratio > plan["support_bias"]:
        params.append({
            "key": "supports", "label": "支撑", "value": "需要", "unit": "",
            "reason": (
                f"超过 45° 的悬垂面占总表面积 {oratio * 100:.1f}%。"
                f"这些面下方没有材料承接，不打支撑会塌陷或拉丝。"
            ),
        })
        rationale.append(f"悬垂 {oratio * 100:.1f}% > {plan['support_bias'] * 100:.0f}% 阈值 → 需要支撑")
    else:
        params.append({
            "key": "supports", "label": "支撑", "value": "可选", "unit": "",
            "reason": f"悬垂面仅占 {oratio * 100:.1f}%，模型自支撑性良好。",
        })
        rationale.append(f"悬垂 {oratio * 100:.1f}% 较低 → 支撑可选")

    # ---- 分件判断
    if g["max_dim"] > max(bx, by, bz):
        warnings.append(
            f"模型最大尺寸 {g['max_dim']:.0f}mm 超出 {printer['name']} 的成型空间 "
            f"{bx}×{by}×{bz}mm，必须分件或更换机型。"
        )
        rationale.append(f"最大尺寸 {g['max_dim']:.0f}mm 超出成型空间 → 必须分件或换机型")
    if g["height"] > bz * 0.9:
        warnings.append(f"模型高度 {g['height']:.0f}mm 接近成型高度上限 {bz}mm，支撑和取件都会很勉强。")
        rationale.append(f"高度 {g['height']:.0f}mm 接近上限 {bz}mm → 支撑与取件勉强")

    # ---- 质检前置条件
    if not g["watertight"]:
        warnings.append("模型当前不水密。切片软件可能拒绝切片或生成错误截面，请先执行补洞。")
        rationale.append("模型不水密 → 建议先补洞再切片")
    if g["min_wall"] is not None and g["min_wall"] < 0.4:
        warnings.append(f"最薄处 {g['min_wall']:.2f}mm 低于 0.4mm，无论什么工艺都打不出来，需先加厚。")
        rationale.append(f"最薄处 {g['min_wall']:.2f}mm < 0.4mm → 无论工艺都打不出，需先加厚")
    if g["components"] and g["components"] > 1:
        warnings.append(f"模型含 {g['components']} 个互不连通的部件，切片时会各自生成支撑，注意检查是否需要合并。")
        rationale.append(f"含 {g['components']} 个不连通部件 → 注意是否需合并")

    lh_val = next((p["value"] for p in params if p["key"] == "layer_height"), 0.05)
    exp_val = next((p["value"] for p in params if p["key"] == "exposure"), None)
    inf_val = next((p["value"] for p in params if p["key"] == "infill"), None)
    hollow_flag = next((p["value"] for p in params if p["key"] == "hollow"), False)
    wall_val = next((p["value"] for p in params if p["key"] == "wall_thickness"), 2.0)
    cost = _estimate_cost(g, process, float(lh_val), exp_val, inf_val,
                          hollow=bool(hollow_flag), wall=float(wall_val or 2.0))
    params.append({
        "key": "cost", "label": "预估耗材", "unit": "",
        "value": f"{cost['grams']} g / {cost['hours']} h",
        "reason": cost["basis"] + "。实际会受摆放密度、支撑量和机器效率影响。",
    })

    if plan_key != DEFAULT_PLAN:
        rationale.append(f"方案「{plan['label']}」：层高 ×{plan['layer_mult']}、填充 {plan['infill_delta']:+d}%、曝光 ×{plan['exposure_mult']}")

    sources = _sources(g, printer, process, mat)
    summary = (
        f"{printer['name']} / {params[0]['value']}，层高 {lh_val}mm，"
        f"预估 {cost['grams']}g / {cost['hours']}h。"
        f"方案：{plan['label']}。"
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
        "plan": plan_key,
        "plans": {k: {"label": v["label"], "desc": v["desc"]} for k, v in PLANS.items()},
        "rationale": rationale,
        "sources": sources,
    }
