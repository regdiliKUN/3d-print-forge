"""基准总控：起 worker、汇总、判过/不过、查回归。

用法：
    .venv/Scripts/python.exe bench/run.py                  # 全量，3 次重复
    .venv/Scripts/python.exe bench/run.py --quick          # 快速，1 次重复（迭代时用）
    .venv/Scripts/python.exe bench/run.py --models ball_20k,overhang_t
    .venv/Scripts/python.exe bench/run.py --save-expected  # 把本次指纹存为回归基准

退出码：0 = 全部通过；1 = 有阶段超阈值或结果指纹对不上。
"""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import digest  # noqa: E402
from bench import stages as st  # noqa: E402

BENCH = Path(__file__).resolve().parent
RESULTS = BENCH / "results"
TMP = BENCH / "tmp"
EXPECTED = BENCH / "expected.json"


def _env_fingerprint() -> dict:
    """记录跑基准的机器与关键库版本。

    耗时和内存的绝对值都依赖硬件与库版本。阈值是在「32 核 / 64GB / 特定库版本」
    这台机器上定的，换环境后数字不可比——记下来是为了让 compare.py 在跨环境
    对比时直接报警，而不是让人拿着两台机器的数字算出个「快了 3 倍」。
    """
    import os

    import psutil

    info: dict = {
        "python": platform.python_version(),
        "cpu_logical": os.cpu_count(),
        "ram_total_gb": round(psutil.virtual_memory().total / 2 ** 30, 1),
        "parallel": None,  # 由 main 在填 report 时再补，这里占位
    }
    # 关键第三方库的版本：几何/数值结果可能因它们的大版本变化而变
    for mod in ["numpy", "scipy", "trimesh", "rtree", "psutil"]:
        try:
            m = __import__(mod)
            info[f"lib_{mod}"] = getattr(m, "__version__", "?")
        except Exception:
            info[f"lib_{mod}"] = "missing"
    return info


