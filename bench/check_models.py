"""验证模型集与清单一致。

为什么要有这一层：基准的模型集是脚本生成的，一旦有人改了生成器
（比如把 icosphere 的细分级别调了一级），模型悄悄变了，
而历史基准数据还是按旧模型采的——跨时间的性能曲线就断了。
每次跑基准前先跑这个，保证「这次用的模型」和「上次用的模型」是同一批。

用法：
    .venv/Scripts/python.exe bench/check_models.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import meshio  # noqa: E402

MANIFEST = Path(__file__).resolve().parent / "model_manifest.json"
# 浮点用相对误差：STL 是 float32 存的，读回来和生成时的 float64 必然有微小差异
REL = 1e-4
ABS = 1e-6


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(ABS, REL * max(abs(a), abs(b)))


def main() -> int:
    if not MANIFEST.exists():
        print("缺 model_manifest.json，先跑 bench/make_models.py")
        return 1
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    bad = 0
    print(f"{'模型':<14}{'面数':>8}{'清单面数':>10}{'体积(mm3)':>14}{'清单体积':>14}  判定")
    print("-" * 78)
    for name, rec in man["models"].items():
        p = Path(__file__).resolve().parent / "models" / rec["file"]
        if not p.exists():
            print(f"{name:<14}{'—':>8}{rec['faces']:>10}{'—':>14}{rec['volume_mm3']:>14.1f}  ❌文件缺失")
            bad += 1
            continue
        m = meshio.load_mesh(p)
        vol = abs(float(m.volume)) if len(m.faces) else 0.0
        area = float(m.area) if len(m.faces) else 0.0
        probs = []
        if len(m.faces) != rec["faces"]:
            probs.append(f"面数 {len(m.faces)} ≠ {rec['faces']}")
        if len(m.vertices) != rec["vertices"]:
            probs.append(f"顶点数 {len(m.vertices)} ≠ {rec['vertices']}")
        if not _close(vol, rec["volume_mm3"]):
            probs.append(f"体积 {vol:.1f} ≠ {rec['volume_mm3']:.1f}")
        if not _close(area, rec["area_mm2"]):
            probs.append(f"表面积 {area:.1f} ≠ {rec['area_mm2']:.1f}")
        # 尺寸可以有 float32 舍入，用 1e-3mm 容差
        ext = [float(x) for x in m.extents] if len(m.faces) else [0.0, 0, 0]
        if any(abs(a - b) > 1e-3 for a, b in zip(ext, rec["extents"])):
            probs.append(f"包围盒 {ext} ≠ {rec['extents']}")
        ok = not probs
        bad += 0 if ok else 1
        print(f"{name:<14}{len(m.faces):>8}{rec['faces']:>10}{vol:>14.1f}"
              f"{rec['volume_mm3']:>14.1f}  {'·OK' if ok else '❌' + '; '.join(probs)}")
    print("-" * 78)
    n = len(man["models"])
    if bad:
        print(f"❌ {bad}/{n} 个模型与清单不一致")
        return 1
    print(f"✅ {n}/{n} 个模型与清单完全一致（面数/顶点数/体积/表面积/包围盒）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
