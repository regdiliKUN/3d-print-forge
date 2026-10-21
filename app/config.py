"""全局配置：路径、运行时资源上限、检测阈值。"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
OUTPUTS = DATA / "outputs"
STATE = DATA / "state"
PROJECTS = STATE / "projects"
RUNTIME_FILE = STATE / "runtime.json"

for _p in (DATA, UPLOADS, OUTPUTS, STATE, PROJECTS):
    _p.mkdir(parents=True, exist_ok=True)


@dataclass
class Runtime:
    """可在界面上调整的运行时资源策略。"""

    max_workers: int = 2              # 同时处理的模型数（并发）
    thread_workers: int = 0           # 单模型内部线程数，0 = 自动
    memory_limit_mb: int = 4096       # 内存上限，超限则排队等待
    vram_limit_mb: int = 0            # 显存上限，0 = 不限制
    chunk_faces: int = 200_000        # 流式加载分块大小（面数）
    preview_faces: int = 60_000       # 前端预览网格上限
    auto_gc: bool = True              # 阶段间主动回收

    def normalized(self) -> "Runtime":
        cpu = os.cpu_count() or 4
        if self.thread_workers <= 0:
            self.thread_workers = max(1, min(8, cpu // max(1, self.max_workers)))
        self.max_workers = max(1, min(self.max_workers, 16))
        return self


@dataclass
class Thresholds:
    """检测阈值，决定严重程度分级。"""

    wall_warn: float = 0.8            # mm，低于此值告警
    wall_fail: float = 0.4            # mm，低于此值判为严重
    overhang_deg: float = 45.0        # 悬垂角阈值
    hole_max_edges: int = 10_000      # 补洞时允许的最大洞边界数
    tiny_volume: float = 1.0          # mm³，小于此体积的孤立片视为碎屑
    min_feature: float = 0.05         # mm，低于此尺寸的特征无法打印


@dataclass
class Config:
    runtime: Runtime = field(default_factory=Runtime)
    thresholds: Thresholds = field(default_factory=Thresholds)


_cfg = Config()


_ENV_MAP = {
    "TPF_MAX_WORKERS": ("runtime", "max_workers", int),
    "TPF_THREAD_WORKERS": ("runtime", "thread_workers", int),
    "TPF_MEMORY_LIMIT_MB": ("runtime", "memory_limit_mb", int),
    "TPF_VRAM_LIMIT_MB": ("runtime", "vram_limit_mb", int),
    "TPF_CHUNK_FACES": ("runtime", "chunk_faces", int),
    "TPF_PREVIEW_FACES": ("runtime", "preview_faces", int),
    "TPF_WALL_WARN": ("thresholds", "wall_warn", float),
    "TPF_WALL_FAIL": ("thresholds", "wall_fail", float),
    "TPF_OVERHANG_DEG": ("thresholds", "overhang_deg", float),
}


def _apply_env(cfg: "Config") -> None:
    """环境变量覆盖配置，方便部署时不用改文件。"""
    for key, (section, attr, cast) in _ENV_MAP.items():
        raw = os.environ.get(key)
        if raw is None or raw == "":
            continue
        try:
            setattr(getattr(cfg, section), attr, cast(raw))
        except (TypeError, ValueError):
            pass


def load() -> Config:
    global _cfg
    if RUNTIME_FILE.exists():
        try:
            raw = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
            for k, v in (raw.get("runtime") or {}).items():
                if hasattr(_cfg.runtime, k):
                    setattr(_cfg.runtime, k, v)
            for k, v in (raw.get("thresholds") or {}).items():
                if hasattr(_cfg.thresholds, k):
                    setattr(_cfg.thresholds, k, v)
        except Exception:
            pass
    _apply_env(_cfg)
    _cfg.runtime.normalized()
    return _cfg


def save() -> None:
    RUNTIME_FILE.write_text(
        json.dumps(
            {"runtime": asdict(_cfg.runtime), "thresholds": asdict(_cfg.thresholds)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def get() -> Config:
    return _cfg


load()
