"""生成基准测试用的固定模型集。

设计约束（决定基准能不能被复现）：
1. **确定性**：不使用任何随机数，只依赖 trimesh 的确定性图元与固定索引删除。
   同一份代码在任何机器上生成逐字节相同的 STL，因此不需要把二进制模型
   提交进仓库，也就不会出现「我的模型和你不一样」导致的基准不可比。
2. **典型 + 对抗**：既要有干净的常规件（代表日常工作量），也要有刻意做坏的件
   （破洞 / 反法线 / 自交 / 薄壁 / 英寸单位），因为优化最常在这些路径上回退。
3. **面数分档**：1.2k / 2.5k / 3k / 20k / 82k，用来观察算法随规模的伸缩，
   避免「只在小模型上快」的假收益。

用法：
    .venv/Scripts/python.exe bench/make_models.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / "models"
MANIFEST = ROOT / "model_manifest.json"


def _ico(sub: int, radius: float, scale=None, translate=None) -> trimesh.Trimesh:
    m = trimesh.creation.icosphere(subdivisions=sub, radius=radius)
    if scale is not None:
        m.apply_scale(scale)
    if translate is not None:
        m.apply_translation(translate)
    return m


def _subdivide(m: trimesh.Trimesh, times: int) -> trimesh.Trimesh:
    """细分 n 次，面数 ×4^n。trimesh 的 subdivide 是确定性的。"""
    for _ in range(times):
        v, f = trimesh.remesh.subdivide(m.vertices, m.faces)
        m = trimesh.Trimesh(vertices=v, faces=f, process=False)
    return m


def _box(extents, center=None) -> trimesh.Trimesh:
    m = trimesh.creation.box(extents=extents)
    if center is not None:
        m.apply_translation(center)
    return m


# ---------------------------------------------------------------- 各模型
def make_ball_20k() -> trimesh.Trimesh:
    return _ico(5, 30.0)


def make_ball_82k() -> trimesh.Trimesh:
    return _ico(6, 30.0)


def make_plate_thin() -> trimesh.Trimesh:
    """0.35mm 薄板：顶部与底部间距 0.35mm，应触发壁厚 error（阈值 0.4mm）。"""
    return _subdivide(_box([60.0, 60.0, 0.35]), 4)


def make_broken_1k() -> trimesh.Trimesh:
    """破损件：球面被挖掉 32 个面形成破洞、整体法线朝内、另挂一块碎屑。

    刻意复刻自述里「破损球体 + 圆锥，1343 面」那一类的真实故障件。
    """
    m = _ico(3, 25.0)
    # 固定间隔删面，形成散布的破洞（不随机，保证可复现）
    keep = np.ones(len(m.faces), dtype=bool)
    keep[np.arange(0, len(m.faces), 40)] = False
    m.update_faces(keep)
    m.remove_unreferenced_vertices()
    m.faces = m.faces[:, ::-1]  # 整体翻转 → 有向体积为负
    scrap = _box([3.0, 3.0, 3.0], center=[60.0, 0.0, 0.0])
    return trimesh.util.concatenate([m, scrap])


def make_multi_5() -> trimesh.Trimesh:
    """5 个互不相连的方块：连通分量检测的目标件。"""
    parts = [
        _box([10.0, 10.0, 10.0], center=[0, 0, 5]),
        _box([10.0, 10.0, 10.0], center=[20, 0, 5]),
        _box([10.0, 10.0, 10.0], center=[40, 0, 5]),
        _box([10.0, 10.0, 10.0], center=[60, 0, 5]),
        _box([10.0, 10.0, 10.0], center=[80, 0, 5]),
    ]
    return _subdivide(trimesh.util.concatenate(parts), 1)


def make_xintersect() -> trimesh.Trimesh:
    """两个互相穿插的球：三角形真的穿插，自交检测必须检出。"""
    a = _ico(3, 15.0)
    b = _ico(3, 15.0, translate=[10.0, 10.0, 10.0])
    return trimesh.util.concatenate([a, b])


def make_overhang_t() -> trimesh.Trimesh:
    """T 形伞件：底板 + 细柱 + 悬空顶板，顶板下表面必须打支撑。"""
    base = _box([30.0, 30.0, 3.0], center=[0, 0, 1.5])
    stem = _box([6.0, 6.0, 20.0], center=[0, 0, 13.0])
    cap = _box([40.0, 40.0, 4.0], center=[0, 0, 25.0])
    return _subdivide(trimesh.util.concatenate([base, stem, cap]), 2)


def make_inch_254() -> trimesh.Trimesh:
    """254mm = 10 英寸：单位推断要能识别并给出提示。"""
    return _box([254.0, 100.0, 100.0], center=[0, 0, 50.0])


def make_statue_30k() -> trimesh.Trimesh:
    """非均匀缩放的人形比例件：镂空 / 空腔 / 导出的目标件。"""
    return _ico(5, 1.0, scale=[20.0, 15.0, 45.0], translate=[0, 0, 45.0])


def make_degenerate() -> trimesh.Trimesh:
    """退化件：零面积面 + 空网格边界，用来验证不会崩、且不会给出「可打印」。"""
    v = np.array([[0.0, 0, 0], [1.0, 0, 0], [2.0, 0, 0], [0.0, 1, 0]])
    f = np.array([[0, 1, 2], [0, 1, 1], [0, 2, 3]])
    return trimesh.Trimesh(vertices=v, faces=f, process=False)


GENERATORS = {
    "ball_20k": (make_ball_20k, False),
    "ball_82k": (make_ball_82k, False),
    "plate_thin": (make_plate_thin, False),
    "broken_1k": (make_broken_1k, False),
    "multi_5": (make_multi_5, False),
    "xintersect": (make_xintersect, False),
    "overhang_t": (make_overhang_t, False),
    "inch_254": (make_inch_254, False),
    "statue_30k": (make_statue_30k, False),
    "degenerate": (make_degenerate, False),
    # 与 ball_20k 同几何，但用 ASCII 编码，用来单独测文本解析路径
    "ascii_20k": (make_ball_20k, True),
}

# 每个模型的用途标注，写进 manifest，避免以后有人看着面数以为是随便造的
PURPOSE = {
    "ball_20k": "常规干净件，主力基准（加载/检测/场/预览/导出/镂空）",
    "ball_82k": "高模压力件，观察算法随面数伸缩",
    "plate_thin": "0.35mm 薄板，壁厚检测必须判 error",
    "broken_1k": "破洞+整体反法线+碎屑，修复与检测的典型故障件",
    "multi_5": "5 个互不连通部件，连通分量检测",
    "xintersect": "两球穿插，自交检测必须检出",
    "overhang_t": "T 形伞件，顶板下表面必须生成支撑",
    "inch_254": "254mm=10 英寸，单位推断必须识别",
    "statue_30k": "人形比例件，镂空/空腔/多格式导出",
    "degenerate": "含零面积面的退化件，验证不崩且判不可打印",
    "ascii_20k": "与 ball_20k 同几何的 ASCII STL，单独测文本解析路径",
}


def build() -> dict:
    MODELS.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"generated_by": "bench/make_models.py", "models": {}}
    for name, (fn, ascii_) in GENERATORS.items():
        m = fn()
        path = MODELS / f"{name}.stl"
        if ascii_:
            m.export(path, file_type="stl_ascii")
        else:
            m.export(path)
        rec = {
            "file": path.name,
            "ascii": ascii_,
            "purpose": PURPOSE[name],
            "faces": int(len(m.faces)),
            "vertices": int(len(m.vertices)),
            "volume_mm3": round(float(abs(m.volume)), 3),
            "area_mm2": round(float(m.area), 3),
            "extents": [round(float(x), 4) for x in m.extents],
            "watertight": bool(m.is_watertight),
            "bytes": int(path.stat().st_size),
        }
        manifest["models"][name] = rec
        print(f"  {name:<12} {rec['faces']:>7} 面  {rec['bytes']/1048576:>7.2f} MB  {path.name}")
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    print("生成基准模型集 →", MODELS)
    man = build()
    total = sum(r["bytes"] for r in man["models"].values())
    print(f"完成：{len(man['models'])} 个模型，共 {total/1048576:.1f} MB，清单写入 {MANIFEST.name}")
    sys.exit(0)
