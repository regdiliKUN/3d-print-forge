"""FastAPI 入口：上传、检测、修复、处理、建议、批量、任务控制、导出。

重型几何库（numpy/trimesh/manifold3d）全部在路由内部延迟导入，
保证服务启动只需几十毫秒，不会因为加载几何栈而卡住界面。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import time as _time
import uuid
import uuid as _uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, jobs, store

app = FastAPI(title="3D 打印模型处理工具集", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC = config.ROOT / "static"

LOG_DIR = config.DATA / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "app.log"

_logger = logging.getLogger("tpf")
if not _logger.handlers:
    _handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%Y-%m-%d %H:%M:%S"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


@app.middleware("http")
async def version_rewrite(request, call_next):
    """把 /api/v1/xxx 改写成 /api/xxx。

    现在只有 v1，但先把入口留出来：以后真要改接口时，
    老的 /api/v1 可以继续指向兼容实现，客户端不用一起升级。
    """
    path = request.url.path
    if path.startswith("/api/v1/"):
        request.scope["path"] = "/api/" + path[len("/api/v1/"):]
    return await call_next(request)


@app.middleware("http")
async def trace_middleware(request, call_next):
    """给每个请求一个 trace id，写进日志和响应头，出问题时能顺着 ID 查。"""
    rid = _uuid.uuid4().hex[:8]
    t0 = _time.time()
    try:
        response = await call_next(request)
    except Exception:
        _logger.exception(f"[{rid}] {request.method} {request.url.path} 未捕获异常")
        raise
    ms = (_time.time() - t0) * 1000
    if request.url.path.startswith("/api"):
        lvl = logging.WARNING if response.status_code >= 400 else logging.INFO
        _logger.log(lvl, f"[{rid}] {request.method} {request.url.path} {response.status_code} {ms:.0f}ms")
    response.headers["X-Trace-Id"] = rid
    return response


# ---------------------------------------------------------------- 向导定义
WORKFLOW: list[dict] = [
    {
        "id": "import",
        "title": "导入模型",
        "goal": "把 STL / OBJ / 3MF 读进来，建立工作副本。原文件永不覆盖。",
        "why": "后续所有检测与修改都作用在工作副本上，改坏了可以随时回退到导入态。",
        "params": [],
        "next_hint": "导入后先跑检测。别急着修——先看清问题全貌，避免修一处坏一处。",
    },
    {
        "id": "inspect",
        "title": "模型检测",
        "goal": "七项体检：水密性、法线方向、壁厚、非流形边、自相交、孤立面片、单位与尺寸。",
        "why": "切片软件对这些问题容错极差。水密性决定能否算出内外，壁厚决定能否打出来，单位错 25.4 倍是最常见的低级事故。",
        "params": [
            {"name": "sample_faces", "label": "壁厚采样面数", "type": "int", "default": 1500,
             "min": 500, "max": 80000, "unit": "面",
             "help": "沿每个采样面法线反向打射线量厚度。采样越多越准，也越慢。"},
            {"name": "check_self_intersection", "label": "检测自相交", "type": "bool", "default": True,
             "help": "用 AABB 候选对 + 三角形精确相交测试。大模型上较耗时，可关闭。"},
        ],
        "next_hint": "报告里每一项都带修复按钮。优先修「严重」级，再处理「警告」级。",
    },
    {
        "id": "repair",
        "title": "修复",
        "goal": "补洞、统一法线、移除碎屑、合并重复顶点、消解自相交。",
        "why": "这些是「让模型变成合法实体」的动作，必须在减面和镂空之前做——布尔运算要求输入是闭合流形。",
        "params": [
            {"name": "actions", "label": "修复动作", "type": "multi", "default": ["fill_holes", "fix_normals"],
             "options": ["fill_holes", "fix_normals", "remove_small", "merge_vertices", "resolve_self_intersection"],
             "help": "可多选。建议顺序：合并顶点 → 补洞 → 统一法线 → 移除碎屑 → 消解自交。"},
            {"name": "small_ratio", "label": "碎屑阈值", "type": "float", "default": 0.01,
             "min": 0.0001, "max": 0.5, "unit": "占最大体积比",
             "help": "体积小于最大连通体此比例的部件被判定为碎屑并删除。"},
        ],
        "next_hint": "修完回到检测页复检。水密性没变成「通过」就不要进下一步。",
    },
    {
        "id": "process",
        "title": "处理",
        "goal": "减面、镂空打孔、加支撑、摆放朝向、缩放。",
        "why": "减面降低切片负担；镂空省树脂但要留排液孔；支撑和朝向直接决定成败。",
        "params": [
            {"name": "decimate_ratio", "label": "减面保留比", "type": "float", "default": 1.0,
             "min": 0.05, "max": 1.0,
             "help": "1.0 不减面。0.3 表示保留 30% 面数。细节件别低于 0.5。"},
            {"name": "hollow", "label": "镂空", "type": "bool", "default": False,
             "help": "生成内壳并与外壳合并。要求模型已水密。"},
            {"name": "wall_thickness", "label": "壳厚", "type": "float", "default": 2.0,
             "min": 0.4, "max": 10.0, "unit": "mm",
             "help": "光固化建议 1.5–2.5mm。低于 0.8mm 易在清洗时压塌。"},
            {"name": "drain_holes", "label": "排液孔数量", "type": "int", "default": 2,
             "min": 0, "max": 12, "unit": "个",
             "help": "镂空必须开孔，否则树脂排不出、气压顶住会炸件。至少 2 个且不在同一水平面。"},
            {"name": "hole_diameter", "label": "孔径", "type": "float", "default": 3.0,
             "min": 1.0, "max": 12.0, "unit": "mm",
             "help": "光固化 2.5–4mm 够用。太小洗不干净，太大影响外观。"},
            {"name": "supports", "label": "支撑", "type": "select", "default": "none",
             "options": ["none", "auto", "manual"],
             "help": "auto 按悬垂角自动生成柱状支撑；manual 用点选坐标。"},
            {"name": "overhang_deg", "label": "悬垂角阈值", "type": "float", "default": 45.0,
             "min": 20.0, "max": 80.0, "unit": "°",
             "help": "面法线与竖直方向夹角超过此值判为悬垂。光固化常用 40–50°。"},
            {"name": "orient", "label": "自动摆放", "type": "bool", "default": True,
             "help": "枚举候选朝向，选投影面积大、高度低、悬垂少的姿态。"},
            {"name": "scale", "label": "缩放", "type": "float", "default": 1.0,
             "min": 0.05, "max": 20.0, "unit": "×",
             "help": "整体等比缩放。也可用 target_height 指定目标高度。"},
        ],
        "next_hint": "处理完看一眼前后对比。镂空后务必确认排液孔真的打穿了。",
    },
    {
        "id": "advise",
        "title": "参数建议",
        "goal": "根据几何特征给出材料、层高、填充率、温度，并说明依据。",
        "why": "参数不是拍脑袋定的。最小特征尺寸决定层高上限，投影面积决定底层曝光，体积决定填充策略。",
        "params": [],
        "next_hint": "把推荐参数导出成 JSON，可直接对照着在切片软件里填。",
    },
    {
        "id": "export",
        "title": "导出",
        "goal": "导出 STL、参数配置、操作日志与错误记录。",
        "why": "模型给切片软件，参数配置留档，日志用于复现问题和批量套用。",
        "params": [],
        "next_hint": "批量任务可以直接套用这次导出的参数模板。",
    },
]


def _job_out(job: jobs.Job) -> dict:
    return job.to_dict()


def _spawn(kind: str, title: str, params: dict, worker, pid: str | None) -> dict:
    job = jobs.submit(kind, title, params, worker, pid)
    return _job_out(job)


# ---------------------------------------------------------------- 基础
@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": app.version, "time": datetime.now().isoformat(timespec="seconds")}


@app.get("/api/system/resources")
def system_resources() -> dict:
    return jobs.resource_snapshot()


@app.get("/api/config")
def get_config() -> dict:
    from dataclasses import asdict

    c = config.get()
    return {"runtime": asdict(c.runtime), "thresholds": asdict(c.thresholds)}


@app.post("/api/config")
def set_config(payload: dict = Body(...)) -> dict:
    c = config.get()
    for k, v in (payload.get("runtime") or {}).items():
        if hasattr(c.runtime, k):
            setattr(c.runtime, k, v)
    for k, v in (payload.get("thresholds") or {}).items():
        if hasattr(c.thresholds, k):
            setattr(c.thresholds, k, v)
    c.runtime.normalized()
    config.save()
    jobs.MANAGER._scale_workers()
    from dataclasses import asdict

    return {"ok": True, "runtime": asdict(c.runtime), "thresholds": asdict(c.thresholds)}


@app.get("/api/shortcut")
def shortcut_info() -> dict:
    """桌面快捷方式当前状态（是否存在、桌面路径、启动器路径）。"""
    from . import shortcut

    return shortcut.shortcut_status()


@app.post("/api/shortcut")
def make_shortcut() -> dict:
    """在桌面创建/覆盖一键启动的快捷方式（目标在项目所在盘，如 I 盘）。"""
    from . import shortcut

    result = shortcut.create_desktop_shortcut()
    _logger.info("创建桌面快捷方式：%s", result)
    return result


@app.get("/api/workflow")
def workflow() -> dict:
    return {"steps": WORKFLOW}


@app.get("/api/printers")
def list_printers(process: str | None = Query(None)) -> dict:
    """机型预设列表。界面用它渲染选择器。

    只回摘要字段，参数细节走 /api/printers/{key}，避免一次吐几十个机型的全量参数。
    """
    from . import printers

    items = printers.list_presets(process)
    return {
        "printers": items,
        "count": len(items),
        "defaults": dict(printers.DEFAULT_PRESET),
        "processes": sorted({x["process"] for x in items}),
    }


@app.get("/api/materials")
def list_materials(process: str | None = Query(None)) -> dict:
    """材料清单。前端下拉用它，名称也就只有这一个来源，不会两处漂移。"""
    from . import advisor

    proc = (process or "SLA").upper()
    table = advisor.SLA_MATERIALS if proc == "SLA" else advisor.FDM_MATERIALS
    items = []
    for key, m in table.items():
        items.append({
            "key": key,
            "name": m["name"],
            "note": m.get("note") or "",
            "process": proc,
            # 光固化给层高区间，FDM 给温度区间，前端按工艺显示不同副标题
            "layer_range": m.get("layer"),
            "nozzle_temp": m.get("nozzle"),
            "bed_temp": m.get("bed"),
            "density": m.get("density"),
        })
    default = "standard" if proc == "SLA" else "PLA"
    return {"materials": items, "process": proc,
            "default": default if default in table else next(iter(table), None)}


@app.get("/api/printers/{key}")
def get_printer(key: str) -> dict:
    """单个机型的完整参数，供界面展开查看。"""
    from . import printers

    merged, missing = printers.resolve(key)
    if merged.get("resolved_from") != "preset":
        # 404 里带上可用 key，调用方不用再猜；也便于对照老别名
        raise HTTPException(404, {
            "detail": f"没有这个机型预设：{key}",
            "hint": "可用 key 见 /api/printers；旧别名（sla_small 等）也已支持",
            "available": sorted(printers.PRESETS),
            "aliases": dict(printers.ALIASES),
        })
    merged["name"] = f"{merged['brand']} {merged['model']}"
    merged["missing"] = missing
    return merged


@app.get("/api/logs")
def recent_logs(lines: int = Query(120, ge=1, le=2000)) -> dict:
    if not LOG_FILE.exists():
        return {"lines": []}
    try:
        with LOG_FILE.open("r", encoding="utf-8", errors="ignore") as f:
            tail = f.readlines()[-lines:]
        return {"lines": [x.rstrip("\n") for x in tail]}
    except Exception as e:
        return {"lines": [], "error": str(e)}


# ---------------------------------------------------------------- 项目
SAFE = re.compile(r"[^\w\-.]+")


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)) -> dict:
    created = []
    for f in files:
        raw = Path(f.filename or "model.stl").name
        safe = SAFE.sub("_", raw)
        dest = config.UPLOADS / f"{uuid.uuid4().hex[:8]}_{safe}"
        size = 0
        with dest.open("wb") as out:
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                out.write(chunk)
        proj = store.create(safe, dest)
        proj["stats"] = {"size_bytes": size}
        store.save(proj)
        created.append(proj)
    return {"projects": created}


@app.post("/api/demo")
def create_demo_model() -> dict:
    """生成一个带典型缺陷的示例模型，新用户点一下就能走完整流程。

    故意做进四类问题：破洞、薄壁、碎屑、法线反转——
    这样检测报告、一键修复、热力图这些功能都有东西可看。
    """
    import numpy as np
    import trimesh

    body = trimesh.creation.icosphere(subdivisions=3, radius=20)
    keep = np.ones(len(body.faces), dtype=bool)
    keep[:6] = False  # 挖掉几片造成破洞
    body.update_faces(keep)

    # 0.5mm：明显低于 0.8mm 告警线，检测一定报出来（0.8 正好压在线上容易被判通过）
    thin = trimesh.creation.box(extents=(28, 28, 0.5))
    thin.apply_translation([0, 0, 22])

    debris = trimesh.creation.box(extents=(1.2, 1.2, 1.2))  # 碎屑
    debris.apply_translation([36, 0, 0])

    m = trimesh.util.concatenate([body, thin, debris])
    m.invert()  # 整体法线反转，模拟导出错误

    dest = config.UPLOADS / f"demo_{uuid.uuid4().hex[:8]}.stl"
    m.export(str(dest))
    proj = store.create("示例模型（破洞 / 薄壁 / 碎屑）", dest)
    store.update(proj["id"], tags=["示例"])
    return store.get(proj["id"]) or {}


@app.get("/api/projects")
def list_projects() -> dict:
    # size_bytes / file_type 已在 store.list_all 里算好
    return {"projects": store.list_all()}


@app.get("/api/projects/{pid}")
def get_project(pid: str) -> dict:
    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    return p


@app.get("/api/projects/{pid}/bundle")
def bundle_project(pid: str) -> Response:
    """把项目连模型和历史一起打包，换机器或备份用。"""
    import zipfile
    import io as _io

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    src = Path(p.get("current") or p.get("source") or "")
    if not src.exists():
        raise HTTPException(404, "模型文件不存在")

    meta = {k: v for k, v in p.items() if k not in ("history",)}
    meta["bundle_version"] = 1
    hist = p.get("history") or []

    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("project.json", json.dumps(meta, ensure_ascii=False, indent=2))
        z.write(src, f"model{src.suffix}")
        # 历史里的产出文件也带上，回滚才能在别的机器上生效
        for i, h in enumerate(hist):
            f = Path(h.get("out") or "")
            if f.exists() and f.stat().st_size < 80 * 1024 * 1024:
                z.write(f, f"history/{i:03d}{f.suffix}")
        z.writestr("history.json", json.dumps(hist, ensure_ascii=False, indent=2))
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{Path(p["name"]).stem}.zip"'},
    )


@app.post("/api/projects/import-bundle")
async def import_bundle(file: UploadFile = File(...)) -> dict:
    """导入项目包。防 zip slip：成员路径不允许绝对路径或 .."""
    import zipfile
    import io as _io

    raw = await file.read()
    if len(raw) > 300 * 1024 * 1024:
        raise HTTPException(400, "包太大（上限 300MB）")
    try:
        z = zipfile.ZipFile(_io.BytesIO(raw))
    except Exception as e:
        raise HTTPException(400, f"不是有效的 zip：{e}") from e

    names = z.namelist()
    if "project.json" not in names:
        raise HTTPException(400, "包里缺少 project.json，不是本项目导出的包")
    for n in names:
        if n.startswith("/") or ".." in Path(n).parts or ":" in n:
            raise HTTPException(400, f"包内有非法路径：{n}")

    try:
        meta = json.loads(z.read("project.json").decode("utf-8"))
    except Exception as e:
        raise HTTPException(400, f"project.json 解析失败：{e}") from e

    model_name = next((n for n in names if n.startswith("model.")), None)
    if not model_name:
        raise HTTPException(400, "包里缺少模型文件")

    safe_stem = re.sub(r"[^\w\u4e00-\u9fa5.-]+", "_", Path(str(meta.get("name") or "imported")).stem)[:60]
    dest = config.UPLOADS / f"{uuid.uuid4().hex[:8]}_{safe_stem}{Path(model_name).suffix}"
    dest.write_bytes(z.read(model_name))

    proj = store.create(f"{safe_stem}{Path(model_name).suffix}", dest)
    store.update(proj["id"], tags=list(meta.get("tags") or []),
                 favorite=bool(meta.get("favorite")),
                 analysis=meta.get("analysis"), advice=meta.get("advice"))
    return {"ok": True, "id": proj["id"], "name": proj["name"],
            "history": len(meta.get("history") or []),
            "note": "历史文件已解包，但只保留模型本体，回滚记录需在原机器上使用"}


@app.post("/api/projects/{pid}/print-record")
def add_print_record(pid: str, payload: dict = Body(default={})) -> dict:
    """记一次实际打印结果。攒多了能看出这个模型/参数组合到底靠不靠谱。"""
    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    result = str(payload.get("result") or "success").lower()
    if result not in ("success", "fail"):
        raise HTTPException(400, "result 只能是 success 或 fail")
    rec = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "result": result,
        "note": str(payload.get("note") or "")[:200],
        "faces": ((p.get("analysis") or {}).get("stats") or {}).get("faces"),
    }
    hist = list(p.get("prints") or [])
    hist.append(rec)
    hist = hist[-30:]
    store.update(pid, prints=hist)
    ok = sum(1 for x in hist if x["result"] == "success")
    return {"ok": True, "prints": hist, "success": ok, "total": len(hist),
            "rate": round(ok / len(hist) * 100, 1)}


@app.get("/api/projects/{pid}/print-record")
def get_print_records(pid: str) -> dict:
    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    hist = list(p.get("prints") or [])
    ok = sum(1 for x in hist if x["result"] == "success")
    return {"prints": hist, "success": ok, "total": len(hist),
            "rate": round(ok / len(hist) * 100, 1) if hist else 0.0}


@app.post("/api/projects/{pid}/duplicate")
def duplicate_project(pid: str) -> dict:
    """克隆一个项目：同一个源文件，独立的处理历史。"""
    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    src = Path(p.get("current") or p.get("source") or "")
    if not src.exists():
        raise HTTPException(404, "模型文件不存在")
    new = store.create(f"{Path(p['name']).stem}_副本{src.suffix}", src)
    store.update(new["id"], tags=list(p.get("tags") or []),
                 favorite=bool(p.get("favorite")),
                 analysis=p.get("analysis"), advice=p.get("advice"))
    return store.get(new["id"]) or {}


@app.patch("/api/projects/{pid}")
def patch_project(pid: str, payload: dict = Body(...)) -> dict:
    """改名、收藏、打标签。只接受白名单字段，避免误改 current 之类。"""
    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    patch: dict = {}
    if "name" in payload:
        name = str(payload["name"]).strip()
        if not name:
            raise HTTPException(400, "名称不能为空")
        patch["name"] = name[:120]
    if "favorite" in payload:
        patch["favorite"] = bool(payload["favorite"])
    if "tags" in payload:
        tags = payload["tags"]
        if not isinstance(tags, list):
            raise HTTPException(400, "tags 必须是数组")
        patch["tags"] = [str(t).strip()[:24] for t in tags if str(t).strip()][:12]
    if not patch:
        raise HTTPException(400, "没有可更新的字段")
    return store.update(pid, **patch) or {}


@app.delete("/api/projects/{pid}")
def delete_project(pid: str) -> dict:
    return {"ok": store.delete(pid)}


@app.get("/api/projects/{pid}/mesh")
def project_mesh(
    pid: str,
    which: str = Query("current", pattern="^(current|original)$"),
    max_faces: int = Query(0, ge=0, le=500000),
) -> dict:
    """返回 base64 编码的顶点/索引，前端解成 TypedArray，避免 JSON 数字膨胀。"""
    import numpy as np

    from . import meshio

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    path = p.get(which) or p.get("current")
    if not path or not Path(path).exists():
        raise HTTPException(404, "模型文件不存在")
    limit = max_faces or config.get().runtime.preview_faces
    mesh = meshio.load_mesh(Path(path), preview_faces=limit)
    v = np.ascontiguousarray(mesh.vertices, dtype=np.float32)
    f = np.ascontiguousarray(mesh.faces, dtype=np.uint32)
    return {
        "which": which,
        "vertices": base64.b64encode(v.tobytes()).decode(),
        "indices": base64.b64encode(f.tobytes()).decode(),
        "vertex_count": int(len(v)),
        "face_count": int(len(f)),
        "bbox": mesh.bounds.tolist() if hasattr(mesh, "bounds") else None,
        "stats": meshio.mesh_stats(mesh),
    }


def _support_density_field(mesh, radius: float = 6.0) -> dict:
    """支撑密度场：每个顶点周围 6mm 内有几根支撑。

    太密说明支撑难拆、表面疤痕多；大片 0 说明那块悬垂被漏了。
    """
    import numpy as np
    from scipy.spatial import cKDTree

    from . import process

    _, info = process.build_supports(mesh, 45.0, 1.0, 3.0, 400)
    segs = info.get("segments") or []
    v = np.asarray(mesh.vertices, dtype=np.float64)
    if not segs or len(v) == 0:
        return {"values": [0.0] * len(v), "max": 0, "unit": "根", "count": 0,
                "direction": "higher_is_denser", "note": "没有生成支撑"}
    pts = np.asarray([[x[0], x[1]] for x in segs], dtype=np.float64)
    tree = cKDTree(pts)
    counts = np.array([len(tree.query_ball_point(vv[:2], radius)) for vv in v], dtype=np.float64)
    mx = float(counts.max()) if counts.size else 0.0
    return {
        "values": [round(float(x), 2) for x in counts],
        "max": round(mx, 1),
        "unit": f"根/{radius:.0f}mm",
        "count": len(segs),
        "radius": radius,
        "direction": "higher_is_denser",
        "note": f"{len(segs)} 根支撑，单点最密 {mx:.0f} 根",
    }


@app.get("/api/projects/{pid}/heatmap")
def project_heatmap(
    pid: str,
    kind: str = Query("thickness", pattern="^(thickness|overhang|components|support)$"),
    max_faces: int = Query(0, ge=0, le=500000),
) -> dict:
    """逐顶点标量场，前端用顶点色渲染成热力图。"""
    import numpy as np

    from . import analysis, meshio

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    path = p.get("current") or p.get("source")
    if not path or not Path(path).exists():
        raise HTTPException(404, "模型文件不存在")
    limit = max_faces or config.get().runtime.preview_faces
    mesh = meshio.load_mesh(Path(path), preview_faces=limit)
    if kind == "thickness":
        field = analysis.thickness_field(mesh)
    elif kind == "overhang":
        field = analysis.overhang_field(mesh)
    elif kind == "support":
        field = _support_density_field(mesh)
    else:
        field = analysis.component_field(mesh)
    v = np.ascontiguousarray(mesh.vertices, np.float32)
    f = np.ascontiguousarray(mesh.faces, np.uint32)
    vals = np.ascontiguousarray(np.asarray(field.get("values") or [], dtype=np.float32))
    return {
        "kind": kind,
        "vertices": base64.b64encode(v.tobytes()).decode(),
        "indices": base64.b64encode(f.tobytes()).decode(),
        "values": base64.b64encode(vals.tobytes()).decode(),
        "vertex_count": int(len(v)),
        "face_count": int(len(f)),
        "meta": {k: val for k, val in field.items() if k != "values"},
    }


@app.post("/api/projects/{pid}/analyze")
def analyze(pid: str, payload: dict = Body(default={})) -> dict:
    from . import analysis

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    params = {"sample_faces": 1500, "check_self_intersection": True}
    params.update(payload or {})
    return _spawn("analyze", f"检测 {p['name']}", params, analysis.run_job, pid)


@app.post("/api/projects/{pid}/repair")
def repair(pid: str, payload: dict = Body(...)) -> dict:
    from . import repair as repair_mod

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    actions = payload.get("actions") or ["fill_holes", "fix_normals"]
    if not isinstance(actions, (list, tuple)) or not actions:
        raise HTTPException(422, "actions 必须是非空数组")
    actions = [str(a) for a in actions]
    unknown = [a for a in actions if a not in repair_mod.ACTIONS]
    if unknown:
        raise HTTPException(422, f"未知修复动作: {', '.join(unknown)}")
    # 动作参数必须原样透传：旧接口只转发 small_ratio，
    # 前端改加厚量/重网格精度/补洞细分时后端其实一直用默认值。
    params = {"actions": actions}
    for key in (
        "max_loop", "fill_levels", "fill_smooth", "small_ratio",
        "remesh_pitch", "remesh_smooth", "thicken_mm", "thicken_smooth",
        "scale_factor",
    ):
        if key in payload:
            params[key] = payload[key]
    params.setdefault("small_ratio", 0.01)
    return _spawn("repair", f"修复 {p['name']}", params, repair_mod.run_job, pid)


@app.post("/api/projects/{pid}/process")
def process(pid: str, payload: dict = Body(...)) -> dict:
    from . import process as process_mod

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    return _spawn("process", f"处理 {p['name']}", payload, process_mod.run_job, pid)


@app.post("/api/projects/{pid}/split")
def split_project(pid: str, payload: dict = Body(...)) -> dict:
    from . import split as split_mod

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    return _spawn("split", f"分件 {p['name']}", payload, split_mod.run_job, pid)


@app.post("/api/projects/{pid}/resize")
def resize_project(pid: str, payload: dict = Body(...)) -> dict:
    """按目标尺寸缩放。lock_ratio=True 以最长边为准等比，否则三轴各自独立。"""

    from . import meshio

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    src = Path(p.get("current") or p.get("source") or "")
    if not src.exists():
        raise HTTPException(404, "模型文件不存在")
    mesh = meshio.load_mesh(src)
    cur = [float(x) for x in mesh.extents]
    if min(cur) <= 1e-9:
        raise HTTPException(400, "模型尺寸为零，无法缩放")

    tw = payload.get("width")
    td = payload.get("depth")
    th = payload.get("height")
    targets = [float(x) for x in (tw, td, th) if x and float(x) > 0]
    if not targets:
        raise HTTPException(400, "至少给一个目标尺寸")

    lock = bool(payload.get("lock_ratio", True))
    if lock:
        factor = max(targets) / max(cur)
        mesh.apply_scale(factor)
        applied = [round(factor, 4)] * 3
    else:
        fx = float(tw) / cur[0] if tw and float(tw) > 0 else 1.0
        fy = float(td) / cur[1] if td and float(td) > 0 else 1.0
        fz = float(th) / cur[2] if th and float(th) > 0 else 1.0
        mesh.apply_scale([fx, fy, fz])
        applied = [round(fx, 4), round(fy, 4), round(fz, 4)]

    mesh.apply_translation([0, 0, -float(mesh.bounds[0][2])])
    out = config.OUTPUTS / f"{pid}_resize_{int(_time.time())}.stl"
    mesh.export(str(out))
    store.add_history(pid, "resize", {"targets": targets, "lock": lock}, out,
                      note=f"缩放 ×{applied[0]}" + ("" if lock else " (非等比)"))
    return {"ok": True, "output": str(out), "factors": applied, "lock": lock,
            "extents": [round(float(x), 2) for x in mesh.extents]}


@app.post("/api/projects/{pid}/center")
def center_project(pid: str) -> dict:
    """把模型在平台上居中：XY 中心对齐原点，Z 底面贴 0。"""
    from . import meshio

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    src = Path(p.get("current") or p.get("source") or "")
    if not src.exists():
        raise HTTPException(404, "模型文件不存在")
    mesh = meshio.load_mesh(src)
    lo, hi = mesh.bounds
    cx = float((lo[0] + hi[0]) / 2)
    cy = float((lo[1] + hi[1]) / 2)
    mesh.apply_translation([-cx, -cy, -float(lo[2])])
    out = config.OUTPUTS / f"{pid}_center_{int(_time.time())}.stl"
    mesh.export(str(out))
    store.add_history(pid, "center", {"dx": -cx, "dy": -cy}, out, note="平台居中")
    return {"ok": True, "output": str(out),
            "moved": [round(-cx, 2), round(-cy, 2)],
            "extents": [round(float(x), 2) for x in mesh.extents]}


@app.get("/api/projects/{pid}/layer-times")
def layer_times(pid: str, layer_h: float = Query(0.05, gt=0.001, le=1.0),
                exposure: float = Query(2.5, gt=0, le=120),
                samples: int = Query(40, ge=5, le=200)) -> dict:
    """每层耗时估算：曝光 + 抬升固定开销 + 按截面积折算的扫描时间。"""
    from . import meshio

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    src = Path(p.get("current") or p.get("source") or "")
    if not src.exists():
        raise HTTPException(404, "模型文件不存在")
    mesh = meshio.load_mesh(Path(src), preview_faces=60000)
    lo, hi = float(mesh.bounds[0][2]), float(mesh.bounds[1][2])
    span = max(hi - lo, 1e-6)
    lift = 6.0  # 抬升 + 回落 + 停顿的经验值
    rows = []
    for i in range(samples):
        z = lo + span * (i + 0.5) / samples
        area = 0.0
        try:
            sec = mesh.section(plane_origin=[0.0, 0.0, z], plane_normal=[0.0, 0.0, 1.0])
            if sec is not None:
                planar, _ = sec.to_planar()
                area = float(sum(poly.area for poly in planar.polygons_full))
        except Exception:
            pass
        # 扫描时间按面积折算：经验值 1mm² 约 0.012s
        scan = area * 0.012
        rows.append({
            "z": round(z, 2),
            "area": round(area, 1),
            "seconds": round(exposure + lift + scan, 2),
        })
    total_layers = int(span / layer_h) if span else 0
    avg = sum(r["seconds"] for r in rows) / max(1, len(rows))
    return {
        "layers": rows,
        "layer_height": layer_h,
        "total_layers": total_layers,
        "avg_seconds": round(avg, 2),
        "est_hours": round(avg * total_layers / 3600, 2),
        "note": f"{total_layers} 层 × 平均 {avg:.1f}s",
    }


@app.post("/api/projects/{pid}/slice-plane")
def slice_by_plane(pid: str, payload: dict = Body(...)) -> dict:
    """沿任意平面切成两半。上半写回当前项目，下半另存为新项目。"""
    import numpy as np

    from . import meshio

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    src = Path(p.get("current") or p.get("source") or "")
    if not src.exists():
        raise HTTPException(404, "模型文件不存在")
    mesh = meshio.load_mesh(src)
    if not mesh.is_watertight:
        raise HTTPException(400, "切割要求模型水密，切面才能正确封盖")

    origin = np.asarray(payload.get("origin") or [0, 0, 0], dtype=np.float64)
    normal = np.asarray(payload.get("normal") or [0, 0, 1], dtype=np.float64)
    norm = float(np.linalg.norm(normal))
    if norm < 1e-9:
        raise HTTPException(400, "法向量不能为零")
    normal = normal / norm

    try:
        upper = mesh.slice_plane(origin, normal, cap=True)
        lower = mesh.slice_plane(origin, -normal, cap=True)
    except Exception as e:
        raise HTTPException(500, f"切割失败：{e}") from e
    if upper is None or lower is None or len(upper.faces) == 0 or len(lower.faces) == 0:
        raise HTTPException(400, "平面没有穿过模型，或切开后有一侧为空")

    ts = int(_time.time())
    out_a = config.OUTPUTS / f"{pid}_cut_keep_{ts}.stl"
    out_b = config.OUTPUTS / f"{pid}_cut_other_{ts}.stl"
    upper.export(str(out_a))
    lower.export(str(out_b))
    store.add_history(pid, "slice_plane", {"normal": normal.tolist()}, out_a,
                      note=f"沿法线切割，保留 {len(upper.faces)} 面")
    new = store.create(f"{Path(p['name']).stem}_另一半.stl", out_b)
    return {
        "ok": True,
        "keep": {"faces": int(len(upper.faces)), "volume_mm3": round(float(upper.volume), 1)},
        "other": {"id": new["id"], "name": new["name"],
                  "faces": int(len(lower.faces)), "volume_mm3": round(float(lower.volume), 1)},
        "normal": [round(float(x), 3) for x in normal],
    }


@app.post("/api/projects/{pid}/bore")
def bore_holes(pid: str, payload: dict = Body(...)) -> dict:
    """在指定位置挖圆柱孔。depth=0 表示穿透。"""
    import numpy as np
    import trimesh

    from . import meshio

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    src = Path(p.get("current") or p.get("source") or "")
    if not src.exists():
        raise HTTPException(404, "模型文件不存在")
    pts = payload.get("points")
    if not isinstance(pts, list) or not pts:
        raise HTTPException(400, "需要 points 数组")
    diameter = float(payload.get("diameter", 3.0))
    if diameter <= 0:
        raise HTTPException(400, "孔径必须大于 0")
    depth = float(payload.get("depth", 0.0))
    direction = np.asarray(payload.get("direction") or [0, 0, -1], dtype=np.float64)
    dn = float(np.linalg.norm(direction))
    if dn < 1e-9:
        raise HTTPException(400, "方向不能为零")
    direction = direction / dn

    mesh = meshio.load_mesh(src)
    if not mesh.is_watertight:
        raise HTTPException(400, "挖孔要求模型水密")

    # 圆柱要够长，保证穿透或达到指定深度
    span = float(np.linalg.norm(mesh.extents)) * 1.5 + 10.0
    length = span if depth <= 0 else depth + 1.0
    axis_z = np.array([0.0, 0.0, 1.0])
    rot = trimesh.geometry.align_vectors(axis_z, direction)
    cutters = []
    for pt in pts[:50]:
        if not isinstance(pt, (list, tuple)) or len(pt) < 3:
            continue
        c = trimesh.creation.cylinder(radius=diameter / 2.0, height=length, sections=32)
        c.apply_transform(rot)
        # 圆柱默认中心在原点、沿 z 延伸 ±length/2；变换后沿 direction 延伸。
        # 把中心放到 pt + direction*length/2，一端正好在 pt、另一端朝 direction 伸出。
        c.apply_translation(np.asarray(pt[:3], dtype=np.float64) + direction * (length / 2.0))
        cutters.append(c)
    if not cutters:
        raise HTTPException(400, "没有有效的孔位")

    try:
        result = trimesh.boolean.difference([mesh] + cutters, engine="manifold")
    except Exception as e:
        raise HTTPException(500, f"挖孔失败：{e}") from e
    if result is None or len(result.faces) == 0:
        raise HTTPException(400, "挖孔结果为空，请检查孔位是否在模型内")

    out = config.OUTPUTS / f"{pid}_bore_{int(_time.time())}.stl"
    result.export(str(out))
    store.add_history(pid, "bore", {"count": len(cutters), "diameter": diameter}, out,
                      note=f"挖 {len(cutters)} 个 φ{diameter}mm 孔")
    return {"ok": True, "holes": len(cutters), "faces": int(len(result.faces)),
            "volume_mm3": round(float(result.volume), 1),
            "watertight": bool(result.is_watertight)}


@app.post("/api/projects/{pid}/boolean")
def boolean_project(pid: str, payload: dict = Body(...)) -> dict:
    """两件之间做并集 / 差集 / 交集。结果覆盖当前工作副本，原版本仍在历史里。"""
    import trimesh

    from . import meshio

    op = str(payload.get("op", "union"))
    if op not in ("union", "difference", "intersection"):
        raise HTTPException(400, "op 只能是 union / difference / intersection")
    other_id = str(payload.get("other_id") or "")
    if not other_id or other_id == pid:
        raise HTTPException(400, "需要选择另一个不同的模型")

    pa, pb = store.get(pid), store.get(other_id)
    if not pa or not pb:
        raise HTTPException(404, "项目不存在")
    fa = Path(pa.get("current") or pa.get("source") or "")
    fb = Path(pb.get("current") or pb.get("source") or "")
    if not fa.exists() or not fb.exists():
        raise HTTPException(404, "模型文件不存在")

    a = meshio.load_mesh(fa)
    b = meshio.load_mesh(fb)
    if not a.is_watertight or not b.is_watertight:
        raise HTTPException(400, "布尔运算要求两个模型都水密，请先修复")

    # 默认沿 X 并排放置；difference / intersection 需要重叠，传 overlap=true 就不挪
    if not payload.get("overlap"):
        gap = float(payload.get("gap", 2.0))
        shift = float(a.extents[0]) / 2 + float(b.extents[0]) / 2 + gap
        b = b.copy()
        b.apply_translation([shift, 0, 0])
    offset = payload.get("offset")
    if isinstance(offset, (list, tuple)) and len(offset) == 3:
        b = b.copy()
        b.apply_translation([float(x) for x in offset])

    try:
        if op == "union":
            r = trimesh.boolean.union([a, b], engine="manifold")
        elif op == "difference":
            r = trimesh.boolean.difference([a, b], engine="manifold")
        else:
            r = trimesh.boolean.intersection([a, b], engine="manifold")
    except Exception as e:
        raise HTTPException(500, f"布尔运算失败：{e}") from e
    if r is None or len(r.faces) == 0:
        raise HTTPException(400, "运算结果为空——两个模型可能没有交集")

    r.apply_translation([0, 0, -float(r.bounds[0][2])])
    out = config.OUTPUTS / f"{pid}_bool_{op}_{int(_time.time())}.stl"
    r.export(str(out))
    label = {"union": "并集", "difference": "差集", "intersection": "交集"}[op]
    store.add_history(pid, f"boolean_{op}", {"other_id": other_id}, out,
                      note=f"与 {pb['name']} 做{label}")
    return {
        "ok": True, "output": str(out), "op": op, "label": label,
        "faces": int(len(r.faces)),
        "volume_mm3": round(float(r.volume), 1),
        "extents": [round(float(x), 2) for x in r.extents],
        "watertight": bool(r.is_watertight),
    }


@app.post("/api/projects/{pid}/merge")
def merge_projects(pid: str, payload: dict = Body(...)) -> dict:
    """把多个模型直接合并成一个网格（不做布尔），保留各自相对位置。"""
    import trimesh

    from . import meshio

    ids = [str(x) for x in (payload.get("project_ids") or [])]
    if pid not in ids:
        ids.insert(0, pid)
    if len(ids) < 2:
        raise HTTPException(400, "至少要选两个模型")
    meshes = []
    names = []
    for x in ids:
        p = store.get(x)
        if not p:
            continue
        f = Path(p.get("current") or p.get("source") or "")
        if not f.exists():
            continue
        meshes.append(meshio.load_mesh(f))
        names.append(p["name"])
    if len(meshes) < 2:
        raise HTTPException(400, "可用模型不足两个")
    r = trimesh.util.concatenate(meshes)
    r.apply_translation([0, 0, -float(r.bounds[0][2])])
    out = config.OUTPUTS / f"{pid}_merged_{int(_time.time())}.stl"
    r.export(str(out))
    store.add_history(pid, "merge", {"ids": ids}, out, note=f"合并 {len(meshes)} 件")
    return {"ok": True, "output": str(out), "faces": int(len(r.faces)),
            "count": len(meshes), "names": names}


@app.get("/api/projects/{pid}/section-profile")
def section_profile(pid: str, layers: int = Query(40, ge=5, le=200)) -> dict:
    """沿 Z 轴等距切若干层，返回每层截面积——画成曲线能看出哪里突然收窄或塌陷。"""
    from . import meshio

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    path = p.get("current") or p.get("source")
    if not path or not Path(path).exists():
        raise HTTPException(404, "模型文件不存在")
    mesh = meshio.load_mesh(Path(path), preview_faces=60000)
    lo, hi = float(mesh.bounds[0][2]), float(mesh.bounds[1][2])
    span = max(hi - lo, 1e-6)
    rows = []
    for i in range(layers):
        z = lo + span * (i + 0.5) / layers
        area, n = 0.0, 0
        try:
            sec = mesh.section(plane_origin=[0.0, 0.0, z], plane_normal=[0.0, 0.0, 1.0])
            if sec is not None:
                planar, _ = sec.to_planar()
                area = float(sum(poly.area for poly in planar.polygons_full))
                n = len(sec.entities)
        except Exception:
            pass
        rows.append({"z": round(z, 2), "area": round(area, 1), "contours": n})
    areas = [r["area"] for r in rows]
    return {
        "layers": rows,
        "zmin": round(lo, 2),
        "zmax": round(hi, 2),
        "max_area": round(max(areas) if areas else 0.0, 1),
        "min_area": round(min(areas) if areas else 0.0, 1),
        "note": f"{layers} 层扫描，截面最大 {max(areas) if areas else 0:.0f}mm²",
    }


@app.get("/api/projects/{pid}/section")
def project_section(pid: str, z: float = Query(..., ge=-1e6, le=1e6)) -> dict:
    """指定高度的截面轮廓，前端画成线框——用来逐层检查内部结构。"""
    from . import meshio

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    path = p.get("current") or p.get("source")
    if not path or not Path(path).exists():
        raise HTTPException(404, "模型文件不存在")
    mesh = meshio.load_mesh(Path(path), preview_faces=120000)
    try:
        sec = mesh.section(plane_origin=[0.0, 0.0, z], plane_normal=[0.0, 0.0, 1.0])
    except Exception as e:
        raise HTTPException(500, f"切片失败：{e}") from e
    if sec is None:
        return {"z": z, "paths": [], "count": 0, "area": 0.0}
    paths: list[list[list[float]]] = []
    try:
        for ent in sec.entities:
            pts = sec.vertices[ent.points]
            if len(pts) >= 2:
                paths.append([[round(float(q), 2) for q in row] for row in pts])
    except Exception:
        pass
    area = 0.0
    try:
        # Path3D 没有直接的面积属性，投影到平面后用多边形面积求和
        planar, _ = sec.to_planar()
        area = round(float(sum(poly.area for poly in planar.polygons_full)), 2)
    except Exception:
        pass
    bounds = mesh.bounds
    return {
        "z": round(float(z), 2),
        "paths": paths[:300],
        "count": len(paths),
        "area": area,
        "zmin": round(float(bounds[0][2]), 2),
        "zmax": round(float(bounds[1][2]), 2),
    }


@app.post("/api/projects/{pid}/rotate")
def rotate_project(pid: str, payload: dict = Body(...)) -> dict:
    """按轴旋转指定角度，用于手动微调姿态。"""
    import numpy as np
    import trimesh

    from . import meshio

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    path = p.get("current") or p.get("source")
    if not path or not Path(path).exists():
        raise HTTPException(404, "模型文件不存在")
    axis = str(payload.get("axis", "z")).lower()
    deg = float(payload.get("degrees", 90))
    vec = {"x": [1, 0, 0], "y": [0, 1, 0], "z": [0, 0, 1]}.get(axis)
    if vec is None:
        raise HTTPException(400, "axis 只能是 x / y / z")
    mesh = meshio.load_mesh(Path(path))
    T = trimesh.transformations.rotation_matrix(np.radians(deg), vec)
    mesh.apply_transform(T)
    mesh.apply_translation([0, 0, -float(mesh.bounds[0][2])])
    out = config.OUTPUTS / f"{pid}_rot_{int(_time.time())}.stl"
    mesh.export(str(out))
    store.add_history(pid, "rotate", {"axis": axis, "degrees": deg}, out,
                      note=f"绕 {axis.upper()} 轴转 {deg}°")
    return {
        "ok": True,
        "output": str(out),
        "extents": [round(float(x), 2) for x in mesh.extents],
    }


@app.get("/api/projects/{pid}/issues")
def project_issues(pid: str) -> dict:
    """把检测出的问题定位到 3D 坐标，前端直接打点。"""
    import numpy as np

    from . import analysis, meshio, repair

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    path = p.get("current") or p.get("source")
    if not path or not Path(path).exists():
        raise HTTPException(404, "模型文件不存在")
    mesh = meshio.load_mesh(Path(path), preview_faces=60000)
    v = np.asarray(mesh.vertices, dtype=np.float64)
    f = np.asarray(mesh.faces)

    holes: list = []
    try:
        for ring in repair.boundary_loops(f, len(v)):
            holes.append([round(float(x), 2) for x in v[ring].mean(axis=0)])
    except Exception:
        pass

    self_int: list = []
    try:
        res = analysis.find_self_intersections(v, f)
        if res.get("pairs"):
            pr = np.asarray(res["pairs"])
            centers = v[f[pr[:, 0]]].mean(axis=1)
            self_int = [[round(float(x), 2) for x in c] for c in centers[:150]]
    except Exception:
        pass

    debris: list = []
    try:
        n, labels = analysis.union_components(f, len(v))
        if n > 1:
            areas = analysis.tri_areas(v, f)
            comp = np.array([areas[labels == c].sum() for c in range(n)])
            mx = float(comp.max())
            for c in range(n):
                if comp[c] < mx * 0.01:
                    debris.append([round(float(x), 2) for x in v[f[labels == c]].reshape(-1, 3).mean(axis=0)])
    except Exception:
        pass

    return {
        "holes": holes,
        "self_intersection": self_int,
        "debris": debris,
        "counts": {"holes": len(holes), "self_intersection": len(self_int), "debris": len(debris)},
    }


@app.post("/api/projects/{pid}/preview-supports")
def preview_supports(pid: str, payload: dict = Body(default={})) -> dict:
    """只算支撑位置，不改模型——让用户先看清楚支撑会打在哪。"""
    from . import meshio, process

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    path = p.get("current") or p.get("source")
    if not path or not Path(path).exists():
        raise HTTPException(404, "模型文件不存在")
    mesh = meshio.load_mesh(Path(path), preview_faces=80000)
    try:
        _, info = process.build_supports(
            mesh,
            float(payload.get("overhang_deg", 45.0)),
            float(payload.get("diameter", 1.0)),
            float(payload.get("spacing", 3.0)),
            400,
        )
    except Exception as e:
        raise HTTPException(500, f"支撑计算失败：{e}") from e
    return {
        "segments": info.get("segments", []),
        "count": info.get("count", 0),
        "note": info.get("note", ""),
        "min_height": info.get("min_height"),
        "max_height": info.get("max_height"),
    }


@app.post("/api/projects/{pid}/pack")
def pack_models(pid: str, payload: dict = Body(...)) -> dict:
    from . import pack as pack_mod

    return _spawn("pack", "拼盘排版", payload, pack_mod.run_job, pid)


@app.get("/api/projects/{pid}/diff")
def project_diff(pid: str, tol: float = Query(0.05, ge=0.0, le=10.0)) -> dict:
    from . import compare

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    a, b = p.get("original"), p.get("current")
    if not a or not b or a == b:
        return {"identical": True, "note": "还没有修改过，无差异可比较"}
    if not Path(a).exists() or not Path(b).exists():
        raise HTTPException(404, "版本文件已不存在")
    return compare.diff_files(Path(a), Path(b), tol)


@app.post("/api/projects/{pid}/checkout")
def project_checkout(pid: str, payload: dict = Body(...)) -> dict:
    """把工作副本切到指定文件（重做用）。只允许指向本项目产出的目录。"""
    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    raw = str(payload.get("path") or "")
    if not raw:
        raise HTTPException(400, "缺少 path")
    target = Path(raw).resolve()
    allowed = [config.OUTPUTS.resolve(), config.UPLOADS.resolve()]
    # 只允许落在产出目录里，防止把任意文件设成工作副本
    if not any(target.is_relative_to(a) for a in allowed):
        raise HTTPException(400, "路径不在允许的目录内")
    if not target.exists():
        raise HTTPException(404, "目标文件不存在")
    store.update(pid, current=str(target))
    store.add_history(pid, "checkout", {"path": str(target)}, target, note="重做")
    return {"ok": True, "current": str(target)}


@app.post("/api/projects/{pid}/revert")
def project_revert(pid: str, payload: dict = Body(default={})) -> dict:
    """回到某个历史版本。index=-1 表示回到刚导入的状态。"""
    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    hist = p.get("history") or []
    idx = int((payload or {}).get("index", -1))
    if idx < 0:
        target = p.get("original")
        label = "导入时"
    else:
        if idx >= len(hist):
            raise HTTPException(400, "历史索引越界")
        target = hist[idx].get("out")
        label = hist[idx].get("action", f"第 {idx + 1} 步")
    if not target or not Path(target).exists():
        raise HTTPException(404, "目标版本文件已不存在")
    store.update(pid, current=target)
    store.add_history(pid, "revert", {"index": idx}, Path(target), note=f"回滚到 {label}")
    return {"ok": True, "current": target, "label": label}


SEV_COLOR = {"ok": "#5dcaa5", "info": "#378add", "warning": "#ef9f27", "error": "#e24b4a"}
SEV_LABEL = {"ok": "通过", "info": "提示", "warning": "警告", "error": "严重"}


def _build_report_html(p: dict) -> str:
    """自包含 HTML 报告：样式内联，不依赖任何外部资源，直接发出去就能看。"""
    import html as _html

    rep = p.get("analysis") or {}
    items = rep.get("items") or []
    summary = rep.get("summary") or {}
    stats = rep.get("stats") or {}
    adv = p.get("advice") or {}
    hist = p.get("history") or []
    esc = _html.escape

    rows = "".join(
        f'<tr><td><span class="dot" style="background:{SEV_COLOR.get(it.get("severity"), "#888")}"></span>'
        f'{esc(str(it.get("name")))}</td>'
        f'<td class="sev">{SEV_LABEL.get(it.get("severity"), it.get("severity"))}</td>'
        f'<td>{esc(str(it.get("message")))}</td></tr>'
        for it in items
    ) or '<tr><td colspan="3" class="muted">还没有检测结果</td></tr>'

    params = "".join(
        f'<tr><td>{esc(str(x.get("label")))}</td>'
        f'<td class="val">{esc(str(x.get("value")))} {esc(str(x.get("unit") or ""))}</td>'
        f'<td class="muted">{esc(str(x.get("reason")))}</td></tr>'
        for x in (adv.get("params") or [])
    ) or '<tr><td colspan="3" class="muted">还没有参数建议</td></tr>'

    hist_rows = "".join(
        f'<tr><td class="mono">{esc(str(h.get("at", ""))[:19].replace("T", " "))}</td>'
        f'<td>{esc(str(h.get("action")))}</td><td class="muted">{esc(str(h.get("note") or ""))}</td></tr>'
        for h in hist
    ) or '<tr><td colspan="3" class="muted">还没有操作记录</td></tr>'

    warns = "".join(f"<li>{esc(str(w))}</li>" for w in (adv.get("warnings") or []))

    q = rep.get("quality") or {}
    grade_color = {"A": "#5dcaa5", "B": "#97c459", "C": "#ef9f27", "D": "#e24b4a"}.get(q.get("grade"), "#888")
    quality_block = ""
    if q:
        bars = "".join(
            f'<tr><td>{esc(str(p.get("name")))}</td>'
            f'<td style="width:46%"><div class="qbar"><i style="width:'
            f'{(float(p.get("score", 0)) / max(1, int(p.get("max", 1))) * 100):.0f}%;background:'
            f'{"#5dcaa5" if float(p.get("score", 0)) / max(1, int(p.get("max", 1))) > 0.8 else "#ef9f27"}"></i></div></td>'
            f'<td class="val" style="text-align:right">{p.get("score")}/{p.get("max")}</td>'
            f'<td class="muted">{esc(str(p.get("note")))}</td></tr>'
            for p in (q.get("parts") or [])
        )
        quality_block = f"""
