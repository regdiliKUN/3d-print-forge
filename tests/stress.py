"""并发压测：同时提交多个任务，验证队列、内存闸门和进度上报不会互相踩。

运行：python tests/stress.py [并发数]
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

from app import config, jobs, store  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✔' if cond else '✖'} {name}" + (f"  {detail}" if detail else ""))


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    print(f"\n=== 并发压测（{n} 个任务）===\n")

    # 准备一批不同规模的模型
    proj_ids = []
    tmp = config.OUTPUTS / "_stress"
    tmp.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        sub = 3 + (i % 3)
        m = trimesh.creation.icosphere(subdivisions=sub, radius=10 + i * 3)
        f = tmp / f"s{i}.stl"
        m.export(str(f))
        proj_ids.append(store.create(f"s{i}.stl", f)["id"])

    from app import analysis

    proc = psutil.Process(os.getpid()) if psutil else None
    rss0 = proc.memory_info().rss / 1048576 if proc else 0
    t0 = time.perf_counter()

    submitted = [
        jobs.submit("analyze", f"压测 {i}", {"sample_faces": 800}, analysis.run_job, pid)
        for i, pid in enumerate(proj_ids)
    ]
    check("任务全部入队", all(j.status in ("queued", "running") for j in submitted),
          f"{len(submitted)} 个")

    deadline = time.time() + 240
    while time.time() < deadline:
        if all(j.status in ("done", "failed", "cancelled") for j in submitted):
            break
        time.sleep(0.3)

    dt = time.perf_counter() - t0
    done = [j for j in submitted if j.status == "done"]
    failed = [j for j in submitted if j.status == "failed"]
    rss1 = proc.memory_info().rss / 1048576 if proc else 0

    check("全部完成", len(done) == len(submitted), f"{len(done)}/{len(submitted)}")
    check("没有失败", not failed, "; ".join(j.error or "" for j in failed[:2])[:120])
    check("都有结果", all((j.result or {}).get("report") for j in done))
    check("进度都到 100%", all(abs(j.progress - 1.0) < 1e-6 for j in done))
    check("耗时合理", dt < 240, f"{dt:.1f}s")
    if proc:
        check("内存增长可控", (rss1 - rss0) < 1500, f"+{rss1 - rss0:.0f}MB")

    # 并发期间不能有任务卡在 running
    check("没有残留 running", not any(j.status == "running" for j in submitted))

    for pid in proj_ids:
        store.delete(pid)
    for f in tmp.glob("*"):
        f.unlink()
    tmp.rmdir()

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败  （{dt:.1f}s）")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
