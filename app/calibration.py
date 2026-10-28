"""打印结果反馈与经验贝叶斯标定。

物理模型给的是「理论上该是多少」，但每台机器的光源衰减、料盘松紧、
料的实际密度都不一样。这个模块负责把「实际打出来的」攒下来，
反推一个修正系数盖回物理模型上。

为什么用经验贝叶斯而不是直接取平均：
    新机型只打过 1~2 件，直接取平均会被单个偶然结果带跑；
    完全不信它又白攒了数据。所以按样本量做收缩——
        权重 w = n / (n + K)
        修正后的对数偏差 mu = w·组均值 + (1-w)·全局均值
    n 小的时候 mu 贴近全局经验（大家都差不多），
    n 大的时候 mu 贴近这台机器自己的历史（它确实不一样）。
    K 是「先验相当于几条样本」的强度，越大越保守。

冷启动（一条记录都没有）时 w=0、mu=全局均值=0，
修正系数正好是 1.0，等价于完全相信物理模型，不会退化。
"""
from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime, timezone

from . import config

_LOCK = threading.RLock()
_FILE = config.STATE / "calibration.json"
MAX_RECORDS = 500

# 先验强度：相当于 K 条「全局经验」样本。K 越大，单机数据要攒越多才敢信。
PRIOR_STRENGTH = 4.0

# 残差对数标准差的下限。即使数据完全吻合也不给零宽度区间——
# 那是过度自信，实测里从没出现过。
MIN_LOG_SD = 0.05

# 冷启动（无任何记录）时的默认对数标准差，约 ±25%。
DEFAULT_LOG_SD = 0.22

# P10/P90 对应的正态分位数
Z80 = 1.2816

KEYS = ("hours", "grams")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _blank() -> dict:
    return {"version": 1, "records": []}


def load() -> dict:
    with _LOCK:
        if not _FILE.exists():
            return _blank()
        try:
            data = json.loads(_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("records"), list):
                return _blank()
            return data
        except Exception:
            return _blank()


def _write(data: dict) -> dict:
    _FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _FILE)
    return data


def add_record(process: str, printer: str, material: str, pred: dict,
               actual: dict, result: str = "success", note: str = "") -> dict:
    """记一条「预测 vs 实际」。返回值里带上这条算出的偏差。"""
    rec = {
        "at": _now(),
        "process": str(process or ""),
        "printer": str(printer or ""),
        "material": str(material or ""),
        "pred": {k: _f(pred.get(k)) for k in KEYS},
        "actual": {k: _f(actual.get(k)) for k in KEYS},
        "result": "success" if str(result or "success").lower() == "success" else "fail",
        "note": str(note or "")[:200],
    }
    with _LOCK:
        data = load()
        data["records"].append(rec)
        if len(data["records"]) > MAX_RECORDS:
            data["records"] = data["records"][-MAX_RECORDS:]
        _write(data)
    out = dict(rec)
    out["ratios"] = {k: (round(_f(actual.get(k)) / _f(pred.get(k)), 4)
                         if _f(pred.get(k)) > 0 else None) for k in KEYS}
    return out


def clear() -> None:
    with _LOCK:
        _write(_blank())


def _f(x) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _ratios_for(records: list[dict], key: str) -> list[float]:
    """取出有效的对数偏差 log(实际/预测)。两边都得是正数才有意义。"""
    out: list[float] = []
    for r in records:
        p = _f((r.get("pred") or {}).get(key))
        a = _f((r.get("actual") or {}).get(key))
        if p > 0 and a > 0:
            out.append(math.log(a / p))
    return out


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _sd(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def estimate(process: str, printer: str, material: str, key: str) -> dict:
    """给出某个量（时长/耗材）的修正系数与区间宽度。

    先验固定为「零修正」（factor=1.0，即完全信物理模型），
    按本组样本量收缩：
        w = n / (n + K)
        mu = w · 本组对数偏差均值
        factor = exp(mu)

    先验不取全局均值，是因为样本少的时候全局均值自己都不可靠，
    拿它当先验等于把一台机器的偶然结果摊给所有机器。宁可保守。
    """
    data = load()
    recs = [r for r in data["records"] if isinstance(r, dict)]
    mine = [r for r in recs
            if r.get("process") == process
            and r.get("printer") == printer
            and r.get("material") == material]
    gvals = _ratios_for(mine, key)
    n_g = len(gvals)

    m_g = _mean(gvals)
    w = n_g / (n_g + PRIOR_STRENGTH)
    mu = w * m_g

    # 残差：样本少时 sd 不可靠，用一个随样本量收紧的保守下界兜底
    if n_g >= 3:
        sd = _sd([x - m_g for x in gvals])
    else:
        sd = 0.0
    sd = math.sqrt(sd * sd + (1.0 - w) * DEFAULT_LOG_SD * DEFAULT_LOG_SD)
    sd = max(sd, MIN_LOG_SD)

    # 标签必须跟算法一致：这里**只用本组样本**，没有做任何跨机型池化。
    # 旧代码在 1~2 条记录时标成 "global"，上层据此在界面上写「本机历史修正」，
    # 实际上只是一条记录被 1/(1+8) 权重缩过——名字对不上做的事。
    src = "group" if n_g >= 3 else ("sparse" if n_g else "prior")
    gsuccess = [r for r in recs
                if r.get("process") == process and r.get("printer") == printer
                and r.get("material") == material and r.get("result")]
    rate = None
    if gsuccess:
        rate = round(sum(1 for r in gsuccess if r["result"] == "success") / len(gsuccess), 3)

    return {
        "factor": round(math.exp(mu), 4),
        "lo_mult": round(math.exp(-Z80 * sd), 4),
        "hi_mult": round(math.exp(Z80 * sd), 4),
        "n": n_g,
        "source": src,
        "spread": round(sd, 4),
        "success_rate": rate,
    }


def summary() -> dict:
    """标定总览：攒了多少条、覆盖哪些机型材料组合。"""
    data = load()
    recs = [r for r in data["records"] if isinstance(r, dict)]
    groups: dict[tuple, int] = {}
    for r in recs:
        key = (r.get("process"), r.get("printer"), r.get("material"))
        groups[key] = groups.get(key, 0) + 1
    ok = sum(1 for r in recs if r.get("result") == "success")
    return {
        "total": len(recs),
        "success": ok,
        "fail": len(recs) - ok,
        "rate": round(ok / len(recs) * 100, 1) if recs else 0.0,
        "groups": [
            {"process": p, "printer": pr, "material": m, "n": n}
            for (p, pr, m), n in sorted(groups.items(), key=lambda kv: -kv[1])
        ],
    }