<h2>网格质量</h2>
<div style="display:flex;align-items:center;gap:20px;margin-bottom:14px">
  <div style="font:500 40px Consolas,monospace;color:{grade_color}">{q.get("score")}<span style="font-size:16px;color:#8b93a5"> /100</span></div>
  <div>
    <div style="color:{grade_color};font-size:14px">{q.get("grade")} 级 · {esc(str(q.get("verdict")))}</div>
  </div>
</div>
<table><tr><th>项目</th><th>得分</th><th style="text-align:right">分数</th><th>说明</th></tr>{bars}</table>
"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>{esc(str(p.get("name")))} · 处理报告</title>
<style>
body{{background:#0e1014;color:#e6e9ef;font:14px/1.7 "Segoe UI","PingFang SC",system-ui,sans-serif;margin:0;padding:40px 24px}}
.wrap{{max-width:980px;margin:0 auto}}
h1{{font-size:22px;font-weight:500;margin:0 0 6px}}
h2{{font-size:15px;font-weight:500;margin:32px 0 12px;padding-bottom:8px;border-bottom:1px solid #262b38}}
.sub{{color:#8b93a5;font-size:12px;margin-bottom:24px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;color:#8b93a5;font-weight:400;font-size:12px;padding:8px 10px;border-bottom:1px solid #262b38}}
td{{padding:9px 10px;border-bottom:1px solid #1f2430;vertical-align:top}}
.dot{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px}}
.sev{{white-space:nowrap;color:#8b93a5}}
.val{{color:#5dcaa5;font-family:Consolas,monospace;white-space:nowrap}}
.mono{{font-family:Consolas,monospace;font-size:12px;color:#8b93a5}}
.muted{{color:#8b93a5}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:8px}}
.card{{background:#151821;border:1px solid #262b38;border-radius:8px;padding:12px 16px;min-width:130px}}
.card b{{display:block;font-size:18px;font-weight:500;color:#5dcaa5;font-family:Consolas,monospace}}
.card span{{font-size:11px;color:#8b93a5}}
.qbar{{height:6px;background:#1f2430;border-radius:3px;overflow:hidden}}
.qbar i{{display:block;height:100%;border-radius:3px}}
.warn{{background:#1e1710;border:1px solid #4a3410;border-radius:8px;padding:12px 18px;color:#e8c48a;font-size:13px}}
ul{{margin:0;padding-left:20px}}
footer{{margin-top:40px;color:#5f6675;font-size:11px;border-top:1px solid #262b38;padding-top:12px}}
</style></head><body><div class="wrap">
<h1>{esc(str(p.get("name")))}</h1>
<div class="sub">处理报告 · 生成于 {datetime.now().strftime("%Y-%m-%d %H:%M")} · 项目 {esc(str(p.get("id")))}</div>

<div class="cards">
  <div class="card"><b>{stats.get("faces", 0):,}</b><span>面数</span></div>
  <div class="card"><b>{float(stats.get("volume_mm3") or 0) / 1000:.1f}</b><span>体积 cm³</span></div>
  <div class="card"><b>{"是" if stats.get("is_watertight") else "否"}</b><span>水密</span></div>
  <div class="card"><b>{summary.get("counts", {}).get("error", 0)}</b><span>严重项</span></div>
  <div class="card"><b>{len(hist)}</b><span>操作次数</span></div>
</div>

{quality_block}
<h2>检测报告</h2>
<table><tr><th>项目</th><th>结论</th><th>说明</th></tr>{rows}</table>

<h2>推荐参数</h2>
<table><tr><th>参数</th><th>推荐值</th><th>依据</th></tr>{params}</table>
{f'<div class="warn" style="margin-top:16px"><ul>{warns}</ul></div>' if warns else ""}

<h2>操作历史</h2>
<table><tr><th>时间</th><th>动作</th><th>说明</th></tr>{hist_rows}</table>

<footer>3D Print Forge · 报告由本地工具生成，数据未上传</footer>
</div></body></html>"""


@app.get("/api/projects/{pid}/report.html")
def report_html(pid: str) -> Response:
    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    body = _build_report_html(p)
    return Response(
        body.encode("utf-8"),
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'inline; filename="{Path(p["name"]).stem}_report.html"'},
    )


@app.get("/api/projects/{pid}/param-history")
def project_param_history(pid: str) -> dict:
    """历次操作的参数快照，并标出相对上一次改了哪几项。"""
    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    hist = p.get("history") or []
    rows = []
    prev: dict = {}
    for h in hist:
        cur = h.get("params") or {}
        changed = {}
        for k, v in cur.items():
            if k in prev and prev[k] != v:
                changed[k] = {"from": prev[k], "to": v}
        rows.append({
            "at": h.get("at"),
            "action": h.get("action"),
            "params": cur,
            "changed": changed,
            "note": h.get("note"),
        })
        prev = {**prev, **cur}
    return {"rows": rows, "count": len(rows),
            "note": "只对比相邻两次都出现过的参数，新增参数不算「改变」"}


@app.post("/api/projects/{pid}/check-assembly")
def check_assembly(pid: str, payload: dict = Body(default={})) -> dict:
    """检查分件各块摆回原位后是否互相干涉（重叠体积 > 0.5mm³ 即报警）。"""
    import trimesh

    from . import meshio

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    asm = p.get("assembly")
    if not asm or not asm.get("files"):
        raise HTTPException(404, "还没有分件结果")

    tol = float(payload.get("tolerance_mm3", 0.5))
    meshes = []
    names = []
    for f, off, name in zip(asm["files"], asm["offsets"], asm["names"]):
        fp = Path(f)
        if not fp.exists():
            continue
        m = meshio.load_mesh(fp)
        cur = m.bounds.mean(axis=0)
        m.apply_translation([float(off[0]) - float(cur[0]),
                             float(off[1]) - float(cur[1]),
                             float(off[2]) - float(cur[2])])
        meshes.append(m)
        names.append(name)
    if len(meshes) < 2:
        return {"ok": True, "interferences": [], "parts": len(meshes),
                "note": "少于两块，无需检查"}

    interferences = []
    checked = 0
    for i in range(len(meshes)):
        for j in range(i + 1, len(meshes)):
            checked += 1
            vol = 0.0
            try:
                inter = trimesh.boolean.intersection([meshes[i], meshes[j]], engine="manifold")
                if inter is not None and len(inter.faces) > 0:
                    vol = abs(float(inter.volume))
            except Exception:
                vol = 0.0
            if vol > tol:
                interferences.append({
                    "a": i + 1, "b": j + 1,
                    "name_a": names[i], "name_b": names[j],
                    "volume_mm3": round(vol, 1),
                })
    return {
        "ok": not interferences,
        "parts": len(meshes),
        "pairs_checked": checked,
        "interferences": interferences,
        "tolerance_mm3": tol,
        "note": (f"{checked} 对组合全部无干涉" if not interferences
                 else f"{len(interferences)} 对存在干涉，装配时会顶住"),
    }


@app.get("/api/projects/{pid}/assembly")
def project_assembly(pid: str) -> dict:
    """把分件各块按原始位置摆回去，合并成一个带顶点色的网格——装配预览用。"""
    import numpy as np

    from . import meshio

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    asm = p.get("assembly")
    if not asm or not asm.get("files"):
        raise HTTPException(404, "这个项目还没有分件结果，先在导出页执行分件")

    palette = [
        (0.22, 0.54, 0.87), (0.36, 0.79, 0.65), (0.94, 0.62, 0.15),
        (0.66, 0.33, 0.97), (0.89, 0.29, 0.29), (0.45, 0.78, 0.36),
    ]
    verts_all, faces_all, colors_all, parts = [], [], [], []
    base = 0
    for i, (f, off, name) in enumerate(zip(asm["files"], asm["offsets"], asm["names"])):
        fp = Path(f)
        if not fp.exists():
            continue
        m = meshio.load_mesh(fp)
        # 分件时每块被移到 z=0，中心从「原始中心」变成了「h/2」。
        # 所以不能直接平移 off，要按「目标中心 - 当前中心」算差值。
        cur = m.bounds.mean(axis=0)
        m.apply_translation([float(off[0]) - float(cur[0]),
                             float(off[1]) - float(cur[1]),
                             float(off[2]) - float(cur[2])])
        v = np.asarray(m.vertices, dtype=np.float32)
        fz = np.asarray(m.faces, dtype=np.uint32) + base
        c = palette[i % len(palette)]
        verts_all.append(v)
        faces_all.append(fz)
        colors_all.append(np.tile(np.array(c, dtype=np.float32), (len(v), 1)))
        parts.append({"name": name, "faces": int(len(m.faces)), "offset": off,
                      "color": [round(x, 3) for x in c]})
        base += len(v)

    if not verts_all:
        raise HTTPException(404, "分件文件已丢失，请重新分件")
    verts = np.ascontiguousarray(np.vstack(verts_all), dtype=np.float32)
    faces = np.ascontiguousarray(np.vstack(faces_all), dtype=np.uint32)
    colors = np.ascontiguousarray(np.vstack(colors_all), dtype=np.float32)
    return {
        "vertices": base64.b64encode(verts.tobytes()).decode(),
        "indices": base64.b64encode(faces.tobytes()).decode(),
        "colors": base64.b64encode(colors.tobytes()).decode(),
        "vertex_count": int(len(verts)),
        "face_count": int(len(faces)),
        "parts": parts,
        "at": asm.get("at"),
    }


@app.get("/api/projects/{pid}/split/download")
def split_download(pid: str) -> Response:
    files = sorted(config.OUTPUTS.glob(f"{pid}_split_*.zip"),
                   key=lambda x: x.stat().st_mtime, reverse=True)
    if not files:
        raise HTTPException(404, "还没有分件结果")
    return FileResponse(files[0], media_type="application/zip", filename=files[0].name)


@app.get("/api/projects/{pid}/advice")
def advice(
    pid: str,
    plan: str = Query("balanced"),
    printer: str | None = Query(None),
    process: str | None = Query(None),
    material: str | None = Query(None),
    calibrate: bool = Query(True),
) -> dict:
    from . import advisor

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    overrides: dict = {"plan": plan, "calibrate": calibrate}
    if printer:
        overrides["printer"] = printer
    if process:
        overrides["process"] = process
    if material:
        overrides["material"] = material
    return advisor.recommend(p, overrides=overrides)


@app.post("/api/projects/{pid}/advice-feedback")
def advice_feedback(pid: str, payload: dict = Body(default={})) -> dict:
    """把「这次预测 vs 实际打出来的」记下来。

    攒够几条之后，建议会自动按这台机器、这种料的历史偏差做校正。
    只记这个组合的偏差，不记别的——不同机器差很多，混在一起反而更不准。
    """
    from . import advisor, calibration

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")

    printer_key = str(payload.get("printer") or "sla_mid")
    printer = advisor.PRINTERS.get(printer_key, advisor.PRINTERS["sla_mid"])
    process = str(payload.get("process") or printer["process"])
    material_key = str(payload.get("material")
                       or ("standard" if process == "SLA" else "PLA"))

    pred = payload.get("pred") or {}
    actual = payload.get("actual") or {}
    if not isinstance(pred, dict) or not isinstance(actual, dict):
        raise HTTPException(400, "pred 与 actual 都必须是对象")
    def _pos(v) -> bool:
        try:
            return float(v) > 0
        except (TypeError, ValueError):
            return False

    if not (_pos(actual.get("hours")) or _pos(actual.get("grams"))):
        raise HTTPException(400, "actual 至少要有 hours 或 grams 其中一项")

    rec = calibration.add_record(
        process=process, printer=printer_key, material=material_key,
        pred=pred, actual=actual,
        result=str(payload.get("result") or "success"),
        note=str(payload.get("note") or ""),
    )
    est_h = calibration.estimate(process, printer_key, material_key, "hours")
    est_g = calibration.estimate(process, printer_key, material_key, "grams")
    return {
        "ok": True,
        "record": rec,
        "correction": {"hours": est_h, "grams": est_g},
        "summary": calibration.summary(),
    }


@app.get("/api/calibration")
def calibration_status() -> dict:
    """标定总览：攒了多少条、覆盖哪些机型/材料组合。"""
    from . import calibration

    return calibration.summary()


@app.delete("/api/calibration")
def calibration_reset() -> dict:
    """清空标定记录，回到纯物理估算。"""
    from . import calibration

    calibration.clear()
    return {"ok": True, "summary": calibration.summary()}


# ---------------------------------------------------------------- 任务
@app.get("/api/jobs")
def list_jobs() -> dict:
    return {"jobs": jobs.MANAGER.list()}


@app.get("/api/jobs/{jid}")
def get_job(jid: str) -> dict:
    j = jobs.MANAGER.get(jid)
    if not j:
        raise HTTPException(404, "任务不存在")
    return _job_out(j)


@app.post("/api/jobs/{jid}/{action}")
def job_action(jid: str, action: str) -> dict:
    if action not in ("pause", "resume", "cancel"):
        raise HTTPException(400, "未知操作")
    ok = getattr(jobs.MANAGER, action)(jid)
    if not ok:
        raise HTTPException(409, f"当前状态不允许 {action}")
    return {"ok": True, "job": _job_out(jobs.MANAGER.get(jid))}


# ---------------------------------------------------------------- 模板
TEMPLATE_FILE = config.STATE / "templates.json"


def _load_templates() -> list[dict]:
    if TEMPLATE_FILE.exists():
        try:
            return json.loads(TEMPLATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_templates(items: list[dict]) -> None:
    TEMPLATE_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/templates")
def list_templates() -> dict:
    return {"templates": _load_templates()}


@app.post("/api/templates")
def create_template(payload: dict = Body(...)) -> dict:
    items = _load_templates()
    tpl = {
        "id": uuid.uuid4().hex[:8],
        "name": payload.get("name") or "未命名模板",
        "created": datetime.now().isoformat(timespec="seconds"),
        "params": payload.get("params") or {},
        "advice": payload.get("advice") or {},
    }
    items.append(tpl)
    _save_templates(items)
    return {"template": tpl}


@app.get("/api/templates/export")
def export_templates() -> Response:
    items = _load_templates()
    return Response(
        json.dumps({"version": 1, "templates": items}, ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="templates.json"'},
    )


@app.post("/api/templates/import")
def import_templates(payload: dict = Body(...)) -> dict:
    """导入模板文件。同名模板加后缀，不覆盖已有配置。"""
    incoming = payload.get("templates")
    if not isinstance(incoming, list):
        raise HTTPException(400, "需要 templates 数组")
    items = _load_templates()
    existing = {t.get("name") for t in items}
    added = 0
    for raw in incoming[:100]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "未命名模板")[:60]
        if name in existing:
            name = f"{name} (导入)"
        items.append({
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "created": datetime.now().isoformat(timespec="seconds"),
            "params": raw.get("params") or {},
            "advice": raw.get("advice") or {},
        })
        existing.add(name)
        added += 1
    _save_templates(items)
    return {"ok": True, "added": added, "total": len(items)}


@app.delete("/api/templates/{tid}")
def delete_template(tid: str) -> dict:
    items = [t for t in _load_templates() if t.get("id") != tid]
    _save_templates(items)
    return {"ok": True}


# ---------------------------------------------------------------- 批量
@app.post("/api/batch")
def batch(payload: dict = Body(...)) -> dict:
    from . import batch as batch_mod

    ids = payload.get("project_ids") or []
    if not ids:
        raise HTTPException(400, "未选择项目")
    return _spawn("batch", f"批量处理 {len(ids)} 个模型", payload, batch_mod.run_job, None)


QUEUE_FILE = config.STATE / "print_queue.json"


def _load_queue() -> list:
    if QUEUE_FILE.exists():
        try:
            data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []
    return []


def _save_queue(items: list) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/queue")
def get_queue() -> dict:
    items = _load_queue()
    out = []
    for q in items:
        p = store.get(str(q.get("id") or ""))
        out.append({**q, "name": (p or {}).get("name") or q.get("name") or "（已删除）",
                    "exists": p is not None})
    return {"items": out, "count": len(out)}


@app.post("/api/queue")
def add_to_queue(payload: dict = Body(...)) -> dict:
    pid = str(payload.get("id") or "")
    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    items = _load_queue()
    if any(x.get("id") == pid for x in items):
        return {"ok": True, "items": items, "note": "已在队列中"}
    items.append({
        "id": pid, "name": p["name"],
        "added": datetime.now().isoformat(timespec="seconds"),
        "note": str(payload.get("note") or "")[:100],
    })
    _save_queue(items)
    return {"ok": True, "items": items, "count": len(items)}


@app.delete("/api/queue/{index}")
def remove_from_queue(index: int) -> dict:
    items = _load_queue()
    if 0 <= index < len(items):
        items.pop(index)
        _save_queue(items)
    return {"ok": True, "items": items, "count": len(items)}


@app.post("/api/queue/reorder")
def reorder_queue(payload: dict = Body(...)) -> dict:
    order = [str(x) for x in (payload.get("order") or [])]
    items = _load_queue()
    by_id = {str(x.get("id")): x for x in items}
    new = [by_id[i] for i in order if i in by_id]
    new += [x for x in items if str(x.get("id")) not in order]
    _save_queue(new)
    return {"ok": True, "items": new, "count": len(new)}


@app.get("/api/batch/report.csv")
def batch_report_csv() -> Response:
    """批量结果导出成 CSV，方便贴到表格里逐条排查。"""
    import csv as _csv
    import io as _io

    f = config.STATE / "batch_report.json"
    if not f.exists():
        raise HTTPException(404, "还没有批量结果")
    try:
        rep = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"批量报告损坏：{e}") from e
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["项目", "状态", "阶段", "面数", "输出", "错误", "警告"])
    for it in rep.get("items") or []:
        w.writerow([
            it.get("name", ""),
            it.get("status", ""),
            it.get("stage", ""),
            it.get("faces", ""),
            Path(it.get("output", "")).name if it.get("output") else "",
            it.get("error", ""),
            " | ".join(it.get("warnings") or []),
        ])
    body = "\ufeff" + buf.getvalue()  # BOM，Excel 打开不乱码
    return Response(
        body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="batch_report.csv"'},
    )


@app.get("/api/batch/export.zip")
def batch_export_zip(ids: str = Query("", description="逗号分隔的项目 id，留空导出全部")) -> Response:
    """把多个项目的当前模型打包成一个 zip。"""
    import zipfile
    import io as _io

    wanted = [x.strip() for x in ids.split(",") if x.strip()]
    projects = []
    if wanted:
        for pid in wanted:
            p = store.get(pid)
            if p:
                projects.append(p)
    else:
        for item in store.list_all():
            p = store.get(item["id"])
            if p:
                projects.append(p)
    if not projects:
        raise HTTPException(404, "没有可导出的项目")

    buf = _io.BytesIO()
    manifest = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in projects:
            src = Path(p.get("current") or p.get("source") or "")
            if not src.exists():
                continue
            name = f"{Path(p['name']).stem}_{p['id'][:6]}{src.suffix}"
            z.write(src, name)
            st = (p.get("analysis") or {}).get("stats") or {}
            manifest.append({
                "id": p["id"], "name": p["name"], "file": name,
                "faces": st.get("faces"), "volume_mm3": st.get("volume_mm3"),
            })
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="models.zip"'},
    )


@app.get("/api/batch/report")
def batch_report() -> dict:
    p = config.STATE / "batch_report.json"
    if not p.exists():
        return {"items": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"items": []}


# ---------------------------------------------------------------- 导出
@app.get("/api/projects/{pid}/download")
def download(
    pid: str,
    kind: str = Query("stl", pattern="^(stl|3mf|obj|ply|config|log|report|slicer)$"),
    printer: str | None = Query(None),
    material: str | None = Query(None),
    nozzle: float | None = Query(None),
    plan: str | None = Query(None),
) -> Response:
    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    if kind == "stl":
        src = Path(p.get("current") or p["source"])
        if not src.exists():
            raise HTTPException(404, "模型文件不存在")
        return FileResponse(src, media_type="model/stl", filename=f"{Path(p['name']).stem}_processed.stl")
    if kind in ("obj", "ply"):
        from . import meshio

        src = Path(p.get("current") or p["source"])
        if not src.exists():
            raise HTTPException(404, "模型文件不存在")
        try:
            m = meshio.load_mesh(src)
            buf = io.BytesIO()
            m.export(buf, file_type=kind)
        except Exception as e:
            raise HTTPException(500, f"{kind.upper()} 导出失败：{e}") from e
        media = "text/plain" if kind == "obj" else "application/octet-stream"
        return Response(
            buf.getvalue(),
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{Path(p["name"]).stem}.{kind}"'},
        )
    if kind == "3mf":
        from . import meshio

        src = Path(p.get("current") or p["source"])
        if not src.exists():
            raise HTTPException(404, "模型文件不存在")
        try:
            m = meshio.load_mesh(src)
            buf = io.BytesIO()
            m.export(buf, file_type="3mf")
        except Exception as e:
            raise HTTPException(500, f"3MF 导出失败：{e}") from e
        return Response(
            buf.getvalue(),
            media_type="model/3mf",
            headers={"Content-Disposition": f'attachment; filename="{Path(p["name"]).stem}.3mf"'},
        )
    if kind == "slicer":
        from . import advisor

        # 必须用界面上选的那台机器导出，否则用户选 A 机型却导出了 B 机型的参数
        ov: dict = {}
        for _k, _v in (("printer", printer), ("material", material), ("plan", plan)):
            if _v:
                ov[_k] = _v
        if nozzle:
            ov["nozzle"] = nozzle
        adv = advisor.recommend(p, ov)
        g = adv.get("geometry") or {}
        kv = {x["key"]: x["value"] for x in (adv.get("params") or [])}
        cfg = {
            "_comment": "通用切片参数配置，字段名对齐常见光固化/FDM 切片器。导入前请按机型微调。",
            "process": adv.get("process"),
            "printer": adv.get("printer_name"),
            "material": kv.get("material"),
            "model": {
                "file": Path(p.get("current") or p["source"]).name,
                "size_mm": g.get("extents"),
                "volume_cm3": round((g.get("volume_mm3") or 0) / 1000, 2),
                "watertight": g.get("watertight"),
            },
            "params": {
                "layer_height_mm": kv.get("layer_height"),
                "wall_thickness_mm": kv.get("wall_thickness"),
                "infill_percent": kv.get("infill"),
                "exposure_time_s": kv.get("exposure"),
                "nozzle_temp_c": kv.get("nozzle_temp"),
                "bed_temp_c": kv.get("bed_temp"),
                "supports": kv.get("supports"),
                "hollow": kv.get("hollow"),
            },
            # 机型全量参数：用户拿去切片器直接照着填，不用再回头查手册
            "printer_preset": adv.get("printer_preset"),
            "feasible": adv.get("feasible"),
            "estimate": adv.get("cost"),
            "uncertainty": adv.get("uncertainty"),
            "confidence": adv.get("confidence"),
            "warnings": adv.get("warnings") or [],
        }
        return Response(
            json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8"),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{Path(p["name"]).stem}_slicer.json"'},
        )
    if kind == "config":
        from . import advisor

        data = advisor.recommend(p)
        buf = io.BytesIO(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
        return Response(
            buf.getvalue(),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{Path(p["name"]).stem}_params.json"'},
        )
    if kind == "report":
        body = json.dumps(p.get("analysis") or {}, ensure_ascii=False, indent=2)
        return Response(
            body.encode("utf-8"),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{Path(p["name"]).stem}_report.json"'},
        )
    lines = [f"项目: {p['name']}  id={p['id']}", f"创建: {p['created']}", ""]
    for h in p.get("history", []):
        lines.append(f"[{h['at']}] {h['action']}  {json.dumps(h.get('params', {}), ensure_ascii=False)}")
        if h.get("note"):
            lines.append(f"    {h['note']}")
    body = "\n".join(lines).encode("utf-8")
    return Response(
        body,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{Path(p["name"]).stem}_log.txt"'},
    )


# ---------------------------------------------------------------- 静态
@app.get("/", response_class=HTMLResponse)
def index() -> str:
    f = STATIC / "index.html"
    if not f.exists():
        return "<h1>界面尚未生成</h1>"
    return f.read_text(encoding="utf-8")


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.on_event("startup")
def _startup() -> None:
    jobs.MANAGER.start()
    _warm_gpu()


def _warm_gpu() -> None:
    """后台预热 GPU 后端。

    cupy 首次 import 要 400~600ms，若等到用户点「生成建议」才触发，
    第一次请求会凭空慢半秒以上。这里在启动时扔到后台线程做掉，
    用户感知不到，首屏之后的每次建议都是热路径。
    """
    import threading

    def _work() -> None:
        try:
            from . import gpu

            gpu.backend()
            gpu.device_name()
        except Exception:
            pass

    threading.Thread(target=_work, name="gpu-warmup", daemon=True).start()
