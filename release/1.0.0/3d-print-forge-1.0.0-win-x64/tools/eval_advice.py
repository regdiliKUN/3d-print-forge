"""建议精度评估：拿攒下来的真实打印记录，算准不准、区间靠不靠谱。

用法：
    python tools/eval_advice.py                # 用 data/state/calibration.json
    python tools/eval_advice.py --json         # 机器可读输出
    python tools/eval_advice.py --group        # 按 机型/材料 分组看

为什么需要这个：
    「建议准不准」不能靠感觉。没有这个脚本，改了参数也不知道是变好还是变坏。
    三个指标各管一段：
      MAE/MAPE  点估计偏多少
      PICP      区间覆盖概率，标 80% 就该盖住 80%
      MPIW      区间平均宽度，太宽等于没说
    理想状态是 PICP 贴着 80% 且 MPIW 尽量窄。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import calibration  # noqa: E402

KEYS = ("hours", "grams")
TARGET_COVERAGE = 0.80


def _pos(v) -> float | None:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _metrics(pairs: list[tuple[float, float]]) -> dict:
    """给定 (预测, 实际) 序列，算误差与区间指标。"""
    errs = [abs(a - p) for p, a in pairs]
    rels = [abs(a - p) / a for p, a in pairs if a > 0]
    covered = 0
    widths: list[float] = []
    for p, a in pairs:
        lo = p * (1.0 - TARGET_COVERAGE / 2.0)
        hi = p * (1.0 + TARGET_COVERAGE / 2.0)
        if lo <= a <= hi:
            covered += 1
        widths.append(hi - lo)
    out = {
        "n": len(pairs),
        "mae": round(statistics.fmean(errs), 4) if errs else None,
        "mape_pct": round(statistics.fmean(rels) * 100, 2) if rels else None,
        "bias_pct": (round(statistics.fmean([(a - p) / p for p, a in pairs]) * 100, 2)
                    if pairs else None),
    }
    if pairs:
        out["picp_pct"] = round(covered / len(pairs) * 100, 1)
        out["mpiw"] = round(statistics.fmean(widths), 3)
    return out


def evaluate(records: list[dict]) -> dict:
    per_key: dict[str, dict] = {}
    for key in KEYS:
        pairs: list[tuple[float, float]] = []
        for r in records:
            p = _pos((r.get("pred") or {}).get(key))
            a = _pos((r.get("actual") or {}).get(key))
            if p and a:
                pairs.append((p, a))
        per_key[key] = _metrics(pairs)

    fails = [r for r in records if r.get("result") == "fail"]
    return {
        "total_records": len(records),
        "success": len(records) - len(fails),
        "fail": len(fails),
        "success_rate_pct": round((len(records) - len(fails)) / len(records) * 100, 1) if records else 0.0,
        "per_key": per_key,
    }


def by_group(records: list[dict]) -> list[dict]:
    buckets: dict[tuple, list[dict]] = {}
    for r in records:
        k = (r.get("process"), r.get("printer"), r.get("material"))
        buckets.setdefault(k, []).append(r)
    rows = []
    for (proc, pr, mat), recs in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        m = evaluate(recs)
        rows.append({
            "process": proc, "printer": pr, "material": mat,
            "n": len(recs), "success_rate_pct": m["success_rate_pct"],
            "hours_mape_pct": (m["per_key"]["hours"] or {}).get("mape_pct"),
            "hours_bias_pct": (m["per_key"]["hours"] or {}).get("bias_pct"),
            "grams_mape_pct": (m["per_key"]["grams"] or {}).get("mape_pct"),
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="评估打印建议的实测精度")
    ap.add_argument("--file", default=str(calibration._FILE), help="标定记录文件")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--group", action="store_true", help="按机型/材料分组")
    args = ap.parse_args()

    src = Path(args.file)
    if not src.exists():
        print(f"没有找到记录文件：{src}\n先在界面上打印几次并提交反馈。")
        return 1
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"记录文件读不出来：{e}")
        return 1
    records = [r for r in (data.get("records") or []) if isinstance(r, dict)]
    if not records:
        print(f"{src} 里还没有记录。")
        return 1

    result = evaluate(records)
    if args.group:
        result["groups"] = by_group(records)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"记录 {result['total_records']} 条   成功 {result['success']}   失败 {result['fail']}   "
          f"成功率 {result['success_rate_pct']}%")
    for key in KEYS:
        m = result["per_key"][key]
        if not m.get("n"):
            print(f"  {key:6s} 无有效对比数据")
            continue
        print(f"  {key:6s} n={m['n']:<3d} MAE={m['mae']}  MAPE={m['mape_pct']}%  "
              f"偏差={m['bias_pct']}%  区间覆盖={m.get('picp_pct')}%（目标 80%）")
    if args.group:
        print("\n分组：")
        for g in result["groups"]:
            print(f"  {g['process']}/{g['printer']}/{g['material']}  n={g['n']}  "
                  f"成功率={g['success_rate_pct']}%  时长MAPE={g['hours_mape_pct']}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
