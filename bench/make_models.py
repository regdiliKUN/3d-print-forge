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

# 这个脚本既会被 `python bench/make_models.py` 直接跑，也会被别的模块 import。
# 直接跑时 sys.path[0] 是 bench/ 本身，找不到项目根的 app 包，必须显式补上。
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

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


def make_needle() -> trimesh.Trimesh:
    """细长针：0.2 x 0.2 x 200mm，长宽比 1000:1。

    专治「用包围盒对角线当尺度」的那类代码——这种件上，对角线被单轴主导，
    按对角线算出来的射线长度会比实际壁厚大三个数量级，
    包围盒裁剪与分段查询在这种尺度失真下还能不能对，必须实测。
    顺带它本身 0.2mm 壁厚，壁厚检测必须判 error。
    """
    return _subdivide(_box([0.2, 0.2, 200.0], center=[0, 0, 100.0]), 3)


def make_empty() -> trimesh.Trimesh:
    """空模型：0 个三角面。

    用户真的会导入空文件（导出失败的产物、被清空的文件）。
    它不该让检测崩掉，也不该给出「可以打印」。
    """
    return trimesh.Trimesh(vertices=np.zeros((0, 3)), faces=np.zeros((0, 3), dtype=np.int64),
                           process=False)


GENERATORS = {
    # 极端形态放在最前面：它们最容易被生成脚本漏掉，也最容易让几何代码崩
    "empty": (make_empty, False),
    "needle": (make_needle, False),
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
    "empty": "0 面空文件，验证不崩且不可打印",
    "needle": "0.2x0.2x200mm 细长针，长宽比 1000:1，专治按包围盒对角线取尺度的代码；壁厚须判 error",
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
    # 落盘前先记一份「生成器的意图」，落盘+回读后再记一份「基准真正会加载的几何」。
    # 两者会不一致：STL 每个三角形存独立顶点，加载时要焊接；
    # 退化面会在焊接时被剔除。清单必须以**回读后**的为准，
    # 否则基准数据与模型清单之间就有一层说不清的对不上。
    from app import meshio

    manifest: dict = {"generated_by": "bench/make_models.py", "models": {}}
    for name, (fn, ascii_) in GENERATORS.items():
        m = fn()
        intent_faces, intent_verts = int(len(m.faces)), int(len(m.vertices))
        path = MODELS / f"{name}.stl"
        if ascii_:
            m.export(path, file_type="stl_ascii")
        else:
            m.export(path)

        loaded = meshio.load_mesh(path)
        has = len(loaded.faces) > 0
        rec = {
            "file": path.name,
            "ascii": ascii_,
            "purpose": PURPOSE[name],
            # 基准实际吃到的几何（回读后）
            "faces": int(len(loaded.faces)),
            "vertices": int(len(loaded.vertices)),
            "volume_mm3": round(float(abs(loaded.volume)), 3) if has else 0.0,
            "area_mm2": round(float(loaded.area), 3) if has else 0.0,
            "extents": [round(float(x), 4) for x in loaded.extents] if has else [0.0, 0.0, 0.0],
            "watertight": bool(loaded.is_watertight),
            # 生成器意图：用来解释「为什么清单里的数比造的时候少」
            "intent_faces": intent_faces,
            "intent_vertices": intent_verts,
            "bytes": int(path.stat().st_size),
        }
        manifest["models"][name] = rec
        note = "" if rec["faces"] == intent_faces else f"（造时 {intent_faces}，回读少了 {intent_faces - rec['faces']}）"
        print(f"  {name:<12} {rec['faces']:>7} 面  {rec['bytes']/1048576:>7.2f} MB  {path.name}{note}")
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    print("生成基准模型集 →", MODELS)
    man = build()
    total = sum(r["bytes"] for r in man["models"].values())
    print(f"完成：{len(man['models'])} 个模型，共 {total/1048576:.1f} MB，清单写入 {MANIFEST.name}")
    sys.exit(0)
