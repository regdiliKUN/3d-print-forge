import { Viewer, b64ToTyped, round } from "./viewer.js";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const S = {
  step: 0,
  workflow: [],
  projects: [],
  pid: null,
  project: null,
  analysis: null,
  advice: null,
  job: null,
  jobTimer: null,
  viewers: [],
  showOriginal: false,
  repairActions: ["merge_vertices", "fill_holes", "fix_normals", "remove_small"],
  rescaleFactor: 1,
  proc: {
    decimate_ratio: 1.0, hollow: false, wall_thickness: 2.0, drain_holes: 2,
    hole_diameter: 3.0, supports: "none", overhang_deg: 45.0,
    support_diameter: 1.0, support_spacing: 3.0, support_points: [], orient: true, scale: 1.0,
  },
  batchSel: new Set(),
  search: "",
  favOnly: false,
  redoStack: [],
  borePoints: [],
};

const PRESETS = {
  figure: {
    label: "手办（光固化）",
    hint: "细层高、镂空省料、自动支撑",
    proc: { decimate_ratio: 0.85, hollow: true, wall_thickness: 2.0, drain_holes: 2,
            hole_diameter: 3.0, supports: "auto", overhang_deg: 45.0,
            support_diameter: 1.0, support_spacing: 3.0, orient: true, scale: 1.0 },
  },
  fdm: {
    label: "FDM 功能件",
    hint: "不镂空、粗支撑、50° 悬垂",
    proc: { decimate_ratio: 1.0, hollow: false, wall_thickness: 1.2, drain_holes: 0,
            hole_diameter: 3.0, supports: "auto", overhang_deg: 50.0,
            support_diameter: 1.6, support_spacing: 5.0, orient: true, scale: 1.0 },
  },
  proto: {
    label: "快速验证",
    hint: "重度减面、不加支撑，只看形状",
    proc: { decimate_ratio: 0.5, hollow: false, supports: "none", orient: false, scale: 1.0 },
  },
};

const SEV = { ok: "通过", info: "提示", warning: "警告", error: "严重" };
const ACT_LABEL = {
  merge_vertices: "合并重复顶点", fill_holes: "补洞", fix_normals: "统一法线",
  remove_small: "移除碎屑", resolve_self_intersection: "体素重网格消解自交",
  rescale: "单位换算", thicken: "加厚",
};

/* ---------------------------------------------------------------- 基础 */
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const j = await res.json(); msg = j.detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg, kind = "") {
  const d = document.createElement("div");
  d.className = "toast " + kind;
  d.textContent = msg;
  $("#toasts").appendChild(d);
  setTimeout(() => d.remove(), kind === "err" ? 7000 : 3800);
}

function fmtBytes(n) {
  if (!n) return "-";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + u[i];
}

function fmtEta(s) {
  if (s == null || !isFinite(s)) return "";
  if (s < 60) return `剩余 ${Math.ceil(s)} 秒`;
  return `剩余 ${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒`;
}

/* ---------------------------------------------------------------- 3D 预览 */
/* ---------------------------------------------------------------- 步骤 */
const STEPS = [
  { id: "import", title: "导入模型", hint: "选择或拖入文件" },
  { id: "inspect", title: "模型检测", hint: "七项体检" },
  { id: "repair", title: "修复", hint: "补洞 / 法线 / 碎屑" },
  { id: "process", title: "处理", hint: "减面 / 镂空 / 支撑" },
  { id: "advise", title: "参数建议", hint: "材料 / 层高 / 温度" },
  { id: "export", title: "导出", hint: "模型 / 配置 / 日志" },
];

function renderSteps() {
  const nav = $("#steps");
  nav.innerHTML = STEPS.map((s, i) => {
    const cls = i === S.step ? "active" : (i < S.step ? "done" : "");
    return `<div class="step ${cls}" data-step="${i}">
      <div class="step-idx">${i < S.step ? "✓" : i + 1}</div>
      <div class="step-body">
        <div class="step-title">${s.title}</div>
        <div class="step-hint">${s.hint}</div>
      </div>
    </div>`;
  }).join("");
  $$(".step", nav).forEach((el) => el.onclick = () => goto(+el.dataset.step));
}

function goto(i) {
  if (i > 0 && !S.pid) { toast("先导入一个模型", "warn"); return; }
  if (i >= 2 && !S.analysis) { toast("先跑一次检测，否则不知道该修什么", "warn"); return; }
  S.step = i;
  renderSteps();
  render();
}

function guideOf(id) {
  const w = S.workflow.find((x) => x.id === id);
  if (!w) return "";
  return `<div class="guide">
    <h3>${w.title}</h3>
    <p>${w.goal}</p>
    <p class="why">为什么这样做：${w.why}</p>
  </div>`;
}

/* ---------------------------------------------------------------- 渲染 */
function render() {
  disposeViewers();
  const stage = $("#stage");
  if (S.step === 0) return renderImport(stage);
  if (!S.pid) return renderImport(stage);
  if (S.step === 1) return renderInspect(stage);
  if (S.step === 2) return renderRepair(stage);
  if (S.step === 3) return renderProcess(stage);
  if (S.step === 4) return renderAdvice(stage);
  if (S.step === 5) return renderExport(stage);
}

