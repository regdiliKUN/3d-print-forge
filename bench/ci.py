"""一键 CI 门禁：跑全套基准并聚合三道闸门的退出码。

三道闸门各管一件事，必须全部通过才放行：
1. run.py  —— 阶段耗时/内存不超阈值，且结果指纹与回归基线 expected.json 一致
             （指纹对不上 = 可能悄悄算错，比变慢危险）
2. verify_raycast.py —— 射线求交新旧实现逐位一致（核心算法改动的安全网）
3. audit_thresholds.py —— 阈值表与阶段矩阵没有脱节（缺阈值/松到形同虚设）

单独跑任何一个都行，但这个入口把它们串起来，且：
- 任一道闸门失败，整体返回非 0，CI 直接红
- 若 expected.json 不存在，第 1 道闸门无法证明「没退化」，视为未就绪而失败
  （避免有人删了基线还以为绿了）

用法：
    .venv/Scripts/python.exe bench/ci.py
    .venv/Scripts/python.exe bench/ci.py --repeats 1 --parallel 4   # 快检
退出码：0 = 全绿；1 = 有闸门不过；2 = 用法/前置条件不满足
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parents[1]
EXPECTED = BENCH / "expected.json"


def _run(name: str, cmd: list[str]) -> tuple[int, str]:
    print(f"\n{'=' * 72}\n▶ {name}\n{'=' * 72}", flush=True)
    r = subprocess.run(cmd, cwd=str(ROOT))
    return r.returncode, name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--skip-verify", action="store_true",
                    help="跳过射线一致性验证（仅当 verify 自身在改动时临时用）")
    ap.add_argument("--skip-audit", action="store_true")
    args = ap.parse_args()

    # 前置条件：回归基线必须存在，否则 run.py 的第三道闸门等于没开
    if not EXPECTED.exists():
        print(f"❌ 回归基线缺失：{EXPECTED}")
        print("   先跑一次 `bench/run.py --save-expected` 建立基线，CI 才能证明「没退化」。")
        return 2

    gates: list[tuple[int, str]] = []

    # 闸门 1：基准（阈值 + 指纹回归）
    cmd = [sys.executable, str(BENCH / "run.py"),
           "--repeats", str(args.repeats), "--parallel", str(args.parallel)]
    gates.append(_run("基准：耗时/内存阈值 + 结果指纹回归", cmd))

    # 闸门 2：射线求交新旧一致性
    if not args.skip_verify:
        gates.append(_run("射线求交：新旧实现逐位一致性",
                          [sys.executable, str(BENCH / "verify_raycast.py")]))

    # 闸门 3：阈值表审计
    if not args.skip_audit:
        gates.append(_run("阈值审计：矩阵项缺阈值 / 过松过紧",
                          [sys.executable, str(BENCH / "audit_thresholds.py")]))

    print("\n" + "=" * 72)
    print("闸门结果：")
    all_ok = True
    for code, name in gates:
        status = "✅ 通过" if code == 0 else "❌ 未过"
        print(f"  [{status}] {name}（退出码 {code}）")
        if code != 0:
            all_ok = False
    print("=" * 72)
    if all_ok:
        print("🟢 CI 全绿：阈值、指纹回归、射线一致性、阈值表一致性 全部守住")
        return 0
    print("🔴 CI 未过：上面标 ❌ 的闸门需要修")
    return 1


if __name__ == "__main__":
    sys.exit(main())
