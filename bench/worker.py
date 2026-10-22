"""单个模型的基准执行器（跑在独立进程里，保证内存归因干净）。

要点：
1. 每个阶段单独计时、单独测内存，阶段之间**清空全部缓存**再 gc，
   否则测出来的是「缓存命中后的第二次」，优化会被自己的缓存假象掩盖。
2. 每个阶段重复 repeats 次，取中位数——单次结果在这台机器上抖动能有 20%。
3. 网格加载不计入被测阶段（记为 prep_ms），因为它本身是另一个被测阶段。
4. 同时记录结果指纹，让「变快了但算错了」无处可藏。
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402

# 提前把重依赖全部导入，把 import 开销排除在计时之外
from app import (  # noqa: E402,F401
    analysis,
    meshio,
    printmodel,
    process,
    repair,
    voxelops,
)
from app.main import _support_density_field  # noqa: E402,F401

from bench import digest, stages  # noqa: E402

MB = 1048576


def clear_all_caches() -> None:
    """把所有模块级缓存清空，确保测的是冷路径。

    这里做**两次** gc.collect()：第一次回收直接引用，第二次回收因 finalizer
    连锁释放的对象（例如带 __del__ 的几何中间体）。只收一次时，上一阶段刚释放
    的大数组可能还没真正归还给堆，于是下一阶段测 base_mb 时堆仍偏高，而采样器
    又会在下一阶段的 S/E 窗口里抓到那个「残影」峰值——表现为同一阶段有时 +1MB、
    有时 +328MB 的离谱抖动（基准自测中已观察到）。彻底回收后再测 base_mb 能把这种
    跨阶段内存归属噪声压下去。
    """
    try:
        meshio.clear_cache()
    except Exception:
        pass
    try:
        analysis._RAY_INDEX_CACHE.clear()
    except Exception:
        pass
    try:
        from app import advisor

        advisor._MESH_CACHE.clear()
        advisor._SECTIONS_CACHE.clear()
    except Exception:
        pass
    gc.collect()
    gc.collect()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--models-dir", default=str(Path(__file__).resolve().parent / "models"))
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--stages", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--sampler-out", default="")
    args = ap.parse_args()

    self_proc = psutil.Process(os.getpid())
    rss = lambda: self_proc.memory_info().rss  # noqa: E731

    models_dir = Path(args.models_dir)
    path = models_dir / f"{args.model}.stl"
    manifest = json.loads((Path(__file__).resolve().parent / "model_manifest.json").read_text(encoding="utf-8"))
    rec = manifest["models"][args.model]

    want = [s for s in (args.stages.split(",") if args.stages else stages.MATRIX[args.model]) if s]

    sampler_out = args.sampler_out or (str(Path(args.out).with_suffix("")) + ".mem.json")
    sampler = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().parent / "mem_sampler.py"),
         "--pid", str(os.getpid()), "--out", sampler_out],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
    )
    sampler.stdout.readline()  # 等 READY

    results: dict[str, dict] = {}
    for stage in want:
        fn = stages.STAGES[stage]
        ms_list: list[float] = []
        prep_list: list[float] = []
        end_delta_list: list[float] = []
        base_list: list[float] = []
        fingerprint: dict = {}
        err = None
        for rep in range(args.repeats):
            clear_all_caches()
            mesh = None
            load_err = None
            if stage not in stages.NO_MESH:
                p0 = time.perf_counter()
                try:
                    mesh = meshio.load_mesh(path)
                except Exception as e:
                    import traceback

                    tb = traceback.format_exc().strip().splitlines()
                    load_err = (f"{type(e).__name__}: {e} @ "
                                f"{' | '.join(x.strip() for x in tb[-4:])}")
                prep_list.append((time.perf_counter() - p0) * 1000)
            b = rss()
            base_list.append(b / MB)
            sampler.stdin.write(f"S {stage}\n")
            sampler.stdin.flush()
            t0 = time.perf_counter()
            try:
                if load_err:
                    raise RuntimeError(load_err)
                raw, summary = fn(mesh, path, rec)
            except Exception as e:  # 记录失败但不让整个基准挂掉
                import traceback

                tb = traceback.format_exc().strip().splitlines()
                raw, summary = None, {"_error": f"{type(e).__name__}: {e}"}
                err = f"{type(e).__name__}: {e} @ {' | '.join(x.strip() for x in tb[-4:])}"
            elapsed = (time.perf_counter() - t0) * 1000
            e = rss()
            sampler.stdin.write(f"E {stage}\n")
            sampler.stdin.flush()
            ms_list.append(round(elapsed, 2))
            end_delta_list.append((e - b) / MB)
            if rep == 0:
                fingerprint = digest.collect(summary)
            raw = None
            del mesh, raw
            gc.collect()
            if err:
                break

        results[stage] = {
            "ms": ms_list,
            "median_ms": round(statistics.median(ms_list), 2) if ms_list else None,
            "min_ms": round(min(ms_list), 2) if ms_list else None,
            "max_ms": round(max(ms_list), 2) if ms_list else None,
            "prep_ms": round(statistics.median(prep_list), 2) if prep_list else 0.0,
            "base_mb": round(statistics.median(base_list), 2),
            "end_delta_mb": round(statistics.median(end_delta_list), 2),
            "fingerprint": fingerprint,
            "error": err,
        }
        print(f"    {args.model}/{stage}: {results[stage]['median_ms']} ms", flush=True)

    try:
        sampler.stdin.write("Q\n")
        sampler.stdin.flush()
    except Exception:
        pass
    sampler.wait(timeout=30)

    mem: dict = {}
    try:
        mem = json.loads(Path(sampler_out).read_text(encoding="utf-8"))
    except Exception:
        pass

    for stage, r in results.items():
        m = mem.get(stage) or {}
        r["peak_mb"] = m.get("peak_mb")
        r["peak_delta_mb"] = (round(m["peak_mb"] - r["base_mb"], 2)
                              if m.get("peak_mb") is not None else None)

    Path(args.out).write_text(
        json.dumps({"model": args.model, "faces": rec["faces"], "stages": results},
                   ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
