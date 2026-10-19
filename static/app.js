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
  proc: {
    decimate_ratio: 1.0, hollow: false, wall_thickness: 2.0, drain_holes: 2,
    hole_diameter: 3.0, supports: "none", overhang_deg: 45.0, orient: true, scale: 1.0,
  },
  batchSel: new Set(),
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

function b64ToTyped(b64, T) {
  const bin = atob(b64);
  const buf = new ArrayBuffer(bin.length);
  const u8 = new Uint8Array(buf);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  return new T(buf);
}

/* ---------------------------------------------------------------- 3D 预览 */
class Viewer {
  constructor(el) {
    this.el = el;
    this.ok = false;
    this.wire = false;
    this._init();
  }

  async _init() {
    try {
      this.THREE = await import("https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js");
    } catch {
      this.el.innerHTML = '<div style="display:grid;place-items:center;height:100%;color:#5f6675;font-size:12px">3D 预览需要联网加载渲染库<br>其余功能不受影响</div>';
      return;
    }
    const T = this.THREE;
    const w = this.el.clientWidth || 600, h = this.el.clientHeight || 340;
    this.renderer = new T.WebGLRenderer({ antialias: true, alpha: false });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.renderer.setSize(w, h);
    this.renderer.setClearColor(0x0a0c10);
    this.el.appendChild(this.renderer.domElement);

    this.scene = new T.Scene();
    this.camera = new T.PerspectiveCamera(42, w / h, 0.1, 5000);
    this.group = new T.Group();
    this.group.rotation.x = -Math.PI / 2;
    this.scene.add(this.group);

    this.scene.add(new T.AmbientLight(0xffffff, 1.5));
    const d1 = new T.DirectionalLight(0xffffff, 2.2); d1.position.set(1, 1.4, 1);
    const d2 = new T.DirectionalLight(0x88aaff, 1.0); d2.position.set(-1, -0.8, -0.6);
    this.scene.add(d1, d2);

    this.mat = new T.MeshStandardMaterial({ color: 0xb9c6d8, metalness: 0.12, roughness: 0.62 });
    this.mesh = null;

    this.theta = 0.9; this.phi = 1.15; this.radius = 3; this.target = new T.Vector3();
    this._bind();
    this._loop();
    this.ok = true;
    this.resize();
  }

