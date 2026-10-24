"""清理数据目录里没人引用的文件。

测试跑多了 data/outputs 会积累几百个 STL。这里只删「不被任何项目引用」的文件，
并且默认先干跑，确认后再加 --apply 真删。

用法：
    python tools/cleanup.py            # 只看会删什么
    python tools/cleanup.py --apply    # 真删
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config, store  # noqa: E402


def referenced() -> set[Path]:
    """所有被项目引用的文件：原始文件、工作副本、历史产出、分件文件。"""
    keep: set[Path] = set()
    for item in store.list_all():
        p = store.get(item["id"])
        if not p:
            continue
        for key in ("source", "current", "original"):
            v = p.get(key)
            if v:
                keep.add(Path(v).resolve())
        for h in p.get("history") or []:
            v = h.get("out")
            if v:
                keep.add(Path(v).resolve())
        asm = p.get("assembly") or {}
        for f in asm.get("files") or []:
            keep.add(Path(f).resolve())
    return keep


def scan(dirs: list[Path], keep: set[Path], min_age_hours: float) -> list[tuple[Path, int]]:
    now = time.time()
    out = []
    for d in dirs:
        if not d.exists():
            continue
        for f in d.iterdir():
            if not f.is_file():
                continue
            if f.resolve() in keep:
                continue
            age_h = (now - f.stat().st_mtime) / 3600
            if age_h < min_age_hours:
                continue
            out.append((f, f.stat().st_size))
    return sorted(out, key=lambda x: -x[1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的删除（默认只列出）")
    ap.add_argument("--min-age", type=float, default=1.0, help="只清理多少小时前的文件，默认 1")
    ap.add_argument("--keep-zip", action="store_true", help="保留 zip 打包文件")
    args = ap.parse_args()

    keep = referenced()
    dirs = [config.OUTPUTS, config.UPLOADS]
    victims = scan(dirs, keep, args.min_age)
    if args.keep_zip:
        victims = [(f, s) for f, s in victims if f.suffix != ".zip"]

    total = sum(s for _, s in victims)
    print(f"\n被项目引用：{len(keep)} 个文件")
    print(f"可清理：{len(victims)} 个，共 {total / 1048576:.1f} MB\n")
    for f, s in victims[:30]:
        print(f"  {s / 1024:8.0f} KB  {f.name}")
    if len(victims) > 30:
        print(f"  … 还有 {len(victims) - 30} 个")

    if not args.apply:
        print("\n这是干跑。确认无误后加 --apply 真正删除。")
        return 0

    removed = 0
    for f, _ in victims:
        try:
            f.unlink()
            removed += 1
        except Exception as e:
            print(f"  删除失败 {f.name}: {e}")
    print(f"\n已删除 {removed} 个文件，释放 {total / 1048576:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
