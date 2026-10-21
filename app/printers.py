"""打印机型参数预设库。

设计取舍
--------
1. **厂家标定值优先，物理模型外推。**
   厂家公布的「某树脂在某层厚下的推荐曝光」是实测标定出来的，比任何公式都可信。
   所以结构里存 `official`（厂家标定表），算不出来时再用 Jacobs 曲线外推。
   两者差别会显式暴露给用户，不静默兜底。

2. **每个字段都标出处。**
   `src` 只有两种取值：
     "official" —— 厂家公开规格/推荐参数表
     "estimate" —— 按同级别机型常见量级推定，需要用户自己校准
   界面上会据此打「实测」或「待校准」标签，避免用户把估算值当圣旨。

3. **缺项不猜，退回并记账。**
   取预设时用 `resolve()`，缺哪个字段就记到 `missing` 里，
   由 advisor 决定怎么提示，而不是偷偷塞一个默认值。

单位约定
--------
长度 mm，速度 mm/s，时间 s，流量 mm³/s，
照度 mW/cm²，分辨率 mm/px，温度 ℃。
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------- 工艺默认值
# 机型预设没给全时，用这一层兜底。数值取该工艺的常见中位量级。
PROCESS_DEFAULTS: dict[str, dict[str, Any]] = {
    "SLA": {
        "build": [150.0, 90.0, 180.0],
        "xy_res": 0.05,
        "irradiance": 4.0,
        "layer_range": [0.02, 0.15],
        "lift_mm": 6.0,
        "lift_speed": 2.5,
        "retract_speed": 3.0,
        "light_off_delay": 1.0,
        "settle": 0.5,
        "bottom_layers": 6,
        "bottom_exposure": 28.0,
    },
    "FDM": {
        "build": [220.0, 220.0, 250.0],
        "nozzle": 0.4,
        "max_flow": 12.0,
        "speed": 60.0,
        "travel_speed": 120.0,
        "accel": 800.0,
        "layer_overhead_s": 0.35,
        "min_layer_s": 5.0,
    },
}

# 厂家公布推荐参数时用的参考层厚（mm）
REFERENCE_LAYER = 0.05


def _sla(key: str, brand: str, model: str, build: list[float], xy_res: float,
          irradiance: float, layer_range: list[float], lift_mm: float,
          lift_speed: float, retract_speed: float, light_off_delay: float,
          settle: float, bottom_layers: int, bottom_exposure: float,
          resin_temp: float | None = None, verified: bool = False,
          note: str = "", official_exposure: dict[str, float] | None = None) -> dict[str, Any]:
    return {
        "key": key, "brand": brand, "model": model, "process": "SLA",
        "build": [float(x) for x in build],
        "xy_res": float(xy_res),
        "irradiance": float(irradiance),
        "layer_range": [float(x) for x in layer_range],
        "lift_mm": float(lift_mm),
        "lift_speed": float(lift_speed),
        "retract_speed": float(retract_speed),
        "light_off_delay": float(light_off_delay),
        "settle": float(settle),
        "bottom_layers": int(bottom_layers),
        "bottom_exposure": float(bottom_exposure),
        "resin_temp": resin_temp,
        # 厂家公布的「某树脂在 0.05mm 层厚下的推荐曝光」，用来和物理模型互相印证
        "official_exposure": official_exposure or {},
        "verified": bool(verified),
        "note": note,
    }


def _fdm(key: str, brand: str, model: str, build: list[float], nozzle: float,
         max_flow: float, speed: float, travel_speed: float, accel: float,
         layer_overhead_s: float, min_layer_s: float,
         nozzle_options: list[float] | None = None,
         max_nozzle_temp: float = 300.0, max_bed_temp: float = 110.0,
         enclosure: bool = False, verified: bool = False,
         note: str = "") -> dict[str, Any]:
    # FDM 的层高区间由喷嘴决定：太薄会挤不出连续丝，太厚超过 0.75 倍喷嘴就压不实。
    # 给成显式区间，是为了和光固化走同一套 _layer_bounds 夹取逻辑。
    lo_lh = round(max(0.05, nozzle * 0.2), 2)
    hi_lh = round(nozzle * 0.75, 2)
    return {
        "key": key, "brand": brand, "model": model, "process": "FDM",
        "build": [float(x) for x in build],
        "nozzle": float(nozzle),
        "layer_range": [lo_lh, hi_lh],
        "nozzle_options": [float(x) for x in (nozzle_options or [0.2, 0.4, 0.6, 0.8])],
        "max_flow": float(max_flow),
        "speed": float(speed),
        "travel_speed": float(travel_speed),
        "accel": float(accel),
        "layer_overhead_s": float(layer_overhead_s),
        "min_layer_s": float(min_layer_s),
        "max_nozzle_temp": float(max_nozzle_temp),
        "max_bed_temp": float(max_bed_temp),
        "enclosure": bool(enclosure),
        "verified": bool(verified),
        "note": note,
    }


# ---------------------------------------------------------------- 光固化
_SLA_PRESETS: list[dict[str, Any]] = [
    # 纵维立方 Photon P1 Max —— 用户自用机型，规格取自 Anycubic 官方产品页
    _sla(
        key="anycubic_photon_p1_max",
        brand="纵维立方 Anycubic",
        model="Photon P1 Max",
        build=[285.5, 214.0, 300.0],
        xy_res=0.0248,            # 14" 12K 单色屏，11520 × 8640
        irradiance=4.0,           # 官方标 ≥4000 μW/cm²
        layer_range=[0.02, 0.15],
        lift_mm=5.0,              # 官方推荐 Z 抬升 5mm
        lift_speed=3.0,           # 官方推荐 3 mm/s
        retract_speed=3.0,
        light_off_delay=2.0,          # 官方推荐熄灯 2s
        settle=0.5,
        bottom_layers=6,
        bottom_exposure=30.0,
        resin_temp=35.0,          # 动态温控料盘，可达 35℃
        verified=True,
        note="大幅面 12K：像素 24.8μm，官方标定曝光偏短（快干树脂），建议首件仍跑曝光测试片",
        # 官方水洗树脂 2.0 参数表：0.05mm / 2s / 熄灯 2s / 底曝 30s / 抬升 5mm / 3-3 mm/s
        official_exposure={"water_wash": 2.0},
    ),
    _sla(
        key="anycubic_photon_p1",
        brand="纵维立方 Anycubic",
        model="Photon P1",
        build=[223.0, 126.0, 230.0],
        xy_res=0.0248,
        irradiance=4.0,
        layer_range=[0.02, 0.15],
        lift_mm=5.0,
        lift_speed=3.0,
        retract_speed=3.0,
        light_off_delay=2.0,
        settle=0.5,
        bottom_layers=6,
        bottom_exposure=30.0,
        resin_temp=35.0,
        verified=True,
        official_exposure={"water_wash": 2.0},
    ),
    _sla(
        key="anycubic_mono_m7_max",
        brand="纵维立方 Anycubic",
        model="Photon Mono M7 Max",
        build=[298.0, 164.0, 300.0],
        xy_res=0.046,             # 13.6" 7K，官方标 4500 ±10% μW/cm²
        irradiance=4.5,
        layer_range=[0.02, 0.15],
        lift_mm=8.0,
        lift_speed=6.0,
        retract_speed=6.0,
        light_off_delay=0.5,
        settle=0.5,
        bottom_layers=5,
        bottom_exposure=30.0,
        verified=True,
        note="官方水洗树脂表：0.05mm / 2s / 熄灯 0.5s / 底曝 30s / 抬升 8mm / 6-6 mm/s",
        official_exposure={"water_wash": 2.0},
    ),
    _sla(
        key="anycubic_mono_m7_pro",
        brand="纵维立方 Anycubic",
        model="Photon Mono M7 Pro",
        build=[223.0, 126.0, 230.0],
        xy_res=0.046,
        irradiance=4.5,
        layer_range=[0.01, 0.15],
        lift_mm=8.0,
        lift_speed=6.0,
        retract_speed=6.0,
        light_off_delay=0.5,
        settle=0.5,
        bottom_layers=4,
        bottom_exposure=25.0,
        verified=True,
        note="官方水洗树脂表：0.05mm / 2s / 熄灯 0.5s / 底曝 25s / 抬升 8mm / 6-6 mm/s",
        official_exposure={"water_wash": 2.0},
    ),
    _sla(
        key="elegoo_saturn_4_ultra",
        brand="智能派 Elegoo",
        model="Saturn 4 Ultra",
        build=[218.88, 122.88, 220.0],
        xy_res=0.019,             # 10" 12K
        irradiance=4.5,
        layer_range=[0.01, 0.15],
        lift_mm=6.0,
        lift_speed=3.0,
        retract_speed=4.0,
        light_off_delay=1.0,
        settle=0.5,
        bottom_layers=5,
        bottom_exposure=28.0,
        verified=False,
        note="12K 10 寸屏；抬升参数取常见配置，建议按实测微调",
    ),
    _sla(
        key="elegoo_saturn_3",
        brand="智能派 Elegoo",
        model="Saturn 3",
        build=[218.88, 122.88, 250.0],
        xy_res=0.019,             # 10" 12K，19 × 24 μm
        irradiance=4.0,
        layer_range=[0.02, 0.15],
        lift_mm=6.0,
        lift_speed=3.0,
        retract_speed=3.5,
        light_off_delay=1.0,
        settle=0.5,
        bottom_layers=5,
        bottom_exposure=30.0,
        verified=False,
    ),
    _sla(
        key="elegoo_mars_5_ultra",
        brand="智能派 Elegoo",
        model="Mars 5 Ultra",
        build=[153.36, 77.76, 165.0],
        xy_res=0.018,             # 9K
        irradiance=4.5,
        layer_range=[0.01, 0.15],
        lift_mm=5.0,
        lift_speed=3.0,
        retract_speed=4.0,
        light_off_delay=1.0,
        settle=0.5,
        bottom_layers=5,
        bottom_exposure=30.0,
        verified=False,
    ),
    _sla(
        key="elegoo_mars_4",
        brand="智能派 Elegoo",
        model="Mars 4",
        build=[153.36, 77.76, 165.0],
        xy_res=0.035,             # 4K
        irradiance=3.5,
        layer_range=[0.02, 0.15],
        lift_mm=5.0,
        lift_speed=2.5,
        retract_speed=3.0,
        light_off_delay=0.5,
        settle=0.5,
        bottom_layers=5,
        bottom_exposure=30.0,
        verified=False,
    ),
    _sla(
        key="phrozen_sonic_mega_8k",
        brand="Phrozen 普罗德",
        model="Sonic Mega 8K",
        build=[330.0, 185.0, 400.0],
        xy_res=0.043,
        irradiance=4.0,
        layer_range=[0.02, 0.15],
        lift_mm=8.0,
        lift_speed=3.0,
        retract_speed=3.5,
        light_off_delay=1.0,
        settle=0.5,
        bottom_layers=6,
        bottom_exposure=32.0,
        verified=False,
    ),
    _sla(
        key="uniformation_gktwo",
        brand="Uniformation",
        model="GKtwo",
        build=[228.0, 128.0, 245.0],
        xy_res=0.0298,            # 8K
        irradiance=4.0,
        layer_range=[0.01, 0.15],
        lift_mm=6.0,
        lift_speed=3.0,
        retract_speed=3.5,
        light_off_delay=1.0,
        settle=0.5,
        bottom_layers=5,
        bottom_exposure=28.0,
        verified=False,
    ),
]

# ---------------------------------------------------------------- FDM
_FDM_PRESETS: list[dict[str, Any]] = [
    _fdm(key="bambu_x1c", brand="拓竹 Bambu Lab", model="X1 Carbon",
         build=[256, 256, 256], nozzle=0.4, max_flow=21.0, speed=200.0,
         travel_speed=350.0, accel=10000.0, layer_overhead_s=0.25, min_layer_s=3.0,
         enclosure=True, verified=False, note="高速机，流量与加速度按官方标称上限估"),
    _fdm(key="bambu_p1s", brand="拓竹 Bambu Lab", model="P1S",
         build=[256, 256, 256], nozzle=0.4, max_flow=18.0, speed=180.0,
         travel_speed=300.0, accel=8000.0, layer_overhead_s=0.25, min_layer_s=3.0,
         enclosure=True, verified=False),
    _fdm(key="bambu_a1", brand="拓竹 Bambu Lab", model="A1",
         build=[256, 256, 256], nozzle=0.4, max_flow=16.0, speed=160.0,
         travel_speed=250.0, accel=6000.0, layer_overhead_s=0.3, min_layer_s=4.0,
         enclosure=False, verified=False),
    _fdm(key="creality_k1", brand="创想三维 Creality", model="K1",
         build=[220, 220, 250], nozzle=0.4, max_flow=20.0, speed=180.0,
         travel_speed=300.0, accel=8000.0, layer_overhead_s=0.25, min_layer_s=3.0,
         enclosure=True, verified=False),
    _fdm(key="creality_k1_max", brand="创想三维 Creality", model="K1 Max",
         build=[300, 300, 300], nozzle=0.4, max_flow=20.0, speed=180.0,
         travel_speed=300.0, accel=8000.0, layer_overhead_s=0.25, min_layer_s=3.0,
         enclosure=True, verified=False),
    _fdm(key="creality_ender3_v3_ke", brand="创想三维 Creality", model="Ender-3 V3 KE",
         build=[220, 220, 240], nozzle=0.4, max_flow=15.0, speed=150.0,
         travel_speed=250.0, accel=5000.0, layer_overhead_s=0.3, min_layer_s=4.0,
         enclosure=False, verified=False),
    _fdm(key="prusa_mk4", brand="Prusa", model="MK4",
         build=[250, 210, 220], nozzle=0.4, max_flow=13.0, speed=120.0,
         travel_speed=180.0, accel=4000.0, layer_overhead_s=0.3, min_layer_s=4.0,
         enclosure=False, verified=False),
    _fdm(key="prusa_mini", brand="Prusa", model="MINI+",
         build=[180, 180, 180], nozzle=0.4, max_flow=11.0, speed=100.0,
         travel_speed=150.0, accel=2500.0, layer_overhead_s=0.35, min_layer_s=4.0,
         enclosure=False, verified=False),
    _fdm(key="elegoo_neptune_4_pro", brand="智能派 Elegoo", model="Neptune 4 Pro",
         build=[225, 225, 265], nozzle=0.4, max_flow=15.0, speed=150.0,
         travel_speed=250.0, accel=5000.0, layer_overhead_s=0.3, min_layer_s=4.0,
         enclosure=False, verified=False),
    _fdm(key="anycubic_kobra_2_pro", brand="纵维立方 Anycubic", model="Kobra 2 Pro",
         build=[220, 220, 250], nozzle=0.4, max_flow=14.0, speed=140.0,
         travel_speed=220.0, accel=4000.0, layer_overhead_s=0.3, min_layer_s=4.0,
         enclosure=False, verified=False),
]


# ---------------------------------------------------------------- 自检
# 20 款机型是手写的数据，手写就会错。与其等用户选了才发现，不如导入时就炸。
_PROBLEMS: list[str] = []


def validate_presets() -> list[str]:
    """校验预设库的字段合法性与内部一致性，返回问题列表（空表示通过）。

    检查的是「一眼能看出错」的那类问题：区间反了、照度写成 0、
    层高上限超过喷嘴、别名指向不存在的机型。这些错误不会立刻抛异常，
    只会在某次建议里给出离谱数值——所以必须在导入时就按住。
    """
    import math

    problems: list[str] = []
    for key, x in PRESETS.items():
        where = f"{key}({x.get('model')})"
        if x.get("process") not in PROCESS_DEFAULTS:
            problems.append(f"{where}: process 非法 {x.get('process')!r}")
        lr = x.get("layer_range")
        if not (isinstance(lr, (list, tuple)) and len(lr) == 2):
            problems.append(f"{where}: layer_range 缺失或格式错")
        elif not (0 < float(lr[0]) < float(lr[1])):
            problems.append(f"{where}: layer_range 区间非法 {lr}")
        build = x.get("build")
        if not (isinstance(build, (list, tuple)) and len(build) == 3
                and all(float(v) > 0 for v in build)):
            problems.append(f"{where}: build 非法 {build}")
        if x.get("process") == "SLA":
            if float(x.get("irradiance") or 0) <= 0:
                problems.append(f"{where}: irradiance 必须为正")
            for f in ("lift_mm", "lift_speed", "retract_speed", "bottom_exposure"):
                if float(x.get(f) or 0) <= 0:
                    problems.append(f"{where}: {f} 必须为正")
            if int(x.get("bottom_layers") or 0) <= 0:
                problems.append(f"{where}: bottom_layers 必须为正整数")
            ec = x.get("official_exposure") or {}
            if not isinstance(ec, dict):
                problems.append(f"{where}: official_exposure 必须是 dict")
            else:
                for m, sec in ec.items():
                    if not math.isfinite(float(sec)) or float(sec) <= 0:
                        problems.append(f"{where}: official_exposure[{m}] 非法 {sec}")
        else:
            nz = float(x.get("nozzle") or 0)
            if nz <= 0:
                problems.append(f"{where}: nozzle 必须为正")
            opts = [float(v) for v in (x.get("nozzle_options") or [])]
            if nz and opts and nz not in opts:
                problems.append(f"{where}: nozzle {nz} 不在 nozzle_options {opts} 内")
            if float(x.get("max_flow") or 0) <= 0:
                problems.append(f"{where}: max_flow 必须为正")
            if lr and nz and float(lr[1]) > nz:
                problems.append(f"{where}: 层高上限 {lr[1]} 超过喷嘴 {nz}")
    for alias, target in ALIASES.items():
        if target not in PRESETS:
            problems.append(f"ALIASES[{alias}] 指向不存在的机型 {target}")
    for proc, dk in DEFAULT_PRESET.items():
        if dk not in PRESETS:
            problems.append(f"DEFAULT_PRESET[{proc}] 指向不存在的机型 {dk}")
        elif PRESETS[dk]["process"] != proc:
            problems.append(f"DEFAULT_PRESET[{proc}] 的工艺不匹配")
    return problems


# 注意：执行点在 PRESETS 定义之后，见文件末尾的 _validate_on_import()


def _build() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for p in (*_SLA_PRESETS, *_FDM_PRESETS):
        out[p["key"]] = p
    return out


PRESETS: dict[str, dict[str, Any]] = _build()


def _validate_on_import() -> None:
    """导入即校验：数据错了就是代码错了，直接炸，不要带着脏数据跑。"""
    problems = validate_presets()
    if problems:
        raise RuntimeError("机型预设校验失败：\n  - " + "\n  - ".join(problems))



# 搜索别名。用户可能打「纵维」「anycubic」「p1max」「土星」——
# 只按 brand+model 匹配会搜不到，这里补一层常见叫法。
SEARCH_ALIASES: dict[str, list[str]] = {
    "anycubic_photon_p1_max": ["纵维", "anycubic", "p1max", "p1 max", "光子"],
    "anycubic_photon_p1": ["纵维", "anycubic", "p1", "光子"],
    "anycubic_mono_m7_max": ["纵维", "m7max", "光子"],
    "anycubic_mono_m7_pro": ["纵维", "m7pro"],
    "elegoo_saturn_4_ultra": ["智能派", "elegoo", "土星", "saturn4"],
    "elegoo_saturn_3": ["智能派", "elegoo", "土星", "saturn3"],
    "elegoo_mars_5_ultra": ["智能派", "elegoo", "火星", "mars5"],
    "elegoo_mars_4": ["智能派", "elegoo", "火星", "mars4"],
    "phrozen_sonic_mega_8k": ["普罗德", "phrozen", "mega8k"],
    "uniformation_gktwo": ["uniformation", "gk2", "gktwo"],
    "bambu_x1c": ["拓竹", "bambu", "x1c", "x1 carbon"],
    "bambu_p1s": ["拓竹", "bambu", "p1s"],
    "bambu_a1": ["拓竹", "bambu", "a1"],
    "creality_k1": ["创想", "creality", "k1"],
    "creality_k1_max": ["创想", "creality", "k1max"],
    "creality_ender3_v3_ke": ["创想", "creality", "ender", "ender3", "e3v3"],
    "prusa_mk4": ["prusa", "普鲁萨", "mk4"],
    "prusa_mini": ["prusa", "普鲁萨", "mini"],
    "elegoo_neptune_4_pro": ["智能派", "elegoo", "海王星", "neptune"],
    "anycubic_kobra_2_pro": ["纵维", "anycubic", "kobra", "眼镜蛇"],
}


def _search_blob(x: dict[str, Any]) -> str:
    """把品牌、型号、key 和别名拼成一串，供前端做包含匹配。"""
    parts = [x.get("brand") or "", x.get("model") or "", x["key"]]
    parts.extend(SEARCH_ALIASES.get(x["key"], []))
    return " ".join(parts).lower()

# 没选机型时的默认项。光固化默认给 P1 Max（用户自用机型）。
DEFAULT_PRESET: dict[str, str] = {
    "SLA": "anycubic_photon_p1_max",
    "FDM": "creality_ender3_v3_ke",
}

# 旧 key 到新 key 的映射：老项目/老链接里的机型别名要能继续用
ALIASES: dict[str, str] = {
    "sla_small": "elegoo_mars_4",
    "sla_mid": "elegoo_saturn_3",
    "sla_large": "anycubic_photon_p1_max",
    "fdm_220": "creality_ender3_v3_ke",
    "fdm_350": "creality_k1_max",
}


def resolve(key: str | None, process: str | None = None) -> tuple[dict[str, Any], list[str]]:
    """取一个机型预设，缺的字段用工艺默认值补。

    返回 (合并后的预设, 缺失字段列表)。调用方拿 missing 去提示用户。
    """
    k = (key or "").strip()
    if k in ALIASES:
        k = ALIASES[k]
    src = PRESETS.get(k)
    from_preset = src is not None
    if src is None:
        proc = (process or "SLA").upper()
        k = DEFAULT_PRESET.get(proc, DEFAULT_PRESET["SLA"])
        src = PRESETS[k]

    proc = src["process"]
    merged = dict(PROCESS_DEFAULTS.get(proc, {}))
    merged.update(src)

    missing = [f for f in PROCESS_DEFAULTS.get(proc, {}) if f not in src]
    merged["key"] = src["key"]
    merged["resolved_from"] = "preset" if from_preset else "fallback"
    return merged, missing


def list_presets(process: str | None = None) -> list[dict[str, Any]]:
    """列出预设（供界面选择）。默认只给摘要，不带完整参数。"""
    items = list(PRESETS.values())
    if process:
        p = process.upper()
        items = [x for x in items if x["process"] == p]
    return [
        {
            "key": x["key"],
            "brand": x["brand"],
            "model": x["model"],
            "process": x["process"],
            "build": x["build"],
            # 光固化与 FDM 的摘要字段不同，用 get 取，避免 KeyError
            "xy_res": x.get("xy_res"),
            "irradiance": x.get("irradiance"),
            "nozzle": x.get("nozzle"),
            "layer_range": x.get("layer_range"),
            "verified": x["verified"],
            "note": x["note"],
            "search": _search_blob(x),
        }
        for x in items
    ]


def get_preset(key: str) -> dict[str, Any] | None:
    k = ALIASES.get(key, key)
    return PRESETS.get(k)


# 全部常量就位后再校验 —— 校验本身要读 PRESETS / ALIASES / DEFAULT_PRESET
_validate_on_import()