  _bind() {
    const c = this.renderer.domElement;
    let drag = null;
    c.addEventListener("pointerdown", (e) => {
      drag = { x: e.clientX, y: e.clientY, btn: e.button };
      c.setPointerCapture(e.pointerId);
    });
    c.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      drag.x = e.clientX; drag.y = e.clientY;
      if (drag.btn === 2) {
        const s = this.radius * 0.0016;
        this.target.x -= dx * s * Math.sin(this.theta);
        this.target.z += dx * s * Math.cos(this.theta);
        this.target.y += dy * s;
      } else {
        this.theta -= dx * 0.007;
        this.phi = Math.max(0.08, Math.min(Math.PI - 0.08, this.phi - dy * 0.007));
      }
    });
    c.addEventListener("pointerup", () => { drag = null; });
    c.addEventListener("contextmenu", (e) => e.preventDefault());
    c.addEventListener("wheel", (e) => {
      e.preventDefault();
      this.radius = Math.max(this.minR || 0.5, Math.min(this.maxR || 50, this.radius * (1 + Math.sign(e.deltaY) * 0.12)));
    }, { passive: false });
  }

  setGeometry(vb64, ib64) {
    if (!this.ok) return;
    const T = this.THREE;
    const verts = b64ToTyped(vb64, Float32Array);
    const idx = b64ToTyped(ib64, Uint32Array);
    const g = new T.BufferGeometry();
    g.setAttribute("position", new T.BufferAttribute(verts, 3));
    g.setIndex(new T.BufferAttribute(idx, 1));
    g.computeVertexNormals();
    g.computeBoundingSphere();
    if (this.mesh) { this.group.remove(this.mesh); this.mesh.geometry.dispose(); }
    this.mesh = new T.Mesh(g, this.mat);
    if (this.wire) this.mat.wireframe = true;
    this.group.add(this.mesh);
    const bs = g.boundingSphere;
    this.target.set(0, 0, 0);
    this.radius = bs.radius * 2.6;
    this.minR = bs.radius * 0.9;
    this.maxR = bs.radius * 12;
    this.camera.near = bs.radius * 0.01;
    this.camera.far = bs.radius * 40;
    this.camera.updateProjectionMatrix();
  }

  toggleWire() {
    this.wire = !this.wire;
    this.mat.wireframe = this.wire;
    return this.wire;
  }

  resize() {
    if (!this.ok) return;
    const w = this.el.clientWidth, h = this.el.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  _loop() {
    const T = this.THREE;
    const tick = () => {
      requestAnimationFrame(tick);
      const sp = Math.sin(this.phi), cp = Math.cos(this.phi);
      this.camera.position.set(
        this.target.x + this.radius * sp * Math.cos(this.theta),
        this.target.y + this.radius * cp,
        this.target.z + this.radius * sp * Math.sin(this.theta),
      );
      this.camera.lookAt(this.target);
      this.renderer.render(this.scene, this.camera);
    };
    tick();
  }

  dispose() {
    if (this.renderer) { this.renderer.dispose(); this.renderer.domElement.remove(); }
    this.ok = false;
  }
}

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
  const rows = S.projects.map((p) => {
    const sev = p.severity;
    const pill = sev === "error" ? "bad" : sev === "warning" ? "warn" : (sev ? "good" : "");
    return `<div class="project-row ${p.id === S.pid ? "active" : ""}" data-pid="${p.id}">
      <span class="project-name">${p.name}</span>
      ${sev ? `<span class="pill ${pill}">${SEV[sev] || sev}</span>` : '<span class="pill">未检测</span>'}
      <span class="project-meta">${p.history || 0} 次操作</span>
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
        <div class="project-list">${rows || '<div class="queue-empty">还没有模型</div>'}</div>
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
          <span class="dim" style="font-size:11px">统一跑：补洞 → 统一法线 → 去碎屑 → 处理</span>
        </div>
      </div>
    </div>`;

  $("#btn-add").onclick = () => $("#file-input").click();
  $$(".project-row", stage).forEach((el) => el.onclick = () => loadProject(el.dataset.pid));
  $$("[data-batch]", stage).forEach((el) => el.onclick = (e) => {
    e.preventDefault();
    const id = el.dataset.batch;
    if (S.batchSel.has(id)) S.batchSel.delete(id); else S.batchSel.add(id);
    el.classList.toggle("on", S.batchSel.has(id));
    $("input", el).checked = S.batchSel.has(id);
    $("#btn-batch").textContent = `处理选中（${S.batchSel.size}）`;
  });
  $("#btn-batch").onclick = runBatch;
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
          <span class="spacer"></span>
          <span class="dim mono" style="font-size:11px" id="meshstat"></span>
        </div>
      </div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">检测报告</div>
        <div class="block-sub">每项都带可执行的修复动作</div></div>
      ${body}
    </div>`;

  mountViewer("viewer", S.showOriginal ? "original" : "current");
  $("#btn-scan").onclick = runAnalyze;
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
  $$("[data-fix]", stage).forEach((el) => el.onclick = () => {
    const a = el.dataset.fix;
    if (!S.repairActions.includes(a)) S.repairActions.push(a);
    S.step = 2; renderSteps(); render();
    toast(`已把「${ACT_LABEL[a] || a}」加到修复队列`);
  });
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
          </select>
          <div class="field-help">按悬垂角找需要支撑的面，向下打射线找落脚点，生成柱状支撑。</div>
        </div>
        <div class="field">
          <div class="field-top"><span class="field-label">悬垂角阈值</span><span class="field-unit">°</span></div>
          <input type="number" id="p-over" value="${p.overhang_deg}" step="5" min="20" max="80">
          <div class="field-help">面法线与竖直方向夹角超过此值判为悬垂。光固化常用 40–50°。</div>
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
  bind("#p-scale", "scale");
  $("#p-sup").onchange = (e) => { S.proc.supports = e.target.value; };
  $("#p-hollow").onchange = (e) => { S.proc.hollow = e.target.checked; $("#row-hollow").classList.toggle("on", e.target.checked); };
  $("#p-orient").onchange = (e) => { S.proc.orient = e.target.checked; $("#row-orient").classList.toggle("on", e.target.checked); };
  $("#btn-proc").onclick = runProcess;
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
      <div class="block-head"><div class="block-title">导出</div>
        <div class="block-sub">模型给切片软件，配置留档，日志用于复现</div></div>
      <div class="block-body">
        <div class="row">
          <button class="primary" data-dl="stl">下载处理后的 STL</button>
          <button class="ghost" data-dl="config">下载参数配置 JSON</button>
          <button class="ghost" data-dl="report">下载检测报告</button>
          <button class="ghost" data-dl="log">下载操作日志</button>
        </div>
      </div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">操作历史</div>
        <div class="block-sub">${hist.length} 次</div></div>
      <div class="block-body">
        ${hist.length ? hist.map((h) => `<div class="metric-row">
          <span><span class="mono dim">${h.at.slice(11, 19)}</span> &nbsp;${h.action}</span>
          <b class="dim" style="font-size:10.5px">${h.note || ""}</b></div>`).join("")
        : '<div class="queue-empty">还没有操作记录</div>'}
      </div>
    </div>`;

  if (changed) {
    mountViewer("view-orig", "original");
    mountViewer("view-now", "current");
  }
  $$("[data-dl]", stage).forEach((el) => el.onclick = () => {
    const kind = el.dataset.dl;
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
    scale_factor: parseFloat($("#p-scale")?.value || "1"),
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

async function loadAdvice() {
  try {
    S.advice = await api(`/api/projects/${S.pid}/advice`);
    if (S.step !== 4) { S.step = 4; renderSteps(); }
    render();
  } catch (e) { toast("建议生成失败：" + e.message, "err"); }
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
}

boot();