/* ---- 导入 */
function renderImport(stage) {
  if (!S.projects.length) {
    stage.innerHTML = `<div class="empty-state">
      <div class="empty-ring"></div>
      <h2>把 STL 拖进来开始</h2>
      <p>原文件不会被修改，所有操作都作用在工作副本上。支持 STL / OBJ / PLY / 3MF，可一次拖入多个。</p>
      <button class="primary" id="btn-pick2">选择模型文件</button>
    </div>`;
    $("#btn-pick2").onclick = () => $("#file-input").click();
    return;
  }
  const q = (S.search || "").toLowerCase();
  let list = S.projects.filter((p) => {
    if (S.favOnly && !p.favorite) return false;
    if (!q) return true;
    return (p.name || "").toLowerCase().includes(q)
      || (p.tags || []).some((t) => t.toLowerCase().includes(q));
  });
  list = [...list].sort((a, b) => (b.favorite ? 1 : 0) - (a.favorite ? 1 : 0));
  const rows = list.map((p) => {
    const sev = p.severity;
    const pill = sev === "error" ? "bad" : sev === "warning" ? "warn" : (sev ? "good" : "");
    return `<div class="project-row ${p.id === S.pid ? "active" : ""}" data-pid="${p.id}">
      <span class="fav ${p.favorite ? "on" : ""}" data-fav="${p.id}" title="收藏">${p.favorite ? "★" : "☆"}</span>
      <span class="project-name">${p.name}</span>
      ${(p.tags || []).map((t) => `<span class="pill tag clickable" data-tagfilter="${t}">${t}</span>`).join("")}
      ${sev ? `<span class="pill ${pill}">${SEV[sev] || sev}</span>` : '<span class="pill">未检测</span>'}
      <span class="project-meta">${p.volume_cm3 ? p.volume_cm3 + "cm³" : ""} ${p.history || 0} 次操作</span>
      <span class="row-ops">
        <button data-rename="${p.id}" title="重命名">改名</button>
        <button data-tag="${p.id}" title="编辑标签">标签</button>
      </span>
    </div>`;
  }).join("");

  stage.innerHTML = `
    ${guideOf("import")}
    <div class="block">
      <div class="block-head">
        <div><div class="block-title">已导入的模型</div><div class="block-sub">点一行载入，或继续添加新文件</div></div>
        <button class="primary" id="btn-add">添加文件</button>
      </div>
      <div class="block-body">
        <div class="row" style="margin-bottom:10px">
          <input type="text" id="proj-search" placeholder="搜索名称或标签…" value="${S.search}"
                 style="flex:1;max-width:280px">
          <button class="ghost ${S.favOnly ? "on" : ""}" id="btn-fav-only">只看收藏</button>
          <span class="dim" style="font-size:11px">${list.length}/${S.projects.length}</span>
        </div>
        <div class="project-list">${rows || '<div class="queue-empty">没有匹配的模型</div>'}</div>
      </div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">打印队列</div>
        <div class="block-sub" id="queue-stat">空队列</div></div>
      <div class="block-body">
        <div class="row" style="margin-bottom:10px">
          <button class="ghost" id="btn-queue-add">把当前项目加入队列</button>
        </div>
        <div id="queue-list"><div class="queue-empty">队列为空</div></div>
      </div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">批量处理</div>
        <div class="block-sub">勾选多个模型，套用同一套流程</div></div>
      <div class="block-body">
        <div class="project-list" id="batch-list">
          ${S.projects.map((p) => `<label class="check-row ${S.batchSel.has(p.id) ? "on" : ""}" data-batch="${p.id}">
            <input type="checkbox" ${S.batchSel.has(p.id) ? "checked" : ""}>
            <span>${p.name}</span>
            <small>${p.id}</small>
          </label>`).join("") || '<div class="queue-empty">先导入模型</div>'}
        </div>
        <div class="row" style="margin-top:12px">
          <button class="ghost" id="btn-batch">处理选中（${S.batchSel.size}）</button>
          <button class="ghost" id="btn-pack">拼盘排版（${S.batchSel.size}）</button>
          <button class="ghost" id="btn-zip">打包下载（${S.batchSel.size}）</button>
          <button class="ghost" id="btn-bundle">导出项目包</button>
          <button class="ghost" id="btn-bundle-import">导入项目包</button>
          <input type="file" id="bundle-file" accept=".zip" hidden>
          <span class="dim" style="font-size:11px">统一跑：补洞 → 统一法线 → 去碎屑 → 处理</span>
        </div>
        <div class="field-row" style="margin-top:12px">
          <div class="field">
            <div class="field-top"><span class="field-label">机型</span></div>
            <select id="pack-printer">
              <option value="">自定义</option>
              <option value="192,120">Mono X / 中型（192×120）</option>
              <option value="143,89">Mars 3（143×89）</option>
              <option value="134,75">Sonic Mini 8K（134×75）</option>
              <option value="298,165">Mono X 6K / 大型（298×165）</option>
              <option value="220,220">FDM 220×220</option>
              <option value="350,350">FDM 350×350</option>
            </select>
          </div>
          <div class="field">
            <div class="field-top"><span class="field-label">平台宽</span><span class="field-unit">mm</span></div>
            <input type="number" id="pack-w" value="192" step="10" min="40" max="1000">
          </div>
          <div class="field">
            <div class="field-top"><span class="field-label">平台深</span><span class="field-unit">mm</span></div>
            <input type="number" id="pack-d" value="120" step="10" min="40" max="1000">
          </div>
        </div>
        <div id="pack-result"></div>
      </div>
    </div>`;

  $("#btn-add").onclick = () => $("#file-input").click();
  $$(".project-row", stage).forEach((el) => el.onclick = (e) => {
    if (e.target.closest("[data-fav],[data-rename],[data-tag]")) return;
    loadProject(el.dataset.pid);
  });
  const search = $("#proj-search");
  if (search) {
    search.oninput = (e) => { S.search = e.target.value; render(); };
    search.onkeydown = (e) => { if (e.key === "Escape") { S.search = ""; render(); } };
  }
  if ($("#btn-fav-only")) $("#btn-fav-only").onclick = () => { S.favOnly = !S.favOnly; render(); };
  $$("[data-tagfilter]", stage).forEach((el) => el.onclick = (e) => {
    e.stopPropagation();
    S.search = el.dataset.tagfilter;
    render();
  });
  $$("[data-fav]", stage).forEach((el) => el.onclick = async (e) => {
    e.stopPropagation();
    const id = el.dataset.fav;
    const cur = S.projects.find((x) => x.id === id);
    try {
      await api(`/api/projects/${id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ favorite: !(cur && cur.favorite) }),
      });
      await loadProjects();
      render();
    } catch (err) { toast("操作失败：" + err.message, "err"); }
  });
  $$("[data-rename]", stage).forEach((el) => el.onclick = async (e) => {
    e.stopPropagation();
    const id = el.dataset.rename;
    const cur = S.projects.find((x) => x.id === id);
    const name = prompt("新名称", cur ? cur.name : "");
    if (!name || !name.trim()) return;
    try {
      await api(`/api/projects/${id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim() }),
      });
      await loadProjects();
      if (S.pid === id) S.project = await api(`/api/projects/${id}`);
      render();
      toast("已重命名");
    } catch (err) { toast("重命名失败：" + err.message, "err"); }
  });
  $$("[data-tag]", stage).forEach((el) => el.onclick = async (e) => {
    e.stopPropagation();
    const id = el.dataset.tag;
    const cur = S.projects.find((x) => x.id === id);
    const raw = prompt("标签（用逗号分隔，留空清除）", ((cur && cur.tags) || []).join(","));
    if (raw === null) return;
    const tags = raw.split(/[,，]/).map((t) => t.trim()).filter(Boolean);
    try {
      await api(`/api/projects/${id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tags }),
      });
      await loadProjects();
      render();
      toast(tags.length ? `已打 ${tags.length} 个标签` : "已清除标签");
    } catch (err) { toast("操作失败：" + err.message, "err"); }
  });
  $$("[data-batch]", stage).forEach((el) => el.onclick = (e) => {
    e.preventDefault();
    const id = el.dataset.batch;
    if (S.batchSel.has(id)) S.batchSel.delete(id); else S.batchSel.add(id);
    el.classList.toggle("on", S.batchSel.has(id));
    $("input", el).checked = S.batchSel.has(id);
    $("#btn-batch").textContent = `处理选中（${S.batchSel.size}）`;
  });
  $("#btn-batch").onclick = runBatch;
  $("#btn-pack").onclick = runPack;
  if ($("#btn-bundle")) $("#btn-bundle").onclick = () => {
    if (!S.pid) return toast("先选一个项目", "warn");
    window.location.href = `/api/projects/${S.pid}/bundle`;
    toast("开始导出项目包");
  };
  if ($("#btn-bundle-import")) $("#btn-bundle-import").onclick = () => $("#bundle-file").click();
  if ($("#bundle-file")) $("#bundle-file").onchange = async (e) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    try {
      const r = await api("/api/projects/import-bundle", { method: "POST", body: fd });
      await loadProjects();
      await loadProject(r.id);
      toast(`已导入「${r.name}」`);
    } catch (err) { toast("导入失败：" + err.message, "err"); }
  };
  $("#btn-zip").onclick = () => {
    const ids = [...S.batchSel];
    if (!ids.length) return toast("先勾选要打包的模型", "warn");
    window.location.href = `/api/batch/export.zip?ids=${ids.join(",")}`;
    toast(`开始打包 ${ids.length} 个模型`);
  };
  if ($("#pack-printer")) $("#pack-printer").onchange = (e) => {
    if (!e.target.value) return;
    const [w, d] = e.target.value.split(",");
    $("#pack-w").value = w;
    $("#pack-d").value = d;
  };
}

/* ---- 检测 */
function renderInspect(stage) {
  const p = S.project;
  const rep = S.analysis;
  const body = rep ? reportHTML(rep) : `<div class="queue-empty">还没有检测结果</div>`;
  stage.innerHTML = `
    ${guideOf("inspect")}
    <div class="block">
      <div class="block-head">
        <div><div class="block-title">${p ? p.name : ""}</div>
          <div class="block-sub">${rep ? `共 ${rep.summary.total} 项 · 最严重：${SEV[rep.summary.worst]}` : "尚未检测"}</div></div>
        <button class="primary" id="btn-scan">${rep ? "重新检测" : "开始检测"}</button>
      </div>
      <div class="block-body">
        <div class="viewer-wrap" id="viewer"></div>
        <div class="row" style="margin-top:10px">
          <button class="ghost" id="btn-wire">线框</button>
          <button class="ghost" id="btn-orig">看原模型</button>
          <button class="ghost" id="btn-thick">壁厚热力</button>
          <button class="ghost" id="btn-over">悬垂热力</button>
          <button class="ghost" id="btn-box">包围盒</button>
          <button class="ghost" id="btn-dims">尺寸标注</button>
          <button class="ghost" id="btn-grid">平台网格</button>
          <button class="ghost" id="btn-parts">部件分色</button>
          <button class="ghost" id="btn-supd">支撑密度</button>
          <button class="ghost" id="btn-axes">坐标轴</button>
          <button class="ghost" id="btn-normals">法线</button>
          <button class="ghost" id="btn-measure">测量</button>
          <button class="ghost" id="btn-issues">标出问题</button>
          <button class="ghost" id="btn-sup">支撑预览</button>
          <button class="ghost" id="btn-heat-off">还原</button>
          <span class="spacer"></span>
          <span class="dim mono" style="font-size:11px" id="meshstat"></span>
        </div>
        <div class="field" style="margin-top:12px">
          <div class="field-top"><span class="field-label">剖切预览</span>
            <span class="field-unit" id="clip-label">关闭</span></div>
          <input type="range" id="clip-slider" min="0" max="100" value="100">
          <div class="field-help">沿 Z 轴拖动剖开模型，检查内部空腔、壁厚和有没有打穿。</div>
        </div>
        <div class="field">
          <div class="field-top"><span class="field-label">切片层预览</span>
            <span class="field-unit" id="slice-label">关闭</span></div>
          <input type="range" id="slice-slider" min="0" max="100" value="0">
          <div class="field-help">拖动看每一层的真实截面轮廓，比剖切更适合检查内部结构和排液孔是否打通。</div>
        </div>
        <div id="heat-scale" class="heat-scale" style="display:none">
          <span class="hs-label" id="hs-lo"></span>
          <div class="hs-bar" id="hs-bar"></div>
          <span class="hs-label" id="hs-hi"></span>
        </div>
        <div class="dim" style="font-size:11px;margin-top:2px" id="heat-legend"></div>
      </div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">检测报告</div>
        <div class="block-sub">每项都带可执行的修复动作</div></div>
      ${body}
    </div>
    ${rep && rep.quality ? scorePanel(rep.quality) : ""}
    ${rep ? thicknessPanel(rep) : ""}
    <div class="block">
      <div class="block-head"><div class="block-title">三角面质量分布</div>
        <button class="ghost" id="btn-tri-hist">统计</button></div>
      <div class="block-body" id="tri-hist-body">
        <div class="queue-empty">点「统计」看三角形面积分布——大量极小的面会拖慢切片</div>
      </div>
    </div>
    ${rep ? `<div class="block">
      <div class="block-head"><div class="block-title">截面面积曲线</div>
        <button class="ghost" id="btn-profile">扫描 40 层</button></div>
      <div class="block-body" id="profile-body">
        <div class="queue-empty">点「扫描 40 层」看截面积随高度的变化——突然收窄的地方最容易断</div>
      </div>
    </div>` : ""}`;

  mountViewer("viewer", S.showOriginal ? "original" : "current");
  $("#btn-scan").onclick = runAnalyze;
  if ($("#btn-profile")) $("#btn-profile").onclick = loadProfile;
  if ($("#btn-tri-hist")) $("#btn-tri-hist").onclick = renderTriHist;
  $("#btn-wire").onclick = (e) => {
    const v = S.viewers[S.viewers.length - 1];
    if (v) e.target.classList.toggle("on", v.toggleWire());
  };
  $("#btn-orig").onclick = (e) => {
    S.showOriginal = !S.showOriginal;
    e.target.classList.toggle("on", S.showOriginal);
    const cur = S.viewers.find((v) => v.el.id === "viewer");
    if (cur) { cur.dispose(); S.viewers = S.viewers.filter((x) => x !== cur); }
    mountViewer("viewer", S.showOriginal ? "original" : "current");
  };
  $("#btn-thick").onclick = () => loadHeat("thickness");
  $("#btn-over").onclick = () => loadHeat("overhang");
  $("#btn-box").onclick = (e) => {
    const v = S.viewers.find((x) => x.el.id === "viewer");
    if (v) e.target.classList.toggle("on", v.toggleBox());
  };
  $("#btn-dims").onclick = (e) => {
    const v = S.viewers.find((x) => x.el.id === "viewer");
    if (!v) return;
    if (v.labels && v.labels.length) { v.clearLabels(); e.target.classList.remove("on"); }
    else { const n = v.showDimensions(); e.target.classList.toggle("on", n > 0); }
  };
  $("#clip-slider").oninput = (e) => {
    const t = +e.target.value / 100;
    const v = S.viewers.find((x) => x.el.id === "viewer");
    if (!v) return;
    v.setClip(t);
    $("#clip-label").textContent = t >= 1 ? "关闭" : `剖切 ${(t * 100).toFixed(0)}%`;
    // 剖开时在切面处画出截面轮廓——否则看到的只是一个空心壳
    const bb = S.modelBBox;
    if (t >= 1) { v.clearSection(); return; }
    if (bb) {
      const zmin = bb[0][2], zmax = bb[1][2];
      loadSection(zmin + (zmax - zmin) * t, true);
    }
  };
  $("#btn-heat-off").onclick = () => {
    const v = S.viewers.find((x) => x.el.id === "viewer");
    if (v) { v.clearHeatmap(); v.clearMarkers(); v.clearSegments(); }
    $("#heat-legend").textContent = "";
    $("#btn-thick").classList.remove("on");
    $("#btn-over").classList.remove("on");
    $("#btn-issues").classList.remove("on");
    $("#btn-sup").classList.remove("on");
    $("#btn-parts").classList.remove("on");
    $("#btn-supd").classList.remove("on");
    $("#heat-scale").style.display = "none";
  };
  $("#btn-grid").onclick = (e) => {
    const v = S.viewers.find((x) => x.el.id === "viewer");
    if (v) e.target.classList.toggle("on", v.setGrid(!v.grid));
  };
  $("#slice-slider").oninput = (e) => {
    const t = +e.target.value;
    const v = S.viewers.find((x) => x.el.id === "viewer");
    if (!v) return;
    if (t <= 0) {
      v.clearSection();
      $("#slice-label").textContent = "关闭";
      return;
    }
    const bb = S.modelBBox;
    if (!bb) return;
    const zmin = bb[0][2], zmax = bb[1][2];
    const z = zmin + ((zmax - zmin) * t) / 100;
    loadSection(z);
  };
  $("#btn-parts").onclick = () => loadHeat("components");
  $("#btn-supd").onclick = () => loadHeat("support");
  $("#btn-axes").onclick = (e) => {
    const v = S.viewers.find((x) => x.el.id === "viewer");
    if (v) e.target.classList.toggle("on", v.toggleAxes());
  };
  $("#btn-normals").onclick = (e) => {
    const v = S.viewers.find((x) => x.el.id === "viewer");
    if (!v) return;
    if (v.normals) { v.clearNormals(); e.target.classList.remove("on"); $("#heat-legend").textContent = ""; }
    else {
      const n = v.drawNormals();
      e.target.classList.toggle("on", n > 0);
      $("#heat-legend").textContent = `显示 ${n} 条面法线。箭头朝内说明该处法线反了，切片软件会把实体当空腔。`;
    }
  };
  $("#btn-measure").onclick = (e) => {
    const v = S.viewers.find((x) => x.el.id === "viewer");
    if (!v) return;
    if (v.pickMode) { v.disablePick(); e.target.classList.remove("on"); return; }
    S.measure = [];
    e.target.classList.add("on");
    v.enablePick((pt) => {
      S.measure.push(pt);
      v.markPoints(S.measure, 0x378add);
      if (S.measure.length === 2) {
        const [a, b] = S.measure;
        const dx = Math.abs(a[0] - b[0]), dy = Math.abs(a[1] - b[1]), dz = Math.abs(a[2] - b[2]);
        const d = Math.hypot(dx, dy, dz);
        $("#heat-legend").textContent =
          `距离 ${d.toFixed(2)} mm　（ΔX ${dx.toFixed(2)} · ΔY ${dy.toFixed(2)} · ΔZ ${dz.toFixed(2)}）`;
        S.measure = [];
        v.disablePick();
        e.target.classList.remove("on");
      } else {
        $("#heat-legend").textContent = "已选第 1 点，再点第二点";
      }
    });
    toast("点击模型上两点测距离");
  };
  $("#btn-issues").onclick = loadIssues;
  $("#btn-sup").onclick = loadSupportPreview;
  $$("[data-fix]", stage).forEach((el) => el.onclick = () => {
    const a = el.dataset.fix;
    if (!S.repairActions.includes(a)) S.repairActions.push(a);
    // 单位换算要把报告算出的系数带过去，否则执行的是 ×1
    const hit = (S.analysis?.items || []).find((x) => x.fix && x.fix.action === a && x.fix.factor);
    if (hit) S.rescaleFactor = hit.fix.factor;
    S.step = 2; renderSteps(); render();
    toast(`已把「${ACT_LABEL[a] || a}」加到修复队列`);
  });
}

function scorePanel(q) {
  const r = 40, c = 2 * Math.PI * r;
  const off = c * (1 - Math.min(100, q.score) / 100);
  const color = q.grade === "A" ? "#5dcaa5" : q.grade === "B" ? "#97c459"
              : q.grade === "C" ? "#ef9f27" : "#e24b4a";
  return `<div class="block">
    <div class="block-head"><div class="block-title">网格质量</div>
      <div class="block-sub">七项检测的综合评分</div></div>
    <div class="block-body">
      <div class="score-box">
        <svg viewBox="0 0 100 100" class="score-gauge">
          <circle cx="50" cy="50" r="${r}" fill="none" stroke="#1f2430" stroke-width="8"/>
          <circle cx="50" cy="50" r="${r}" fill="none" stroke="${color}" stroke-width="8"
            stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}"
            transform="rotate(-90 50 50)" stroke-linecap="round"/>
          <text x="50" y="46" text-anchor="middle" fill="${color}" font-size="26" font-weight="500">${q.score}</text>
          <text x="50" y="63" text-anchor="middle" fill="#8b93a5" font-size="11">${q.grade} 级</text>
        </svg>
        <div class="score-detail">
          <div class="score-verdict" style="color:${color}">${q.verdict}</div>
          ${(q.parts || []).map((p) => `<div class="score-row">
            <span>${p.name}</span>
            <div class="score-bar"><i style="width:${(p.score / p.max * 100).toFixed(0)}%;background:${p.score / p.max > 0.8 ? "#5dcaa5" : p.score / p.max > 0.5 ? "#ef9f27" : "#e24b4a"}"></i></div>
            <b>${p.score}/${p.max}</b>
          </div>`).join("")}
        </div>
      </div>
    </div>
  </div>`;
}

function renderTriHist() {
  const box = $("#tri-hist-body");
  const d = S.meshData;
  if (!box || !d) { toast("模型还没加载完", "warn"); return; }
  const verts = b64ToTyped(d.vertices, Float32Array);
  const idx = b64ToTyped(d.indices, Uint32Array);
  const n = idx.length / 3;
  const areas = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const a = idx[i * 3] * 3, b = idx[i * 3 + 1] * 3, c = idx[i * 3 + 2] * 3;
    const ux = verts[b] - verts[a], uy = verts[b + 1] - verts[a + 1], uz = verts[b + 2] - verts[a + 2];
    const vx = verts[c] - verts[a], vy = verts[c + 1] - verts[a + 1], vz = verts[c + 2] - verts[a + 2];
    const cx = uy * vz - uz * vy, cy = uz * vx - ux * vz, cz = ux * vy - uy * vx;
    areas[i] = 0.5 * Math.hypot(cx, cy, cz);
  }
  const sorted = Array.from(areas).sort((x, y) => x - y);
  const q = (p) => sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * p))];
  const p50 = q(0.5), p95 = q(0.95), p999 = q(0.999);
  const tiny = sorted.filter((x) => x < 0.01).length;
  const huge = sorted.filter((x) => x > p50 * 20).length;

  // 对数分桶
  const bins = 18;
  const lo = Math.log10(Math.max(sorted[0], 1e-6));
  const hi = Math.log10(Math.max(sorted[sorted.length - 1], 1e-5));
  const counts = new Array(bins).fill(0);
  for (const a of areas) {
    const t = (Math.log10(Math.max(a, 1e-6)) - lo) / Math.max(hi - lo, 1e-6);
    counts[Math.min(bins - 1, Math.max(0, Math.floor(t * bins)))]++;
  }
  const maxC = Math.max(...counts, 1);
  const w = 640, h = 120, bw = w / bins;
  const bars = counts.map((c, i) => {
    const bh = (c / maxC) * (h - 18);
    return `<rect x="${(i * bw).toFixed(1)}" y="${(h - 18 - bh).toFixed(1)}" width="${(bw - 1.5).toFixed(1)}"
      height="${bh.toFixed(1)}" fill="#378add" opacity=".75" rx="1"/>`;
  }).join("");

  box.innerHTML = `
    <svg viewBox="0 0 ${w} ${h}" class="profile-chart">
      ${bars}
      <line x1="0" y1="${h - 18}" x2="${w}" y2="${h - 18}" stroke="#262b38"/>
      <text x="2" y="${h - 6}" fill="#5f6675" font-size="9">${Math.pow(10, lo).toFixed(3)} mm²</text>
      <text x="${w - 2}" y="${h - 6}" text-anchor="end" fill="#5f6675" font-size="9">${Math.pow(10, hi).toFixed(1)} mm²</text>
    </svg>
    <div class="metric-row"><span class="dim">三角形总数</span><b>${n.toLocaleString()}</b></div>
    <div class="metric-row"><span class="dim">中位面积 / P95</span><b>${p50.toFixed(4)} / ${p95.toFixed(4)} mm²</b></div>
    <div class="metric-row"><span class="dim">极小面（&lt;0.01mm²）</span><b style="color:${tiny > n * 0.05 ? "#ef9f27" : "#5dcaa5"}">${tiny.toLocaleString()} 个（${(tiny / n * 100).toFixed(1)}%）</b></div>
    <div class="metric-row"><span class="dim">超大面（&gt;20×中位）</span><b>${huge.toLocaleString()} 个</b></div>
    <div class="field-help">${tiny > n * 0.05
      ? "极小面占比偏高，切片时会生成大量空走路径。建议先做一次减面或体素重网格。"
      : "面尺寸分布正常，切片负担不大。"}</div>`;
}

async function loadProfile() {
  const box = $("#profile-body");
  if (!box) return;
  box.innerHTML = '<div class="queue-empty">扫描中…</div>';
  try {
    const d = await api(`/api/projects/${S.pid}/section-profile?layers=40`);
    const rows = d.layers || [];
    if (!rows.length) { box.innerHTML = '<div class="queue-empty">没有数据</div>'; return; }
    const w = 660, h = 170, padL = 44, padR = 12, padT = 12, padB = 26;
    const maxA = Math.max(...rows.map((r) => r.area), 1);
    const x = (i) => padL + (w - padL - padR) * i / Math.max(1, rows.length - 1);
    const y = (a) => h - padB - (h - padT - padB) * (a / maxA);
    const line = rows.map((r, i) => `${x(i).toFixed(1)},${y(r.area).toFixed(1)}`).join(" ");
    const area = `M ${x(0).toFixed(1)},${(h - padB).toFixed(1)} L ` +
      rows.map((r, i) => `${x(i).toFixed(1)},${y(r.area).toFixed(1)}`).join(" L ") +
      ` L ${x(rows.length - 1).toFixed(1)},${(h - padB).toFixed(1)} Z`;
    const ticks = [0, 0.5, 1].map((f) => {
      const a = maxA * f;
      return `<text x="${padL - 6}" y="${y(a) + 3}" text-anchor="end" fill="#5f6675" font-size="9">${a.toFixed(0)}</text>`;
    }).join("");
    box.innerHTML = `
      <svg viewBox="0 0 ${w} ${h}" class="profile-chart">
        <line x1="${padL}" y1="${h - padB}" x2="${w - padR}" y2="${h - padB}" stroke="#262b38"/>
        <path d="${area}" fill="rgba(55,138,221,.14)"/>
        <polyline points="${line}" fill="none" stroke="#378add" stroke-width="1.5"/>
        ${ticks}
        <text x="${padL}" y="${h - 8}" fill="#5f6675" font-size="9">${d.zmin}mm</text>
        <text x="${w - padR}" y="${h - 8}" text-anchor="end" fill="#5f6675" font-size="9">${d.zmax}mm</text>
        <text x="${padL - 6}" y="${padT}" text-anchor="end" fill="#5f6675" font-size="9">mm²</text>
      </svg>
      <div class="field-help">${d.note}。曲线突然变窄的地方强度最弱，也是最容易断的高度。</div>`;
  } catch (e) {
    box.innerHTML = `<div class="queue-empty">扫描失败：${e.message}</div>`;
  }
}

function thicknessPanel(rep) {
  const it = (rep.items || []).find((x) => x.id === "thickness");
  if (!it || !it.value || it.value.min == null) return "";
  const v = it.value;
  const max = Math.max(Number(v.max) || 3, 2.5);
  const pct = (x) => Math.max(0, Math.min(100, (Number(x) / max) * 100));
  const mode = v.mode === "coarse" ? "粗筛" : "全量";
  return `<div class="block">
    <div class="block-head"><div class="block-title">壁厚分布</div>
      <div class="block-sub">红区 &lt; 0.4mm 打不出来，黄区 &lt; 0.8mm 偏薄</div></div>
    <div class="block-body">
      <div class="hist-track">
        <div class="hist-zone danger" style="width:${pct(0.4)}%"></div>
        <div class="hist-zone warn" style="left:${pct(0.4)}%;width:${Math.max(0, pct(0.8) - pct(0.4))}%"></div>
        <div class="hist-mark thin" style="left:${pct(v.min)}%"><i></i><span>${v.lower_bound ? "≥" : "最薄 "}${v.min}</span></div>
        ${v.p5 != null ? `<div class="hist-mark" style="left:${pct(v.p5)}%"><i></i><span>P5 ${v.p5}</span></div>` : ""}
        ${v.median != null ? `<div class="hist-mark med" style="left:${pct(v.median)}%"><i></i><span>中位 ${v.median}</span></div>` : ""}
      </div>
      <div class="hist-scale"><span>0</span><span>${max.toFixed(1)} mm</span></div>
      <div class="field-help">${v.lower_bound
        ? `粗筛结论：所有采样点都厚于 ${v.min}mm，未做全量扫描。点「壁厚热力」可看完整分布。`
        : `采样 ${v.samples} 面 · 射线方向 ${v.directions || 1} 组 · ${mode}模式。点「壁厚热力」可在 3D 里看分布。`}</div>
    </div>
  </div>`;
}

function reportHTML(rep) {
  const meta = (it) => {
    const v = it.value || {};
    const bits = [];
    if (it.id === "watertight") bits.push(`开放边 ${v.boundary_edges}`, `非流形边 ${v.nonmanifold_edges ?? 0}`);
    if (it.id === "normals") bits.push(`有向体积 ${v.signed_volume}`, `退化面 ${v.degenerate_faces}`);
    if (it.id === "thickness") bits.push(`最薄 ${v.min}mm`, `中位 ${v.median}mm`, `P5 ${v.p5}mm`, `采样 ${v.samples}`);
    if (it.id === "nonmanifold") bits.push(`边数 ${v.count}`);
    if (it.id === "self_intersection") bits.push(`穿插 ${v.count} 处`, `候选 ${v.candidate_pairs ?? 0} 组`);
    if (it.id === "components") bits.push(`部件 ${v.components}`, `碎屑 ${v.tiny ?? 0}`);
    if (it.id === "units") bits.push(`单位 ${v.unit}`, `最大 ${v.max_dim}`);
    bits.push(`${it.elapsed ?? 0}s`);
    return bits.join(" · ");
  };
  return rep.items.map((it) => `
    <div class="report-item">
      <div class="sev ${it.severity}"></div>
      <div>
        <div class="ri-name">${it.name}<span class="ri-tag">${SEV[it.severity]}</span></div>
        <div class="ri-msg">${it.message}</div>
        <div class="ri-meta">${meta(it)}</div>
      </div>
      <div>${it.fix ? `<button class="ghost" data-fix="${it.fix.action}">${it.fix.label}</button>` : ""}</div>
    </div>`).join("");
}

/* ---- 修复 */
function renderRepair(stage) {
  const acts = ["merge_vertices", "fill_holes", "fix_normals", "remove_small", "resolve_self_intersection", "rescale", "thicken"];
  const hints = {
    merge_vertices: "STL 每个三角形存独立顶点，先合并才能判断流形性",
    fill_holes: "把开放边围成的洞补成三角面",
    fix_normals: "让所有面法线朝外一致",
    remove_small: "删掉体积过小的碎屑部件",
    resolve_self_intersection: "体素重网格，一次性消解穿插（会轻微圆化锐边）",
    rescale: "按推断的单位换算尺寸",
    thicken: "体素膨胀补厚薄壁",
  };
  stage.innerHTML = `
    ${guideOf("repair")}
    <div class="block">
      <div class="block-head"><div><div class="block-title">选择修复动作</div>
        <div class="block-sub">执行时会按拓扑依赖自动排序，不用自己排</div></div></div>
      <div class="block-body">
        ${acts.map((a) => `<label class="check-row ${S.repairActions.includes(a) ? "on" : ""}" data-act="${a}">
          <input type="checkbox" ${S.repairActions.includes(a) ? "checked" : ""}>
          <span>${ACT_LABEL[a]}</span><small>${hints[a]}</small>
        </label>`).join("")}
        <div class="field-row" style="margin-top:14px">
          <div class="field">
            <div class="field-top"><span class="field-label">碎屑阈值</span><span class="field-unit">占最大部件体积比</span></div>
            <input type="number" id="p-small" value="0.01" step="0.005" min="0.0001" max="0.5">
            <div class="field-help">体积小于最大连通体此比例的部件会被删除。0.01 表示 1%。</div>
          </div>
          <div class="field">
            <div class="field-top"><span class="field-label">加厚量</span><span class="field-unit">mm</span></div>
            <input type="number" id="p-thicken" value="0.5" step="0.1" min="0.1" max="5">
            <div class="field-help">仅当勾选「加厚」时生效。体素法，锐边会轻微圆化。</div>
          </div>
        </div>
        <div class="field">
          <div class="field-top"><span class="field-label">单位换算系数</span><span class="field-unit">×</span></div>
          <input type="number" id="p-rescale" value="${S.rescaleFactor || 1}" step="0.001" min="0.001" max="1000">
          <div class="field-help">仅当勾选「单位换算」时生效。英寸模型填 25.4，厘米模型填 10；从检测报告的换算按钮进来会自动填好。</div>
        </div>
        <div class="row" style="margin-top:6px">
          <button class="primary" id="btn-repair">执行修复（${S.repairActions.length} 个动作）</button>
          <span class="dim" style="font-size:11px">修完会自动回到检测页复检</span>
        </div>
      </div>
    </div>`;

  $$("[data-act]", stage).forEach((el) => el.onclick = (e) => {
    e.preventDefault();
    const a = el.dataset.act;
    const i = S.repairActions.indexOf(a);
    if (i >= 0) S.repairActions.splice(i, 1); else S.repairActions.push(a);
    el.classList.toggle("on", S.repairActions.includes(a));
    $("input", el).checked = S.repairActions.includes(a);
    $("#btn-repair").textContent = `执行修复（${S.repairActions.length} 个动作）`;
  });
  $("#btn-repair").onclick = runRepair;
}

/* ---- 处理 */
function renderProcess(stage) {
  const p = S.proc;
  stage.innerHTML = `
    ${guideOf("process")}
    <div class="block">
      <div class="block-head"><div class="block-title">当前模型</div>
        <div class="block-sub">拖动旋转。参数改动不会立刻重算，点「开始处理」才执行</div></div>
      <div class="block-body"><div class="viewer-wrap" id="proc-view" style="height:260px"></div></div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">快速预设</div>
        <div class="block-sub">一键套用常用组合，之后还能逐项微调</div></div>
      <div class="block-body">
        <div class="row">
          ${Object.entries(PRESETS).map(([k, v]) =>
            `<button class="ghost" data-preset="${k}" title="${v.hint}">${v.label}</button>`).join("")}
          <span class="spacer"></span>
          <select id="tpl-select" style="width:170px"><option value="">套用模板…</option></select>
        </div>
        <div class="field-help" style="margin-top:8px">预设会覆盖全部处理参数；模板是你在导出页存过的组合。</div>
      </div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">处理参数</div>
        <div class="block-sub">改动会立即生效，执行前先看一眼上面</div></div>
      <div class="block-body">
        <div class="field">
          <div class="field-top"><span class="field-label">减面保留比</span><span class="field-unit" id="v-dec">${p.decimate_ratio.toFixed(2)}</span></div>
          <input type="range" id="p-dec" min="0.05" max="1" step="0.05" value="${p.decimate_ratio}">
          <div class="field-help">1.0 不减面。0.3 表示保留 30% 面数。细节件别低于 0.5，否则曲面会出棱。</div>
        </div>

        <label class="check-row ${p.hollow ? "on" : ""}" id="row-hollow">
          <input type="checkbox" id="p-hollow" ${p.hollow ? "checked" : ""}>
          <span>镂空</span><small>需要模型已水密</small>
        </label>
        <div class="field-row">
          <div class="field">
            <div class="field-top"><span class="field-label">壳厚</span><span class="field-unit">mm</span></div>
            <input type="number" id="p-wall" value="${p.wall_thickness}" step="0.1" min="0.4" max="10">
            <div class="field-help">光固化建议 1.5–2.5mm。低于 0.8mm 清洗时容易压塌。</div>
          </div>
          <div class="field">
            <div class="field-top"><span class="field-label">排液孔数量</span><span class="field-unit">个</span></div>
            <input type="number" id="p-drain" value="${p.drain_holes}" step="1" min="0" max="12">
            <div class="field-help">镂空必须开孔，否则树脂排不出、气压顶住会炸件。</div>
          </div>
        </div>
        <div class="field">
          <div class="field-top"><span class="field-label">孔径</span><span class="field-unit">mm</span></div>
          <input type="number" id="p-holed" value="${p.hole_diameter}" step="0.5" min="1" max="12">
          <div class="field-help">光固化 2.5–4mm 够用。太小洗不干净，太大影响外观。</div>
        </div>

        <div class="field">
          <div class="field-top"><span class="field-label">支撑</span></div>
          <select id="p-sup">
            <option value="none" ${p.supports === "none" ? "selected" : ""}>不加支撑</option>
            <option value="auto" ${p.supports === "auto" ? "selected" : ""}>自动生成</option>
            <option value="manual" ${p.supports === "manual" ? "selected" : ""}>手动点选</option>
          </select>
          <div class="field-help">自动：按悬垂角找面、向下打射线找落脚点。手动：在模型上点哪里加哪里。</div>
        </div>
        <div class="field">
          <div class="field-top"><span class="field-label">手动支撑点</span>
            <span class="field-unit" id="pick-count">已选 ${(p.support_points || []).length} 个</span></div>
          <div class="row">
            <button class="ghost" id="btn-pick-mode">开启点选</button>
            <button class="ghost" id="btn-pick-clear">清空</button>
            <button class="ghost" id="btn-sup-csv">导出 CSV</button>
            <button class="ghost" id="btn-sup-import">导入 CSV</button>
          </div>
          <input type="file" id="sup-csv-file" accept=".csv,.txt" hidden>
          <div class="field-help">点「开启点选」后直接点模型表面加点，绿色小球就是支撑落点。点完记得把支撑切到「手动点选」。</div>
        </div>
        <div class="field">
          <div class="field-top"><span class="field-label">悬垂角阈值</span><span class="field-unit">°</span></div>
          <input type="number" id="p-over" value="${p.overhang_deg}" step="5" min="20" max="80">
          <div class="field-help">面法线与竖直方向夹角超过此值判为悬垂。光固化常用 40–50°。</div>
        </div>
        <div class="field-row">
          <div class="field">
            <div class="field-top"><span class="field-label">支撑顶径</span><span class="field-unit">mm</span></div>
            <input type="number" id="p-supd" value="${p.support_diameter}" step="0.2" min="0.4" max="4">
            <div class="field-help">柱底会自动放大 1.8 倍。顶径太粗会在表面留疤。</div>
          </div>
          <div class="field">
            <div class="field-top"><span class="field-label">支撑间距</span><span class="field-unit">mm</span></div>
            <input type="number" id="p-sups" value="${p.support_spacing}" step="0.5" min="1" max="20">
            <div class="field-help">悬垂面上每隔多远放一根。越小越密，也越难拆。</div>
          </div>
        </div>

        <label class="check-row ${p.orient ? "on" : ""}" id="row-orient">
          <input type="checkbox" id="p-orient" ${p.orient ? "checked" : ""}>
          <span>自动摆放</span><small>枚举 6 个朝向，选悬垂少、高度低的</small>
        </label>

        <div class="field">
          <div class="field-top"><span class="field-label">缩放</span><span class="field-unit">×</span></div>
          <input type="number" id="p-scale" value="${p.scale}" step="0.05" min="0.05" max="20">
          <div class="field-help">整体等比缩放。也可用「目标高度」反算比例。</div>
        </div>

        <div class="row" style="margin-top:16px">
          <button class="primary" id="btn-proc">开始处理</button>
          <span class="dim" style="font-size:11px">处理完会生成新版本，原文件保留</span>
        </div>
      </div>
    </div>`;

  const bind = (id, key, parse = parseFloat) => {
    const el = $(id);
    if (el) el.oninput = () => { S.proc[key] = parse(el.value); };
  };
  $("#p-dec").oninput = (e) => { S.proc.decimate_ratio = +e.target.value; $("#v-dec").textContent = (+e.target.value).toFixed(2); };
  bind("#p-wall", "wall_thickness");
  bind("#p-drain", "drain_holes", (v) => parseInt(v, 10));
  bind("#p-holed", "hole_diameter");
  bind("#p-over", "overhang_deg");
  bind("#p-supd", "support_diameter");
  bind("#p-sups", "support_spacing");
  bind("#p-scale", "scale");
  $("#p-sup").onchange = (e) => { S.proc.supports = e.target.value; };
  $("#p-hollow").onchange = (e) => { S.proc.hollow = e.target.checked; $("#row-hollow").classList.toggle("on", e.target.checked); };
  $("#p-orient").onchange = (e) => { S.proc.orient = e.target.checked; $("#row-orient").classList.toggle("on", e.target.checked); };
  $$("[data-preset]", stage).forEach((el) => el.onclick = () => {
    const p = PRESETS[el.dataset.preset];
    if (!p) return;
    Object.assign(S.proc, p.proc);
    toast(`已套用「${p.label}」预设`);
    render();
  });
  (async () => {
    try {
      const t = await api("/api/templates");
      const sel = $("#tpl-select");
      if (sel) {
        for (const item of t.templates || []) {
          const o = document.createElement("option");
          o.value = item.id;
          o.textContent = item.name;
          sel.appendChild(o);
        }
        sel.onchange = async () => {
          if (!sel.value) return;
          const hit = (t.templates || []).find((x) => x.id === sel.value);
          if (!hit) return;
          Object.assign(S.proc, hit.params?.process || hit.params || {});
          toast(`已套用模板「${hit.name}」`);
          render();
        };
      }
    } catch {}
  })();
  $("#btn-proc").onclick = runProcess;
  $("#btn-pick-mode").onclick = (e) => {
    const v = S.viewers.find((x) => x.el.id === "proc-view");
    if (!v) return toast("预览还没准备好", "warn");
    if (v.pickMode) {
      v.disablePick();
      e.target.classList.remove("on");
      e.target.textContent = "开启点选";
      return;
    }
    v.enablePick((pt) => {
      // 点到已有点附近就删掉它，否则新增——比另外做个删除模式顺手
      const near = S.proc.support_points.findIndex((q) =>
        Math.hypot(q[0] - pt[0], q[1] - pt[1], q[2] - pt[2]) < 2.0);
      if (near >= 0) {
        S.proc.support_points.splice(near, 1);
        toast("已移除该支撑点");
      } else {
        S.proc.support_points.push(pt);
      }
      S.proc.supports = "manual";
      $("#p-sup").value = "manual";
      v.markPoints(S.proc.support_points, 0x5dcaa5);
      $("#pick-count").textContent = `已选 ${S.proc.support_points.length} 个`;
    });
    e.target.classList.add("on");
    e.target.textContent = "点选中（点模型加点）";
    toast("点击模型表面添加支撑点");
  };
  $("#btn-sup-csv").onclick = () => {
    const pts = S.proc.support_points || [];
    if (!pts.length) return toast("还没有支撑点", "warn");
    const csv = "x,y,z\n" + pts.map((q) => q.join(",")).join("\n");
    const blob = new Blob(["\ufeff" + csv], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "support_points.csv";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    toast(`已导出 ${pts.length} 个支撑点`);
  };
  $("#btn-sup-import").onclick = () => $("#sup-csv-file").click();
  $("#sup-csv-file").onchange = async (e) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    try {
      const text = await f.text();
      const lines = text.replace(/^\ufeff/, "").trim().split(/\r?\n/);
      const body = /[a-zA-Z]/.test(lines[0]) ? lines.slice(1) : lines;
      const pts = body.map((l) => l.split(/[,;\s]+/).map(Number))
        .filter((q) => q.length >= 3 && q.slice(0, 3).every(Number.isFinite))
        .map((q) => [round(q[0], 2), round(q[1], 2), round(q[2], 2)]);
      if (!pts.length) return toast("CSV 里没解析出有效坐标", "warn");
      S.proc.support_points.push(...pts);
      S.proc.supports = "manual";
      $("#p-sup").value = "manual";
      $("#pick-count").textContent = `已选 ${S.proc.support_points.length} 个`;
      const v = S.viewers.find((x) => x.el.id === "proc-view");
      if (v) v.markPoints(S.proc.support_points, 0x5dcaa5);
      toast(`导入 ${pts.length} 个支撑点`);
    } catch (err) { toast("导入失败：" + err.message, "err"); }
  };
  $("#btn-pick-clear").onclick = () => {
    S.proc.support_points = [];
    const v = S.viewers.find((x) => x.el.id === "proc-view");
    if (v) v.clearMarkers();
    $("#pick-count").textContent = "已选 0 个";
  };
  const pv = S.viewers.find((x) => x.el.id === "proc-view");
  if (pv && (S.proc.support_points || []).length) {
    setTimeout(() => pv.markPoints(S.proc.support_points, 0x5dcaa5), 1200);
  }
  mountViewer("proc-view", "current");
}

/* ---- 建议 */
function renderAdvice(stage) {
  const a = S.advice;
  if (!a) {
    stage.innerHTML = `${guideOf("advise")}
      <div class="block"><div class="block-body" style="text-align:center;padding:34px">
        <button class="primary" id="btn-adv">生成参数建议</button>
        <div class="field-help" style="margin-top:10px">依据检测报告里的几何量推算，不是套模板</div>
      </div></div>`;
    $("#btn-adv").onclick = loadAdvice;
    return;
  }
  const g = a.geometry;
  stage.innerHTML = `
    ${guideOf("advise")}
    <div class="block">
      <div class="block-head"><div><div class="block-title">${a.printer_name}</div>
        <div class="block-sub">${a.summary}</div></div>
        <button class="ghost" id="btn-adv2">重新生成</button></div>
      <div class="block-body">
        <div class="compare-grid">
          <div class="compare-col">
            <h4>用于决策的几何量</h4>
            ${metric("最大尺寸", g.max_dim.toFixed(1) + " mm")}
            ${metric("高度", g.height.toFixed(1) + " mm")}
            ${metric("体积", (g.volume_mm3 / 1000).toFixed(2) + " cm³")}
            ${metric("表面积", (g.area_mm2 / 100).toFixed(1) + " cm²")}
          </div>
          <div class="compare-col">
            <h4>&nbsp;</h4>
            ${metric("最薄处", g.min_wall != null ? g.min_wall.toFixed(2) + " mm" : "未测")}
            ${metric("中位壁厚", g.median_wall != null ? g.median_wall.toFixed(2) + " mm" : "未测")}
            ${metric("悬垂占比", (g.overhang_ratio * 100).toFixed(1) + " %")}
            ${metric("连通部件", g.components)}
          </div>
        </div>
      </div>
    </div>
    ${a.warnings.length ? `<div class="warn-box">${a.warnings.map((w) => `<div>⚠ ${w}</div>`).join("")}</div>` : ""}
    ${layerCompare(a)}
    <div class="block">
      <div class="block-head"><div class="block-title">推荐参数</div>
        <div class="block-sub">每条都附推导依据</div></div>
      ${a.params.map((p) => `
        <div class="advice-item">
          <div class="advice-head">
            <span class="advice-label">${p.label}</span>
            <span class="advice-value">${p.value}${p.unit ? " " + p.unit : ""}</span>
          </div>
          <div class="advice-reason">${p.reason}</div>
        </div>`).join("")}
    </div>`;

  $("#btn-adv2").onclick = loadAdvice;
}

function layerCompare(a) {
  const g = a.geometry || {};
  const base = (a.params || []).find((x) => x.key === "layer_height");
  const exp = (a.params || []).find((x) => x.key === "exposure");
  if (!base || !g.height) return "";
  const cur = Number(base.value) || 0.05;
  const curExp = Number(exp?.value) || 2.5;
  const options = [0.03, 0.05, 0.08, 0.1].filter((x) => Math.abs(x - cur) > 1e-6);
  options.unshift(cur);
  const rows = options.map((lh) => {
    const layers = Math.round(g.height / lh);
    const minutes = (layers * (curExp + 6) + 10 * 30) / 60;
    const hours = minutes / 60;
    const tag = Math.abs(lh - cur) < 1e-6 ? '<span class="pill good">推荐</span>' : "";
    return `<div class="metric-row">
      <span>${lh}mm ${tag}</span>
      <b class="dim" style="font-size:10.5px">${layers.toLocaleString()} 层 · ${hours.toFixed(2)} h</b>
    </div>`;
  }).join("");
  return `<div class="block">
    <div class="block-head"><div class="block-title">层高对比</div>
      <div class="block-sub">同一模型换层高的层数与耗时</div></div>
    <div class="block-body">${rows}
      <div class="field-help">层数按高度 ${g.height.toFixed(1)}mm 折算，每层按「曝光 + 6s 抬升」估算。层高越小表面越细，但时间线性增长。</div>
    </div>
  </div>`;
}

function metric(k, v) {
  return `<div class="metric-row"><span class="dim">${k}</span><b>${v}</b></div>`;
}

/* ---- 导出 */
function renderExport(stage) {
  const p = S.project;
  const hist = (p?.history || []).slice().reverse();
  const changed = p && p.current !== p.original;
  stage.innerHTML = `
    ${guideOf("export")}
    ${changed ? `<div class="block">
      <div class="block-head"><div class="block-title">处理前后对比</div>
        <div class="block-sub">左边是刚导入的样子，右边是当前版本，拖动可旋转</div></div>
      <div class="block-body">
        <div class="compare-grid">
          <div class="compare-col"><h4>导入时</h4>
            <div class="viewer-wrap" id="view-orig"></div>
            <div class="dim mono" id="view-orig-stat" style="font-size:10.5px;margin-top:6px"></div></div>
          <div class="compare-col"><h4>当前</h4>
            <div class="viewer-wrap" id="view-now"></div>
            <div class="dim mono" id="view-now-stat" style="font-size:10.5px;margin-top:6px"></div></div>
        </div>
      </div>
    </div>` : ""}
    <div class="block">
      <div class="block-head"><div class="block-title">任意平面切割</div>
        <div class="block-sub">沿自定义法线切开，保留侧写回当前项目，另一侧另存新项目</div></div>
      <div class="block-body">
        <div class="field-row">
          <div class="field">
            <div class="field-top"><span class="field-label">法向量</span></div>
            <div class="row">
              <input type="number" id="cut-nx" value="0" step="0.1" style="width:70px">
              <input type="number" id="cut-ny" value="0" step="0.1" style="width:70px">
              <input type="number" id="cut-nz" value="1" step="0.1" style="width:70px">
            </div>
            <div class="field-help">(0,0,1) 水平切；(1,0,0) 竖直切；(1,1,0) 斜切。</div>
          </div>
          <div class="field">
            <div class="field-top"><span class="field-label">切面位置</span>
              <span class="field-unit" id="cut-pos-label">50%</span></div>
            <input type="range" id="cut-pos" min="5" max="95" value="50">
            <div class="field-help">沿法线方向的位置百分比。</div>
          </div>
        </div>
        <div class="row"><button class="ghost" id="btn-cut">切开</button></div>
      </div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">挖孔</div>
        <div class="block-sub">点模型表面确定位置，布尔挖圆柱孔</div></div>
      <div class="block-body">
        <div class="field-row">
          <div class="field">
            <div class="field-top"><span class="field-label">孔径</span><span class="field-unit">mm</span></div>
            <input type="number" id="bore-d" value="3" step="0.5" min="0.5" max="30">
          </div>
          <div class="field">
            <div class="field-top"><span class="field-label">深度</span><span class="field-unit">mm</span></div>
            <input type="number" id="bore-depth" value="0" step="1" min="0" max="200">
            <div class="field-help">0 表示穿透。</div>
          </div>
        </div>
        <div class="row">
          <button class="ghost" id="btn-bore-pick">点选孔位（0）</button>
          <button class="ghost" id="btn-bore-apply">执行挖孔</button>
          <button class="ghost" id="btn-bore-clear">清空孔位</button>
        </div>
      </div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">打印记录</div>
        <div class="block-sub" id="print-stat">还没记录过</div></div>
      <div class="block-body">
        <div class="row">
          <button class="ghost" data-print="success">打成功了</button>
          <button class="ghost" data-print="fail">打失败了</button>
          <input type="text" id="print-note" placeholder="备注（可选，例如「支撑倒了」）" style="flex:1;max-width:280px">
        </div>
        <div id="print-list"></div>
      </div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">布尔运算 / 合并</div>
        <div class="block-sub">两件之间做并集、差集、交集，或直接合并成一个网格</div></div>
      <div class="block-body">
        <div class="field-row">
          <div class="field">
            <div class="field-top"><span class="field-label">另一个模型</span></div>
            <select id="bool-other">
              <option value="">选择…</option>
              ${S.projects.filter((x) => x.id !== S.pid)
                .map((x) => `<option value="${x.id}">${x.name}</option>`).join("")}
            </select>
          </div>
          <div class="field">
            <div class="field-top"><span class="field-label">放置方式</span></div>
            <select id="bool-layout">
              <option value="side">并排（间隔 2mm）</option>
              <option value="overlap">重合（差集/交集用）</option>
            </select>
          </div>
        </div>
        <div class="row">
          <button class="ghost" data-bool="union">并集</button>
          <button class="ghost" data-bool="difference">差集</button>
          <button class="ghost" data-bool="intersection">交集</button>
          <button class="ghost" id="btn-merge">直接合并</button>
        </div>
        <div class="field-help" style="margin-top:8px">
          布尔运算要求两个模型都水密。差集和交集需要「重合」放置才有结果。
        </div>
      </div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">精确尺寸</div>
        <div class="block-sub">按目标尺寸缩放，或一键在平台居中</div></div>
      <div class="block-body">
        <div class="field-row">
          <div class="field">
            <div class="field-top"><span class="field-label">宽 X</span><span class="field-unit">mm</span></div>
            <input type="number" id="rs-w" step="1" min="1" placeholder="不改">
          </div>
          <div class="field">
            <div class="field-top"><span class="field-label">深 Y</span><span class="field-unit">mm</span></div>
            <input type="number" id="rs-d" step="1" min="1" placeholder="不改">
          </div>
          <div class="field">
            <div class="field-top"><span class="field-label">高 Z</span><span class="field-unit">mm</span></div>
            <input type="number" id="rs-h" step="1" min="1" placeholder="不改">
          </div>
        </div>
        <div class="row" style="margin-top:8px">
          <label class="dim" style="font-size:11px;display:flex;align-items:center;gap:6px">
            <input type="checkbox" id="rs-lock" checked> 保持比例（以最大目标值等比）
          </label>
        </div>
        <div class="row" style="margin-top:10px">
          <button class="ghost" id="btn-resize">缩放</button>
          <button class="ghost" id="btn-center">平台居中</button>
          <span class="dim" style="font-size:11px" id="rs-cur"></span>
        </div>
      </div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">姿态微调</div>
        <div class="block-sub">按 90° 步进转，转完自动落到平台</div></div>
      <div class="block-body">
        <div class="row">
          ${["x", "y", "z"].map((a) => `<button class="ghost" data-rot="${a}:-90">${a.toUpperCase()} −90°</button>
            <button class="ghost" data-rot="${a}:90">${a.toUpperCase()} +90°</button>`).join("")}
        </div>
      </div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">参数模板</div>
        <div class="block-sub">把当前处理参数存成模板，下次一键套用</div></div>
      <div class="block-body">
        <div class="row">
          <input type="text" id="tpl-name" placeholder="模板名，例如「手办 1/7」" style="flex:1;max-width:260px">
          <button class="ghost" id="btn-save-tpl">存为模板</button>
        </div>
      </div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">分件</div>
        <div class="block-sub">超出成型尺寸时沿主轴切开，切面自动封盖，导出 zip</div></div>
      <div class="block-body">
        <div class="field-row">
          <div class="field">
            <div class="field-top"><span class="field-label">成型尺寸上限</span><span class="field-unit">mm</span></div>
            <input type="number" id="split-size" value="192" step="10" min="20" max="1000">
            <div class="field-help">超过这个长度的方向会被等分切开。192 对应常见中型光固化平台。</div>
          </div>
          <div class="field">
            <div class="field-top"><span class="field-label">切割轴</span></div>
            <select id="split-axis">
              <option value="auto">自动（最长边）</option>
              <option value="x">X 轴</option><option value="y">Y 轴</option><option value="z">Z 轴</option>
            </select>
            <div class="field-help">自动按最长边切，通常能切出最少的块数。</div>
          </div>
        </div>
        <div class="row" style="margin-top:6px">
          <button class="ghost" id="btn-split">执行分件</button>
          <span class="dim" style="font-size:11px">要求模型已水密</span>
        </div>
        <div id="split-result"></div>
      </div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">导出</div>
        <div class="block-sub">模型给切片软件，配置留档，日志用于复现</div></div>
      <div class="block-body">
        <div class="row">
          <button class="primary" data-dl="stl">下载处理后的 STL</button>
          <button class="ghost" data-dl="3mf">下载 3MF</button>
          <button class="ghost" data-dl="config">下载参数配置 JSON</button>
          <button class="ghost" data-dl="report">下载检测报告</button>
          <button class="ghost" data-dl="log">下载操作日志</button>
          <button class="ghost" data-dl="html">HTML 报告</button>
          <button class="ghost" data-dl="slicer">切片参数配置</button>
          <button class="ghost" data-dl="obj">导出 OBJ</button>
          <button class="ghost" data-dl="ply">导出 PLY</button>
        </div>
        <div class="field-help" style="margin-top:8px">3MF 保留单位和多体结构，比 STL 更适合交给现代切片软件。</div>
      </div>
    </div>
    ${changed ? `<div class="block">
      <div class="block-head"><div class="block-title">版本对比</div>
        <button class="ghost" id="btn-diff">计算差异</button></div>
      <div class="block-body" id="diff-body">
        <div class="queue-empty">比较「导入时」和「当前版本」的面数、体积、顶点位移</div>
      </div>
    </div>` : ""}
    <div class="block">
      <div class="block-head"><div class="block-title">操作历史</div>
        <div class="block-sub">${hist.length} 次 · 可回滚到任意一步</div></div>
      <div class="block-body">
        ${hist.length ? hist.map((h, i) => `<div class="metric-row">
          <span><span class="mono dim">${h.at.slice(11, 19)}</span> &nbsp;${h.action}</span>
          <span class="dim" style="font-size:10.5px;margin-left:auto;margin-right:10px">${h.note || ""}</span>
          <button class="ghost" style="font-size:10px;padding:2px 8px" data-revert="${(p.history || []).length - 1 - i}">回到这版</button>
        </div>`).join("")
        : '<div class="queue-empty">还没有操作记录</div>'}
        ${hist.length ? `<div class="row" style="margin-top:10px">
          <button class="ghost" id="btn-undo">撤销（Ctrl+Z）</button>
          <button class="ghost" id="btn-redo">重做（Ctrl+Y）</button>
          <button class="ghost" data-revert="-1">回到导入时</button></div>` : ""}
      </div>
    </div>`;

  if (changed) {
    mountViewer("view-orig", "original");
    mountViewer("view-now", "current");
  }
  $("#btn-split").onclick = runSplit;
  if ($("#btn-diff")) $("#btn-diff").onclick = loadDiff;
  if ($("#btn-undo")) $("#btn-undo").onclick = undoStep;
  if ($("#btn-redo")) $("#btn-redo").onclick = redoStep;
  if ($("#cut-pos")) $("#cut-pos").oninput = (e) => {
    $("#cut-pos-label").textContent = `${e.target.value}%`;
  };
  if ($("#btn-cut")) $("#btn-cut").onclick = async () => {
    const n = [Number($("#cut-nx").value), Number($("#cut-ny").value), Number($("#cut-nz").value)];
    if (!n.some((x) => Math.abs(x) > 1e-9)) return toast("法向量不能全为零", "warn");
    const bb = S.modelBBox;
    if (!bb) return toast("模型还没加载完", "warn");
    const t = Number($("#cut-pos").value) / 100;
    const center = [0, 1, 2].map((i) => bb[0][i] + (bb[1][i] - bb[0][i]) * t);
    try {
      const r = await api(`/api/projects/${S.pid}/slice-plane`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ origin: center, normal: n }),
      });
      toast(`已切开：保留 ${r.keep.faces} 面，另一半另存为「${r.other.name}」`);
      S.project = await api(`/api/projects/${S.pid}`);
      await loadProjects();
      render();
    } catch (e) { toast("切割失败：" + e.message, "err"); }
  };
  if ($("#btn-bore-pick")) $("#btn-bore-pick").onclick = (e) => {
    const v = S.viewers.find((x) => x.el.id === "viewer");
    if (!v) return toast("预览还没准备好", "warn");
    if (v.pickMode) {
      v.disablePick();
      e.target.classList.remove("on");
      e.target.textContent = `点选孔位（${S.borePoints.length}）`;
      return;
    }
    v.enablePick((pt) => {
      S.borePoints.push(pt);
      v.markPoints(S.borePoints, 0xe24b4a);
      e.target.textContent = `点选中（${S.borePoints.length}）`;
    });
    e.target.classList.add("on");
    toast("点击模型表面确定孔位");
  };
  if ($("#btn-bore-apply")) $("#btn-bore-apply").onclick = async () => {
    if (!S.borePoints.length) return toast("先点选孔位", "warn");
    try {
      const r = await api(`/api/projects/${S.pid}/bore`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          points: S.borePoints,
          diameter: Number($("#bore-d").value) || 3,
          depth: Number($("#bore-depth").value) || 0,
        }),
      });
      toast(`挖了 ${r.holes} 个孔，体积 ${(r.volume_mm3 / 1000).toFixed(2)}cm³`);
      S.borePoints = [];
      const v = S.viewers.find((x) => x.el.id === "viewer");
      if (v) v.clearMarkers();
      S.project = await api(`/api/projects/${S.pid}`);
      await loadProjects();
      render();
    } catch (e) { toast("挖孔失败：" + e.message, "err"); }
  };
  if ($("#btn-bore-clear")) $("#btn-bore-clear").onclick = () => {
    S.borePoints = [];
    const v = S.viewers.find((x) => x.el.id === "viewer");
    if (v) v.clearMarkers();
    if ($("#btn-bore-pick")) $("#btn-bore-pick").textContent = "点选孔位（0）";
    toast("已清空孔位");
  };
  if ($("#btn-queue-add")) $("#btn-queue-add").onclick = async () => {
    if (!S.pid) return toast("先选一个项目", "warn");
    try {
      await api("/api/queue", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: S.pid }),
      });
      loadQueue();
      toast("已加入打印队列");
    } catch (e) { toast("加入失败：" + e.message, "err"); }
  };
  loadQueue();
  $$("[data-print]", stage).forEach((el) => el.onclick = async () => {
    try {
      const r = await api(`/api/projects/${S.pid}/print-record`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ result: el.dataset.print, note: $("#print-note")?.value || "" }),
      });
      if ($("#print-note")) $("#print-note").value = "";
      renderPrintList(r);
      toast(`已记录：成功 ${r.success}/${r.total}（${r.rate}%）`);
    } catch (e) { toast("记录失败：" + e.message, "err"); }
  });
  loadPrintRecords();
  $$("[data-bool]", stage).forEach((el) => el.onclick = async () => {
    const other = $("#bool-other")?.value;
    if (!other) return toast("先选另一个模型", "warn");
    const overlap = $("#bool-layout")?.value === "overlap";
    el.disabled = true;
    try {
      const r = await api(`/api/projects/${S.pid}/boolean`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ op: el.dataset.bool, other_id: other, overlap }),
      });
      toast(`${r.label}完成：${r.faces.toLocaleString()} 面，${(r.volume_mm3 / 1000).toFixed(2)} cm³`);
      S.project = await api(`/api/projects/${S.pid}`);
      await loadProjects();
      render();
    } catch (e) { toast("布尔运算失败：" + e.message, "err"); }
    finally { el.disabled = false; }
  });
  if ($("#btn-merge")) $("#btn-merge").onclick = async () => {
    const other = $("#bool-other")?.value;
    if (!other) return toast("先选另一个模型", "warn");
    try {
      const r = await api(`/api/projects/${S.pid}/merge`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_ids: [other] }),
      });
      toast(`已合并 ${r.count} 件，共 ${r.faces.toLocaleString()} 面`);
      S.project = await api(`/api/projects/${S.pid}`);
      await loadProjects();
      render();
    } catch (e) { toast("合并失败：" + e.message, "err"); }
  };
  if ($("#rs-cur") && S.modelBBox) {
    const b = S.modelBBox;
    $("#rs-cur").textContent = `当前 ${(b[1][0] - b[0][0]).toFixed(1)}×${(b[1][1] - b[0][1]).toFixed(1)}×${(b[1][2] - b[0][2]).toFixed(1)}mm`;
  }
  if ($("#btn-resize")) $("#btn-resize").onclick = async () => {
    const body = { lock_ratio: $("#rs-lock").checked };
    const w = Number($("#rs-w").value), d = Number($("#rs-d").value), h = Number($("#rs-h").value);
    if (w > 0) body.width = w;
    if (d > 0) body.depth = d;
    if (h > 0) body.height = h;
    if (!body.width && !body.depth && !body.height) return toast("至少填一个目标尺寸", "warn");
    try {
      const r = await api(`/api/projects/${S.pid}/resize`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      toast(`已缩放 ×${r.factors[0]}，新尺寸 ${r.extents.map((x) => x.toFixed(1)).join("×")}mm`);
      S.project = await api(`/api/projects/${S.pid}`);
      await loadProjects();
      render();
    } catch (e) { toast("缩放失败：" + e.message, "err"); }
  };
  if ($("#btn-center")) $("#btn-center").onclick = async () => {
    try {
      const r = await api(`/api/projects/${S.pid}/center`, { method: "POST" });
      toast(`已居中，移动 (${r.moved[0]}, ${r.moved[1]})mm`);
      S.project = await api(`/api/projects/${S.pid}`);
      await loadProjects();
      render();
    } catch (e) { toast("居中失败：" + e.message, "err"); }
  };
  $$("[data-rot]", stage).forEach((el) => el.onclick = async () => {
    const [axis, deg] = el.dataset.rot.split(":");
    try {
      const r = await api(`/api/projects/${S.pid}/rotate`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ axis, degrees: Number(deg) }),
      });
      toast(`已绕 ${axis.toUpperCase()} 轴转 ${deg}°，新尺寸 ${r.extents.map((x) => x.toFixed(1)).join("×")}mm`);
      S.project = await api(`/api/projects/${S.pid}`);
      await loadProjects();
      render();
    } catch (e) { toast("旋转失败：" + e.message, "err"); }
  });
  if ($("#btn-save-tpl")) $("#btn-save-tpl").onclick = async () => {
    const name = ($("#tpl-name")?.value || "").trim();
    if (!name) return toast("先给模板起个名字", "warn");
    try {
      await api("/api/templates", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, params: { process: S.proc } }),
      });
      toast(`模板「${name}」已保存，可在处理页套用`);
      $("#tpl-name").value = "";
    } catch (e) { toast("保存失败：" + e.message, "err"); }
  };
  $$("[data-revert]", stage).forEach((el) => el.onclick = () => doRevert(+el.dataset.revert));
  $$("[data-dl]", stage).forEach((el) => el.onclick = () => {
    const kind = el.dataset.dl;
    if (kind === "html") {
      window.open(`/api/projects/${S.pid}/report.html`, "_blank");
      return;
    }
    window.location.href = `/api/projects/${S.pid}/download?kind=${kind}`;
    toast("已开始下载");
  });
}

/* ---------------------------------------------------------------- 3D 挂载 */
function disposeViewers() {
  (S.viewers || []).forEach((v) => { try { v.dispose(); } catch {} });
  S.viewers = [];
}

function mountViewer(elId, which = "current") {
  const el = document.getElementById(elId);
  if (!el || !S.pid) return;
  const v = new Viewer(el);
  S.viewers.push(v);
  const wait = setInterval(async () => {
    if (!v.ok) return;
    clearInterval(wait);
    try {
      const m = await api(`/api/projects/${S.pid}/mesh?which=${which}&max_faces=40000`);
      v.setGeometry(m.vertices, m.indices);
      if (m.bbox) S.modelBBox = m.bbox;
      S.meshData = { vertices: m.vertices, indices: m.indices, stats: m.stats };
      const st = m.stats || {};
      const tag = document.getElementById(elId + "-stat");
      if (tag) {
        tag.textContent = `${(st.faces || 0).toLocaleString()} 面 · ${(st.volume_mm3 || 0).toFixed(0)} mm³ · ${(st.extents || []).map((x) => x.toFixed(1)).join("×")} mm`;
      }
    } catch (e) {
      el.insertAdjacentHTML("beforeend",
        `<div style="position:absolute;inset:0;display:grid;place-items:center;color:#5f6675;font-size:12px">预览加载失败：${e.message}</div>`);
    }
  }, 60);
  setTimeout(() => clearInterval(wait), 9000);
}

/* ---------------------------------------------------------------- 错误卡片 */
function showErrorCard(job) {
  const stage = $("#stage");
  if (!stage) return;
  const err = job.error || "未知错误";
  const tips = [];
  if (/水密|开放边|补洞/.test(err)) tips.push({ step: 2, label: "去修复补洞" });
  if (/布尔|流形|manifold/i.test(err)) tips.push({ step: 3, label: "关掉镂空再试" });
  if (/壳厚|超过/.test(err)) tips.push({ step: 3, label: "减小壳厚" });
  if (/内存|memory|OOM/i.test(err)) tips.push({ step: 3, label: "先减面再处理" });
  tips.push({ retry: true, label: "重试一次" });

  const card = document.createElement("div");
  card.className = "error-card";
  card.innerHTML = `
    <div class="ec-head"><span class="ec-dot"></span><b>${job.title} 失败</b>
      <span class="ec-trace dim mono">trace ${job.id}</span></div>
    <div class="ec-detail">${err}</div>
    <div class="ec-actions">${tips.map((t, i) => `<button class="ghost" data-tip="${i}">${t.label}</button>`).join("")}</div>`;
  stage.prepend(card);
  $$("[data-tip]", card).forEach((b) => b.onclick = () => {
    const t = tips[+b.dataset.tip];
    if (t.retry) runAgain(job.kind);
    else goto(t.step);
  });
}

function runAgain(kind) {
  if (kind === "analyze") return runAnalyze();
  if (kind === "repair") return runRepair();
  if (kind === "process") return runProcess();
  if (kind === "batch") return runBatch();
  if (kind === "split") return runSplit();
  if (kind === "pack") return runPack();
}

/* ---------------------------------------------------------------- 动作 */
async function loadWorkflow() {
  try { S.workflow = (await api("/api/workflow")).steps; } catch { S.workflow = []; }
}

async function loadProjects() {
  try { S.projects = (await api("/api/projects")).projects; } catch { S.projects = []; }
}

async function loadProject(pid) {
  S.pid = pid;
  const p = await api(`/api/projects/${pid}`);
  S.project = p;
  S.analysis = p.analysis || null;
  S.advice = p.advice || null;
  S.step = S.analysis ? 1 : 1;
  renderSteps(); render();
}

async function uploadFiles(files) {
  if (!files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  toast(`正在导入 ${files.length} 个文件…`);
  try {
    const r = await api("/api/upload", { method: "POST", body: fd });
    await loadProjects();
    const first = r.projects[0];
    if (first) {
      toast(`已导入 ${first.name}`);
      await loadProject(first.id);
      if (files.length === 1) runAnalyze();
    }
  } catch (e) {
    toast("导入失败：" + e.message, "err");
  }
}

function startJob(res, onDone) {
  S.job = res;
  setGauge(res);
  clearInterval(S.jobTimer);
  S.jobTimer = setInterval(async () => {
    let j;
    try { j = await api(`/api/jobs/${res.id}`); } catch { return; }
    S.job = j;
    setGauge(j);
    refreshQueue();
    if (["done", "failed", "cancelled"].includes(j.status)) {
      clearInterval(S.jobTimer);
      $("#btn-pause").disabled = true;
      $("#btn-cancel").disabled = true;
      if (j.status === "failed") { toast(j.error || "任务失败", "err"); showErrorCard(j); }
      if (j.status === "cancelled") toast("已中断，可点「恢复」从断点继续", "warn");
      if (j.status === "done") onDone && onDone(j);
    }
  }, 350);
}

function setGauge(j) {
  const fill = $("#gauge-fill");
  fill.style.width = `${j.percent || 0}%`;
  fill.className = j.status === "paused" ? "paused" : (j.status === "failed" ? "failed" : "");
  $("#gauge-pct").textContent = `${(j.percent || 0).toFixed(0)}%`;
  $("#gauge-stage").textContent = j.stage || j.status;
  $("#gauge-eta").textContent = j.status === "running" ? fmtEta(j.eta) : (j.status === "paused" ? "已暂停" : "");
  $("#btn-pause").disabled = !["running", "paused"].includes(j.status);
  $("#btn-pause").textContent = j.status === "paused" ? "恢复" : "暂停";
  $("#btn-cancel").disabled = !["running", "paused", "queued"].includes(j.status);
}

async function jobCmd(cmd) {
  if (!S.job) return;
  try {
    await api(`/api/jobs/${S.job.id}/${cmd}`, { method: "POST" });
    if (cmd === "resume") {
      const j = await api(`/api/jobs/${S.job.id}`);
      startJob(j, () => refreshAfterJob(j));
    }
  } catch (e) { toast(e.message, "err"); }
}

function refreshAfterJob(j) {
  if (j.kind === "analyze") return afterAnalyze(j);
  if (j.kind === "repair") return afterRepair(j);
  if (j.kind === "process") return afterProcess(j);
  if (j.kind === "batch") return afterBatch(j);
  if (j.kind === "split") return afterSplit(j);
  if (j.kind === "pack") return afterPack(j);
}

async function runAnalyze() {
  try {
    const r = await api(`/api/projects/${S.pid}/analyze`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    startJob(r, afterAnalyze);
  } catch (e) { toast("检测启动失败：" + e.message, "err"); }
}

async function afterAnalyze(j) {
  const p = await api(`/api/projects/${S.pid}`);
  S.project = p;
  S.analysis = p.analysis;
  await loadProjects();
  renderSteps(); render();
  const s = S.analysis?.summary;
  if (s) toast(`检测完成：${s.counts.error} 项严重 / ${s.counts.warning} 项警告`, s.counts.error ? "warn" : "");
}

async function runRepair() {
  if (!S.repairActions.length) return toast("至少选一个修复动作", "warn");
  const body = {
    actions: S.repairActions,
    small_ratio: parseFloat($("#p-small")?.value || "0.01"),
    thicken_mm: parseFloat($("#p-thicken")?.value || "0.5"),
    scale_factor: parseFloat($("#p-rescale")?.value || "1"),
  };
  try {
    const r = await api(`/api/projects/${S.pid}/repair`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    startJob(r, afterRepair);
  } catch (e) { toast("修复启动失败：" + e.message, "err"); }
}

async function afterRepair(j) {
  const p = await api(`/api/projects/${S.pid}`);
  S.project = p;
  await loadProjects();
  const b = j.result?.before, a = j.result?.after;
  if (b && a) {
    toast(`修复完成：面数 ${b.faces} → ${a.faces}，水密 ${b.is_watertight ? "是" : "否"} → ${a.is_watertight ? "是" : "否"}`);
  }
  S.step = 1; renderSteps(); render();
  runAnalyze();
}

async function runProcess() {
  try {
    const r = await api(`/api/projects/${S.pid}/process`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(S.proc),
    });
    startJob(r, afterProcess);
  } catch (e) { toast("处理启动失败：" + e.message, "err"); }
}

async function afterProcess(j) {
  const p = await api(`/api/projects/${S.pid}`);
  S.project = p;
  await loadProjects();
  const rep = j.result?.report || {};
  const msgs = [];
  if (rep.hollow) msgs.push(`镂空省料 ${rep.hollow.resin_saved_pct}%`);
  if (rep.drill) msgs.push(`打了 ${rep.drill.holes} 个孔`);
  if (rep.supports?.count) msgs.push(`${rep.supports.count} 根支撑`);
  if (rep.orient) msgs.push(`姿态：${rep.orient.chosen}`);
  toast("处理完成：" + (msgs.join("，") || "无变化"));
  S.step = 4; renderSteps(); render();
  loadAdvice();
}

async function loadDiff() {
  const box = $("#diff-body");
  if (!box) return;
  box.innerHTML = '<div class="queue-empty">计算中…</div>';
  try {
    const d = await api(`/api/projects/${S.pid}/diff`);
    if (d.identical) {
      box.innerHTML = `<div class="queue-empty">${d.note || "两个版本完全一致"}</div>`;
      return;
    }
    const a = d.a || {}, b = d.b || {}, dl = d.delta || {}, vv = d.vertices || {};
    const sign = (x) => (x > 0 ? "+" : "");
    box.innerHTML = `
      <div class="compare-grid">
        <div class="compare-col"><h4>导入时</h4>
          ${metric("面数", (a.faces || 0).toLocaleString())}
          ${metric("体积", ((a.volume_mm3 || 0) / 1000).toFixed(2) + " cm³")}
          ${metric("尺寸", (a.extents || []).map((x) => x.toFixed(1)).join("×") + " mm")}
        </div>
        <div class="compare-col"><h4>当前</h4>
          ${metric("面数", (b.faces || 0).toLocaleString())}
          ${metric("体积", ((b.volume_mm3 || 0) / 1000).toFixed(2) + " cm³")}
          ${metric("尺寸", (b.extents || []).map((x) => x.toFixed(1)).join("×") + " mm")}
        </div>
      </div>
      <div style="margin-top:12px">
        ${metric("面数变化", sign(dl.faces) + (dl.faces || 0).toLocaleString())}
        ${metric("体积变化", sign(dl.volume_mm3) + ((dl.volume_mm3 || 0) / 1000).toFixed(2) + " cm³")}
        ${metric("表面积变化", sign(dl.area_mm2) + ((dl.area_mm2 || 0) / 100).toFixed(1) + " cm²")}
        ${metric("位移顶点", `${(vv.moved || 0).toLocaleString()} / ${(vv.b || 0).toLocaleString()}（${((vv.moved_ratio || 0) * 100).toFixed(1)}%）`)}
        ${metric("最大位移", (vv.max_displacement || 0).toFixed(2) + " mm")}
        ${metric("消失区域顶点", (vv.removed_regions || 0).toLocaleString())}
      </div>
      <div class="field-help" style="margin-top:10px">${d.note || ""}</div>`;
  } catch (e) {
    box.innerHTML = `<div class="queue-empty">对比失败：${e.message}</div>`;
  }
}

function undoStep() {
  const h = (S.project?.history) || [];
  if (!h.length) { toast("没有可撤销的操作", "warn"); return; }
  // 记下当前版本，redo 才能回去
  if (S.project?.current) S.redoStack.push(S.project.current);
  if (S.redoStack.length > 30) S.redoStack.shift();
  // 从后往前找第一条不是 revert 的记录——revert 本身也会进历史，
  // 不跳过的话连续撤销会一直在 revert 记录之间来回弹
  for (let i = h.length - 2; i >= 0; i--) {
    if (h[i].action !== "revert") { doRevert(i); return; }
  }
  doRevert(-1);
}

async function redoStep() {
  if (!S.redoStack.length) { toast("没有可重做的操作", "warn"); return; }
  const target = S.redoStack.pop();
  try {
    await api(`/api/projects/${S.pid}/checkout`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: target }),
    });
    S.project = await api(`/api/projects/${S.pid}`);
    S.analysis = S.project.analysis || null;
    await loadProjects();
    render();
    toast("已重做");
  } catch (e) { toast("重做失败：" + e.message, "err"); }
}

async function doRevert(index) {
  try {
    const r = await api(`/api/projects/${S.pid}/revert`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ index }),
    });
    toast(`已回滚到 ${r.label}`);
    S.project = await api(`/api/projects/${S.pid}`);
    S.analysis = S.project.analysis || null;
    await loadProjects();
    render();
  } catch (e) { toast("回滚失败：" + e.message, "err"); }
}

async function runSplit() {
  const maxSize = parseFloat($("#split-size")?.value || "192");
  const axis = $("#split-axis")?.value || "auto";
  try {
    const r = await api(`/api/projects/${S.pid}/split`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_size: maxSize, axis }),
    });
    startJob(r, afterSplit);
  } catch (e) { toast("分件启动失败：" + e.message, "err"); }
}

function afterSplit(j) {
  const res = j.result || {};
  const box = $("#split-result");
  if (!box) return;
  if (!res.pieces || !res.pieces.length) {
    box.innerHTML = `<div class="field-help" style="margin-top:8px">${res.note || "不需要分件"}</div>`;
    toast(res.note || "不需要分件");
    return;
  }
  const p = res.plan || {};
  box.innerHTML = `
    <div class="warn-box" style="margin-top:12px"><div>沿 ${(p.axis || "").toUpperCase()} 轴切成 ${res.pieces.length} 块。
      切面质心已给出，建议在该位置加 φ3mm 定位桩再装配。</div></div>
    <div class="metric-row"><span class="dim">分块</span>
      <b><a href="/api/projects/${S.pid}/download?kind=stl" style="display:none"></a>${res.pieces.length} 块已打包</b></div>
    ${res.pieces.map((x) => `<div class="metric-row">
      <span>第 ${x.index} 块 · ${x.file}</span>
      <b class="dim" style="font-size:10.5px">${x.faces.toLocaleString()} 面 · ${(x.extents || []).map((v) => v.toFixed(1)).join("×")} mm${x.watertight ? "" : " · 未闭合"}</b>
    </div>`).join("")}
    <div class="row" style="margin-top:10px">
      <button class="ghost" id="btn-split-dl">下载分件 zip</button>
    </div>`;
  $("#btn-split-dl").onclick = () => { window.location.href = `/api/projects/${S.pid}/split/download`; };
  toast(`分件完成：${res.pieces.length} 块`);
}

let sliceTimer = null;
async function loadSection(z, silent) {
  clearTimeout(sliceTimer);
  sliceTimer = setTimeout(async () => {
    const v = S.viewers.find((x) => x.el.id === "viewer");
    if (!v) return;
    try {
      const d = await api(`/api/projects/${S.pid}/section?z=${z.toFixed(2)}`);
      const n = v.drawSection(d.paths || []);
      if (silent) return;  // 剖切联动时只画线，不改切片滑块的标签
      $("#slice-label").textContent = `Z = ${d.z} mm · ${n} 条轮廓 · ${d.area} mm²`;
      $("#heat-legend").textContent = n
        ? `第 ${d.z}mm 层有 ${n} 条封闭轮廓，截面面积 ${d.area}mm²。多条轮廓说明这一层有内孔或空腔。`
        : `${d.z}mm 高度处没有实体。`;
    } catch (e) { toast("切片失败：" + e.message, "err"); }
  }, 120);
}

function showHeatScale(kind, meta) {
  const box = $("#heat-scale");
  if (!box) return;
  if (kind === "components") { box.style.display = "none"; return; }
  const isT = kind === "thickness";
  const hi = isT ? 1.5 : 90.0;
  const unit = isT ? "mm" : "°";
  $("#hs-lo").textContent = `0${unit}`;
  $("#hs-hi").textContent = `${hi}${unit}`;
  $("#hs-bar").style.background = "linear-gradient(90deg,#ff1f1f,#ffbf00,#22e022)";
  box.style.display = "flex";
}

async function loadIssues() {
  const v = S.viewers.find((x) => x.el.id === "viewer");
  if (!v) return toast("预览还没准备好", "warn");
  try {
    const d = await api(`/api/projects/${S.pid}/issues`);
    const c = d.counts || {};
    v.clearMarkers();
    // 三类问题用不同颜色，避免混在一起看不清
    const groups = [
      [d.self_intersection, 0xe24b4a, "自交"],
      [d.holes, 0xef9f27, "破洞"],
      [d.debris, 0xa855f7, "碎屑"],
    ];
    let total = 0;
    for (const [pts, color] of groups) {
      if (!pts || !pts.length) continue;
      v.markPoints(pts, color);
      total += pts.length;
    }
    $("#heat-legend").textContent = total
      ? `已在模型上标出 ${total} 处：红=自交（${c.self_intersection || 0}）、橙=破洞（${c.holes || 0}）、紫=碎屑（${c.debris || 0}）`
      : "没找到可定位的问题点（模型可能是干净的，或问题太小定位不到）";
    $("#btn-issues").classList.toggle("on", total > 0);
    if (!total) toast("没有可定位的问题点");
  } catch (e) { toast("问题定位失败：" + e.message, "err"); }
}

async function loadSupportPreview() {
  const v = S.viewers.find((x) => x.el.id === "viewer");
  if (!v) return toast("预览还没准备好", "warn");
  toast("正在计算支撑位置…");
  try {
    const d = await api(`/api/projects/${S.pid}/preview-supports`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        overhang_deg: S.proc.overhang_deg,
        diameter: S.proc.support_diameter || 1.0,
        spacing: S.proc.support_spacing || 3.0,
      }),
    });
    const n = v.drawSegments(d.segments || []);
    $("#heat-legend").textContent = n
      ? `支撑预览：${n} 根，高度 ${d.min_height}–${d.max_height}mm。橙色线就是将要生成的支撑柱。`
      : d.note || "没有需要支撑的悬垂面";
    $("#btn-sup").classList.toggle("on", n > 0);
  } catch (e) { toast("支撑预览失败：" + e.message, "err"); }
}

async function loadHeat(kind) {
  const v = S.viewers.find((x) => x.el.id === "viewer");
  if (!v) return toast("预览还没准备好", "warn");
  toast(kind === "thickness" ? "正在算壁厚场…" : "正在算悬垂场…");
  try {
    const m = await api(`/api/projects/${S.pid}/heatmap?kind=${kind}&max_faces=50000`);
    v.setGeometry(m.vertices, m.indices);
    const vals = b64ToTyped(m.values, Float32Array);
    v.setHeatmap(vals, kind);
    const meta = m.meta || {};
    const el = $("#heat-legend");
    if (el && kind === "support") {
      window.__supportMax = meta.max || 10;
      el.textContent = `${meta.note}。颜色按 ${meta.unit} 归一，越红越密——支撑过密会难拆且留疤，大片灰说明悬垂被漏了。`;
    } else if (el && kind === "components") {
      const sizes = meta.sizes || [];
      el.textContent = `模型由 ${meta.components ?? "-"} 个互不连通的部件组成，同色即同一部件。`
        + (sizes.length > 1 ? ` 最大部件占 ${(sizes[0].area / sizes.reduce((a, x) => a + x.area, 0) * 100).toFixed(1)}% 表面积。` : "");
    } else if (el) {
      el.textContent = kind === "thickness"
        ? `壁厚场 ${meta.min ?? "-"}–${meta.max ?? "-"} mm，P5 = ${meta.p5 ?? "-"} mm。颜色按 0–1.5mm 映射，红=打不出来，橙=偏薄，绿=安全。`
        : `悬垂角 ${meta.min ?? "-"}–${meta.max ?? "-"}°，阈值 ${meta.threshold ?? 45}°。颜色按 0–90° 映射，红=必须加支撑。`;
    }
    showHeatScale(kind, meta);
    $("#btn-thick").classList.toggle("on", kind === "thickness");
    $("#btn-over").classList.toggle("on", kind === "overhang");
    $("#btn-parts").classList.toggle("on", kind === "components");
    $("#btn-supd").classList.toggle("on", kind === "support");
  } catch (e) {
    toast("热力图生成失败：" + e.message, "err");
  }
}

async function loadAdvice() {
  try {
    S.advice = await api(`/api/projects/${S.pid}/advice`);
    if (S.step !== 4) { S.step = 4; renderSteps(); }
    render();
  } catch (e) { toast("建议生成失败：" + e.message, "err"); }
}

async function runPack() {
  if (!S.batchSel.size) return toast("先勾选要排版的模型", "warn");
  const ids = [...S.batchSel];
  try {
    const r = await api(`/api/projects/${ids[0]}/pack`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_ids: ids,
        platform_w: parseFloat($("#pack-w")?.value || "192"),
        platform_d: parseFloat($("#pack-d")?.value || "120"),
        gap: 4,
      }),
    });
    startJob(r, afterPack);
  } catch (e) { toast("拼盘启动失败：" + e.message, "err"); }
}

async function loadQueue() {
  const list = $("#queue-list");
  const stat = $("#queue-stat");
  if (!list) return;
  try {
    const d = await api("/api/queue");
    const items = d.items || [];
    stat.textContent = items.length ? `${items.length} 件待打` : "空队列";
    if (!items.length) { list.innerHTML = '<div class="queue-empty">队列为空</div>'; return; }
    list.innerHTML = items.map((q, i) => `<div class="metric-row">
      <span>${i + 1}. ${q.name}${q.exists ? "" : ' <span class="pill bad">已删除</span>'}</span>
      <span class="dim" style="font-size:10.5px;margin-left:auto;margin-right:8px">${q.note || ""}</span>
      <span class="row-ops" style="opacity:1">
        ${i > 0 ? `<button data-qmove="${i}:-1" title="上移">↑</button>` : ""}
        ${i < items.length - 1 ? `<button data-qmove="${i}:1" title="下移">↓</button>` : ""}
        <button data-qdel="${i}" title="移出队列">✕</button>
      </span>
    </div>`).join("");
    $$("[data-qdel]", list).forEach((el) => el.onclick = async () => {
      await api(`/api/queue/${el.dataset.qdel}`, { method: "DELETE" });
      loadQueue();
    });
    $$("[data-qmove]", list).forEach((el) => el.onclick = async () => {
      const [idx, delta] = el.dataset.qmove.split(":").map(Number);
      const ids = items.map((x) => x.id);
      const j = idx + delta;
      if (j < 0 || j >= ids.length) return;
      [ids[idx], ids[j]] = [ids[j], ids[idx]];
      await api("/api/queue/reorder", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ order: ids }),
      });
      loadQueue();
    });
  } catch {}
}

async function loadPrintRecords() {
  if (!$("#print-list")) return;
  try {
    renderPrintList(await api(`/api/projects/${S.pid}/print-record`));
  } catch {}
}

function renderPrintList(d) {
  const stat = $("#print-stat");
  const list = $("#print-list");
  if (!stat || !list) return;
  const rows = d.prints || [];
  stat.textContent = rows.length
    ? `共 ${d.total} 次，成功 ${d.success} 次（${d.rate}%）`
    : "还没记录过";
  if (!rows.length) { list.innerHTML = ""; return; }
  list.innerHTML = rows.slice().reverse().map((r) => `<div class="metric-row">
    <span>${r.result === "success" ? '<span class="pill good">成功</span>' : '<span class="pill bad">失败</span>'}
      <span class="mono dim" style="font-size:10px;margin-left:6px">${r.at.slice(5, 16).replace("T", " ")}</span></span>
    <b class="dim" style="font-size:10.5px">${r.note || ""}</b>
  </div>`).join("");
}

function packPlan(res) {
  const pw = (res.platform || [192, 120])[0];
  const pd = (res.platform || [192, 120])[1];
  const placed = res.placed || [];
  if (!placed.length) return "";
  const W = 320, scale = W / Math.max(pw, pd);
  const w = pw * scale, h = pd * scale;
  const rects = placed.map((x, i) => {
    const colors = ["#378add", "#5dcaa5", "#ef9f27", "#a855f7", "#e24b4a", "#97c459"];
    const c = colors[i % colors.length];
    return `<rect x="${(x.x * scale).toFixed(1)}" y="${(x.y * scale).toFixed(1)}"
      width="${(x.w * scale).toFixed(1)}" height="${(x.d * scale).toFixed(1)}"
      fill="${c}" fill-opacity="0.22" stroke="${c}" stroke-width="1" rx="2"/>
      <text x="${(x.x * scale + x.w * scale / 2).toFixed(1)}"
            y="${(x.y * scale + x.d * scale / 2 + 3).toFixed(1)}"
            text-anchor="middle" fill="${c}" font-size="9">${i + 1}</text>`;
  }).join("");
  return `<div style="margin-top:12px">
    <div class="dim" style="font-size:11px;margin-bottom:6px">平台俯视图 ${pw}×${pd}mm</div>
    <svg viewBox="0 0 ${w} ${h}" style="width:${w}px;max-width:100%;background:#0a0c10;border:1px solid var(--line);border-radius:6px">
      <g>${rects}</g>
    </svg>
  </div>`;
}

function afterPack(j) {
  const res = j.result || {};
  const box = $("#pack-result");
  if (!box) return;
  const placed = res.placed || [];
  const skipped = res.skipped || [];
  box.innerHTML = `
    <div class="warn-box" style="margin-top:12px">
      <div>平台 ${res.platform?.[0]}×${res.platform?.[1]}mm，间距 ${res.gap}mm。${res.note || ""}</div>
      ${skipped.map((x) => `<div>⚠ ${x.name}：${x.reason}</div>`).join("")}
    </div>
    ${placed.map((x, i) => `<div class="metric-row">
      <span>${i + 1}. ${x.name}</span>
      <b class="dim" style="font-size:10.5px">${x.w}×${x.d}mm → 位置 (${x.x}, ${x.y})</b>
    </div>`).join("")}
    ${packPlan(res)}
    ${res.output ? `<div class="row" style="margin-top:10px">
      <button class="ghost" id="btn-pack-dl">下载拼盘 STL</button></div>` : ""}`;
  const dl = $("#btn-pack-dl");
  if (dl) dl.onclick = () => { window.location.href = `/api/projects/${S.pid}/download?kind=stl`; };
  toast(res.note || "拼盘完成");
}

async function runBatch() {
  if (!S.batchSel.size) return toast("先勾选要处理的模型", "warn");
  const body = {
    project_ids: [...S.batchSel],
    params: {
      repair_actions: ["merge_vertices", "fill_holes", "fix_normals", "remove_small"],
      process: { ...S.proc, hollow: false },
    },
  };
  try {
    const r = await api("/api/batch", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    startJob(r, afterBatch);
  } catch (e) { toast("批量启动失败：" + e.message, "err"); }
}

async function afterBatch(j) {
  const rep = j.result || {};
  const fails = (rep.items || []).filter((x) => x.status === "failed");
  await loadProjects();
  renderSteps(); render();
  const q = $("#queue");
  if (q && rep.items) {
    q.insertAdjacentHTML("afterbegin",
      `<div class="job-card"><div class="job-top"><span class="job-title">批量结果</span></div>
       <div class="job-msg">${rep.ok}/${rep.total} 成功</div>
       <div class="job-ops"><a href="/api/batch/report.csv" style="font-size:10px;padding:2px 7px;border:1px solid var(--line);border-radius:4px;color:var(--muted)">导出 CSV</a></div></div>`);
  }
  if (fails.length) {
    toast(`批量完成 ${rep.ok}/${rep.total}，${fails.length} 个失败`, "warn");
    fails.slice(0, 4).forEach((f) => toast(`${f.name} · ${f.stage}：${f.error}`, "err"));
  } else {
    toast(`批量完成：${rep.ok}/${rep.total} 全部成功`);
  }
}

/* ---------------------------------------------------------------- 队列与资源 */
async function refreshQueue() {
  let jobs = [];
  try { jobs = (await api("/api/jobs")).jobs; } catch { return; }
  const box = $("#queue");
  if (!jobs.length) { box.className = "queue-empty"; box.textContent = "暂无任务"; return; }
  box.className = "";
  box.innerHTML = jobs.slice(0, 12).map((j) => `
    <div class="job-card">
      <div class="job-top">
        <span class="job-title">${j.title}</span>
        <span class="job-state st-${j.status}">${statusText(j.status)}</span>
      </div>
      <div class="job-bar"><i style="width:${j.percent}%"></i></div>
      <div class="job-msg">${j.message || j.stage || ""}</div>
      <div class="job-ops">
        ${j.status === "running" || j.status === "paused"
          ? `<button data-jcmd="pause" data-jid="${j.id}">${j.status === "paused" ? "恢复" : "暂停"}</button>` : ""}
        ${["failed", "cancelled"].includes(j.status) ? `<button data-jcmd="resume" data-jid="${j.id}">断点续跑</button>` : ""}
        ${["running", "paused", "queued"].includes(j.status) ? `<button data-jcmd="cancel" data-jid="${j.id}">中断</button>` : ""}
      </div>
    </div>`).join("");

  $$("[data-jcmd]", box).forEach((b) => b.onclick = async () => {
    const cmd = b.dataset.jcmd === "pause" && b.textContent === "恢复" ? "resume" : b.dataset.jcmd;
    try {
      await api(`/api/jobs/${b.dataset.jid}/${cmd}`, { method: "POST" });
      if (S.job && S.job.id === b.dataset.jid) {
        const j = await api(`/api/jobs/${b.dataset.jid}`);
        startJob(j, () => refreshAfterJob(j));
      }
      refreshQueue();
    } catch (e) { toast(e.message, "err"); }
  });
}

function statusText(s) {
  return { pending: "等待", queued: "排队", running: "运行中", paused: "已暂停", done: "完成", failed: "失败", cancelled: "已中断" }[s] || s;
}

async function pollResources() {
  try {
    const r = await api("/api/system/resources");
    $("#res-cpu").style.width = `${Math.min(100, r.cpu_percent)}%`;
    $("#res-cpu-v").textContent = `${(r.cpu_percent || 0).toFixed(0)}%`;
    const memPct = r.mem_limit_mb ? Math.min(100, (r.proc_mem_mb / r.mem_limit_mb) * 100) : r.mem_percent;
    $("#res-mem").style.width = `${memPct}%`;
    $("#res-mem-v").textContent = `${(r.proc_mem_mb || 0).toFixed(0)}M`;
    $("#res-mem").style.background = memPct > 85 ? "var(--error)" : "var(--info)";
    const g = r.gpu || {};
    $("#res-gpu").textContent = g.available ? `${g.name} ${(g.vram_mb / 1024).toFixed(0)}G` : "CPU 模式";
  } catch {}
}

/* ---------------------------------------------------------------- 启动 */
function bindDrop() {
  const veil = $("#dropveil");
  let depth = 0;
  window.addEventListener("dragenter", (e) => { e.preventDefault(); depth++; veil.classList.add("on"); });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; veil.classList.remove("on"); } });
  window.addEventListener("drop", (e) => {
    e.preventDefault(); depth = 0; veil.classList.remove("on");
    if (e.dataTransfer?.files?.length) uploadFiles([...e.dataTransfer.files]);
  });
  $("#file-input").onchange = (e) => { uploadFiles([...e.target.files]); e.target.value = ""; };
  $("#btn-pick")?.addEventListener("click", () => $("#file-input").click());
}

async function boot() {
  await loadWorkflow();
  await loadProjects();
  renderSteps();
  render();
  bindDrop();
  $("#btn-pause").onclick = () => jobCmd(S.job?.status === "paused" ? "resume" : "pause");
  $("#btn-cancel").onclick = () => jobCmd("cancel");
  pollResources();
  setInterval(pollResources, 2000);
  refreshQueue();
  setInterval(refreshQueue, 1500);
  window.addEventListener("resize", () => (S.viewers || []).forEach((v) => v.resize()));
  window.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea") return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !e.shiftKey) {
      e.preventDefault();
      undoStep();
    }
    if ((e.ctrlKey || e.metaKey) && (e.key.toLowerCase() === "y"
        || (e.key.toLowerCase() === "z" && e.shiftKey))) {
      e.preventDefault();
      redoStep();
    }
  });
}

boot();