def load_thresholds() -> dict:
    return json.loads((BENCH / "thresholds.json").read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="")
    ap.add_argument("--stages", default="")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--save-expected", action="store_true")
    ap.add_argument("--tag", default="")
    ap.add_argument("--parallel", type=int, default=1,
                    help="同时跑几个模型。省时间，但各 worker 会争 CPU，"
                         "测出来的耗时与串行结果**不可比**，会记进结果文件")
    args = ap.parse_args()
    if args.quick:
        args.repeats = 1
    if args.parallel > 1:
        print(f"⚠ 并行度 {args.parallel}：各模型进程会争抢 CPU，"
              f"本次耗时数据与串行基准不可直接比较")

    RESULTS.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)

    manifest = json.loads((BENCH / "model_manifest.json").read_text(encoding="utf-8"))
    models = [m for m in (args.models.split(",") if args.models else list(st.MATRIX)) if m]

    thr_all = load_thresholds()
    report: dict = {
        "tag": args.tag,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "repeats": args.repeats,
        "parallel": args.parallel,
        # 环境指纹：耗时和内存的绝对值都依赖机器与库版本。
        # 阈值是在特定环境下定的，换环境后数字不可比——
        # 记下来是为了让 compare.py 能在跨环境对比时直接报警，
        # 而不是让人拿着两台机器的数字算出个「快了 3 倍」。
        "env": _env_fingerprint(),
        "models": {},
    }
    report["env"]["parallel"] = args.parallel
    t_start = time.perf_counter()

    def _collect(model: str, code: int) -> None:
        out = TMP / f"{model}.json"
        if out.exists():
            report["models"][model] = json.loads(out.read_text(encoding="utf-8"))
        else:
            print(f"  !! {model} 没有产出结果（退出码 {code}）")

    def _run_one(model: str) -> tuple[str, int]:
        out = TMP / f"{model}.json"
        cmd = [sys.executable, str(BENCH / "worker.py"), "--model", model,
               "--repeats", str(args.repeats), "--out", str(out)]
        if args.stages:
            cmd += ["--stages", args.stages]
        r = subprocess.run(cmd, cwd=str(ROOT))
        if out.exists():
            return model, r.returncode
        return model, -2

    if args.parallel > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=args.parallel) as ex:
            for model, code in ex.map(_run_one, models):
                print(f"[bench] {model} 完成（退出码 {code}）", flush=True)
                _collect(model, code)
    else:
        for model in models:
            print(f"[bench] {model} …", flush=True)
            _, code = _run_one(model)
            _collect(model, code)

    report["wall_s"] = round(time.perf_counter() - t_start, 1)

    # ---------------- 判过 / 不过 ----------------
    rows: list[dict] = []
    violations: list[str] = []
    fallbacks: list[str] = []
    for model, data in report["models"].items():
        model_thr = thr_all.get(model, {})
        for sname, s in data["stages"].items():
            # 阈值没为该 (模型,阶段) 专门定义时回落 default。
            # 这个回落必须显式标出来：否则一个 576 面的小模型拿 2000ms 的默认线
            # 去判，PASS 是假的——它只是在说"没给这条定标准"。
            explicit = sname in model_thr
            th = model_thr.get(sname, thr_all["default"])
            ms = s.get("median_ms")
            peak = s.get("peak_delta_mb")
            ok_ms = ms is None or ms <= th["max_ms"]
            ok_mem = peak is None or peak <= th["max_peak_delta_mb"]
            if not explicit:
                fallbacks.append(f"{model}/{sname}（用了默认 {th['max_ms']}ms / "
                                 f"{th['max_peak_delta_mb']}MB）")
            if s.get("error"):
                violations.append(f"{model}/{sname}: 阶段执行失败 {s['error']}")
            if not ok_ms:
                violations.append(
                    f"{model}/{sname}: {ms}ms 超过阈值 {th['max_ms']}ms（超出 {ms - th['max_ms']:.0f}ms）")
            if not ok_mem:
                violations.append(
                    f"{model}/{sname}: 峰值内存 +{peak}MB 超过阈值 {th['max_peak_delta_mb']}MB")
            rows.append({
                "model": model, "stage": sname, "ms": ms, "max_ms": th["max_ms"],
                "peak_mb": peak, "max_peak_mb": th["max_peak_delta_mb"],
                "end_delta_mb": s.get("end_delta_mb"),
                "leak_mb": s.get("end_delta_mb", 0) or 0,
                "faces": data.get("faces"),
                "threshold_explicit": explicit,
                "status": "FAIL" if (not ok_ms or not ok_mem or s.get("error")) else "PASS",
            })

    # ---------------- 结果指纹回归 ----------------
    regressions: list[str] = []
    expected = json.loads(EXPECTED.read_text(encoding="utf-8")) if EXPECTED.exists() else None
    if expected:
        for model, data in report["models"].items():
            for sname, s in data["stages"].items():
                base = (expected.get("models", {}).get(model, {}).get("stages", {})
                        .get(sname, {}).get("fingerprint"))
                if not base or s.get("error"):
                    continue
                d = digest.diff(base, s.get("fingerprint") or {})
                if d:
                    regressions.append(f"{model}/{sname}: " + "; ".join(d[:6]))

    # ---------------- 输出 ----------------
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if args.tag:
        stamp = f"{stamp}-{args.tag}"
    outfile = RESULTS / f"bench-{stamp}.json"
    outfile.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (RESULTS / "latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                         encoding="utf-8")

    print()
    print(f"{'模型':<12}{'阶段':<18}{'面数':>8}   中位耗时       峰值内存   残留  判定")
    print("-" * 92)
    for r in rows:
        mark = "❌" if r["status"] == "FAIL" else " "
        flag = "限" if r["threshold_explicit"] else "默"
        ms = f"{r['ms']:>7.0f}ms ({flag}{r['max_ms']})"
        mb = f"{r['peak_mb']:>6.0f}MB ({flag}{r['max_peak_mb']})"
        print(f"{r['model']:<12}{r['stage']:<18}{r['faces']:>8}   {ms:<16}{mb:<16}"
              f"{r['leak_mb']:>4.0f}MB {mark}{r['status']}")
    print("-" * 92)
    n_fail = sum(1 for r in rows if r["status"] == "FAIL")
    n_fb = sum(1 for r in rows if not r["threshold_explicit"])
    print(f"墙钟 {report['wall_s']}s ｜ 阶段 {len(rows)} 个 ｜ 超阈值 {n_fail} 个 ｜ "
          f"结果指纹偏差 {len(regressions)} 个 ｜ 阈值未定义 {n_fb} 个")
    if fallbacks:
        print("\n以下阶段没有专属阈值，用的是默认线，其 PASS 不代表该操作真的够快：")
        for f in fallbacks:
            print("  -", f)
    if violations:
        print("\n超阈值明细：")
        for v in violations:
            print("  -", v)
    if regressions:
        print("\n结果指纹偏差（可能是算错了，不只是变慢了）：")
        for v in regressions:
            print("  -", v)

    if args.save_expected:
        EXPECTED.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已把本次指纹存为回归基准 → {EXPECTED.name}")

    print(f"\n结果 → {outfile}")
    return 1 if (violations or regressions) else 0


if __name__ == "__main__":
    sys.exit(main())
