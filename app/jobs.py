"""任务队列：进度上报、暂停/恢复/取消、断点续跑、资源闸门与并发控制。"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from . import config

try:
    import psutil  # type: ignore

    _HAS_PSUTIL = True
except Exception:  # pragma: no cover
    _HAS_PSUTIL = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 资源快照
_PROC = psutil.Process(os.getpid()) if _HAS_PSUTIL else None


def resource_snapshot() -> dict:
    """实时 CPU / 内存 / 进程占用。"""
    snap: dict[str, Any] = {
        "cpu_count": os.cpu_count() or 1,
        "cpu_percent": 0.0,
        "mem_total_mb": 0,
        "mem_used_mb": 0,
        "mem_percent": 0.0,
        "proc_mem_mb": 0.0,
        "proc_cpu_percent": 0.0,
        "threads": threading.active_count(),
        "gpu": detect_gpu(),
    }
    if _HAS_PSUTIL:
        try:
            vm = psutil.virtual_memory()
            snap.update(
                cpu_percent=psutil.cpu_percent(interval=None),
                mem_total_mb=int(vm.total / 1048576),
                mem_used_mb=int((vm.total - vm.available) / 1048576),
                mem_percent=float(vm.percent),
            )
            if _PROC is not None:
                snap["proc_mem_mb"] = round(_PROC.memory_info().rss / 1048576, 1)
                snap["proc_cpu_percent"] = _PROC.cpu_percent(interval=None)
        except Exception:
            pass
    rt = config.get().runtime
    snap["mem_limit_mb"] = rt.memory_limit_mb
    snap["mem_headroom_mb"] = max(0, rt.memory_limit_mb - int(snap["proc_mem_mb"]))
    snap["over_limit"] = rt.memory_limit_mb > 0 and snap["proc_mem_mb"] > rt.memory_limit_mb
    return snap


_GPU_CACHE: dict | None = None


def detect_gpu() -> dict:
    """探测可用 GPU 后端。没有就是没有，不假装。"""
    global _GPU_CACHE
    if _GPU_CACHE is not None:
        return _GPU_CACHE
    info = {"available": False, "backend": None, "name": None, "vram_mb": 0}
    try:
        import cupy  # type: ignore

        n = cupy.cuda.runtime.getDeviceCount()
        if n > 0:
            props = cupy.cuda.runtime.getDeviceProperties(0)
            info = {
                "available": True,
                "backend": "cupy",
                "name": props["name"].decode() if isinstance(props["name"], bytes) else str(props["name"]),
                "vram_mb": int(props["totalGlobalMem"] / 1048576),
            }
    except Exception:
        pass
    if not info["available"]:
        try:
            import torch  # type: ignore

            if torch.cuda.is_available():
                info = {
                    "available": True,
                    "backend": "torch",
                    "name": torch.cuda.get_device_name(0),
                    "vram_mb": int(torch.cuda.get_device_properties(0).total_memory / 1048576),
                }
        except Exception:
            pass
    _GPU_CACHE = info
    return info


# ---------------------------------------------------------------- 异常
class JobCancelled(Exception):
    pass


# ---------------------------------------------------------------- 上下文
class JobContext:
    """交给 worker 的唯一句柄：上报进度、检查暂停、记录断点。"""

    def __init__(self, job: "Job"):
        self.job = job

    # -- 进度 ------------------------------------------------------
    def tick(self, frac: float | None = None, msg: str = "", stage: str | None = None) -> None:
        j = self.job
        while j._pause_evt.is_set() is False:
            if j._cancel_evt.is_set():
                raise JobCancelled()
            j.status = "paused"
            time.sleep(0.08)
        if j._cancel_evt.is_set():
            raise JobCancelled()
        j.status = "running"
        if frac is not None:
            j.progress = max(j.progress, min(1.0, float(frac)))
        if msg:
            j.message = msg
        if stage:
            j.stage = stage
        j._touch()

    def log(self, msg: str) -> None:
        self.job.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        if len(self.job.logs) > 400:
            del self.job.logs[:100]

    # -- 断点 ------------------------------------------------------
    def step_done(self, name: str) -> dict | None:
        """已完成则返回该步产物数据，用于跳过重算。"""
        rec = self.job.steps.get(name)
        if rec and rec.get("status") == "done":
            self.log(f"断点续跑：跳过 {name}")
            return rec.get("data") or {}
        return None

    def complete_step(self, name: str, data: dict | None = None) -> None:
        self.job.steps[name] = {"status": "done", "at": _now(), "data": data or {}}
        self.job._persist()

    def fail_step(self, name: str, reason: str) -> None:
        self.job.steps[name] = {"status": "failed", "at": _now(), "reason": reason}
        self.job._persist()


# ---------------------------------------------------------------- 任务
class Job:
    def __init__(self, kind: str, title: str, params: dict, project_id: str | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.title = title
        self.params = params
        self.project_id = project_id
        self.status = "pending"      # pending|queued|running|paused|done|failed|cancelled
        self.progress = 0.0
        self.stage = "等待中"
        self.message = ""
        self.created = _now()
        self.started: str | None = None
        self.finished: str | None = None
        self.error: str | None = None
        self.result: dict = {}
        self.logs: list[str] = []
        self.steps: dict[str, dict] = {}
        self.waiting_resource = False
        self._worker: Worker | None = None
        self._pause_evt = threading.Event()
        self._pause_evt.set()
        self._cancel_evt = threading.Event()
        self._last_touch = time.time()

    def _touch(self) -> None:
        self._last_touch = time.time()

    def _persist(self) -> None:
        try:
            JOB_FILE.parent.mkdir(parents=True, exist_ok=True)
            all_jobs = {}
            if JOB_FILE.exists():
                try:
                    all_jobs = json.loads(JOB_FILE.read_text(encoding="utf-8"))
                except Exception:
                    all_jobs = {}
            all_jobs[self.id] = self.to_dict(light=True)
            tmp = JOB_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(all_jobs, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, JOB_FILE)
        except Exception:
            pass

    def eta_seconds(self) -> float | None:
        if self.status not in ("running", "paused") or self.progress <= 0.02:
            return None
        try:
            t0 = datetime.fromisoformat(self.started).timestamp() if self.started else None
        except Exception:
            t0 = None
        if not t0:
            return None
        elapsed = time.time() - t0
        return max(0.0, elapsed / self.progress * (1 - self.progress))

    def to_dict(self, light: bool = False) -> dict:
        d = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "project_id": self.project_id,
            "status": self.status,
            "progress": round(self.progress, 4),
            "percent": round(self.progress * 100, 1),
            "stage": self.stage,
            "message": self.message,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "error": self.error,
            "eta": self.eta_seconds(),
            "steps": {k: {"status": v.get("status"), "at": v.get("at")} for k, v in self.steps.items()},
            "waiting_resource": self.waiting_resource,
        }
        if not light:
            d["result"] = self.result
            d["logs"] = self.logs[-120:]
            d["params"] = self.params
        return d


JOB_FILE = config.STATE / "jobs.json"

Worker = Callable[[JobContext, dict], dict]


# ---------------------------------------------------------------- 管理器
class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._queue: deque[str] = deque()
        self._lock = threading.RLock()
        self._workers: list[threading.Thread] = []
        self._cv = threading.Condition(self._lock)
        self._running = 0

    # -- 生命周期 --------------------------------------------------
    def start(self) -> None:
        self._scale_workers()

    def _scale_workers(self) -> None:
        want = config.get().runtime.max_workers
        with self._lock:
            alive = [t for t in self._workers if t.is_alive()]
            self._workers = alive
            while len(self._workers) < want:
                t = threading.Thread(target=self._loop, name=f"job-{len(self._workers)}", daemon=True)
                t.start()
                self._workers.append(t)

    def submit(self, kind: str, title: str, params: dict, worker: Worker, project_id: str | None = None) -> Job:
        job = Job(kind, title, params, project_id)
        job._worker = worker  # 必须在入队前绑定，否则工作线程可能取到空执行体
        with self._lock:
            self._jobs[job.id] = job
            job.status = "queued"
            self._queue.append(job.id)
            self._cv.notify()
        job._persist()
        self._scale_workers()
        return job

    def get(self, jid: str) -> Job | None:
        return self._jobs.get(jid)

    def list(self) -> list[dict]:
        with self._lock:
            return [j.to_dict() for j in sorted(self._jobs.values(), key=lambda x: x.created, reverse=True)]

    # -- 控制 ------------------------------------------------------
    def pause(self, jid: str) -> bool:
        j = self._jobs.get(jid)
        if not j or j.status not in ("running", "queued"):
            return False
        j._pause_evt.clear()
        j.status = "paused"
        j.message = "已暂停，可随时恢复"
        return True

    def resume(self, jid: str) -> bool:
        j = self._jobs.get(jid)
        if not j:
            return False
        if j.status == "paused":
            j._pause_evt.set()
            j.status = "running"
            j.message = "已恢复"
            return True
        # 失败/取消的任务 → 重新入队，从断点继续
        if j.status in ("failed", "cancelled"):
            j._cancel_evt.clear()
            j._pause_evt.set()
            j.error = None
            j.status = "queued"
            j.message = "从断点重新排队"
            with self._lock:
                self._queue.append(j.id)
                self._cv.notify()
            return True
        return False

    def cancel(self, jid: str) -> bool:
        j = self._jobs.get(jid)
        if not j:
            return False
        j._cancel_evt.set()
        j._pause_evt.set()
        if j.status == "queued":
            with self._lock:
                try:
                    self._queue.remove(j.id)
                except ValueError:
                    pass
            j.status = "cancelled"
            j.finished = _now()
        return True

    # -- 执行 ------------------------------------------------------
    def _next_job(self) -> Job | None:
        with self._lock:
            while self._queue:
                jid = self._queue.popleft()
                j = self._jobs.get(jid)
                if j and j.status in ("queued", "paused"):
                    return j
            return None

    def _loop(self) -> None:
        while True:
            job = None
            with self._lock:
                job = self._next_job()
                if job is None:
                    self._cv.wait(timeout=0.4)
                    continue
                self._running += 1
            try:
                self._run(job)
            finally:
                with self._lock:
                    self._running -= 1

    def _await_resources(self, job: Job) -> bool:
        """内存闸门：超限时排队等待，避免 OOM。"""
        rt = config.get().runtime
        if rt.memory_limit_mb <= 0:
            return True
        while True:
            if job._cancel_evt.is_set():
                return False
            snap = resource_snapshot()
            if snap["proc_mem_mb"] < rt.memory_limit_mb:
                job.waiting_resource = False
                return True
            job.waiting_resource = True
            job.status = "queued"
            job.stage = "等待内存资源"
            job.message = f"进程占用 {snap['proc_mem_mb']:.0f}MB / 上限 {rt.memory_limit_mb}MB，等待中"
            time.sleep(0.5)

    def _run(self, job: Job) -> None:
        if not self._await_resources(job):
            job.status = "cancelled"
            job.finished = _now()
            job._persist()
            return
        ctx = JobContext(job)
        job.status = "running"
        job.started = job.started or _now()
        job.stage = "开始"
        job.message = ""
        job._persist()
        fn: Worker | None = getattr(job, "_worker", None)
        if fn is None:
            job.status = "failed"
            job.error = "任务缺少执行体"
            job.finished = _now()
            job._persist()
            return
        try:
            ctx.log(f"任务开始：{job.title}")
            res = fn(ctx, job.params) or {}
            job.result = res
            job.progress = 1.0
            job.status = "done"
            job.stage = "完成"
            job.message = "处理完成"
            ctx.log("任务完成")
        except JobCancelled:
            job.status = "cancelled"
            job.stage = "已中断"
            job.message = "用户中断，可从断点续跑"
            ctx.log("任务被中断")
        except Exception as e:
            job.status = "failed"
            job.error = f"{type(e).__name__}: {e}"
            job.stage = "失败"
            job.message = job.error
            ctx.log("失败：" + job.error)
            job.logs.append(traceback.format_exc()[-1500:])
        finally:
            job.finished = _now()
            job.waiting_resource = False
            job._persist()
            if config.get().runtime.auto_gc:
                import gc

                gc.collect()


MANAGER = JobManager()


def submit(kind: str, title: str, params: dict, worker: Worker, project_id: str | None = None) -> Job:
    return MANAGER.submit(kind, title, params, worker, project_id)
