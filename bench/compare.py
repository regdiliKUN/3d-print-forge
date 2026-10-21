"""对比两次基准结果：耗时、内存、以及结果指纹有没有变。

判"变快了多少"只要两个数相除；
判"有没有算错"必须比指纹——这是这个工具存在的全部理由。
性能改动最常见的翻车方式是「变快了但悄悄少算了点什么」，
光看耗时曲线永远不会暴露。

用法：
    .venv/Scripts/python.exe bench/compare.py bench/results/A.json bench/results/B.json
    .venv/Scripts/python.exe bench/results/latest.json bench/results/bench-xxx-baseline.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import digest  # noqa: E402


def _load(p: str) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _get(d: dict, m: str, s: str) -> dict | None:
    return (d.get("models", {}).get(m, {}).get("stages", {}) or {}).get(s)


def _fmt_change(before_ms: float, after_ms: float) -> str:
    """倍数与方向分开写，别让人去猜 1.06x 是快了还是慢了。"""
    ratio = after_ms / before_ms
    if 0.9 <= ratio <= 1.1:
        return "持平"
    if ratio > 1:
        return f"慢 {ratio:.1f}x"
    return f"快 {1.0 / ratio:.1f}x"


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    a, b = _load(sys.argv[1]), _load(sys.argv[2])

    print(f"A = {Path(sys.argv[1]).name}   {a.get('when')}")
    print(f"B = {Path(sys.argv[2]).name}   {b.get('when')}")
    print()
    print(f"{'模型/阶段':<26}{'A 耗时':>9}{'B 耗时':>9}{'变化':>9}"
          f"{'A 内存':>9}{'B 内存':>9}  结果指纹")
    print("-" * 84)

    keys = []
    for m in a.get("models", {}):
        for s in a["models"][m]["stages"]:
            keys.append((m, s))
    for m in b.get("models", {}):
        for s in b["models"][m]["stages"]:
            if (m, s) not in keys:
                keys.append((m, s))

    regressed = 0
    improved = 0
    fp_changed = 0
    for m, s in sorted(keys):
        sa, sb = _get(a, m, s), _get(b, m, s)
        if sa is None or sb is None:
            print(f"{m + '/' + s:<26}{'缺失，无法对比':>40}")
            continue
        ta, tb = sa.get("median_ms"), sb.get("median_ms")
        ma, mb = sa.get("peak_delta_mb"), sb.get("peak_delta_mb")
        if ta and tb:
            chg = _fmt_change(ta, tb)
            ratio = tb / ta
            # 15% 以内算噪声：这台机器上 3 次重复的抖动本身就有这么大
            if ratio > 1.15:
                regressed += 1
            elif ratio < 0.87:
                improved += 1
        else:
            chg = "—"
        fa = sa.get("fingerprint") or {}
        fb = sb.get("fingerprint") or {}
        diffs = digest.diff(fa, fb) if (fa and fb) else []
        if diffs:
            fp_changed += 1
            fp = f"⚠ {len(diffs)} 项变化: {diffs[0][:48]}"
        else:
            fp = "一致"
        print(f"{m + '/' + s:<26}{ta:>7.0f}ms{tb:>7.0f}ms{chg:>9}"
              f"{ma:>7.0f}MB{mb:>7.0f}MB  {fp}")
    print("-" * 84)
    print(f"变快的阶段 {improved} 个 ｜ 变慢的阶段 {regressed} 个 ｜ 结果指纹变化的阶段 {fp_changed} 个")
    if fp_changed:
        print("\n⚠ 指纹变化不等于算错——但每一处都必须能解释清楚，否则就是算错了。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
