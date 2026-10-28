"""打印参数建议引擎。

原则：不给「经验值」，只给「从这个模型的这个数字推出来的值」。
每条建议都带 reason，前端直接把 reason 展示给用户，
用户不同意时可以顺着依据去改，而不是盲从一个数字。

三条硬规矩：

1. 数字必须来自模型，不能来自「一般」。
   曝光用 Jacobs 固化曲线反解，时长用截面周长积分算挤出路径长度，
   每条都能往前追到「哪个几何量 × 哪个物性」。

2. 凡给数字必给区间。
   单点估算假装自己很准，是最容易骗人的形式。
   这里用蒙特卡洛把输入的不确定度传下去，输出 P10/P50/P90。

3. 数据不足就认。
   几何缺项、多路信号打架、没有本机历史记录，都会压低置信度，
   并且把「凭什么这么定」摊开给用户看。

「AI 建议」这部分刻意不接大模型生成数字：
参数是要拿去打件的，编错了就是废料。模型的活儿是把算出来的结果讲清楚，
不是替计算过程拍板。
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from . import analysis, calibration, gpu, meshio, printmodel, printers

# ---------------------------------------------------------------- 机型
# 光固化：irradiance 是屏幅照度（mW/cm²），直接决定曝光时间，
#         不同机型差 2 倍很常见，老版本给死值就是在这上面翻车。
#         lift_* 是抬升回落机构参数，占单层时间的六成以上。
def _build_printers() -> dict:
    """把 printers.py 的预设摊平成老接口的形状。

    保留 PRINTERS 这个名字是因为已有调用方（反馈接口、旧测试）按
    PRINTERS[key]["name"] / ["build"] 取值，换名字会散架。
    """
    out: dict = {}
    for key in printers.PRESETS:
        merged, _ = printers.resolve(key)
        merged["name"] = f"{merged['brand']} {merged['model']}"
        out[key] = merged
    # 别名也挂上，老 key 照样能解析
    for alias, target in printers.ALIASES.items():
        if target in out:
            out[alias] = dict(out[target], key=alias, name=f"{out[target]['name']}（兼容旧名）")
    return out


PRINTERS = _build_printers()


# ---------------------------------------------------------------- 材料
# dp  穿透深度 Dp（mm）：光在树脂里衰减到 1/e 的距离
# ec  临界曝光 Ec（mJ/cm²）：刚好凝胶所需的最小剂量
# 这两项是 Jacobs 固化曲线的全部参数，也是曝光计算的唯一来源。
# 数值按常见 405nm 树脂的量级给定，换料时优先填厂家数据；
# 填不了就跑一次曝光测试片，用 calibration 模块反推。
SLA_MATERIALS = {
    "standard": {
        "name": "标准刚性树脂", "layer": [0.03, 0.1], "exposure": 2.5,
        "default_layer": 0.05, "dp": 0.110, "ec": 4.6,
        "density": 1.10, "shrink": 0.5, "tensile": 55.0, "elong": 6.0,
        "note": "通用，细节表现好",
    },
    "tough": {
        "name": "韧性树脂", "layer": [0.03, 0.08], "exposure": 3.5,
        "default_layer": 0.05, "dp": 0.106, "ec": 6.2,
        "density": 1.12, "shrink": 0.8, "tensile": 45.0, "elong": 25.0,
        "note": "细长件、卡扣件首选",
    },
    "water_wash": {
        "name": "水洗树脂", "layer": [0.05, 0.1], "exposure": 2.0,
        "default_layer": 0.05, "dp": 0.110, "ec": 3.6,
        "density": 1.08, "shrink": 0.9, "tensile": 35.0, "elong": 12.0,
        "note": "后处理省事，强度略低",
    },
    "castable": {
        "name": "铸造树脂", "layer": [0.025, 0.05], "exposure": 4.0,
        "default_layer": 0.04, "dp": 0.090, "ec": 7.5,
        "density": 1.15, "shrink": 1.2, "tensile": 30.0, "elong": 3.0,
        "note": "首饰倒模用",
    },
}

# z_factor 层间结合强度折减：FDM 沿 Z 方向拉伸强度明显低于层内，
# 承力件按这个折减算截面才不会断。
FDM_MATERIALS = {
    "PLA": {
        "name": "PLA", "nozzle": 210, "bed": 60, "fan": 100,
        "density": 1.24, "tensile": 50.0, "z_factor": 0.75, "flow": 1.0,
        "note": "易打印，耐温低（约 55℃）",
    },
    "PETG": {
        "name": "PETG", "nozzle": 240, "bed": 80, "fan": 50,
        "density": 1.27, "tensile": 45.0, "z_factor": 0.65, "flow": 0.95,
        "note": "韧性好，耐温约 75℃",
    },
    "ABS": {
        "name": "ABS", "nozzle": 250, "bed": 100, "fan": 0,
        "density": 1.04, "tensile": 40.0, "z_factor": 0.60, "flow": 1.0,
        "note": "耐温 100℃，需要封闭仓",
    },
    "TPU": {
        "name": "TPU", "nozzle": 225, "bed": 50, "fan": 60,
        "density": 1.20, "tensile": 30.0, "z_factor": 0.90, "flow": 0.90,
        "note": "弹性体，需直驱挤出",
    },
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
        "adhesion": 2.0,
    },
    "balanced": {
        "label": "折中",
        "desc": "质量与时间兼顾，默认方案。",
        "layer_mult": 1.0,
        "infill_delta": 0,
        "wall_delta_lines": 0,
        "exposure_mult": 1.0,
        "support_bias": 0.02,
        "adhesion": 2.2,
    },
    "conservative": {
        "label": "保守",
        "desc": "更细腻、更结实：层高偏细、填充偏高、曝光略长。适合手办、承力件。",
        "layer_mult": 0.75,
        "infill_delta": 5,
        "wall_delta_lines": 1,
        "exposure_mult": 1.1,
        "support_bias": 0.01,
        "adhesion": 2.6,
    },
}
DEFAULT_PLAN = "balanced"

# ---------------------------------------------------------------- 物理常量
# 新层必须熔进上一层，否则层间分层。固化深度取层高的 2~3 倍是行业通行做法。
ADHESION_MIN = 1.6

# 大截面剥离修正：截面越大，离型时 FEP 膜吸力越强，
# 固化不足的层会被从支撑上拽下来。超过参考面积开始线性加曝光，封顶 15%。
PEEL_AREA_REF_MM2 = 2500.0
PEEL_MAX_GAIN = 0.15

# 底层（burn-in）参数：底层曝光通常是正常层的 3~6 倍，否则粘不住平台。
BOTTOM_LAYERS = 6
BOTTOM_EXPOSURE_MULT = 4.0

# 剥离风险阈值：最大截面 ÷ 平台接触面积超过这个值，
# 离型时的吸力足以把件从平台上拽下来，需要支撑或底 raft。
PEEL_RISK_WARN = 5.0

# FDM 挤出线宽系数：实际挤出宽度约为喷嘴的 1.1~1.15 倍
LINE_WIDTH_FACTOR = 1.125
INFILL_WIDTH_FACTOR = 1.25

# 顶/底面实心层数：这几层是 100% 填充，不跟着填充率走
SKIN_LAYERS = 4

# 支撑耗材估算（启发式）：单位接触面积摊到的支撑体积，mm³/mm²。
# 支撑是树状而非实心柱，这个系数是「接触面 + 支柱 + 底盘」按面积摊薄后的经验值。
SUPPORT_VOL_PER_MM2 = 1.8

# 蒙特卡洛不确定度（对数标准差）
SD_DENSITY = 0.02      # 密度：厂家标称值本身有波动
SD_FLOW = 0.12         # 有效流量：受温度、料、挤出机一致性影响
SD_GEOMETRY = 0.05     # 截面采样积分误差
SD_OVERHEAD = 0.18     # 每层固定开销、抬升回落、加减速
SD_EXPOSURE = 0.10     # 曝光相关的 Dp/Ec 误差

# Jacobs 曲线三个参数本身的对数标准差，用于解析法求曝光区间
SD_DP = 0.10           # 穿透深度：同批料也有 ±10% 波动
SD_EC = 0.15           # 临界曝光：对配方和温度最敏感
SD_IRRADIANCE = 0.08   # 屏幅照度：随灯珠衰减缓慢下降

Z80 = 1.2816           # P10/P90 对应的正态分位数

MC_SAMPLES = 400


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _round_to(x: float, step: float) -> float:
    # 步长为 0 会除零；这里兜一下，正常调用不会走到
    if not step or step <= 0:
        return round(float(x), 4)
    return round(round(x / step) * step, 4)


def _round_in_range(x: float, step: float, lo: float, hi: float) -> float:
    """先夹到区间内再取整，取整后若越界就退一格。

    只做 clamp 再 round 是不够的：hi=0.298、步长 0.01 时，0.298 会被取整成
    0.30，反而超出上限；切片器拿到超限层高会直接拒绝。所以取整必须再夹一次。
    """
    lo, hi = float(lo), float(hi)
    if not (hi > 0) or not (lo > 0):
        # 区间本身坏了，退回一个安全值而不是产出 0 或负数
        return max(step, 0.01)
    if lo > hi:
        # 上下界矛盾（材料区间与机型区间不重叠），取更细的一侧 —— 打不出来
        # 比打出来糊更难接受，所以宁可慢也不可厚
        return round(lo, 4)
    v = _round_to(_clamp(float(x), lo, hi), step)
    if v < lo:
        v = _round_to(lo + step, step) if lo + step <= hi else round(lo, 4)
    if v > hi:
        v = _round_to(hi - step, step) if hi - step >= lo else round(hi, 4)
    return round(min(max(v, lo), hi), 4)


def _num(x, default: float = 0.0) -> str:
    """数字转字符串，去掉多余的尾零。

    基准表里写的是 1.1g/cm³，界面上就不能显示成 1.10g/cm³——
    后者看着像「精确到百分位」，其实只是格式化产物。
    """
    v = _f(x, default)
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _f(x, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- D-S 证据合成
# 层高有好几路信号在拉扯：壁厚要求细、尺寸要求粗、悬垂要求细。
# 旧版一律「取最细」，等于永远只听最保守那一路，另外几路白算。
# 这里用 Dempster-Shafer 合成：每路信号给出对各档的置信质量，
# 合成后取 pignistic 概率加权。好处是冲突能量化——
# 两路信号打架时 K 会升高，直接拿来压置信度，而不是偷偷取个值糊过去。
FRAME = ("F", "M", "C")


def _mk_mass(spec: dict[tuple[str, ...], float]) -> dict[frozenset, float]:
    """构造置信质量函数，剩余质量自动归给全集 Θ（无知）。"""
    m: dict[frozenset, float] = {}
    for k, v in spec.items():
        m[frozenset(k)] = m.get(frozenset(k), 0.0) + max(0.0, float(v))
    total = sum(m.values())
    if total > 1.0:
        m = {k: v / total for k, v in m.items()}
        total = 1.0
    if total < 1.0:
        m[frozenset(FRAME)] = m.get(frozenset(FRAME), 0.0) + (1.0 - total)
    return m


def _ds_combine(m1: dict[frozenset, float],
                m2: dict[frozenset, float]) -> tuple[dict[frozenset, float] | None, float]:
    """Dempster 合成规则。返回 (合成质量, 冲突系数 K)。"""
    out: dict[frozenset, float] = {}
    conflict = 0.0
    for b, mb in m1.items():
        for c, mc in m2.items():
            if mb <= 0 or mc <= 0:
                continue
            inter = b & c
            if not inter:
                conflict += mb * mc
            else:
                out[inter] = out.get(inter, 0.0) + mb * mc
    if conflict >= 1.0 - 1e-12:
        return None, 1.0
    return {k: v / (1.0 - conflict) for k, v in out.items()}, conflict


def _pignistic(m: dict[frozenset, float]) -> dict[str, float]:
    """把置信质量折成单点概率：Bet(A) = Σ_{B⊇A} m(B)/|B|。"""
    bet = {a: 0.0 for a in FRAME}
    for b, mb in m.items():
        if mb <= 0 or not b:
            continue
        for a in b:
            bet[a] += mb / len(b)
    return bet


def _wall_signal(mw):
    if mw is None:
        return None
    if mw < 0.6:
        return (_mk_mass({("F",): 0.85, ("F", "M"): 0.10}),
                f"壁厚 {mw:.2f}mm 极薄→细档 0.03mm")
    if mw < 1.2:
        return (_mk_mass({("F",): 0.35, ("M",): 0.30, ("F", "M"): 0.20}),
                f"壁厚 {mw:.2f}mm 偏薄→偏细")
    return None


def _size_signal(max_dim: float):
    if max_dim > 150:
        return (_mk_mass({("C",): 0.45, ("M", "C"): 0.20}),
                f"尺寸 {max_dim:.0f}mm 属大件→偏粗省时")
    return None


def _overhang_signal(ratio: float):
    if ratio > 0.15:
        return (_mk_mass({("F",): 0.40, ("M",): 0.20, ("F", "M"): 0.20}),
                f"悬垂 {ratio * 100:.0f}%→偏细改善接触面")
    return None


def _layer_height_sla(g: dict, mat: dict) -> tuple[float, str, list[str], float]:
    """层高：D-S 合成多路信号，返回 (层高, 理由, 信号列表, 冲突度)。"""
    lo, hi = mat["layer"]
    mid = _f(mat.get("default_layer"), 0.05)
    reps = {"F": float(lo), "M": mid, "C": float(hi)}

    fired = [s for s in (_wall_signal(g.get("min_wall")),
                         _size_signal(_f(g.get("max_dim"))),
                         _overhang_signal(_f(g.get("overhang_ratio")))) if s]
    signals = [t for _, t in fired]

    if not fired:
        return mid, f"尺寸与壁厚都不苛刻，{mid}mm 是细节与速度的常规平衡点。", signals, 0.0

    comb = _mk_mass({})
    conflict_total = 0.0
    for mass, _ in fired:
        merged, k = _ds_combine(comb, mass)
        if merged is None:
            # 完全冲突：退回最保守档，并把冲突记满
            return float(lo), "多路信号完全冲突，取最保守档，建议手动确认。", signals, 1.0
        comb = merged
        conflict_total = 1.0 - (1.0 - conflict_total) * (1.0 - k)

    bet = _pignistic(comb)
    val = sum(bet[a] * reps[a] for a in FRAME)

    parts = "；".join(signals)
    if len(fired) > 1:
        top = max(bet, key=bet.get)
        reason = (f"多路信号合成（{parts}）："
                  f"细档 {bet['F'] * 100:.0f}% / 中档 {bet['M'] * 100:.0f}% / 粗档 {bet['C'] * 100:.0f}%，"
                  f"加权取 {val:.3f}mm（倾向{ {'F': '细', 'M': '中', 'C': '粗'}[top] }档）。")
        if conflict_total > 0.05:
            reason += f" 信号间存在冲突（冲突度 {conflict_total:.2f}），已按保守侧加权。"
    else:
        reason = f"{parts}，取 {val:.3f}mm。"
    return val, reason, signals, conflict_total


def _layer_height_fdm(g: dict, nozzle: float) -> tuple[float, str, list[str], float]:
    """FDM 层高：薄壁要细，其余按喷嘴 1/2~1/3。"""
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
    return val, reason, signals, 0.0


# ---------------------------------------------------------------- 曝光
def exposure_jacobs(layer_h: float, dp: float, ec: float,
                    irradiance: float, adhesion: float = 2.2) -> tuple[float, dict]:
    """Jacobs 固化曲线反解单层曝光时间。

        Cd = Dp · ln(E / Ec)          E = I · t

    Cd 固化深度、Dp 穿透深度、Ec 临界曝光、I 屏幅照度、t 曝光时间。
    要求新层固化深度达到 adhesion 倍层高（保证熔进上一层），反解得：

        t = (Ec / I) · exp(adhesion · 层高 / Dp)

    返回 (秒, 推导明细)。
    """
    lh = max(_f(layer_h, 0.05), 1e-4)
    dp = max(_f(dp, 0.11), 1e-4)
    ec = max(_f(ec, 5.0), 1e-3)
    ir = max(_f(irradiance, 5.0), 0.1)
    adh = max(_f(adhesion, 2.2), ADHESION_MIN)
    target = adh * lh
    energy = ec * math.exp(target / dp)
    t = energy / ir
    detail = {
        "layer_h": round(lh, 4),
        "adhesion": round(adh, 2),
        "cure_target_mm": round(target, 4),
        "energy_mj_cm2": round(energy, 2),
        "dp_mm": round(dp, 4),
        "ec_mj_cm2": round(ec, 2),
        "irradiance_mw_cm2": round(ir, 2),
    }
    return t, detail


def exposure_interval(layer_h: float, dp: float, ec: float,
                      irradiance: float, adhesion: float = 2.2) -> tuple[float, float]:
    """Jacobs 式各参数误差传到曝光时间上的区间（解析法）。

    ln t = ln Ec − ln I + adhesion·层高/Dp

    三个参数独立，按误差传播：
        σ²(ln t) = σ²(ln Ec) + σ²(ln I) + (adhesion·层高/Dp)² · σ²(ln Dp)

    注意 Dp 在分母上且被 exp 放大，层高越粗，曝光对 Dp 越敏感——
    这也是厚层更容易过曝/欠曝的原因。
    """
    lh = max(_f(layer_h, 0.05), 1e-4)
    dp = max(_f(dp, 0.11), 1e-4)
    sens = adhesion * lh / dp                       # ln t 对 ln Dp 的敏感度
    var = SD_EC ** 2 + SD_IRRADIANCE ** 2 + (sens * SD_DP) ** 2
    sd = math.sqrt(max(var, 0.0))
    t, _ = exposure_jacobs(layer_h, dp, ec, irradiance, adhesion)
    return t * math.exp(-Z80 * sd), t * math.exp(Z80 * sd)


def _peel_factor(max_area_mm2: float, bed_area_mm2: float) -> tuple[float, str]:
    """大截面剥离力修正。

    离型时 FEP 膜对大面积截面的吸力近似正比于面积，
    固化不足的层会被拽离支撑。按截面占台面比例线性加曝光，封顶 15%。
    """
    if max_area_mm2 <= 0 or bed_area_mm2 <= 0:
        return 1.0, "截面数据缺失，未做剥离修正"
    ratio = _clamp(max_area_mm2 / PEEL_AREA_REF_MM2, 0.0, 1.0)
    f = 1.0 + PEEL_MAX_GAIN * ratio
    if ratio <= 0.01:
        return 1.0, f"最大截面 {max_area_mm2:.0f}mm² 不大，无需剥离修正"
    return f, (f"最大截面 {max_area_mm2:.0f}mm²（台面的 "
               f"{max_area_mm2 / max(bed_area_mm2, 1) * 100:.0f}%），剥离力修正 ×{f:.2f}")


# ---------------------------------------------------------------- 几何汇总
# 网格缓存：改方案/换材料/换机型时模型文件没变，没必要每次重读。
# 实测 1MB / 2 万面的 STL 单次加载 44ms，而一次建议里只读一次会被来回调用多次。
# 用 (路径, mtime_ns, 大小) 做键，文件被替换后自然失效。
_MESH_CACHE: dict[tuple, object] = {}
_MESH_CACHE_MAX = 3


def _load_mesh_cached(path: Path):
    """带缓存的网格加载。返回的网格只读，调用方不要原地修改。"""
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return meshio.load_mesh(path)
    hit = _MESH_CACHE.get(key)
    if hit is not None:
        return hit
    mesh = meshio.load_mesh(path)
    if len(_MESH_CACHE) >= _MESH_CACHE_MAX:
        # 简单 FIFO 淘汰：同时打开的模型通常就一两个，不需要真 LRU
        _MESH_CACHE.pop(next(iter(_MESH_CACHE)))
    _MESH_CACHE[key] = mesh
    return mesh


# 截面缓存：切 48 层截面约 88ms，是单次建议里最贵的一步。
# 换方案/换材料/换机型时模型文件没变，截面结果完全一样，没理由重切。
_SECTIONS_CACHE: dict[tuple, tuple] = {}
_SECTIONS_CACHE_MAX = 3
SECTIONS_SAMPLES = 48


def _file_key(path: Path) -> tuple | None:
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _sections_cached(path: Path, mesh, samples: int = SECTIONS_SAMPLES) -> tuple:
    """按文件身份缓存截面。文件一变（mtime/size）自动失效。"""
    fk = _file_key(path)
    key = (fk, samples) if fk else None
    if key is not None:
        hit = _SECTIONS_CACHE.get(key)
        if hit is not None:
            return hit
    sec = printmodel.cross_section_polygons(mesh, samples=samples)
    if key is not None:
        if len(_SECTIONS_CACHE) >= _SECTIONS_CACHE_MAX:
            _SECTIONS_CACHE.pop(next(iter(_SECTIONS_CACHE)))
        _SECTIONS_CACHE[key] = sec
    return sec


def _truthy(v, default: bool = True) -> bool:
    """把 JSON/表单里五花八门的「假」统一认成假。

    前端传的是布尔，但批量模板、导入的 JSON、脚本调用传的是字符串：
    bool("false") 是 True、bool("0") 也是 True——于是用户明确关掉了
    「按本机历史校正」，算出来的却仍是校正后的数，而且看不出被改过。
    """
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v) and v == v  # NaN → False
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on", "y", "t"):
        return True
    if s in ("0", "false", "no", "off", "n", "f", ""):
        return False
    return default


def _geometry(project: dict) -> dict:
    """汇总所有用于决策的几何量，并保证每项都有值。"""
    report = project.get("analysis") or {}
    # 报告过期（几何改过但没重新检测）时，stats/items 里的数字属于上一版文件。
    # 继续用它们会算出「20mm 立方体缩到 60mm 仍按 9.9g 估耗材」这种 8 倍偏差，
    # 所以这里直接丢掉旧报告，改从当前文件重新量。
    stale = bool(project.get("analysis_stale"))
    if stale:
        report = {}
    stats = report.get("stats") or {}
    items = {i.get("id"): i for i in (report.get("items") or [])}
    path = Path(project.get("current") or project.get("source") or "")

    overhang = 0.0
    support: dict = {}
    profile: dict = {}
    has_mesh = False
    mesh = None
    if path.exists():
        try:
            mesh = _load_mesh_cached(path)
            has_mesh = True
            if not stats:
                stats = meshio.mesh_stats(mesh)
            # 切一次截面并缓存：悬垂判别与轮廓积分共用同一份，跨请求也复用
            sections = _sections_cached(path, mesh)
            # 真实悬垂判别：向下投射射线，而不是只看面法线朝不朝下
            support = printmodel.support_analysis(mesh, sections=sections)
            overhang = _f(support.get("overhang_ratio"), 0.0)
            profile = printmodel.cross_section_profile(mesh, sections=sections)
        except Exception:
            pass

    components = ((items.get("components") or {}).get("value") or {}).get("components")
    if components is None and has_mesh:
        # 报告里没有（或已过期）时，部件数别直接当 1：
        # 「1 个连通体」是结论，不是缺省值。多部件会影响支撑与用料估算，
        # 也会漏掉「切片时各自生成支撑」这条提醒。
        try:
            n_comp, _labels = analysis.union_components(
                np.asarray(mesh.faces), len(mesh.vertices))
            components = int(n_comp)
        except Exception:  # noqa: BLE001
            components = None

    thick = (items.get("thickness") or {}).get("value") or {}
    min_wall, wall_note = _clean_wall(thick)
    if stale and has_mesh:
        # 报告过期就当场补一次粗筛测厚：不补的话「最薄处」这一项会缺失，
        # 层高与「细节打不出来」的判断就整条失效，而这恰恰是最常见的打印失败原因。
        try:
            quick = analysis.check_thickness(mesh, {"sample_faces": 600}, None)
            min_wall, wall_note = _clean_wall(quick.get("value") or {})
        except Exception:  # noqa: BLE001
            min_wall, wall_note = None, "检测报告已过期，补测壁厚失败"
    ext = stats.get("extents") or [0, 0, 0]
    return {
        "extents": ext,
        "stale": stale,
        "geometry_source": "recomputed" if stale else ("report" if report else "mesh"),
        "max_dim": float(max(ext)) if ext else 0.0,
        "height": float(ext[2]) if len(ext) > 2 else 0.0,
        "volume_mm3": float(stats.get("volume_mm3") or 0.0),
        "area_mm2": float(stats.get("area_mm2") or 0.0),
        "faces": int(stats.get("faces") or 0),
        "min_wall": min_wall,
        "median_wall": thick.get("median"),
        "wall_note": wall_note,
        "overhang_ratio": round(overhang, 4),
        "watertight": bool(stats.get("is_watertight")),
        "components": components if components is not None else 1,
        "components_known": components is not None,
        "has_mesh": has_mesh,
        "support": support,
        "profile": profile,
    }


def _effective_volume(g: dict, hollow: bool, wall: float) -> tuple[float, str]:
    """实际要固化的体积。

    镂空后真正消耗的不是整件体积，而是「表面积 × 壳厚」——
    直接拿实心体积算会把树脂用量高估好几倍。
    """
    solid = max(0.0, _f(g["volume_mm3"]))
    if not hollow:
        return solid, "实心体积"
    shell = max(0.0, _f(g["area_mm2"])) * max(0.0, wall)
    # 表面积 × 壳厚 是壳体积的上界；球/柱这类件实际约为 0.75~0.9 倍
    est = min(solid, shell * 0.85)
    return est, f"表面积 {g['area_mm2']:.0f}mm² × 壳厚 {wall}mm × 0.85"


# ---------------------------------------------------------------- 成本与时长
def _estimate_cost(g: dict, printer: dict, process: str, mat: dict,
                   layer_h: float, exposure: float | None, infill: float | None,
                   hollow: bool = False, wall: float = 2.0,
                   u: dict | None = None, support: dict | None = None,
                   profile: dict | None = None) -> dict:
    """耗材与时长估算。

    u 是蒙特卡洛采样的扰动倍率（不传就是取中位数的确定性估算）。

    FDM 时长按挤出路径长度算，不按体积除个常数：
        外墙路径长 = 圈数 × ∫截面周长dz ÷ 层高
        速度取「设定速度」和「最大体积流量 ÷ 线宽 ÷ 层高」的较小值
    再叠加每层固定开销与最小层时间——小截面层会被冷却限速，
    这正是旧版「体积 ÷ 6」在大件上还算凑合、在小精细件上离谱的原因。

    SLA 单层时间 = 曝光 + 熄灯延迟 + 抬升 + 回落 + 静置，
    底层另按底曝时间单独加。
    """
    u = u or {}
    dens_mult = _f(u.get("dens"), 1.0)
    flow_mult = _f(u.get("flow"), 1.0)
    geo_mult = _f(u.get("geo"), 1.0)
    oh_mult = _f(u.get("oh"), 1.0)

    eff_vol, vol_basis = _effective_volume(g, hollow, wall)
    eff_vol *= geo_mult
    vol_cm3 = max(0.0, eff_vol) / 1000.0
    height = max(0.0, _f(g["height"]))
    layers = int(height / max(layer_h, 0.01)) if height else 0
    density = _f(mat.get("density"), 1.10 if process == "SLA" else 1.24) * dens_mult

    sup = support or {}
    contact = _f(sup.get("support_contact_mm2"), 0.0) * geo_mult
    sup_vol = contact * SUPPORT_VOL_PER_MM2

    if process == "SLA":
        grams = vol_cm3 * density
        ir = _f(printer.get("irradiance"), 5.0)
        exp = _f(exposure, mat.get("exposure", 2.5))
        light_off = _f(printer.get("light_off_delay"), 0.5)
        settle = _f(printer.get("settle"), 0.5)
        lift_mm = _f(printer.get("lift_mm"), 6.0) * printer_lift_scale(printer, profile)
        up = _f(printer.get("lift_speed"), 2.5)
        down = _f(printer.get("retract_speed"), 3.5)
        motion = lift_mm / max(up, 0.1) + lift_mm / max(down, 0.1)
        per_layer = (exp + light_off + settle) * oh_mult + motion
        # 底层单独按底曝算，多出来的时间 = 层数 × (底曝 − 正常曝光)
        n_bottom = min(BOTTOM_LAYERS, layers)
        extra = n_bottom * exp * (BOTTOM_EXPOSURE_MULT - 1.0)
        minutes = (layers * per_layer + extra) / 60.0
        grams = (eff_vol + sup_vol) / 1000.0 * density
        basis = (f"{vol_basis} → {vol_cm3:.1f}cm³ × {_num(density)}g/cm³"
                 + (f"，另加支撑 {sup_vol / 1000:.1f}cm³" if sup_vol > 0 else "")
                 + f"；{layers} 层 × ({exp:.1f}s 曝光 + {light_off:.1f}s 熄灯 + "
                   f"{settle:.1f}s 静置 + {motion:.1f}s 抬升回落)"
                 + (f" + 底层 {n_bottom} 层额外 {extra / 60:.1f}min" if extra > 0 else ""))
        detail = {"per_layer_s": round(per_layer, 2), "motion_s": round(motion, 2),
                  "bottom_extra_s": round(extra, 1)}
    else:
        nozzle = _f(printer.get("nozzle"), 0.4)
        line_w = nozzle * LINE_WIDTH_FACTOR
        line_w_inf = line_w * INFILL_WIDTH_FACTOR
        inf = _clamp(_f(infill, 15.0), 0.0, 100.0)
        q_max = _f(printer.get("max_flow"), 12.0) * flow_mult
        speed = _f(printer.get("speed"), 60.0)
        overhead = _f(printer.get("layer_overhead_s"), 0.35) * oh_mult
        min_layer = _f(printer.get("min_layer_s"), 5.0) * oh_mult

        wall_lines = max(2, math.ceil(1.2 / nozzle)) + _f(g.get("wall_delta_lines"), 0.0)
        prof = profile or {}
        perim_int = _f(prof.get("perimeter_integral_mm2"), 0.0) * geo_mult
        areas = prof.get("areas") or []
        perims = prof.get("perimeters") or []
        n_s = max(1, len(areas))

        v_wall = wall_lines * line_w * perim_int
        # 顶底面各 SKIN_LAYERS 层实心填充，面积取截面序列首尾的真实值
        a_mean = (eff_vol / height) if height > 0 else 0.0
        a_bot = _f(areas[0], a_mean) if areas else a_mean
        a_top = _f(areas[-1], a_mean) if areas else a_mean
        v_skin = SKIN_LAYERS * layer_h * (a_top + a_bot)
        # 壳体和表皮加起来不可能超过整件体积，超了就按比例缩回去
        if v_wall + v_skin > eff_vol > 0:
            k = eff_vol / (v_wall + v_skin)
            v_wall *= k
            v_skin *= k
        v_inf = max(0.0, eff_vol - v_wall - v_skin) * (inf / 100.0)
        extruded = v_wall + v_skin + v_inf + sup_vol

        # 速度上限：体积流量 ÷ 线宽 ÷ 层高
        v_wall_speed = min(speed * 0.6, q_max / max(line_w * layer_h, 1e-6))
        v_inf_speed = min(speed, q_max / max(line_w_inf * layer_h, 1e-6))
        v_wall_speed = max(v_wall_speed, 1.0)
        v_inf_speed = max(v_inf_speed, 1.0)

        if perims and layers > 0:
            scale = layers / n_s
            per_layer_times = [
                (wall_lines * _f(p) / v_wall_speed
                 + _f(a) * (inf / 100.0) / line_w_inf / v_inf_speed)
                for a, p in zip(areas, perims)
            ]
            # 支撑也要挤出来，按填充线宽折算成路径长度
            t_sup = sup_vol / max(line_w_inf * layer_h * v_inf_speed, 1e-6)
            minutes = (scale * sum(max(t, min_layer) if _f(a) > 0 else 0.0
                                   for t, a in zip(per_layer_times, areas))
                       + t_sup + layers * overhead) / 60.0
        else:
            # 拿不到截面轮廓就退回体积法，并在依据里写明
            minutes = (extruded / max(6.0 * flow_mult, 0.5) + layers * overhead) / 60.0

        grams = extruded / 1000.0 * density
        basis = (f"{vol_basis} → {eff_vol / 1000:.1f}cm³；"
                 f"外墙 {v_wall / 1000:.1f} + 表皮 {v_skin / 1000:.1f} + "
                 f"填充 {v_inf / 1000:.1f} + 支撑 {sup_vol / 1000:.1f} cm³ "
                 f"× {_num(density)}g/cm³；"
                 f"速度上限 {v_wall_speed:.0f}/{v_inf_speed:.0f} mm/s，"
                 f"最小层时间 {min_layer:.0f}s")
        detail = {"wall_cm3": round(v_wall / 1000, 2), "skin_cm3": round(v_skin / 1000, 2),
                  "infill_cm3": round(v_inf / 1000, 2), "support_cm3": round(sup_vol / 1000, 2),
                  "v_wall_mm_s": round(v_wall_speed, 1), "v_infill_mm_s": round(v_inf_speed, 1)}

    return {
        "grams": round(max(0.0, grams), 1),
        "hours": round(max(0.0, minutes / 60.0), 2),
        "layers": layers,
        "effective_volume_cm3": round(vol_cm3, 2),
        "basis": basis,
        "detail": detail,
    }


def printer_lift_scale(printer: dict, profile: dict | None) -> float:
    """大截面抬升要更慢更远：FEP 剥离速度受面积限制，抬不够会拉裂。

    只在截面明显超过参考面积时加行程，最多加 60%。
    """
    if not profile or not profile.get("ok"):
        return 1.0
    max_area = _f(profile.get("max_area_mm2"), 0.0)
    if max_area <= 0:
        return 1.0
    return 1.0 + 0.6 * _clamp(max_area / (PEEL_AREA_REF_MM2 * 2.0), 0.0, 1.0)


# ---------------------------------------------------------------- 不确定性
def _mc_interval(fn, samples: int = MC_SAMPLES, seed: int = 7) -> dict:
    """蒙特卡洛：把输入扰动传下去，返回各量的 P10/P50/P90。

    采样分布用对数正态——耗材、流量这些只能正、且是乘性偏差，
    正态会给出负值，物理上不成立。
    """
    rng = np.random.default_rng(seed)
    gs = np.empty(samples, dtype=np.float64)
    hs = np.empty(samples, dtype=np.float64)
    for i in range(samples):
        u = {
            "dens": float(rng.normal(1.0, SD_DENSITY)),
            "flow": float(rng.lognormal(0.0, SD_FLOW)),
            "geo": float(rng.lognormal(0.0, SD_GEOMETRY)),
            "oh": float(rng.lognormal(0.0, SD_OVERHEAD)),
        }
        r = fn(u)
        gs[i] = r["grams"]
        hs[i] = r["hours"]
    return {
        "grams": {"p10": round(float(np.percentile(gs, 10)), 1),
                  "p50": round(float(np.percentile(gs, 50)), 1),
                  "p90": round(float(np.percentile(gs, 90)), 1)},
        "hours": {"p10": round(float(np.percentile(hs, 10)), 2),
                  "p50": round(float(np.percentile(hs, 50)), 2),
                  "p90": round(float(np.percentile(hs, 90)), 2)},
        "samples": samples,
    }


def _sources(g: dict, printer: dict, process: str, mat: dict, cal: dict) -> list[dict]:
    """参数信息来源汇总：数据从哪来、按什么算、参考了哪些基准值。"""
    bx, by, bz = printer["build"]
    vol_cm3 = _f(g.get("volume_mm3")) / 1000.0
    area_cm2 = _f(g.get("area_mm2")) / 100.0
    mw = g.get("min_wall")
    sup = g.get("support") or {}
    prof = g.get("profile") or {}

    geo = [
        f"体积 {vol_cm3:.2f} cm³ —— 检测报告 stats.volume_mm3",
        f"表面积 {area_cm2:.1f} cm² —— 检测报告 stats.area_mm2",
        f"最薄处 {mw:.2f} mm —— 壁厚检测 items.thickness" if mw is not None
        else ("测厚数据已剔除 —— " + g["wall_note"]) if g.get("wall_note")
        else "最薄处未测 —— 缺少壁厚检测数据",
    ]
    if sup.get("ok"):
        geo.append(
            f"悬垂占比 {_f(sup.get('overhang_ratio')) * 100:.1f}% —— "
            f"向下射线求交，{sup.get('samples')} 点采样，"
            f"最大跨距 {sup.get('max_span_mm')}mm"
        )
        geo.append(
            f"可架桥区域 {_f(sup.get('bridgeable_ratio')) * 100:.1f}% —— "
            f"朝下但水平跨距在 {printmodel.BRIDGE_MM_SLA:.0f}mm 内，无需支撑"
        )
    else:
        geo.append("悬垂占比 —— 未加载到模型文件，未能做射线求交")

    if prof.get("ok"):
        geo.append(
            f"截面周长积分 {prof.get('perimeter_integral_mm2'):.0f} mm² —— "
            f"{prof.get('samples')} 层等距截面梯形积分"
            + (f"（体积自检修正 ×{prof.get('correction')}）" if abs(prof.get("correction", 1.0) - 1.0) > 0.02 else "")
        )
        geo.append(f"最大截面 {prof.get('max_area_mm2'):.0f} mm² —— 决定剥离力与抬升行程")

    geo.extend([
        f"连通部件 {g.get('components', 1)} —— 连通分量分析",
        f"最大尺寸 {_f(g.get('max_dim')):.1f} mm / 高度 {_f(g.get('height')):.1f} mm —— 包围盒",
    ])

    if process == "SLA":
        lo, hi = mat["layer"]
        base = [
            f"{printer['name']} 成型空间 {bx}×{by}×{bz} mm",
            f"{printer['name']} 屏幅照度 {_f(printer.get('irradiance'), 5.0)} mW/cm²",
            f"{printer['name']} 抬升 {_f(printer.get('lift_mm'), 6.0)}mm"
            f"↑{_f(printer.get('lift_speed'), 2.5)} ↓{_f(printer.get('retract_speed'), 3.5)} mm/s",
            f"{mat['name']} 层高区间 {lo}–{hi} mm",
            f"{mat['name']} Dp={mat.get('dp')} mm，Ec={mat.get('ec')} mJ/cm²（Jacobs 曲线参数）",
            f"{mat['name']} 密度 {mat.get('density')} g/cm³，收缩率约 {mat.get('shrink')}%",
        ]
    else:
        base = [
            f"{printer['name']} 成型空间 {bx}×{by}×{bz} mm，喷嘴 {printer.get('nozzle', 0.4)} mm",
            f"{printer['name']} 最大体积流量 {_f(printer.get('max_flow'), 12.0)} mm³/s，"
            f"加速度 {_f(printer.get('accel'), 800.0)} mm/s²",
            f"{mat['name']} 喷头 {mat['nozzle']}℃ / 热床 {mat['bed']}℃",
            f"{mat['name']} 密度 {mat.get('density')} g/cm³，"
            f"拉伸强度 {mat.get('tensile')} MPa（层间折减 ×{mat.get('z_factor')}）",
        ]

    if cal and cal.get("source") == "group":
        base.append(
            f"本机历史修正：{process}/{printer.get('name')}/{mat.get('name')} "
            f"已攒 {cal.get('n')} 条记录，耗材与时长按实测偏差校正"
        )
    elif cal and cal.get("source") == "sparse":
        # 1~2 条记录时权重只有 1/9~2/10，改动力度极小。
        # 说成「已按实测偏差校正」会让用户高估这份建议的本地化程度。
        base.append(
            f"本机仅 {cal.get('n')} 条同机型同材料记录，样本不足，"
            f"只做极小幅度校正（×{cal.get('factor')}，权重 {cal.get('n')}/"
            f"{int(cal.get('n') or 0) + 8}）"
        )
    else:
        base.append("本机暂无打印记录，未做历史修正（纯物理估算）")

    calc = [
        "曝光：Cd = Dp·ln(E/Ec)，E = I·t，要求固化深度 ≥ 2.2×层高，反解 t",
        "FDM 时长：外墙路径长 = 圈数 × ∫截面周长dz ÷ 层高，速度受最大体积流量限制",
        "FDM 时长另计每层固定开销与最小层时间（小截面冷却限速）",
        "SLA 时长 = 曝光 + 熄灯延迟 + 抬升 + 回落 + 静置，底层单独加底曝",
        "支撑判别：朝下面向下投射射线，先看正下方落点，再看水平跨距能否架桥",
        "壳体积 ≈ 表面积 × 壳厚 × 0.85（球/柱件实际约为上界的 0.75~0.9）",
        "区间：蒙特卡洛 400 次采样（密度/流量/几何采样/层开销的对数正态扰动）",
    ]
    return [
        {"title": "几何量（来自检测报告与模型文件）", "items": geo},
        {"title": "机型与材料基准（内置规格表）", "items": base},
        {"title": "计算依据", "items": calc},
    ]


def _confidence(g: dict, conflict: float, model_gap: float, cal: dict) -> dict:
    """整份建议的置信度。

    不是给个感觉分，是把「哪里不确定」一项项扣出来列给用户。
    用户看到「缺少壁厚数据」，就知道该去跑一次检测，
    而不是对着一个数字猜它准不准。
    """
    score = 1.0
    reasons: list[str] = []

    if not g.get("has_mesh"):
        score -= 0.20
        reasons.append("未加载到模型文件，几何量取自缓存报告（−0.20）")
    if g.get("stale"):
        score -= 0.10
        reasons.append("检测报告已过期，几何量已按当前文件重测（−0.10）")
    if not g.get("watertight"):
        score -= 0.15
        reasons.append("模型不水密，体积与壁厚数据不可靠（−0.15）")
    if g.get("min_wall") is None:
        if g.get("wall_note"):
            # 测到了但判为伪影，比「压根没测」更该谨慎：我们已知这次测量是失败
            score -= 0.22
            reasons.append("壁厚测量被判为伪影并剔除，层高只能按尺寸估（−0.22）")
        else:
            score -= 0.15
            reasons.append("缺少壁厚检测数据，层高只能按尺寸估（−0.15）")
    comps = int(g.get("components") or 1)
    if comps > 1:
        pen = min(0.15, 0.05 * comps)
        score -= pen
        reasons.append(f"含 {comps} 个不连通部件，支撑与用料估算偏粗（−{pen:.2f}）")
    if not (g.get("profile") or {}).get("ok"):
        score -= 0.10
        reasons.append("截面轮廓采样失败，FDM 时长退回体积法（−0.10）")
    if not (g.get("support") or {}).get("ok"):
        # 支撑是「能不能打出来」的前置判断，没判定就不能算一份可信的建议
        score -= 0.12
        why = (g.get("support") or {}).get("reason") or "未加载到模型文件"
        reasons.append(f"悬垂/支撑未判定（{why}），支撑结论为空（−0.12）")
    if g.get("degenerate"):
        score -= 0.35
        reasons.append("模型退化（无实体/无厚度），参数不具备可打印性（−0.35）")
    elif g.get("feasible") is False:
        score -= 0.30
        reasons.append("模型超出成型空间，参数仅作分件参考（−0.30）")
    if conflict > 0:
        pen = 0.30 * _clamp(conflict, 0.0, 1.0)
        score -= pen
        reasons.append(f"多路信号存在冲突（冲突度 {conflict:.2f}）（−{pen:.2f}）")
    if model_gap > 1.0:
        pen = min(0.20, 0.12 * (model_gap - 1.0))
        score -= pen
        reasons.append(
            f"物理模型与体积法估算相差 {model_gap:.1f} 倍，取物理模型（−{pen:.2f}）")
    if cal and cal.get("source") == "group":
        score += 0.10
        reasons.append(f"已有 {cal.get('n')} 条本机记录，按实测偏差校正（+0.10）")
    if cal and cal.get("success_rate") is not None and cal["success_rate"] < 0.7:
        score -= 0.10
        reasons.append(
            f"本机成功率 {cal['success_rate'] * 100:.0f}%，参数偏激进（−0.10）")

    score = _clamp(score, 0.05, 1.0)
    grade = "高" if score >= 0.75 else ("中" if score >= 0.5 else "低")
    if not reasons:
        reasons.append("几何数据完整、信号无冲突，按物理模型直接给出")
    return {
        "score": round(score, 2),
        "grade": grade,
        "reasons": reasons,
        "conflict": round(_clamp(conflict, 0.0, 1.0), 2),
        "model_gap": round(model_gap, 2),
    }


# ---------------------------------------------------------------- 主入口
# 壁厚读数的可信性判据。
# 沿法线打射线量厚度，遇到破洞/开放边界时射线会直接穿出去，
# 返回接近 0 的伪值。判据不能只看最小值（离群点太多），
# 要看「连最厚的地方都薄得离谱」——那才说明整组数据废了。
WALL_MAX_PLAUSIBLE_FLOOR = 0.5   # mm，最大壁厚低于此值 → 整组读数不可信
WALL_MIN_TRUSTED = 0.05          # mm，最小值低于此值 → 疑为局部伪影，退回 5% 分位数


def _clean_wall(thick: dict) -> tuple[float | None, str | None]:
    """从测厚结果里取一个可用的最薄处，取不到就返回 None 并说明原因。

    宁可承认「没测到」，也不要拿一个 0.00mm 的伪值去推层高——
    那会把整条建议链（层高→曝光→时长）一起带偏。
    """
    if not thick:
        return None, None
    mn = thick.get("min")
    if mn is None:
        return None, None
    mx = _f(thick.get("max"), 0.0)
    p5 = thick.get("p5")
    med = thick.get("median")
    n = int(_f(thick.get("samples"), 0.0))

    # 最厚处都低于可打印下限 → 射线整体穿透，读数没有意义
    if mx and mx < WALL_MAX_PLAUSIBLE_FLOOR:
        return None, (f"测厚结果最大仅 {mx:.3f}mm（{n} 个有效采样），"
                      f"判定为射线穿透伪影，已忽略该数据")

    mnf = _f(mn)
    if mnf < WALL_MIN_TRUSTED:
        # 这里要区分两种「极小值」：
        #   a) 孤立尖峰——最小值远小于 5% 分位数，是边缘/破洞处的伪影 → 用分位数剔除
        #   b) 整片都薄——最小值与分位数接近，说明模型真的这么薄 → 保留，让下游去报「打不出来」
        # 早期版本把两种情况都当伪影丢掉，结果真的薄壁模型反而得不到警告。
        cand, label = (p5, "5% 分位数") if p5 is not None else (med, "中位数")
        cf = _f(cand, 0.0)
        if cf >= WALL_MIN_TRUSTED and cf > mnf * 3:
            return cf, (f"最小值 {mnf:.3f}mm 疑为孤立伪影（{label} 为 {cf:.2f}mm），"
                        f"已改用{label}")
        return mnf, (f"最薄处 {mnf:.3f}mm 已低于常规可打印特征 {WALL_MIN_TRUSTED}mm，"
                     f"按真实薄壁保留——请确认是否需要加厚")
    return mnf, None


def _resolve_nozzle(printer: dict, want: float) -> str | None:
    """把用户请求的喷嘴落到机型支持的规格上，返回提示文案（无异常则 None）。

    换喷嘴会连锁改变层高上限、线宽、壁线数与流量上限，所以必须在
    算层高之前完成，而不是只在输出里改个数字。
    """
    opts = [float(v) for v in (printer.get("nozzle_options") or [])]
    note = None
    if want > 0:
        if opts and not any(abs(want - o) < 1e-6 for o in opts):
            nearest = min(opts, key=lambda o: abs(o - want))
            note = (f"该机型不支持 {want:g}mm 喷嘴，已改用最接近的 {nearest:g}mm"
                    f"（可选：{'/'.join(f'{o:g}' for o in opts)}mm）")
            want = nearest
        printer["nozzle"] = want
        # 层高区间由喷嘴推导，换喷嘴必须重算，否则会给出超限层高
        printer["layer_range"] = [round(max(0.05, want * 0.2), 2), round(want * 0.75, 2)]
    elif opts and _f(printer.get("nozzle"), 0.0) not in opts:
        printer["nozzle"] = opts[0]
    return note


def _fits_build(extents, build) -> tuple[bool, str]:
    """模型能否放进成型空间（允许任意 90° 旋转摆放）。

    排序后逐轴比较即可覆盖轴交换的情况——比只比最大边严格，
    因为 300×100 的件放不进 200×200 的平台，但最大边只差 100。
    """
    e = sorted([_f(x) for x in (extents or [0, 0, 0])], reverse=True)
    b = sorted([_f(x) for x in (build or [0, 0, 0])], reverse=True)
    for i, (ei, bi) in enumerate(zip(e, b)):
        if ei > bi + 1e-6:
            return False, (f"第 {i + 1} 大边 {ei:.1f}mm 超过成型空间对应尺寸 {bi:.1f}mm")
    return True, ""


def _layer_bounds(mat: dict, printer: dict, warnings: list[str]) -> tuple[float, float]:
    """层高必须同时落在「材料能固化」和「机型能走」两个区间里。

    只按材料区间给会出现「材料说 0.02mm、机型最低只支持 0.03mm」这种建议，
    用户照着设会直接被切片器拒绝。
    """
    mlo, mhi = mat["layer"]
    plo, phi = printer.get("layer_range") or (mlo, mhi)
    lo, hi = max(float(mlo), float(plo)), min(float(mhi), float(phi))
    if lo > hi:
        warnings.append(
            f"{mat['name']} 的层高区间 {mlo}–{mhi}mm 与 {printer['name']} 支持的 "
            f"{plo}–{phi}mm 没有交集，已按材料区间给值，请确认工艺匹配。")
        return float(mlo), float(mhi)
    return lo, hi


def recommend(project: dict, overrides: dict | None = None) -> dict:
    ov = overrides or {}
    g = _geometry(project)
    # 机型走预设库：未知 key 自动退回该工艺的默认机型，并把缺失字段记下来
    process_hint = (ov.get("process") or "").upper() or None
    printer, printer_missing = printers.resolve(ov.get("printer"), process_hint)
    printer_key = printer["key"]
    printer["name"] = f"{printer['brand']} {printer['model']}"
    # 选了具体机型就以机型的工艺为准，避免「FDM 机型 + 光固化参数」这种自相矛盾的组合
    process = printer["process"] if printer.get("resolved_from") == "preset" else (
        process_hint or printer["process"])
    # 机型 key 没命中预设时会退回该工艺的默认机型。参数照样算得出来，
    # 但那台机器不是用户选的那台——成型空间、照度、喷嘴全都不一样。
    # 不说明的话，用户会以为屏幕上的参数是给自己机器的。
    printer_fallback = None
    if printer.get("resolved_from") != "preset" and (ov.get("printer") or "").strip():
        printer_fallback = str(ov.get("printer")).strip()
    nozzle_warn = (_resolve_nozzle(printer, _f(ov.get("nozzle"), 0.0))
                   if process == "FDM" else None)

    material_key = ov.get("material") or ("standard" if process == "SLA" else "PLA")
    plan_key = ov.get("plan") if ov.get("plan") in PLANS else DEFAULT_PLAN
    plan = PLANS[plan_key]
    g["wall_delta_lines"] = plan["wall_delta_lines"]

    params: list[dict] = []
    warnings: list[str] = []
    rationale: list[str] = []
    bx, by, bz = printer["build"]

    if printer_fallback:
        warnings.append(
            f"没有机型预设「{printer_fallback}」，已按 {printer['name']} 的参数计算"
            f"（成型空间 {bx:g}×{by:g}×{bz:g}mm）。"
            f"换成你自己的机型后，成型空间、照度/喷嘴、时长都会变——请先在机型里重选。"
        )
        rationale.append(f"机型「{printer_fallback}」不存在 → 退回默认机型 {printer['name']}")

    # 材料 key 与工艺不匹配时（例如切了工艺但材料还停在另一种），
    # 会静默落到该工艺的默认材料。数字是自洽的，但用户以为自己在用另一种料。
    material_fallback = None
    table = SLA_MATERIALS if process == "SLA" else FDM_MATERIALS
    if material_key not in table:
        material_fallback = material_key
        material_key = "standard" if process == "SLA" else "PLA"
        warnings.append(
            f"材料「{material_fallback}」不属于 {process} 工艺，已按 {table[material_key]['name']} 计算。"
            f"可选：{'、'.join(sorted(table))}。"
        )
        rationale.append(f"材料「{material_fallback}」不在 {process} 材料表内 → 用 {material_key}")

    # ---- 工艺与材料
    if process == "SLA":
        mat = SLA_MATERIALS.get(material_key, SLA_MATERIALS["standard"])
        params.append({
            "key": "material", "label": "材料", "value": mat["name"], "unit": "",
            "reason": f"{printer['name']} 机型 + 模型最大尺寸 {_f(g['max_dim']):.0f}mm。{mat['note']}。",
        })
        # 层高：D-S 合成壁厚/尺寸/悬垂三路信号
        lh, lh_reason, lh_signals, conflict = _layer_height_sla(g, mat)
        lo_lh, hi_lh = _layer_bounds(mat, printer, warnings)
        lh = _round_in_range(lh * plan["layer_mult"], 0.005, lo_lh, hi_lh)
        if plan["layer_mult"] != 1.0:
            lh_reason += f" 方案「{plan['label']}」再 ×{plan['layer_mult']} → {lh}mm。"
        params.append({
            "key": "layer_height", "label": "层高", "value": lh, "unit": "mm", "reason": lh_reason,
        })
        if lh_signals:
            rationale.append("层高交叉查证：" + "；".join(lh_signals) + f" → 合成后 {lh}mm")

        # 曝光：Jacobs 固化曲线
        ir = _f(printer.get("irradiance"), 5.0)
        prof = g.get("profile") or {}
        peel, peel_note = _peel_factor(_f(prof.get("max_area_mm2")), float(bx * by))
        phys_t, detail = exposure_jacobs(lh, mat["dp"], mat["ec"], ir, plan["adhesion"])
        # 厂家在这个机型上实测过的曝光，比任何公式都可信 —— 有就拿来当锚点，
        # 只按层高做物理外推（改变固化深度所需剂量的比例），不重算绝对剂量。
        anchor = (printer.get("official_exposure") or {}).get(material_key)
        if anchor is not None and _f(anchor, 0) > 0:
            exp_t = _f(anchor) * math.exp(
                plan["adhesion"] * (lh - printers.REFERENCE_LAYER) / max(mat["dp"], 1e-4))
            if abs(lh - printers.REFERENCE_LAYER) < 1e-6:
                basis = (f"厂家在 {printers.REFERENCE_LAYER}mm 层高下标定为 "
                         f"{_f(anchor):g}s，与当前层高一致，直接采用")
            else:
                basis = (f"以厂家标定 {_f(anchor):g}s @{printers.REFERENCE_LAYER}mm 为锚点，"
                         f"按 Dp 外推到 {lh}mm")
            cross = f"（纯物理模型会给出 {phys_t:.2f}s，两者差 {abs(phys_t - exp_t) / max(exp_t, 1e-6) * 100:.0f}%）"
        else:
            exp_t = phys_t
            basis = (f"{mat['name']} Dp={mat['dp']}mm、Ec={mat['ec']}mJ/cm²；"
                     f"{printer['name']} 照度 {ir:g}mW/cm²。"
                     f"层高 {lh}mm 要求固化深度 ≥{plan['adhesion']}×层高 = "
                     f"{detail['cure_target_mm']:.3f}mm，反解需剂量 "
                     f"{detail['energy_mj_cm2']:.1f}mJ/cm²")
            cross = ""
        exp = exp_t * peel * plan["exposure_mult"]
        exp_reason = (
            f"{basis}{cross}。{peel_note}，方案「{plan['label']}」曝光 ×{plan['exposure_mult']}，"
            f"取整 {round(exp, 1)}s。"
        )
        params.append({
            "key": "exposure", "label": "单层曝光", "value": round(exp, 1), "unit": "s",
            "reason": exp_reason,
        })
        rationale.append(
            f"曝光来源：{'厂家标定锚点' if anchor else '物理模型'} → {exp_t:.2f}s"
            f"×{peel:.2f}×{plan['exposure_mult']} = {round(exp, 1)}s")

        # 壁厚 / 镂空
        if _f(g["volume_mm3"]) > 50_000:
            params.append({
                "key": "hollow", "label": "镂空", "value": True, "unit": "",
                "reason": (f"体积 {_f(g['volume_mm3']) / 1000:.1f}cm³，实心打印耗树脂多、"
                           f"收缩应力大。建议壳厚 2.0mm 并开 2 个 φ3mm 排液孔。"),
            })
            rationale.append(
                f"体积 {_f(g['volume_mm3']) / 1000:.1f}cm³ > 50cm³ → 建议镂空省料并降收缩应力")
        else:
            params.append({
                "key": "hollow", "label": "镂空", "value": False, "unit": "",
                "reason": (f"体积仅 {_f(g['volume_mm3']) / 1000:.1f}cm³，镂空省不下多少树脂，"
                           f"反而增加排液和清洗负担。"),
            })
            rationale.append(
                f"体积仅 {_f(g['volume_mm3']) / 1000:.1f}cm³ ≤ 50cm³ → 不镂空更省事")
    else:
        mat = FDM_MATERIALS.get(material_key, FDM_MATERIALS["PLA"])
        nozzle = _f(printer.get("nozzle"), 0.4)
        params.append({
            "key": "material", "label": "材料", "value": mat["name"], "unit": "",
            "reason": f"{mat['note']}。模型最大尺寸 {_f(g['max_dim']):.0f}mm，{printer['name']} 可容纳。",
        })
        lh, lh_reason, lh_signals, conflict = _layer_height_fdm(g, nozzle)
        # 层高区间优先用机型预设（由喷嘴推导），工艺分支不再各自硬编码边界
        lo_lh, hi_lh = printer.get("layer_range") or (nozzle * 0.2, nozzle * 0.75)
        lh = _round_in_range(lh * plan["layer_mult"], 0.01, float(lo_lh), float(hi_lh))
        if plan["layer_mult"] != 1.0:
            lh_reason += f" 方案「{plan['label']}」再 ×{plan['layer_mult']} → {lh}mm。"
        params.append({
            "key": "layer_height", "label": "层高", "value": lh, "unit": "mm", "reason": lh_reason,
        })
        if lh_signals:
            rationale.append("层高交叉查证：" + "；".join(lh_signals) + f" → 合成后 {lh}mm")
        exp = None
        wall_lines = max(2, math.ceil(1.2 / nozzle)) + plan["wall_delta_lines"]
        wall_reason = (f"{wall_lines} 圈外墙 × {nozzle}mm 线宽"
                       f"（实际挤出宽 {nozzle * LINE_WIDTH_FACTOR:.2f}mm）。"
                       f"低于 2 圈时强度和密封性都不够。")
        if plan["wall_delta_lines"]:
            wall_reason += f" 方案「{plan['label']}」多 {plan['wall_delta_lines']} 圈。"
        params.append({
            "key": "wall_thickness", "label": "壁厚", "value": round(wall_lines * nozzle, 2),
            "unit": "mm", "reason": wall_reason,
        })
        infill = (15 if _f(g["volume_mm3"]) < 100_000 else 20) + plan["infill_delta"]
        infill = max(5, min(80, infill))
        infill_reason = (
            f"体积 {_f(g['volume_mm3']) / 1000:.1f}cm³。"
            + ("装饰件 15% 足够，强度靠外墙。" if _f(g["volume_mm3"]) < 100_000
               else "体积偏大，20% 陀螺填充在强度和耗材间更均衡。")
        )
        if plan["infill_delta"]:
            infill_reason += f" 方案「{plan['label']}」{plan['infill_delta']:+d}%。"
        params.append({
            "key": "infill", "label": "填充率", "value": infill, "unit": "%",
            "reason": infill_reason,
        })
        params.append({
            "key": "nozzle", "label": "喷嘴直径", "value": nozzle, "unit": "mm",
            "reason": (
                f"{printer['name']} 装机喷嘴 {printer.get('nozzle')}mm"
                + (f"；{nozzle_warn}" if nozzle_warn else "")
                + f"。喷嘴决定线宽（{nozzle * LINE_WIDTH_FACTOR:.2f}mm）与最大体积流量，"
                  f"也决定层高上限（≤{printer['layer_range'][1]}mm）。"
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

    # ---- 支撑：用射线求交的真实悬垂判定
    sup = g.get("support") or {}
    oratio = _f(g.get("overhang_ratio"), 0.0)
    bridge_mm = printmodel.BRIDGE_MM_SLA if process == "SLA" else printmodel.BRIDGE_MM_FDM
    contact = _f(sup.get("support_contact_mm2"), 0.0)
    max_span = _f(sup.get("max_span_mm"), 0.0)
    unsupported_area = _f(sup.get("support_contact_mm2"), 0.0)

    # 剥离风险：光固化每层的离型吸力近似正比于截面面积，
    # 截面大而平台接触面小时，件会被从平台上拽下来——
    # 这才是球、手办这类件要打支撑的真正原因，不是「悬垂角」。
    peel_risk = 0.0
    prof = g.get("profile") or {}
    if process == "SLA" and prof.get("ok"):
        areas_p = [a for a in (prof.get("areas") or []) if a and a > 0]
        if areas_p:
            contact_area = float(areas_p[0])
            peel_risk = float(prof.get("max_area_mm2") or 0.0) / max(contact_area, 1.0)

    if (sup.get("ok") and oratio > plan["support_bias"]
            and contact >= printmodel.MIN_SUPPORT_AREA_MM2):
        params.append({
            "key": "supports", "label": "支撑", "value": "需要", "unit": "",
            "reason": (
                f"射线求交实测：朝下面中 {oratio * 100:.1f}% 的下方无材料承接，"
                f"需支撑面积约 {contact:.0f}mm²，最大悬空跨距 {max_span:.1f}mm"
                f"（超过 {bridge_mm:.0f}mm 的自架桥能力）。"
                f"这些面没有下层承托，不打支撑会塌陷或拉丝。"
            ),
        })
        rationale.append(
            f"悬垂 {oratio * 100:.1f}% > {plan['support_bias'] * 100:.0f}% 阈值，"
            f"接触面 {contact:.0f}mm² ≥ {printmodel.MIN_SUPPORT_AREA_MM2:.0f}mm² → 需要支撑")
    elif sup.get("ok") and contact >= printmodel.MIN_SUPPORT_AREA_MM2:
        params.append({
            "key": "supports", "label": "支撑", "value": "可选", "unit": "",
            "reason": (
                f"朝下面占比 {_f(sup.get('downward_ratio')) * 100:.1f}%，"
                f"其中 {_f(sup.get('bridgeable_ratio')) * 100:.1f}% 的悬空跨距在 "
                f"{bridge_mm:.0f}mm 以内可自行架桥；"
                f"仅 {oratio * 100:.1f}% 需考虑支撑（约 {contact:.0f}mm²）。"
            ),
        })
        rationale.append(
            f"悬垂 {oratio * 100:.1f}% 未过阈值，"
            f"剩余 {contact:.0f}mm² 可按需补支撑 → 支撑可选")
    elif not sup.get("ok"):
        # 「没测出来」不能写成「自支撑性良好」。支撑结论依赖向下射线与截面遮挡判定，
        # 模型没加载到、截面切不出来、采样失败时这些都没跑，
        # 这时给出「不需要支撑」等于让用户拿一个未经验证的结论去打印。
        why = sup.get("reason") or ("未加载到模型文件" if not g.get("has_mesh") else "悬垂判定未完成")
        params.append({
            "key": "supports", "label": "支撑", "value": "未评估", "unit": "",
            "reason": (
                f"{why}，无法判定是否需要支撑。"
                f"请先确认模型文件可读并重新检测，或直接在切片软件里看悬垂预览。"
            ),
        })
        warnings.append(f"支撑结论未评估：{why}。不要据此认为模型不需要支撑。")
        rationale.append(f"悬垂判定未完成（{why}）→ 支撑标为未评估，不给结论")
    else:
        params.append({
            "key": "supports", "label": "支撑", "value": "可选", "unit": "",
            "reason": f"悬垂面仅占 {oratio * 100:.1f}%，模型自支撑性良好。",
        })
        rationale.append(f"悬垂 {oratio * 100:.1f}% 较低 → 支撑可选")

    if peel_risk >= PEEL_RISK_WARN:
        warnings.append(
            f"最大截面 {_f(prof.get('max_area_mm2')):.0f}mm² 约为平台接触面积的 "
            f"{peel_risk:.1f} 倍，离型吸力大，建议加支撑或用底 raft 增大附着。")
        rationale.append(
            f"剥离风险 {peel_risk:.1f} ≥ {PEEL_RISK_WARN} → 建议加支撑")

    # ---- 可装性：放不下的件不该给出可打印的参数，只能给出分件建议
    fits, fit_reason = _fits_build(g.get("extents"), [bx, by, bz])
    feasible = bool(fits)
    if not fits:
        warnings.append(
            f"模型放不进 {printer['name']} 的成型空间 {bx:g}×{by:g}×{bz:g}mm"
            f"（{fit_reason}）。下面的参数只在分件后、或换更大机型时才成立——"
            f"直接用会切出空刀路。"
        )
        rationale.append(f"超出成型空间（{fit_reason}）→ 必须先分件或换机型，参数仅供参考")
    if _f(g["height"]) > bz * 0.9:
        warnings.append(
            f"模型高度 {_f(g['height']):.0f}mm 接近成型高度上限 {bz}mm，支撑和取件都会很勉强。")
        rationale.append(f"高度 {_f(g['height']):.0f}mm 接近上限 {bz}mm → 支撑与取件勉强")

    # ---- 质检前置条件
    if g.get("stale"):
        warnings.append(
            "模型在上次检测之后被修改过（缩放/旋转/打孔/处理等），检测报告已过期。"
            "这里的几何量是按当前文件重新测量的，但水密性、自相交等结论仍是旧的——"
            "建议先重新检测再照着打。"
        )
        rationale.append("检测报告已过期 → 几何量按当前文件重测，结论项不采信旧报告")
    if not g["watertight"]:
        warnings.append("模型当前不水密。切片软件可能拒绝切片或生成错误截面，请先执行补洞。")
        rationale.append("模型不水密 → 建议先补洞再切片")
    if nozzle_warn:
        warnings.append(nozzle_warn)
    # 细节小到打不出来：光固化看像素，FDM 看喷嘴。这是「打出来糊了」最常见的原因
    if g.get("min_wall"):
        if process == "SLA" and _f(printer.get("xy_res"), 0) > 0:
            px = _f(printer["xy_res"])
            if _f(g["min_wall"]) < px * 2:
                warnings.append(
                    f"最薄处 {_f(g['min_wall']):.3f}mm 不足 2 个像素"
                    f"（{printer['name']} 像素 {px * 1000:.1f}μm），细节会被糊掉，建议加厚到 "
                    f"{px * 2:.2f}mm 以上。")
                rationale.append(f"最薄处 {_f(g['min_wall']):.3f}mm < 2×像素 {px * 2:.2f}mm → 细节会丢失")
        elif process == "FDM":
            nz = _f(printer.get("nozzle"), 0.4)
            if _f(g["min_wall"]) < nz:
                warnings.append(
                    f"最薄处 {_f(g['min_wall']):.2f}mm 小于喷嘴 {nz}mm，"
                    f"这一处大概率打不出来或只能打成一两条丝，建议加厚到 {nz * 1.5:.2f}mm 以上。")
                rationale.append(f"最薄处 {_f(g['min_wall']):.2f}mm < 喷嘴 {nz}mm → 打不出")
    if g.get("wall_note"):
        warnings.append(
            g["wall_note"] + "。层高改按尺寸与悬垂信号估算；建议提高壁厚采样面数后重跑检测。")
        rationale.append("壁厚数据被判定不可信 → 已剔除，层高不参考该项")
    if g["min_wall"] is not None and _f(g["min_wall"], 0.0) < 0.4:
        warnings.append(
            f"最薄处 {_f(g['min_wall']):.2f}mm 低于 0.4mm，无论什么工艺都打不出来，需先加厚。")
        rationale.append(f"最薄处 {_f(g['min_wall']):.2f}mm < 0.4mm → 无论工艺都打不出，需先加厚")
    if g["components"] and g["components"] > 1:
        warnings.append(
            f"模型含 {g['components']} 个互不连通的部件，切片时会各自生成支撑，注意检查是否需要合并。")
        rationale.append(f"含 {g['components']} 个不连通部件 → 注意是否需合并")

    # ---- 成本与区间
    lh_val = next((p["value"] for p in params if p["key"] == "layer_height"), 0.05)
    exp_val = next((p["value"] for p in params if p["key"] == "exposure"), None)
    inf_val = next((p["value"] for p in params if p["key"] == "infill"), None)
    hollow_flag = next((p["value"] for p in params if p["key"] == "hollow"), False)
    wall_val = next((p["value"] for p in params if p["key"] == "wall_thickness"), 2.0)

    def _cost_with(u=None):
        return _estimate_cost(
            g, printer, process, mat, float(lh_val), exp_val, inf_val,
            hollow=bool(hollow_flag), wall=float(wall_val or 2.0),
            u=u, support=sup, profile=g.get("profile"))

    cost = _cost_with(None)
    mc = _mc_interval(_cost_with)

    # ---- 本机历史标定
    use_cal = _truthy(ov.get("calibrate"), True)
    cal_h = calibration.estimate(process, printer_key, material_key, "hours")
    cal_g = calibration.estimate(process, printer_key, material_key, "grams")
    if use_cal:
        cost["hours"] = round(max(0.0, cost["hours"] * cal_h["factor"]), 2)
        cost["grams"] = round(max(0.0, cost["grams"] * cal_g["factor"]), 1)
        for key, cal in (("hours", cal_h), ("grams", cal_g)):
            band = mc[key]
            band["p10"] = round(band["p10"] * cal["factor"] * cal["lo_mult"], 2 if key == "hours" else 1)
            band["p50"] = round(band["p50"] * cal["factor"], 2 if key == "hours" else 1)
            band["p90"] = round(band["p90"] * cal["factor"] * cal["hi_mult"], 2 if key == "hours" else 1)
        if cal_h["factor"] != 1.0 or cal_g["factor"] != 1.0:
            cost["basis"] += (f"；本机历史修正 ×{cal_h['factor']}（时长）/ "
                              f"×{cal_g['factor']}（耗材），各 {cal_h['n']}/{cal_g['n']} 条记录")
        else:
            cost["basis"] += "；本机暂无可用记录，未做历史修正"

    # ---- 退化模型：这类输入不该给出「正常参数」，否则用户会以为能打
    degenerate = None
    height_mm = _f(g["height"])
    if int(g.get("faces") or 0) == 0:
        degenerate = "模型里没有任何三角面"
    elif _f(g["volume_mm3"]) < 0.01:
        degenerate = (f"体积仅 {_f(g['volume_mm3']):.4f}mm³，"
                      f"基本是空壳或平面片，没有可打印的实体")
    elif height_mm <= 0:
        degenerate = "模型在 Z 方向没有厚度"
    elif height_mm < float(lh_val):
        degenerate = (f"模型高 {height_mm:.2f}mm，比一层（{lh_val}mm）还矮，"
                      f"切不出完整的一层")
    if degenerate:
        feasible = False
        warnings.append(f"{degenerate}。下面的参数只是占位，请先检查模型是否导错或需要修复。")
        rationale.append(f"退化模型（{degenerate}）→ 参数仅供参考，不具备可打印性")

    # 物理模型与体积法的分歧：差得越多，越该提醒用户去核对
    legacy_hours = _f(g["volume_mm3"]) / 6.0 / 3600.0
    model_gap = 1.0
    if legacy_hours > 1e-6 and cost["hours"] > 1e-6:
        ratio = cost["hours"] / legacy_hours
        model_gap = max(ratio, 1.0 / ratio)

    params.append({
        "key": "cost", "label": "预估耗材", "unit": "",
        "value": f"{cost['grams']} g / {cost['hours']} h",
        "reason": cost["basis"] + "。实际会受摆放密度、支撑量和机器效率影响。",
        "range": {"lo": mc["hours"]["p10"], "hi": mc["hours"]["p90"], "unit": "h"},
        "mass_range": {"lo": mc["grams"]["p10"], "hi": mc["grams"]["p90"], "unit": "g"},
    })
    # 曝光的区间直接从 Jacobs 式解析求出，不套用时长区间（FDM 没有曝光项）
    if process == "SLA":
        exp_lo, exp_hi = exposure_interval(lh, mat["dp"], mat["ec"], ir, plan["adhesion"])
        for p in params:
            if p["key"] == "exposure":
                p["range"] = {
                    "lo": round(exp_lo * peel * plan["exposure_mult"], 1),
                    "hi": round(exp_hi * peel * plan["exposure_mult"], 1),
                    "unit": "s",
                }

    g["degenerate"] = bool(degenerate)
    g["feasible"] = feasible
    confidence = _confidence(g, conflict, model_gap, cal_h)

    # ---- 一屏可打印性：把「阻断」和「提醒」分开，用户不必读完整份建议才知道能不能打
    wall_ok = None
    wall_detail = "壁厚未测"
    if g.get("min_wall") is not None:
        need = (_f(printer.get("xy_res"), 0.05) * 2) if process == "SLA" else _f(printer.get("nozzle"), 0.4)
        wall_ok = _f(g["min_wall"]) >= need
        wall_detail = f"最薄 {_f(g['min_wall']):.2f}mm，建议 ≥ {need:.2f}mm"
    support_detail = (
        f"悬垂 {oratio * 100:.1f}%，需支撑面约 {contact:.0f}mm²"
        if sup.get("ok") else f"未判定：{sup.get('reason') or '模型未加载'}"
    )
    checks = [
        {"key": "fit", "label": "成型空间", "ok": bool(fits),
         "detail": "可放入" if fits else str(fit_reason)},
        {"key": "watertight", "label": "水密性", "ok": bool(g.get("watertight")),
         "detail": "水密" if g.get("watertight") else "有开放边/破面，切片可能失败"},
        # 没判定 ≠ 判定不通过：前者是「没测」，后者是「测了不行」。
        # 界面上前者画「?」，后者画「✕」，混成一个会让用户以为支撑真的有问题。
        {"key": "support", "label": "支撑判定",
         "ok": True if sup.get("ok") else None,
         "detail": support_detail},
        {"key": "wall", "label": "最薄壁厚", "ok": wall_ok, "detail": wall_detail},
    ]
    blockers: list[dict] = []
    if degenerate:
        blockers.append({"key": "geometry", "text": str(degenerate)})
    if not fits:
        blockers.append({"key": "fit", "text": f"放不进成型空间：{fit_reason}"})
    if not g.get("watertight"):
        blockers.append({"key": "watertight", "text": "模型不水密，请先补洞再导出切片"})
    if not sup.get("ok"):
        blockers.append({"key": "support", "text": f"支撑未判定：{sup.get('reason') or '模型未加载'}"})
    if wall_ok is False:
        blockers.append({"key": "wall", "text": f"最薄处 {_f(g['min_wall']):.2f}mm 低于可打印下限"})
    if blockers:
        status, status_label = "blocked", "暂不可直接打印"
    elif warnings:
        status, status_label = "caution", "可打印，但有小风险"
    else:
        status, status_label = "ready", "可直接打印"
    printability = {
        "status": status,
        "label": status_label,
        "blockers": blockers,
        "warning_count": len(warnings),
        "checks": checks,
    }

    if plan_key != DEFAULT_PLAN:
        rationale.append(
            f"方案「{plan['label']}」：层高 ×{plan['layer_mult']}、填充 {plan['infill_delta']:+d}%、"
            f"曝光 ×{plan['exposure_mult']}")

    sources = _sources(g, printer, process, mat, cal_h)
    # 结论先行：打不出来的模型，第一句必须是「打不出来」。
    # 旧文案对空模型是「预估 0.0g / 0.0h（P10–P90：0.0–0.0h）…2 条风险提示」，
    # 读起来像一份正常的估算，只是数字小；真正的原因被埋在后面的 warnings 里。
    if degenerate:
        summary = (
            f"{printer['name']} / {params[0]['value']}：模型不可打印——{degenerate}。"
            f"下面的参数与耗材/时长估算都是占位值，不能作为打印依据。"
        )
    elif not fits:
        summary = (
            f"{printer['name']} / {params[0]['value']}，层高 {lh_val}mm："
            f"模型放不进成型空间（{fit_reason}），需先分件或换机型。"
            f"当前预估 {cost['grams']}g / {cost['hours']}h 只在分件后才成立。"
        )
    else:
        summary = (
            f"{printer['name']} / {params[0]['value']}，层高 {lh_val}mm，"
            f"预估 {cost['grams']}g / {cost['hours']}h"
            f"（P10–P90：{mc['hours']['p10']}–{mc['hours']['p90']}h）。"
            f"方案：{plan['label']}，置信度{confidence['grade']}（{confidence['score']}）。"
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
        "printability": printability,
        # 哪些输入没按用户说的用：界面据此把这些项标黄，而不是静默替换
        "fallbacks": {
            "printer": printer_fallback,
            "material": material_fallback,
            "plan": (str(ov.get("plan")) if ov.get("plan") and ov.get("plan") not in PLANS else None),
        },
        "plans": {k: {"label": v["label"], "desc": v["desc"]} for k, v in PLANS.items()},
        "rationale": rationale,
        "sources": sources,
        "uncertainty": mc,
        "confidence": confidence,
        "calibration": {
            "applied": bool(use_cal),
            "hours": cal_h,
            "grams": cal_g,
        },
        "peel_risk": round(peel_risk, 2),
        "feasible": feasible,
        "degenerate": degenerate,
        "build_fit": {"fits": fits, "reason": fit_reason},
        # 这次计算实际用了什么：CPU 负责几何/切片，GPU 负责大规模点边判定，
        # 前端据此显示「CPU + GPU 协同」，而不是只在顶栏挂个设备名。
        "compute": {
            "device": gpu.device_name(),
            "backend": gpu.backend(),
            "ops": gpu.last_backend(),
        },
        "printer_preset": {
            "key": printer_key,
            "brand": printer.get("brand"),
            "model": printer.get("model"),
            "process": process,
            "build": printer.get("build"),
            "xy_res": printer.get("xy_res"),
            "irradiance": printer.get("irradiance"),
            "layer_range": printer.get("layer_range"),
            "nozzle": printer.get("nozzle"),
            "max_flow": printer.get("max_flow"),
            "verified": bool(printer.get("verified")),
            "note": printer.get("note") or "",
            "resolved_from": printer.get("resolved_from"),
            "missing": printer_missing,
        },
    }
