"""外部进程 RSS 采样器。

为什么不能在工作进程里自己采样：被测代码是 CPU 密集的 numpy/rtee 运算，
基本全程持有 GIL，同进程内的采样线程根本抢不到时间片，采出来的「峰值」会
严重偏低甚至全是同一个值。所以单独起一个进程，它读的是被测进程的 RSS，
由被测进程通过 stdin 发 S/E 标记告诉它「现在开始/结束测某某阶段」。

协议（stdin，每行一条）：
    S <stage>   开始测该阶段
    E <stage>   结束
    Q           退出并写出结果
启动后先往 stdout 打一行 READY，避免和worker 还没接上就开始采样。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=0.004)
    args = ap.parse_args()

    import psutil

    proc = psutil.Process(args.pid)
    state = {"cur": None}
    peaks: dict[str, dict] = {}
    done = {"v": False}

    def _rss() -> int:
        try:
            return proc.memory_info().rss
        except Exception:
            return 0

    def reader() -> None:
        while True:
            line = sys.stdin.readline()
            if not line:
                done["v"] = True
                return
            line = line.strip()
            if not line:
                continue
            if line == "Q":
                done["v"] = True
                return
            op, _, name = line.partition(" ")
            if op == "S":
                rec = peaks.setdefault(name, {"peak": 0, "base": 0, "samples": 0})
                rec["base"] = _rss()
                rec["peak"] = rec["base"]
                state["cur"] = name
            elif op == "E":
                state["cur"] = None

    threading.Thread(target=reader, daemon=True).start()
    print("READY", flush=True)

    while not done["v"]:
        name = state["cur"]
        if name:
            rss = _rss()
            rec = peaks.get(name)
            if rec is not None:
                rec["samples"] += 1
                if rss > rec["peak"]:
                    rec["peak"] = rss
        time.sleep(args.interval)

    # 收尾：再采一次，覆盖 Q 之前那段的尾巴
    out = {
        k: {
            "peak_mb": round(v["peak"] / 1048576, 2),
            "base_mb": round(v["base"] / 1048576, 2),
            "samples": v["samples"],
        }
        for k, v in peaks.items()
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f)


if __name__ == "__main__":
    main()
