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


@app.get("/api/workflow")
def workflow() -> dict:
    return {"steps": WORKFLOW}


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


@app.get("/api/projects")
def list_projects() -> dict:
    return {"projects": store.list_all()}


@app.get("/api/projects/{pid}")
def get_project(pid: str) -> dict:
    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    return p


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


@app.get("/api/projects/{pid}/heatmap")
def project_heatmap(
    pid: str,
    kind: str = Query("thickness", pattern="^(thickness|overhang|components)$"),
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
    params = {"actions": actions, "small_ratio": float(payload.get("small_ratio", 0.01))}
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


@app.get("/api/projects/{pid}/split/download")
def split_download(pid: str) -> Response:
    files = sorted(config.OUTPUTS.glob(f"{pid}_split_*.zip"),
                   key=lambda x: x.stat().st_mtime, reverse=True)
    if not files:
        raise HTTPException(404, "还没有分件结果")
    return FileResponse(files[0], media_type="application/zip", filename=files[0].name)


@app.get("/api/projects/{pid}/advice")
def advice(pid: str, payload: dict | None = None) -> dict:
    from . import advisor

    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    return advisor.recommend(p, overrides=payload or {})


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
def download(pid: str, kind: str = Query("stl", pattern="^(stl|3mf|config|log|report|slicer)$")) -> Response:
    p = store.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    if kind == "stl":
        src = Path(p.get("current") or p["source"])
        if not src.exists():
            raise HTTPException(404, "模型文件不存在")
        return FileResponse(src, media_type="model/stl", filename=f"{Path(p['name']).stem}_processed.stl")
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

        adv = advisor.recommend(p)
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
            "estimate": adv.get("cost"),
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
