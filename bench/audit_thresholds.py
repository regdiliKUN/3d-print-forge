"""审计阈值表：该定义的有没有定义，已定义的有没有松到形同虚设。

阈值表和阶段矩阵是两份独立维护的数据，改了一边忘另一边是必然会发生的事。
这个脚本把两种常见坏状态一次性查出来：

1. **缺定义**：矩阵里会跑的 (模型,阶段) 在阈值表里没有专属条目，
   运行时静默回落到默认线（2000ms/500MB）——一个小模型拿这条线去判，
   PASS 是假的。
2. **过松/过紧**：实测值与阈值的比值。
   - 余量 > 4 倍：阈值形同虚设，真正的退化要退化 4 倍才会被发现；
   - 余量 < 1.3 倍：阈值贴在实测值边上，机器换个型号就会假报警。

用法（需要先有 latest.json）：
    .venv/Scripts/python.exe bench/audit_thresholds.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import stages as st  # noqa: E402

BENCH = Path(__file__).resolve().parent
LOOSE = 5.0    # 实测快于阈值这么多倍，说明阈值没在起作用
TIGHT = 1.3    # 实测离阈值这么近，说明阈值太紧
SUBMS = 1.0    # 实测 < 1ms（四舍五入成 0）的子毫秒操作，不参与「过松」判定：
               # 它的阈值本来就该留一个绝对余量（几十 ms），按比值算无意义


def main() -> int:
    thr = json.loads((BENCH / "thresholds.json").read_text(encoding="utf-8"))
    # 优先用回归基线 expected.json（稳定、可复现）；它不存在时退回 latest.json
    # 并显式警示——latest 可能是并行/快检的脏数据，不宜用于阈值审计。
    base = BENCH / "expected.json"
    fall = BENCH / "results" / "latest.json"
    if base.exists():
        src = base
        src_name = "expected.json（回归基线）"
    elif fall.exists():
        src = fall
        src_name = "latest.json（⚠ 非基线，建议先 run.py --save-expected）"
    else:
        src = None
    data = json.loads(src.read_text(encoding="utf-8")) if src else None

    problems: list[str] = []
    loose: list[str] = []
    tight: list[str] = []

    # 1) 矩阵项是否有专属阈值
    missing = 0
    for model, slist in st.MATRIX.items():
        mt = thr.get(model, {})
        for s in slist:
            if s not in mt:
                missing += 1
                problems.append(f"{model}/{s}")
    print("【缺专属阈值】")
    if missing:
        for p in problems:
            print("  ❌", p)
    else:
        print("  ✅ 矩阵里的每个 (模型,阶段) 都有专属阈值")
    print(f"  共 {sum(len(v) for v in st.MATRIX.values())} 个矩阵项，缺 {missing} 个")

    # 2) 已有阈值的松紧
    print("\n【阈值松紧】（余量 = 阈值 / 实测，数据源：%s）" % src_name)
    if not data:
        print("  没有可用的结果文件，跳过（先跑一次 bench/run.py）")
        return 1 if missing else 0
    print(f"  {'模型/阶段':<28}{'实测':>8}{'阈值':>8}{'余量':>8}  判定")
    n = 0
    for model, dm in data.get("models", {}).items():
        for sname, s in dm["stages"].items():
            th = thr.get(model, {}).get(sname)
            if not th:
                continue
            ms = s.get("median_ms")
            if not ms:
                continue
            n += 1
            head = th["max_ms"] / ms
            # 子毫秒操作不判「过松」：比值被 0 撑成无穷，纯属噪声
            if ms < SUBMS:
                tag, bucket = ("⚠ 过紧" if head < TIGHT else "合适"), (
                    tight if head < TIGHT else None)
            elif head >= LOOSE:
                tag, bucket = "⚠ 过松", loose
            elif head < TIGHT:
                tag, bucket = "⚠ 过紧", tight
            else:
                tag, bucket = "合适", None
            if bucket is not None:
                bucket.append(f"{model}/{sname} 余量 {head:.1f}x（实测 {ms:.0f}ms，阈值 {th['max_ms']}ms）")
            print(f"  {model + '/' + sname:<28}{ms:>6.0f}ms{th['max_ms']:>7}ms{head:>7.1f}x  {tag}")
    print()
    if loose:
        print(f"ℹ 过松的阈值 {len(loose)} 条（仅调优建议，不阻断 CI）：")
        print("   重操作/导出类的宽松上限是刻意留的**安全天花板**（百万面模型导出可能要几秒），")
        print("   不是回归检测线；真要抓退化靠结果指纹回归闸门，不靠时间阈值。")
        for x in loose:
            print("   -", x)
    if tight:
        print(f"⚠ 过紧的阈值 {len(tight)} 条（换机器极易假报警，应放宽）：")
        for x in tight:
            print("   -", x)
    # 只有「缺阈值」（假 PASS）和「过紧」（假报警）算真正的失败；
    # 「过松」只是调优建议，不当作门禁失败，否则 CI 永远红、反而没人看。
    blockers = missing + len(tight)
    if blockers:
        print(f"\n⛔ {blockers} 处会破坏验收有效性，必须处置")
        return 1
    print(f"\n✅ {n} 条阈值无「缺定义 / 过紧」问题（过松 {len(loose)} 条为调优建议）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
