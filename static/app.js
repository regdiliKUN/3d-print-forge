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
    support_diameter: 1.0, support_spacing: 3.0, orient: true, scale: 1.0,
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

    // 剖切：用局部裁剪平面沿模型 Z 轴（世界 Y）切开，看内部空腔
    this.renderer.localClippingEnabled = true;
    this.clipPlane = new T.Plane(new T.Vector3(0, -1, 0), 1e9);
    this.mat.clippingPlanes = [this.clipPlane];
    this.mat.clipShadows = false;
    this.boxHelper = null;
    this.markers = [];

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
    g.computeBoundingBox();
    if (this.mesh) { this.group.remove(this.mesh); this.mesh.geometry.dispose(); }
    this.mesh = new T.Mesh(g, this.mat);
    this.mat.vertexColors = false;
    this.mat.color.set(0xb9c6d8);
    this.mat.needsUpdate = true;
    if (this.wire) this.mat.wireframe = true;
    this.group.add(this.mesh);
    this.box = g.boundingBox.clone();
    this.clipPlane.constant = this.box.max.y + 1;
    this.clearBox();
    const bs = g.boundingSphere;
    this.target.set(0, 0, 0);
    this.radius = bs.radius * 2.6;
    this.minR = bs.radius * 0.9;
    this.maxR = bs.radius * 12;
    this.camera.near = bs.radius * 0.01;
    this.camera.far = bs.radius * 40;
    this.camera.updateProjectionMatrix();
  }

  setHeatmap(vals, kind) {
    if (!this.ok || !this.mesh) return;
    const T = this.THREE;
    const n = vals.length;
    const colors = new Float32Array(n * 3);
    if (kind === "components") {
      // 分类着色：相邻编号用对比色，部件边界一眼可辨
      const pal = [
        [0.36, 0.79, 0.65], [0.22, 0.54, 0.87], [0.94, 0.62, 0.15],
        [0.89, 0.29, 0.29], [0.66, 0.33, 0.97], [0.45, 0.78, 0.36],
        [0.9, 0.45, 0.7], [0.55, 0.62, 0.72],
      ];
      for (let i = 0; i < n; i++) {
        const c = pal[Math.round(vals[i]) % pal.length];
        colors[i * 3] = c[0]; colors[i * 3 + 1] = c[1]; colors[i * 3 + 2] = c[2];
      }
    } else {
      // 壁厚：0–1.5mm 红到绿（1.5mm 以上一律安全色，薄区才醒目）
      // 悬垂角：0–90° 红到绿
      const hi = kind === "thickness" ? 1.5 : 90.0;
      for (let i = 0; i < n; i++) {
        let t = vals[i] / hi;
        t = t < 0 ? 0 : t > 1 ? 1 : t;
        let r, g2, b;
        if (t < 0.5) { r = 1; g2 = t * 2; b = 0.12; }
        else { r = (1 - t) * 2; g2 = 1; b = 0.12; }
        colors[i * 3] = r; colors[i * 3 + 1] = g2; colors[i * 3 + 2] = b;
      }
    }
    this.mesh.geometry.setAttribute("color", new T.BufferAttribute(colors, 3));
    this.mat.vertexColors = true;
    this.mat.color.set(0xffffff);
    this.mat.needsUpdate = true;
  }

  clearHeatmap() {
    if (!this.ok || !this.mesh) return;
    this.mat.vertexColors = false;
    this.mat.color.set(0xb9c6d8);
    this.mat.needsUpdate = true;
  }

  setClip(t) {
    if (!this.ok || !this.box) return;
    const lo = this.box.min.y, hi = this.box.max.y;
    this.clipPlane.constant = t >= 1 ? hi + 1 : lo + (hi - lo) * t;
  }

  toggleBox() {
    if (!this.ok || !this.box) return false;
    const T = this.THREE;
    if (this.boxHelper) { this.clearBox(); return false; }
    this.boxHelper = new T.Box3Helper(this.box, 0x3f8f78);
    this.group.add(this.boxHelper);
    return true;
  }

  clearBox() {
    if (this.boxHelper) {
      this.group.remove(this.boxHelper);
      this.boxHelper.geometry?.dispose?.();
      this.boxHelper = null;
    }
  }

  markPoints(pts, color = 0xe24b4a) {
    if (!this.ok) return;
    const T = this.THREE;
    this.clearMarkers();
    const geo = new T.SphereGeometry(Math.max(0.4, (this.box ? this.box.getSize(new T.Vector3()).length() : 20) * 0.006), 8, 8);
    const mat = new T.MeshBasicMaterial({ color });
    for (const p of pts.slice(0, 400)) {
      const s = new T.Mesh(geo, mat);
      s.position.set(p[0], p[1], p[2]);
      this.group.add(s);
      this.markers.push(s);
    }
  }

  clearMarkers() {
    (this.markers || []).forEach((m) => this.group.remove(m));
    this.markers = [];
  }

  drawSegments(segs) {
    if (!this.ok) return 0;
    const T = this.THREE;
    this.clearSegments();
    if (!segs || !segs.length) return 0;
    const pts = [];
    for (const s of segs) {
      // [x, y, z0, z1] 模型坐标系，加到 group 跟着一起转
      pts.push(s[0], s[1], s[2], s[0], s[1], s[3]);
    }
    const g = new T.BufferGeometry();
    g.setAttribute("position", new T.Float32BufferAttribute(pts, 3));
    const mat = new T.LineBasicMaterial({ color: 0xef9f27, transparent: true, opacity: 0.85 });
    this.segs = new T.LineSegments(g, mat);
    this.group.add(this.segs);
    return segs.length;
  }

  clearSegments() {
    if (this.segs) {
      this.group.remove(this.segs);
      this.segs.geometry?.dispose?.();
      this.segs = null;
    }
  }

  setGrid(on, w = 200, d = 200) {
    if (!this.ok) return false;
    const T = this.THREE;
    this.clearGrid();
    if (!on) return false;
    const step = 10;
    const pts = [];
    const x0 = -w / 2, z0 = -d / 2;
    for (let x = 0; x <= w; x += step) pts.push(x0 + x, 0, z0, x0 + x, 0, z0 + d);
    for (let z = 0; z <= d; z += step) pts.push(x0, 0, z0 + z, x0 + w, 0, z0 + z);
    const g = new T.BufferGeometry();
    g.setAttribute("position", new T.Float32BufferAttribute(pts, 3));
    const mat = new T.LineBasicMaterial({ color: 0x2a3644, transparent: true, opacity: 0.9 });
    this.grid = new T.LineSegments(g, mat);
    // 加到 scene：平台在世界水平面，不该跟着模型转
    this.scene.add(this.grid);
    return true;
  }

  drawNormals(maxCount = 600) {
    if (!this.ok || !this.mesh) return 0;
    const T = this.THREE;
    this.clearNormals();
    const g = this.mesh.geometry;
    const pos = g.attributes.position?.array;
    const nrm = g.attributes.normal?.array;
    const idx = g.index?.array;
    if (!pos || !nrm || !idx) return 0;
    const tri = idx.length / 3;
    const step = Math.max(1, Math.floor(tri / maxCount));
    const size = this.box ? this.box.getSize(new T.Vector3()).length() : 30;
    const len = size * 0.025;
    const pts = [];
    for (let t = 0; t < tri; t += step) {
      const a = idx[t * 3], b = idx[t * 3 + 1], c = idx[t * 3 + 2];
      const cx = (pos[a * 3] + pos[b * 3] + pos[c * 3]) / 3;
      const cy = (pos[a * 3 + 1] + pos[b * 3 + 1] + pos[c * 3 + 1]) / 3;
      const cz = (pos[a * 3 + 2] + pos[b * 3 + 2] + pos[c * 3 + 2]) / 3;
      const nx = nrm[a * 3] + nrm[b * 3] + nrm[c * 3];
      const ny = nrm[a * 3 + 1] + nrm[b * 3 + 1] + nrm[c * 3 + 1];
      const nz = nrm[a * 3 + 2] + nrm[b * 3 + 2] + nrm[c * 3 + 2];
      const ln = Math.hypot(nx, ny, nz) || 1;
      pts.push(cx, cy, cz, cx + (nx / ln) * len, cy + (ny / ln) * len, cz + (nz / ln) * len);
    }
    const geo = new T.BufferGeometry();
    geo.setAttribute("position", new T.Float32BufferAttribute(pts, 3));
    this.normals = new T.LineSegments(geo, new T.LineBasicMaterial({ color: 0x378add }));
    this.group.add(this.normals);
    return pts.length / 6;
  }

  clearNormals() {
    if (this.normals) {
      this.group.remove(this.normals);
      this.normals.geometry?.dispose?.();
      this.normals = null;
    }
  }

  clearGrid() {
    if (this.grid) {
      this.scene.remove(this.grid);
      this.grid.geometry?.dispose?.();
      this.grid = null;
    }
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
          <button class="ghost" id="btn-pack">拼盘排版（${S.batchSel.size}）</button>
          <span class="dim" style="font-size:11px">统一跑：补洞 → 统一法线 → 去碎屑 → 处理</span>
        </div>
        <div class="field-row" style="margin-top:12px">
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
  $("#btn-pack").onclick = runPack;
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
          <button class="ghost" id="btn-grid">平台网格</button>
          <button class="ghost" id="btn-parts">部件分色</button>
          <button class="ghost" id="btn-normals">法线</button>
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
        <div class="dim" style="font-size:11px;margin-top:2px" id="heat-legend"></div>
      </div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">检测报告</div>
        <div class="block-sub">每项都带可执行的修复动作</div></div>
      ${body}
    </div>
    ${rep ? thicknessPanel(rep) : ""}`;

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
  $("#btn-thick").onclick = () => loadHeat("thickness");
  $("#btn-over").onclick = () => loadHeat("overhang");
  $("#btn-box").onclick = (e) => {
    const v = S.viewers.find((x) => x.el.id === "viewer");
    if (v) e.target.classList.toggle("on", v.toggleBox());
  };
  $("#clip-slider").oninput = (e) => {
    const t = +e.target.value / 100;
    const v = S.viewers.find((x) => x.el.id === "viewer");
    if (v) v.setClip(t);
    $("#clip-label").textContent = t >= 1 ? "关闭" : `剖切 ${(t * 100).toFixed(0)}%`;
  };
  $("#btn-heat-off").onclick = () => {
    const v = S.viewers.find((x) => x.el.id === "viewer");
    if (v) { v.clearHeatmap(); v.clearMarkers(); v.clearSegments(); }
    $("#heat-legend").textContent = "";
    $("#btn-thick").classList.remove("on");
    $("#btn-over").classList.remove("on");
    $("#btn-issues").classList.remove("on");
    $("#btn-sup").classList.remove("on");
  };
  $("#btn-grid").onclick = (e) => {
    const v = S.viewers.find((x) => x.el.id === "viewer");
    if (v) e.target.classList.toggle("on", v.setGrid(!v.grid));
  };
  $("#btn-parts").onclick = () => loadHeat("components");
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
        <div class="hist-mark thin" style="left:${pct(v.min)}%"><i></i><span>最薄 ${v.min}</span></div>
        <div class="hist-mark" style="left:${pct(v.p5)}%"><i></i><span>P5 ${v.p5}</span></div>
        <div class="hist-mark med" style="left:${pct(v.median)}%"><i></i><span>中位 ${v.median}</span></div>
      </div>
      <div class="hist-scale"><span>0</span><span>${max.toFixed(1)} mm</span></div>
      <div class="field-help">采样 ${v.samples} 面 · 射线方向 ${v.directions || 1} 组 · ${mode}模式。
        点「壁厚热力」可在 3D 里看分布。</div>
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
          <button class="ghost" data-revert="-1">回到导入时</button></div>` : ""}
      </div>
    </div>`;

  if (changed) {
    mountViewer("view-orig", "original");
    mountViewer("view-now", "current");
  }
  $("#btn-split").onclick = runSplit;
  if ($("#btn-diff")) $("#btn-diff").onclick = loadDiff;
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
    if (el && kind === "components") {
      const sizes = meta.sizes || [];
      el.textContent = `模型由 ${meta.components ?? "-"} 个互不连通的部件组成，同色即同一部件。`
        + (sizes.length > 1 ? ` 最大部件占 ${(sizes[0].area / sizes.reduce((a, x) => a + x.area, 0) * 100).toFixed(1)}% 表面积。` : "");
    } else if (el) {
      el.textContent = kind === "thickness"
        ? `壁厚场 ${meta.min ?? "-"}–${meta.max ?? "-"} mm，P5 = ${meta.p5 ?? "-"} mm。颜色按 0–1.5mm 映射，红=打不出来，橙=偏薄，绿=安全。`
        : `悬垂角 ${meta.min ?? "-"}–${meta.max ?? "-"}°，阈值 ${meta.threshold ?? 45}°。颜色按 0–90° 映射，红=必须加支撑。`;
    }
    $("#btn-thick").classList.toggle("on", kind === "thickness");
    $("#btn-over").classList.toggle("on", kind === "overhang");
    $("#btn-parts").classList.toggle("on", kind === "components");
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
