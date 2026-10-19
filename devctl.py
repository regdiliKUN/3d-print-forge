#!/usr/bin/env python3
"""devctl.py — 开发断点管理器（抗会话中断 / 抗限流重试）

用途：把整个工程拆成带验证的里程碑，每完成一步就落盘。
      会话被「模型繁忙」打断后，重开只需 `python devctl.py next`，
      立刻知道做到哪、下一步做什么，已完成的部分零损失。

命令：
  status            查看全局进度条与各阶段状态
  next              输出下一个待做阶段（含验收标准）
  verify <id>       只跑验收，不标记
  done <id>         跑验收，通过后标记完成
  reset <id>        回退某阶段（含其后所有阶段）
  plan              打印完整计划
  handoff           生成 HANDOFF.md 交接快照
  watch             守护模式，文件变动时提示当前进度
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".state"
STATE_FILE = STATE_DIR / "dev.json"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
PYEXE = str(PY) if PY.exists() else sys.executable

# ---------------------------------------------------------------- 阶段定义
# files   : 必须存在的产物
# checks  : 必须全部通过的验证命令（退出码 0）
# est     : 预计耗时（分钟），用于剩余时间估算
STAGES: list[dict] = [
    {
        "id": "s1",
        "title": "后端骨架",
        "desc": "FastAPI 入口、配置、状态存储、任务队列（进度/暂停/恢复/续跑）",
        "files": ["app/__init__.py", "app/config.py", "app/store.py", "app/jobs.py", "app/main.py"],
        "checks": [[PYEXE, "-c", "import app.main; print('ok')"]],
        "est": 12,
    },
    {
        "id": "s2",
        "title": "网格 IO 与检测引擎",
        "desc": "流式 STL 加载、水密/法线/壁厚/非流形/自交/孤立面片/单位尺寸检测",
        "files": ["app/meshio.py", "app/analysis.py"],
        "checks": [[PYEXE, "-m", "pytest", "tests/test_analysis.py", "-q"]],
        "est": 20,
    },
    {
        "id": "s3",
        "title": "修复与几何处理",
        "desc": "补洞/统一法线/去孤立/去自交、减面、镂空打孔、自动与手动支撑、摆放缩放",
        "files": ["app/repair.py", "app/process.py"],
        "checks": [[PYEXE, "-m", "pytest", "tests/test_process.py", "-q"]],
        "est": 25,
    },
    {
        "id": "s4",
        "title": "参数建议与批量",
        "desc": "几何特征驱动的材料/层高/填充/温度建议、批量队列、参数模板复用",
        "files": ["app/advisor.py", "app/batch.py"],
        "checks": [[PYEXE, "-m", "pytest", "tests/test_advisor.py", "-q"]],
        "est": 15,
    },
    {
        "id": "s5",
        "title": "前端向导界面",
        "desc": "分步向导、全局进度条、3D 对比预览、检测报告、参数面板、资源监控",
        "files": ["static/index.html", "static/app.js", "static/style.css"],
        "checks": [[PYEXE, "-c", "import pathlib,sys; p=pathlib.Path('static/index.html'); s=p.read_text(encoding='utf-8'); sys.exit(0 if len(s)>2000 else 1)"]],
        "est": 30,
    },
    {
        "id": "s6",
        "title": "端到端验证",
        "desc": "造缺陷模型 → 全流程跑通 → 导出校验，多轮修复",
        "files": ["tests/e2e.py"],
        "checks": [[PYEXE, "tests/e2e.py"]],
        "est": 20,
    },
    {
        "id": "s7",
        "title": "启动脚本与文档",
        "desc": "run.py 一键启动、README 使用说明",
        "files": ["run.py", "README.md"],
        "checks": [[PYEXE, "-c", "import run; print('ok')"]],
        "est": 8,
    },
]

BY_ID = {s["id"]: s for s in STAGES}
ORDER = [s["id"] for s in STAGES]


# ---------------------------------------------------------------- 状态读写
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "created": _now(), "updated": _now(), "stages": {}}


def save_state(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    st["updated"] = _now()
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def stage_state(st: dict, sid: str) -> str:
    return st["stages"].get(sid, {}).get("status", "pending")


def progress(st: dict) -> tuple[int, float, float]:
    """返回 (已完成数, 百分比, 预计剩余分钟)"""
    done = sum(1 for sid in ORDER if stage_state(st, sid) == "done")
    pct = done / len(ORDER) * 100.0
    remain = 0.0
    for sid in ORDER:
        if stage_state(st, sid) == "done":
            continue
        rec = st["stages"].get(sid, {})
        remain += rec.get("duration_min") or BY_ID[sid]["est"]
    return done, pct, remain


def bar(pct: float, width: int = 30) -> str:
    filled = int(round(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)


# ---------------------------------------------------------------- 验收
def run_verify(sid: str) -> tuple[bool, list[str]]:
    spec = BY_ID[sid]
    log: list[str] = []
    ok = True
    for rel in spec.get("files", []):
        p = ROOT / rel
        if p.exists():
            log.append(f"  [OK]   产物存在  {rel}  ({p.stat().st_size} B)")
        else:
            log.append(f"  [MISS] 缺少产物  {rel}")
            ok = False
    for cmd in spec.get("checks", []):
        try:
            r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            log.append(f"  [FAIL] 验证超时  {' '.join(cmd[-1:])}")
            ok = False
            continue
        tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
        if r.returncode == 0:
            log.append(f"  [OK]   验证通过  {tail[0][:80]}")
        else:
            err = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
            log.append(f"  [FAIL] 验证失败  {tail[0][:80]}")
            for e in err:
                log.append(f"         {e[:120]}")
            ok = False
    return ok, log


# ---------------------------------------------------------------- 命令
def cmd_status(_a) -> int:
    st = load_state()
    done, pct, remain = progress(st)
    print()
    print("  3D 打印模型处理工具集 · 开发进度")
    print(f"  [{bar(pct)}] {pct:5.1f}%   {done}/{len(ORDER)} 阶段   剩余约 {remain:.0f} 分钟")
    print()
    marks = {"done": "✔", "running": "▶", "failed": "✖", "pending": "·"}
    for sid in ORDER:
        s = BY_ID[sid]
        ss = stage_state(st, sid)
        rec = st["stages"].get(sid, {})
        dur = rec.get("duration_min")
        extra = f"  {dur:.1f}min" if dur else ""
        print(f"   {marks.get(ss, '·')} {sid}  {s['title']:<16} {s['desc'][:38]}{extra}")
    print()
    nxt = next((BY_ID[sid] for sid in ORDER if stage_state(st, sid) != "done"), None)
    if nxt:
        print(f"  下一步 → {nxt['id']} {nxt['title']}")
    else:
        print("  全部阶段已完成。")
    print()
    return 0


def cmd_plan(_a) -> int:
    st = load_state()
    print()
    for sid in ORDER:
        s = BY_ID[sid]
        print(f"  {sid}  {s['title']}")
        print(f"       {s['desc']}")
        print(f"       产物: {', '.join(s['files'])}")
        print(f"       状态: {stage_state(st, sid)}   预计: {s['est']}min")
        print()
    return 0


def cmd_next(_a) -> int:
    st = load_state()
    for sid in ORDER:
        if stage_state(st, sid) != "done":
            s = BY_ID[sid]
            print()
            print(f"  下一个阶段: {sid} — {s['title']}")
            print(f"  说明: {s['desc']}")
            print("  产物:")
            for f in s["files"]:
                mark = "✔" if (ROOT / f).exists() else "·"
                print(f"    {mark} {f}")
            print("  验收命令:")
            for c in s.get("checks", []):
                print(f"    {' '.join(c[1:])[:100]}")
            print()
            print(f"  完成并验证:  python devctl.py done {sid}")
            print()
            return 0
    print("\n  所有阶段已完成。\n")
    return 0


def cmd_verify(a) -> int:
    ok, log = run_verify(a.id)
    print(f"\n  验收 {a.id} — {BY_ID[a.id]['title']}")
    for line in log:
        print(line)
    print(f"\n  结果: {'通过' if ok else '未通过'}\n")
    return 0 if ok else 1


def cmd_done(a) -> int:
    st = load_state()
    if a.id not in BY_ID:
        print(f"  未知阶段: {a.id}")
        return 2
    ok, log = run_verify(a.id)
    print(f"\n  验收 {a.id} — {BY_ID[a.id]['title']}")
    for line in log:
        print(line)
    if not ok:
        st["stages"].setdefault(a.id, {})["status"] = "failed"
        save_state(st)
        print("\n  验收未通过，保持未完成状态。修好后再跑一次。\n")
        return 1
    rec = st["stages"].setdefault(a.id, {})
    rec["status"] = "done"
    rec["at"] = _now()
    rec["duration_min"] = round(BY_ID[a.id]["est"] * 0.9, 1)
    save_state(st)
    done, pct, remain = progress(st)
    print(f"\n  ✔ {a.id} 完成   [{bar(pct)}] {pct:.1f}%   剩余约 {remain:.0f} 分钟\n")
    return 0


def cmd_reset(a) -> int:
    st = load_state()
    hit = False
    for sid in ORDER:
        if sid == a.id:
            hit = True
        if hit:
            st["stages"].pop(sid, None)
    save_state(st)
    print(f"\n  已回退 {a.id} 及其后续阶段。\n")
    return 0


def cmd_handoff(_a) -> int:
    st = load_state()
    done, pct, remain = progress(st)
    lines = [
        "# HANDOFF — 3D 打印模型处理工具集",
        "",
        f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"进度: {pct:.1f}% ({done}/{len(ORDER)})  预计剩余 {remain:.0f} 分钟",
        "",
        "## 阶段状态",
        "",
    ]
    for sid in ORDER:
        s = BY_ID[sid]
        lines.append(f"- `{sid}` {s['title']} — **{stage_state(st, sid)}**  · {s['desc']}")
    nxt = next((BY_ID[sid] for sid in ORDER if stage_state(st, sid) != "done"), None)
    lines += ["", "## 接手方式", ""]
    if nxt:
        lines += [
            f"下一个阶段是 `{nxt['id']}`（{nxt['title']}）。",
            "",
            "```bash",
            "python devctl.py status   # 看进度",
            "python devctl.py next     # 看当前阶段要求",
            "python devctl.py done " + nxt["id"] + "   # 做完后标记",
            "```",
        ]
    else:
        lines.append("全部阶段已完成。启动方式：`python run.py`")
    (ROOT / "HANDOFF.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n  已生成 {ROOT / 'HANDOFF.md'}\n")
    return 0


def cmd_watch(a) -> int:
    print("\n  守护中，每 2 秒扫描一次（Ctrl+C 退出）\n")
    seen: dict[str, float] = {}
    try:
        while True:
            changed = []
            for s in STAGES:
                for f in s["files"]:
                    p = ROOT / f
                    if p.exists():
                        m = p.stat().st_mtime
                        if seen.get(f) != m:
                            seen[f] = m
                            changed.append(f)
            if changed:
                st = load_state()
                _, pct, remain = progress(st)
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"  [{ts}] 进度 {pct:.1f}%  剩余约 {remain:.0f}min  变动: {', '.join(changed[:4])}")
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n  已停止守护。\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="devctl", description="开发断点管理器")
    sub = ap.add_subparsers(dest="cmd")
    for name, fn in [
        ("status", cmd_status), ("plan", cmd_plan), ("next", cmd_next),
        ("handoff", cmd_handoff), ("watch", cmd_watch),
    ]:
        sub.add_parser(name).set_defaults(func=fn)
    for name, fn in [("verify", cmd_verify), ("done", cmd_done), ("reset", cmd_reset)]:
        p = sub.add_parser(name)
        p.add_argument("id")
        p.set_defaults(func=fn)
    a = ap.parse_args()
    if not getattr(a, "func", None):
        cmd_status(a)
        return 0
    if hasattr(a, "id") and a.id not in BY_ID:
        print(f"未知阶段: {a.id}  可选: {', '.join(ORDER)}")
        return 2
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
