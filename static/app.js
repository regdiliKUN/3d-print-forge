import { Viewer, b64ToTyped, round } from "./viewer.js";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
/** 插入 HTML 前统一转义用户可控文本（项目名/标签来自上传与重命名）。 */
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

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
  repairPlan: null,   // 检测报告驱动的修复建议（动作、原因、待确认项）
  lastRepair: null,   // 上次修复的结构化结果（含回退/降级原因）
  lastProcess: null,  // 上次处理的结果摘要与警告
  rescaleFactor: 1,
  proc: {
    decimate_ratio: 1.0, hollow: false, wall_thickness: 2.0, drain_holes: 2,
    hole_diameter: 3.0, supports: "none", overhang_deg: 45.0,
    support_diameter: 1.0, support_spacing: 3.0, support_points: [], orient: true, scale: 1.0,
    target_height: 0, target_max_dim: 0,
  },
  procPreview: null,     // 轻量预检缓存 {pid, key, data}
  procPreviewBusy: false,
  batchSel: new Set(),
  search: "",
  favOnly: false,
  scanMode: "full",       // "quick" | "full"
  analysisFilter: "all",  // "all" | "issues" | "errors"
  page: 1,
  pageSize: 20,
  sort: "updated",
  redoStack: [],
  borePoints: [],
  layout: "list",     // "list" | "grid"（文件列表排布）
  advicePlan: "balanced", // "aggressive" | "balanced" | "conservative"
  // ---- 机型预设
  printers: [],           // 全量预设摘要
  printerKey: null,       // 当前选中的机型 key
  printerDetail: null,    // 当前机型的完整参数（含 official_exposure）
  printerProcess: "SLA",  // 选择器里的工艺页签
  printerQuery: "",       // 选择器搜索词
  appliedAdvice: false,   // 建议参数是否已应用到处理页
  materials: [],          // 当前工艺的材料清单（名称唯一来源在后端）
  materialKey: null,      // 选中的材料 key
};
// 机型记忆：优先按项目存（同一工作台里常有不同机器），没有才退回全局
function rememberPrinter(pid, key) {
  try {
    if (pid) localStorage.setItem(`tpf_printer:${pid}`, key);
    localStorage.setItem("tpf_printer", key);
  } catch { /* 忽略 */ }
}

function recallPrinter(pid) {
  try {
    return (pid && localStorage.getItem(`tpf_printer:${pid}`)) ||
           localStorage.getItem("tpf_printer") || null;
  } catch { return null; }
}
// 记住上次选择的排布方式
try { S.layout = localStorage.getItem("tpf_layout") === "grid" ? "grid" : "list"; } catch { /* 忽略 */ }

const TOUR_STEPS = [
  { title: "欢迎使用 3D Print Forge",
    body: "从导入 STL 到产出可打印成品的完整流程，六步走完。原文件永远不会被改动，每一步都能回退。" },
  { title: "① 导入",
    body: "把 STL 拖进窗口，或者点「加载示例模型」——那是个故意带破洞、薄壁、碎屑的模型，可以立刻体验完整流程。" },
  { title: "② 检测与修复",
    body: "七项检查会告诉你哪里有问题，每项都带一键修复。右上角有 0–100 的质量评分，先看总分再看细节。" },
  { title: "③ 处理",
    body: "减面、镂空打孔、加支撑、自动摆正。左上的「快速预设」有手办 / FDM / 快速验证三套常用组合。" },
  { title: "④ 可视化诊断",
    body: "3D 预览上方一排按钮：壁厚热力、悬垂热力、剖切、切片层、部件分色、法线、支撑密度。鼠标悬停有说明。" },
  { title: "⑤ 导出",
    body: "STL / 3MF / OBJ / PLY 四种格式，还能导出参数配置和自包含 HTML 报告。有问题随时按 ? 看快捷键。" },
];

function tourSeen() {
  try { return localStorage.getItem("tpf_tour_done") === "1"; } catch { return true; }
}

function trapFocus(ov) {
  // 弹层打开时把 Tab 限制在卡片内，Esc/关闭后由调用方恢复原焦点
  ov.addEventListener("keydown", (e) => {
    if (e.key !== "Tab") return;
    const items = [...ov.querySelectorAll(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    )].filter((el) => !el.disabled && el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
}

function showTour(force) {
  if (!force && tourSeen()) return;
  // 已经有引导层就不再叠一层（上传/切步骤会重渲染，容易被重复触发）
  if ($("#tour-overlay")) return;
  const previousFocus = document.activeElement;
  let i = 0;
  const ov = document.createElement("div");
  ov.id = "tour-overlay";
  ov.className = "tour-overlay";
  ov.setAttribute("role", "dialog");
  ov.setAttribute("aria-modal", "true");
  ov.setAttribute("aria-label", "新手引导");
  const render = () => {
    const st = TOUR_STEPS[i];
    ov.innerHTML = `<div class="tour-card">
      <div class="tour-step">${i + 1} / ${TOUR_STEPS.length}</div>
      <h3>${st.title}</h3>
      <p>${st.body}</p>
      <div class="tour-actions">
        <button class="ghost" id="tour-skip">跳过</button>
        <span class="spacer"></span>
        ${i > 0 ? '<button class="ghost" id="tour-prev">上一步</button>' : ""}
        <button class="primary" id="tour-next">${i === TOUR_STEPS.length - 1 ? "开始使用" : "下一步"}</button>
      </div>
      <div class="tour-dots">${TOUR_STEPS.map((_, k) => `<i class="${k === i ? "on" : ""}"></i>`).join("")}</div>
    </div>`;
    $("#tour-skip").onclick = close;
    $("#tour-next").onclick = () => {
      if (i === TOUR_STEPS.length - 1) return close();
      i++; render();
    };
    if ($("#tour-prev")) $("#tour-prev").onclick = () => { i--; render(); };
    $("#tour-next")?.focus();
  };
  const close = () => {
    try { localStorage.setItem("tpf_tour_done", "1"); } catch {}
    ov.remove();
    if (previousFocus && previousFocus.focus) previousFocus.focus();
  };
  ov.onclick = (e) => { if (e.target === ov) close(); };
  document.body.appendChild(ov);
  trapFocus(ov);
  render();
}

const SHORTCUTS = [
  ["1 – 6", "直接跳到导入 / 检测 / 修复 / 处理 / 建议 / 导出"],
  ["Ctrl + Z", "撤销一步"],
  ["Ctrl + Y / Ctrl + Shift + Z", "重做"],
  ["?", "打开这个帮助面板"],
  ["Esc", "关闭弹层 / 退出点选模式"],
  ["拖拽左键", "旋转视角"],
  ["滚轮", "缩放"],
];

function showShortcuts() {
  const old = $("#help-overlay");
  if (old) { old.remove(); return; }
  const previousFocus = document.activeElement;
  const ov = document.createElement("div");
  ov.id = "help-overlay";
  ov.className = "tour-overlay";
  ov.setAttribute("role", "dialog");
  ov.setAttribute("aria-modal", "true");
  ov.setAttribute("aria-label", "快捷键与操作");
  ov.innerHTML = `<div class="tour-card">
    <div class="tour-step">帮助</div>
    <h3>快捷键与操作</h3>
    <table class="shortcut-table">
      ${SHORTCUTS.map(([k, v]) => `<tr><td><kbd>${k}</kbd></td><td>${v}</td></tr>`).join("")}
    </table>
    <p class="dim" style="font-size:11.5px;margin-top:14px">
      每个步骤页顶部的说明会随步骤变化；参数下方的灰字就是该参数的取值建议。
    </p>
    <p class="dim" id="help-shortcut-status" style="font-size:11.5px;margin-top:8px"></p>
    <div class="tour-actions">
      <button class="ghost" id="help-shortcut">在桌面创建快捷方式</button>
      <button class="ghost" id="help-tour">重新看引导</button>
      <span class="spacer"></span>
      <button class="primary" id="help-close">知道了</button>
    </div>
  </div>`;
  const close = () => {
    ov.remove();
    if (previousFocus && previousFocus.focus) previousFocus.focus();
  };
  ov.onclick = (e) => { if (e.target === ov) close(); };
  document.body.appendChild(ov);
  trapFocus(ov);
  $("#help-close").onclick = close;
  $("#help-tour").onclick = () => { ov.remove(); showTour(true); };
  $("#help-shortcut").onclick = (e) => createDesktopShortcut(e.currentTarget, $("#help-shortcut-status"));
  refreshShortcutStatus($("#help-shortcut-status"));
  $("#help-close")?.focus();
}

/** 查一下桌面快捷方式建没建，更新一行状态文案 */
async function refreshShortcutStatus(el) {
  if (!el) return;
  try {
    const d = await (await fetch("/api/shortcut")).json();
    if (!d.supported) { el.textContent = "桌面快捷方式仅支持 Windows。"; return; }
    if (d.exists) el.textContent = `✓ 已在桌面创建：${d.lnk}`;
    else el.textContent = d.desktop ? `将创建到：${d.desktop}` : "未找到桌面目录（可能被 OneDrive 重定向）。";
  } catch { el.textContent = ""; }
}

/** 点一下 → 后端在桌面建 .lnk（目标在 I 盘启动器），成功后提示路径 */
async function createDesktopShortcut(btn, statusEl) {
  if (btn) { btn.disabled = true; btn.dataset.old = btn.textContent; btn.textContent = "创建中…"; }
  try {
    const d = await (await fetch("/api/shortcut", { method: "POST" })).json();
    if (d.ok) {
      toast("桌面快捷方式已创建，双击图标即可启动", "ok");
      if (statusEl) statusEl.textContent = `✓ 已在桌面创建：${d.lnk}`;
    } else {
      toast(`创建失败：${d.error || "未知错误"}`, "err");
    }
  } catch (e) {
    toast(`创建失败：${e.message}`, "err");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = btn.dataset.old || "在桌面创建快捷方式"; }
  }
}

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
// 检测项 id → 中文名，用于「未确认项」提示（后端 summary.degraded 里是 id）
const ITEM_NAME = {
  watertight: "水密性", normals: "法线", thickness: "壁厚", nonmanifold: "非流形边",
  self_intersection: "自相交", components: "孤立面片", units: "单位与尺寸",
};
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
  const box = $("#toasts");
  if (!box) return;
  // 屏幕阅读器需要显式 live region；错误用 assertive，普通提示用 polite
  const d = document.createElement("div");
  d.className = "toast " + kind;
  d.textContent = msg;
  d.setAttribute("role", kind === "err" ? "alert" : "status");
  d.setAttribute("aria-live", kind === "err" ? "assertive" : "polite");
  box.appendChild(d);
  // 连续导入/批量操作时别把屏幕堆满，只保留最近 6 条
  while (box.children.length > 6) box.firstChild.remove();
  setTimeout(() => d.remove(), kind === "err" ? 7000 : 3800);
}

function fmtBytes(n) {
  if (!n) return "-";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + u[i];
}

/** ISO 时间 → "MM-DD HH:mm"（本地时区），用于文件列表的修改时间 */
function fmtTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "-";
  const p = (x) => String(x).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtEta(s) {
  if (s == null || !isFinite(s)) return "";
  if (s < 60) return `剩余 ${Math.ceil(s)} 秒`;
  return `剩余 ${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒`;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch { return false; }
  }
}

async function copyAnalysisReport() {
  const rep = S.analysis;
  if (!rep) return toast("还没有检测报告", "warn");
  const p = S.project || {};
  const q = rep.quality || {};
  const lines = [
    "3D Print Forge 检测报告",
    `项目：${p.name || "-"}`,
    `时间：${rep.at || "-"}`,
    `模型：${rep.stats?.faces ?? "-"} 面 / ${rep.stats?.vertices ?? "-"} 顶点`,
    `健康度：${q.score ?? "-"} / 100（${q.grade || "-"}）${q.verdict ? " · " + q.verdict : ""}`,
    "",
  ];
  for (const it of rep.items || []) {
    const v = it.value || {};
    const bits = [];
    if (it.id === "watertight") bits.push(`开放边 ${v.boundary_edges ?? "-"}`, `非流形边 ${v.nonmanifold_edges ?? 0}`);
    if (it.id === "normals") bits.push(`朝内组件 ${v.inverted_components ?? 0}`, `绕序冲突 ${v.winding_conflicts ?? 0}`);
    if (it.id === "thickness") bits.push(`最薄 ${v.min ?? "-"}mm`, `中位 ${v.median ?? "-"}mm`);
    if (it.id === "components") bits.push(`连通体 ${v.components ?? "-"}`, `碎屑 ${v.tiny ?? 0}`);
    if (it.id === "self_intersection") bits.push(`穿插 ${v.count ?? 0} 处`);
    if (it.id === "units") bits.push(`单位 ${v.unit || "-"}`);
    lines.push(`[${SEV[it.severity] || it.severity}] ${it.name}：${it.message}`);
    if (bits.length) lines.push(`  数据：${bits.join(" · ")}`);
    if (it.fix) lines.push(`  建议：${it.fix.label}`);
  }
  if (q.degraded?.length) lines.push("", `未确认项：${q.degraded.join("、")}`);
  const ok = await copyText(lines.join("\n"));
  if (ok) toast("检测报告已复制到剪贴板", "ok");
  else toast("复制失败，请手动选择文本", "err");
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

const TASKS = [
  { key: "import", label: "导入模型", step: 0, test: (p) => !!p },
  { key: "analyze", label: "运行检测", step: 1, test: (p) => !!(p && p.analysis) },
  { key: "repair", label: "修复问题", step: 2, test: (p, h) => h.some((x) => /repair|fill_holes|fix_normals|remove_small|merge_vertices/.test(x.action || "")) },
  { key: "process", label: "处理模型", step: 3, test: (p, h) => h.some((x) => (x.action || "") === "process") },
  { key: "export", label: "导出成品", step: 5, test: (p, h) => h.some((x) => /process|split|boolean|merge/.test(x.action || "")) },
];

function renderInspector() {
  const panel = $("#inspector");
  if (!panel) return;
  let el = panel;
  // 只重写自己的容器，不能覆盖整个侧栏——里面还有任务队列
  let host = $("#insp-tasks");
  if (!host) {
    el.innerHTML = `<div id="insp-tasks"></div>
      <div class="insp-title">任务队列</div>
      <div id="queue" class="queue-empty">暂无任务</div>`;
    host = $("#insp-tasks");
  }
  el = host;
  if (!S.pid) {
    el.innerHTML = `<div class="insp-empty">
      <div class="insp-empty-title">还没有选中模型</div>
      <div class="insp-empty-body">在「导入模型」页拖入 STL，或点「加载示例模型」用带缺陷的样件试一遍。</div>
    </div>`;
    return;
  }
  const p = S.project || {};
  const hist = p.history || [];
  const done = TASKS.map((t) => t.test(p, hist));
  const n = done.filter(Boolean).length;
  el.innerHTML = `
    <div class="insp-card">
      <div class="insp-head"><span>流程进度</span><b>${n}/${TASKS.length}</b></div>
      <div class="insp-bar"><i style="width:${(n / TASKS.length * 100).toFixed(0)}%"></i></div>
      ${TASKS.map((t, i) => `<div class="insp-item ${done[i] ? "done" : ""}" data-task-step="${t.step}">
        <span class="insp-check">${done[i] ? "✓" : i + 1}</span>
        <span>${t.label}</span>
      </div>`).join("")}
      ${n < TASKS.length
        ? `<button class="ghost" id="btn-autopilot" style="width:100%;margin-top:10px">一键走完剩余步骤</button>`
        : `<div class="insp-finish">流程已走完，可以导出了</div>`}
    </div>`;
  $$("[data-task-step]", el).forEach((x) => x.onclick = () => goto(+x.dataset.taskStep));
  const ap = $("#btn-autopilot");
  if (ap) ap.onclick = autoPilot;
}

async function runAndWait(kind, payload) {
  const r = await api(`/api/projects/${S.pid}/${kind}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  const deadline = Date.now() + 300000;
  while (Date.now() < deadline) {
    const j = await api(`/api/jobs/${r.id}`);
    if (j.status === "done") return j;
    if (j.status === "failed") throw new Error(j.error || "任务失败");
    if (j.status === "cancelled") throw new Error("任务被中断");
    await new Promise((res) => setTimeout(res, 400));
  }
  throw new Error("等待超时");
}

async function autoPilot() {
  if (!S.pid) return toast("先导入模型", "warn");
  const p = S.project || {};
  const hist = p.history || [];
  const todo = [];
  if (!p.analysis) todo.push("analyze");
  if (!TASKS[2].test(p, hist)) todo.push("repair");
  if (!TASKS[3].test(p, hist)) todo.push("process");
  if (!todo.length) return toast("该做的都做完了", "ok");

  const btn = $("#btn-autopilot");
  if (btn) { btn.disabled = true; btn.textContent = "执行中…"; }
  const names = { analyze: "检测", repair: "修复", process: "处理" };
  try {
    for (const k of todo) {
      if (btn) btn.textContent = `${names[k]}中…`;
      const payload = k === "repair"
        ? { actions: S.repairActions, small_ratio: 0.01, scale_factor: 1, thicken_mm: 0.5 }
        : (k === "process" ? S.proc : {});
      await runAndWait(k, payload);
      S.project = await api(`/api/projects/${S.pid}`);
      S.analysis = S.project.analysis || null;
      renderInspector();
    }
    await loadProjects();
    toast("已按顺序完成检测 → 修复 → 处理");
    render();
  } catch (e) {
    toast("自动流程中断：" + e.message, "err");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "一键走完剩余步骤"; }
  }
}

function renderSteps() {
  renderInspector();
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
function hashTarget() {
  const m = /(?:^|[#&])p=([^&]+)/.exec(location.hash || "");
  const s = /(?:^|[#&])s=(\d+)/.exec(location.hash || "");
  return {
    pid: m ? decodeURIComponent(m[1]) : null,
    step: s ? Math.min(5, Math.max(0, Number(s[1]))) : null,
  };
}

function syncHash() {
  // 刷新/分享链接后还能回到同一个项目和步骤；replaceState 不污染前进后退栈
  try {
    const want = S.pid ? `#p=${encodeURIComponent(S.pid)}&s=${S.step}` : location.pathname + location.search;
    const cur = S.pid ? location.hash : location.pathname + location.search;
    if (cur !== want) history.replaceState(null, "", want);
  } catch { /* file:// 或受限环境忽略 */ }
}

function render() {
  disposeViewers();
  const stage = $("#stage");
  let out;
  if (S.step === 0 || !S.pid) out = renderImport(stage);
  else if (S.step === 1) out = renderInspect(stage);
  else if (S.step === 2) out = renderRepair(stage);
  else if (S.step === 3) out = renderProcess(stage);
  else if (S.step === 4) out = renderAdvice(stage);
  else if (S.step === 5) out = renderExport(stage);
  syncHash();
  return out;
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
  const q = (S.search || "").trim().toLowerCase();
  let list = S.projects.filter((p) => {
    if (S.favOnly && !p.favorite) return false;
    if (!q) return true;
    return (p.name || "").toLowerCase().includes(q)
      || (p.tags || []).some((t) => t.toLowerCase().includes(q));
  });
  const sorters = {
    updated: (a, b) => String(b.updated || "").localeCompare(String(a.updated || "")),
    created: (a, b) => String(b.created || "").localeCompare(String(a.created || "")),
    name: (a, b) => String(a.name || "").localeCompare(String(b.name || ""), "zh-Hans-CN"),
    size: (a, b) => (b.size_bytes || 0) - (a.size_bytes || 0),
    issues: (a, b) => (b.issues || 0) - (a.issues || 0),
  };
  list = [...list].sort((a, b) => (b.favorite ? 1 : 0) - (a.favorite ? 1 : 0)
    || (sorters[S.sort] || sorters.updated)(a, b));
  const pageSize = Math.max(5, Number(S.pageSize) || 20);
  const pages = Math.max(1, Math.ceil(list.length / pageSize));
  S.page = Math.min(Math.max(1, S.page | 0), pages);
  const shown = list.slice((S.page - 1) * pageSize, S.page * pageSize);
  const grid = S.layout === "grid";
  const sevPill = (p) => {
    const sev = p.severity;
    const pill = sev === "error" ? "bad" : sev === "warning" ? "warn" : (sev ? "good" : "");
    return sev ? `<span class="pill ${pill}">${SEV[sev] || sev}</span>` : '<span class="pill">未检测</span>';
  };
  const opsBtns = (p) => `<button data-rename="${p.id}" title="重命名">改名</button>
      <button data-tag="${p.id}" title="编辑标签">标签</button>
      <button data-dup="${p.id}" title="复制为独立项目">复制</button>
      <button data-del="${p.id}" class="danger" title="删除项目">删除</button>`;
  const rows = shown.map((p) => {
    const tags = (p.tags || []).map((t) => `<span class="pill tag clickable" data-tagfilter="${esc(t)}">${esc(t)}</span>`).join("");
    if (grid) {
      return `<div class="project-card ${p.id === S.pid ? "active" : ""}" data-pid="${p.id}">
        <div class="pc-top">
          <span class="fav ${p.favorite ? "on" : ""}" data-fav="${p.id}" title="收藏">${p.favorite ? "★" : "☆"}</span>
          ${sevPill(p)}
        </div>
        <div class="pc-name" title="${esc(p.name)}">${esc(p.name)}</div>
        <div class="pc-tags">${tags}</div>
        <div class="pc-meta">
          <div><span class="k">类型</span><span class="v">${p.file_type || "?"}</span></div>
          <div><span class="k">大小</span><span class="v">${fmtBytes(p.size_bytes || 0)}</span></div>
          <div><span class="k">修改</span><span class="v">${fmtTime(p.updated)}</span></div>
          <div><span class="k">体积</span><span class="v">${p.volume_cm3 ? p.volume_cm3 + " cm³" : "—"}</span></div>
        </div>
        <div class="pc-ops">${opsBtns(p)}</div>
      </div>`;
    }
    return `<div class="project-row ${p.id === S.pid ? "active" : ""}" data-pid="${p.id}">
      <span class="fav ${p.favorite ? "on" : ""}" data-fav="${p.id}" title="收藏">${p.favorite ? "★" : "☆"}</span>
      <div class="pr-main">
        <div class="pr-line1"><span class="project-name">${esc(p.name)}</span>${tags}${sevPill(p)}</div>
        <div class="pr-line2">
          <span class="pm-type">${p.file_type || "?"}</span>
          <span class="pm-size">${fmtBytes(p.size_bytes || 0)}</span>
          <span class="pm-time">修改 ${fmtTime(p.updated)}</span>
          ${p.volume_cm3 ? `<span class="pm-vol">${p.volume_cm3} cm³</span>` : ""}
          <span class="pm-ops">${p.history || 0} 次操作</span>
        </div>
      </div>
      <span class="row-ops">${opsBtns(p)}</span>
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
          <input type="text" id="proj-search" placeholder="搜索名称或标签…" value="${esc(S.search)}"
                 style="flex:1;max-width:280px">
          <button class="ghost ${S.favOnly ? "on" : ""}" id="btn-fav-only">只看收藏</button>
          <select id="proj-sort" title="排序方式" style="width:130px">
            <option value="updated" ${S.sort === "updated" ? "selected" : ""}>最近修改</option>
            <option value="created" ${S.sort === "created" ? "selected" : ""}>最近导入</option>
            <option value="name" ${S.sort === "name" ? "selected" : ""}>名称</option>
            <option value="size" ${S.sort === "size" ? "selected" : ""}>文件大小</option>
            <option value="issues" ${S.sort === "issues" ? "selected" : ""}>问题数量</option>
          </select>
          <div class="layout-switch" id="layout-switch" role="group" aria-label="排列方式">
            <button class="seg ${grid ? "" : "on"}" data-layout="list" title="纵向列表">☰ 列表</button>
            <button class="seg ${grid ? "on" : ""}" data-layout="grid" title="网格式">▦ 网格</button>
          </div>
          <span class="dim" style="font-size:11px">${list.length}/${S.projects.length}</span>
        </div>
        <div class="project-list ${grid ? "grid" : ""}">${rows || (S.projects.length === 0
          ? `<div class="empty-guide">
               <div class="eg-title">还没有模型</div>
               <div class="eg-steps">
                 <div><b>1</b> 把 STL 拖进这个窗口</div>
                 <div><b>2</b> 或者点下面的「加载示例模型」，用带缺陷的样件走一遍流程</div>
                 <div><b>3</b> 不确定从哪开始？点右上角的 <kbd>?</kbd> 看引导</div>
                 <div><b>4</b> 想在桌面放个一键启动图标？点下面的「创建桌面快捷方式」</div>
               </div>
               <button class="primary" id="btn-demo-empty">加载示例模型</button>
               <button class="ghost" id="btn-shortcut-empty">创建桌面快捷方式</button>
             </div>`
          : '<div class="queue-empty">没有匹配的模型</div>')}</div>
        ${pages > 1 ? `<div class="row" id="proj-pager" style="justify-content:center;margin-top:12px">
          <button class="ghost" data-page="prev" ${S.page <= 1 ? "disabled" : ""}>上一页</button>
          <span class="dim" style="font-size:11px">第 ${S.page} / ${pages} 页</span>
          <button class="ghost" data-page="next" ${S.page >= pages ? "disabled" : ""}>下一页</button>
        </div>` : ""}
      </div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">打印队列</div>
        <div class="block-sub" id="queue-stat">空队列</div></div>
      <div class="block-body">
        <div class="row" style="margin-bottom:10px">
          <button class="ghost" id="btn-queue-add">把当前项目加入队列</button>
          <button class="ghost" id="btn-queue-prune" title="移除已经被删除的项目">清理失效项</button>
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
            <span>${esc(p.name)}</span>
            <small>${p.id}</small>
          </label>`).join("") || '<div class="queue-empty">先导入模型</div>'}
        </div>
        <div class="row" style="margin-top:12px">
          <button class="ghost" id="btn-batch">处理选中（${S.batchSel.size}）</button>
          <button class="ghost" id="btn-pack">拼盘排版（${S.batchSel.size}）</button>
          <button class="ghost" id="btn-zip">打包下载（${S.batchSel.size}）</button>
          <button class="ghost" id="btn-batch-tag">批量打标签（${S.batchSel.size}）</button>
          <button class="ghost" id="btn-demo">加载示例模型</button>
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
  $$(".project-row, .project-card", stage).forEach((el) => el.onclick = (e) => {
    if (e.target.closest("[data-fav],[data-rename],[data-tag],[data-dup],[data-del]")) return;
    // 在导入页点当前项目：切到检测页，而不是因为 pid 没变继续停在列表
    if (el.dataset.pid === S.pid && S.step === 0) { S.step = 1; renderSteps(); }
    loadProject(el.dataset.pid);
  });
  $$("#layout-switch [data-layout]").forEach((el) => el.onclick = () => {
    S.layout = el.dataset.layout;
    try { localStorage.setItem("tpf_layout", S.layout); } catch { /* 忽略 */ }
    render();
  });
  const search = $("#proj-search");
  if (search) {
    search.oninput = (e) => {
      S.search = e.target.value;
      S.page = 1;
      render();
      // 列表整体重绘后把焦点和光标还回搜索框，否则每输入一个字就失焦
      const next = $("#proj-search");
      if (next) {
        next.focus();
        const end = next.value.length;
        try { next.setSelectionRange(end, end); } catch { /* 忽略 */ }
      }
    };
    search.onkeydown = (e) => { if (e.key === "Escape") { S.search = ""; S.page = 1; render(); } };
  }
  if ($("#proj-sort")) $("#proj-sort").onchange = (e) => {
    S.sort = e.target.value;
    S.page = 1;
    render();
  };
  $$("[data-page]", stage).forEach((el) => el.onclick = () => {
    S.page += el.dataset.page === "next" ? 1 : -1;
    render();
  });
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
  $$("[data-del]", stage).forEach((el) => el.onclick = async (e) => {
    e.stopPropagation();
    const id = el.dataset.del;
    const cur = S.projects.find((x) => x.id === id);
    if (!cur) return;
    if (!confirm(`删除「${cur.name}」？\n\n项目记录会删除；如果文件没有被其他项目共享，也会一并清理。此操作不可撤销。`)) return;
    try {
      await api(`/api/projects/${id}`, { method: "DELETE" });
      if (S.pid === id) { S.pid = null; S.project = null; S.analysis = null; S.advice = null; S.step = 0; }
      S.batchSel.delete(id);
      await loadProjects();
      render();
      toast("项目已删除");
    } catch (err) { toast("删除失败：" + err.message, "err"); }
  });
  $$("[data-dup]", stage).forEach((el) => el.onclick = async (e) => {
    e.stopPropagation();
    const id = el.dataset.dup;
    try {
      const p = await api(`/api/projects/${id}/duplicate`, { method: "POST" });
      await loadProjects();
      render();
      toast(`已复制为「${p.name}」`);
    } catch (err) { toast("复制失败：" + err.message, "err"); }
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
  if ($("#btn-batch-tag")) $("#btn-batch-tag").onclick = async () => {
    const ids = [...S.batchSel];
    if (!ids.length) return toast("先勾选项目", "warn");
    const raw = prompt("给选中项目加标签（逗号分隔，会追加到已有标签）", "");
    if (!raw) return;
    const tags = raw.split(/[,，]/).map((t) => t.trim()).filter(Boolean);
    if (!tags.length) return;
    let ok = 0;
    for (const id of ids) {
      const cur = S.projects.find((x) => x.id === id);
      const merged = Array.from(new Set([...((cur && cur.tags) || []), ...tags]));
      try {
        await api(`/api/projects/${id}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tags: merged }),
        });
        ok++;
      } catch {}
    }
    await loadProjects();
    render();
    toast(`已给 ${ok}/${ids.length} 个项目加上标签`);
  };
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
      toast(`已导入「${r.name}」${r.restored ? `，恢复 ${r.history} 个历史版本` : ""}`);
      if (r.note) setTimeout(() => toast(r.note), 700);
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
  const scanCost = rep ? (rep.items || []).reduce((n, it) => n + (Number(it.elapsed) || 0), 0) : 0;
  const stale = !!(p && p.analysis_stale && rep);
  stage.innerHTML = `
    ${guideOf("inspect")}
    ${stale ? `<div class="block-banner soft">
      <div class="bb-icon">!</div>
      <div class="bb-body">
        <b>这份检测报告已过期</b>
        <span>模型在上次检测之后被缩放 / 旋转 / 打孔 / 处理过，报告里的壁厚、体积、水密结论都还是上一版文件的。
          参数建议会按当前文件重测几何，但要得到可靠的检测结论，请重新检测。</span>
        <div class="row" style="margin-top:8px"><button class="ghost" id="btn-rescan-stale">重新检测</button></div>
      </div>
    </div>` : ""}
    <div class="block">
      <div class="block-head">
        <div><div class="block-title">${esc(p ? p.name : "")}</div>
          <div class="block-sub">${rep ? `共 ${rep.summary.total} 项 · 最严重：${SEV[rep.summary.worst]} · 各项耗时合计 ${scanCost.toFixed(2)}s` : "尚未检测"}</div></div>
        <button class="primary" id="btn-scan">${rep ? "重新检测" : "开始检测"}</button>
      </div>
      <div class="block-body">
        <div class="scan-modes">
          <button class="ghost small ${S.scanMode === "quick" ? "on" : ""}" data-scan-mode="quick"
            title="壁厚只做粗筛，并跳过自相交检测">快速扫描</button>
          <button class="ghost small ${S.scanMode === "full" ? "on" : ""}" data-scan-mode="full"
            title="壁厚采样更密，并检查三角形自相交">完整扫描</button>
          <span class="dim" style="font-size:11px">${S.scanMode === "quick"
            ? "跳过自相交、壁厚只粗筛：更快，但局部薄壁和穿插可能漏检。"
            : "壁厚全量采样 + 自相交检查：更慢，结论更可靠。"}</span>
        </div>
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
      ${rep ? `<div class="report-tools">
        ${[
          ["all", `全部 ${rep.summary?.total ?? (rep.items || []).length}`],
          ["issues", `只看问题 ${((rep.summary?.counts?.error || 0) + (rep.summary?.counts?.warning || 0))}`],
          ["errors", `只看错误 ${rep.summary?.counts?.error || 0}`],
        ].map(([k, label]) => `<button class="ghost small ${S.analysisFilter === k ? "on" : ""}" data-report-filter="${k}">${label}</button>`).join("")}
        <span class="spacer"></span>
        <button class="ghost small" id="btn-copy-report" title="复制为纯文本，方便发到群里或贴进工单">复制报告</button>
      </div>` : ""}
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
  if ($("#btn-rescan-stale")) $("#btn-rescan-stale").onclick = runAnalyze;
  $$("[data-scan-mode]", stage).forEach((el) => el.onclick = () => {
    if (S.scanMode === el.dataset.scanMode) return;
    S.scanMode = el.dataset.scanMode;
    render();
  });
  $$("[data-report-filter]", stage).forEach((el) => el.onclick = () => {
    S.analysisFilter = el.dataset.reportFilter;
    render();
  });
  const copyRep = $("#btn-copy-report");
  if (copyRep) copyRep.onclick = () => copyAnalysisReport();
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
  const hist = (S.project?.analysis_history || []).filter((x) => Number.isFinite(Number(x.score))).slice(-8);
  let trend = "";
  if (hist.length > 1) {
    const delta = Number(hist[hist.length - 1].score) - Number(hist[0].score);
    const arrow = delta > 0 ? `↑ +${delta.toFixed(1)}` : delta < 0 ? `↓ ${delta.toFixed(1)}` : "→ 持平";
    const bars = hist.map((x) => {
      const sc = Math.max(0, Math.min(100, Number(x.score)));
      const col = sc >= 90 ? "#5dcaa5" : sc >= 75 ? "#97c459" : sc >= 60 ? "#ef9f27" : "#e24b4a";
      return `<i style="height:${Math.max(4, sc * 0.32)}px;background:${col}" title="${esc(x.at || "")} · ${x.score} 分 · ${x.grade || ""}"></i>`;
    }).join("");
    trend = `<div class="score-trend">
      <div class="score-trend-head"><span>最近 ${hist.length} 次检测</span>
        <b style="color:${delta > 0 ? "#5dcaa5" : delta < 0 ? "#e24b4a" : "#8b93a5"}">${arrow}</b></div>
      <div class="score-trend-bars">${bars}</div>
      <div class="field-help">柱高按分数缩放；悬停看时间。修复后复检即可看到趋势。</div>
    </div>`;
  }
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
      ${trend}
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
  if (!n) { box.innerHTML = '<div class="queue-empty">模型没有三角面</div>'; return; }
  const areas = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const a = idx[i * 3] * 3, b = idx[i * 3 + 1] * 3, c = idx[i * 3 + 2] * 3;
    const ux = verts[b] - verts[a], uy = verts[b + 1] - verts[a + 1], uz = verts[b + 2] - verts[a + 2];
    const vx = verts[c] - verts[a], vy = verts[c + 1] - verts[a + 1], vz = verts[c + 2] - verts[a + 2];
    const cx = uy * vz - uz * vy, cy = uz * vx - ux * vz, cz = ux * vy - uy * vx;
    areas[i] = 0.5 * Math.hypot(cx, cy, cz);
  }
  // TypedArray.sort() 是原子的数值排序，比 Array.from(...).sort(compare) 快一个量级；
  // 旧写法还会额外分配一份 JS 对象数组，大网格上直接卡主线程。
  const sorted = areas.sort();
  const q = (p) => sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * p))];
  const p50 = q(0.5), p95 = q(0.95);
  let tiny = 0, huge = 0;
  for (let i = 0; i < n; i++) {
    const a = sorted[i];
    if (a < 0.01) tiny++;
    if (a > p50 * 20) huge++;
  }

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
  // 检测没跑成 / 降级的项要和「真的没问题」区分开：
  // 后端已经给了 summary.degraded 和 value.error，界面以前完全没显示，
  // 用户看到一片绿色，以为七项都过了。
  const unconfirmed = new Set(rep.summary?.degraded || []);
  const banner = unconfirmed.size
    ? `<div class="rr-note warn" style="margin:0 0 10px">
         有 ${unconfirmed.size} 项没能给出可信结论（${[...unconfirmed].map((id) => ITEM_NAME[id] || id).join("、")}），
         评分等级已按「未确认」封顶，请补测或人工确认后再打印。
       </div>`
    : "";
  const keep = S.analysisFilter === "issues"
    ? (it) => it.severity === "error" || it.severity === "warning"
    : S.analysisFilter === "errors"
      ? (it) => it.severity === "error"
      : () => true;
  const rank = { error: 0, warning: 1, info: 2, ok: 3 };
  const rows = (rep.items || [])
    .filter(keep)
    .sort((a, b) => (rank[a.severity] ?? 9) - (rank[b.severity] ?? 9));
  if (!rows.length) {
    return banner + `<div class="queue-empty">当前筛选下没有条目${
      unconfirmed.size ? "；但上方有未确认项，请补测" : "，所有检查都通过了"}。</div>`;
  }
  return banner + rows.map((it) => {
    const err = (it.value || {}).error;
    const bad = err || unconfirmed.has(it.id);
    return `
    <div class="report-item">
      <div class="sev ${it.severity}"></div>
      <div>
        <div class="ri-name">${it.name}<span class="ri-tag">${SEV[it.severity]}</span>${
          bad ? `<span class="ri-tag unconfirmed">未确认</span>` : ""}</div>
        <div class="ri-msg">${it.message}</div>
        <div class="ri-meta">${meta(it)}</div>
      </div>
      <div>${it.fix ? `<button class="ghost" data-fix="${it.fix.action}">${it.fix.label}</button>` : ""}</div>
    </div>`;
  }).join("");
}

/* ---- 修复 */
function renderRepair(stage) {
  const CORE = ["merge_vertices", "fill_holes", "fix_normals"];
  const ADV = ["remove_small", "resolve_self_intersection", "rescale", "thicken"];
  const hints = {
    merge_vertices: "先合并重复顶点，才能判断流形性",
    fill_holes: "曲率感知补洞，补面顺曲率、不塌陷",
    fix_normals: "让所有面法线朝外一致",
    remove_small: "删掉体积过小的碎屑部件",
    resolve_self_intersection: "体素重网格，一次性消解穿插（会轻微圆化锐边）",
    rescale: "按推断的单位换算尺寸",
    thicken: "体素膨胀补厚薄壁",
  };
  const has = (a) => S.repairActions.includes(a);
  const plan = S.repairPlan;
  const planReasons = plan?.reasons || {};
  const row = (a) => `<label class="check-row ${has(a) ? "on" : ""} ${planReasons[a] ? "recommended" : ""}" data-act="${a}">
      <input type="checkbox" ${has(a) ? "checked" : ""}>
      <span>${ACT_LABEL[a]}${planReasons[a] ? ` <em class="plan-badge">推荐</em>` : ""}</span>
      <small title="${esc(hints[a])}">${esc(planReasons[a] || hints[a])}</small>
    </label>`;
  const advSel = ADV.filter(has).length;
  const planCard = plan ? `
    <div class="block plan-card">
      <div class="block-head">
        <div><div class="block-title">智能推荐${plan.quality?.grade ? ` · 健康度 ${esc(plan.quality.grade)}` : ""}</div>
          <div class="block-sub">${plan.has_analysis
            ? (plan.actions.length ? `按最近一次检测报告勾选 ${plan.actions.length} 项，逐项写明原因` : "检测报告没有发现需要自动修复的问题")
            : "尚无检测报告，先套用通用三步；运行检测后可获得针对性建议"}</div></div>
      </div>
      <div class="block-body">
        ${(plan.warnings || []).map((w) => `<div class="rr-note warn">${esc(w)}</div>`).join("")}
        ${plan.has_analysis && plan.resolved ? `<div class="rr-note ok">水密、法线、自交、碎屑与壁厚均未触发修复条件，可直接进入处理。</div>` : ""}
        ${plan.has_analysis && !plan.actions.length && !plan.resolved ? `<div class="rr-note dim">没有可自动执行的动作，请先处理上方警告或手动选择。</div>` : ""}
        ${Object.entries(planReasons).map(([a, why]) => `<div class="rr-row dim"><b>${esc(ACT_LABEL[a] || a)}</b>：${esc(why)}</div>`).join("")}
      </div>
    </div>` : "";
  const lr = S.lastRepair;
  const resultCard = lr ? `
    <div class="block repair-result">
      <div class="block-head">
        <div><div class="block-title">上次修复结果 <span class="dim">${lr.at}</span></div>
          <div class="block-sub">${(lr.actions || []).map((a) => `${a.label}${a.ok ? "" : "（回退）"}`).join(" · ")}</div></div>
        <button class="ghost small" id="btn-repair-clear">清除</button>
      </div>
      <div class="block-body">
        ${lr.before && lr.after ? `<div class="rr-row">面数 <b>${lr.before.faces}</b> → <b>${lr.after.faces}</b>　水密 <b>${lr.before.is_watertight ? "是" : "否"}</b> → <b>${lr.after.is_watertight ? "是" : "否"}</b></div>` : ""}
        ${(lr.lines || []).map((t) => `<div class="rr-row dim">${t}</div>`).join("")}
        ${(lr.notes || []).map((n) => `<div class="rr-note ${n.level}">${n.text}</div>`).join("")}
        ${!(lr.notes || []).length ? `<div class="rr-note ok">没有回退或降级项，修复结果可信。</div>` : ""}
      </div>
    </div>` : "";
  stage.innerHTML = `
    ${guideOf("repair")}
    ${resultCard}
    ${planCard}
    <div class="block">
      <div class="block-head">
        <div><div class="block-title">修复动作</div>
          <div class="block-sub">核心三步通常全选；进阶项按需展开</div></div>
        <button class="ghost small" id="btn-repair-rec" title="读取最近一次检测报告，只勾选真正需要的动作">智能推荐</button>
      </div>
      <div class="block-body">
        <div class="repair-group">
          <div class="group-title">核心修复 <span class="group-tag">建议全选</span></div>
          <div class="group-rows">${CORE.map(row).join("")}</div>
        </div>
        <details class="repair-adv" ${advSel ? "open" : ""}>
          <summary>进阶修复 <span class="dim" id="adv-count">（${advSel}/${ADV.length} 已选）</span></summary>
          <div class="group-rows">${ADV.map(row).join("")}</div>
        </details>

        <div class="repair-params">
          <div class="field" data-param-for="fill_holes" ${has("fill_holes") ? "" : "hidden"}>
            <div class="field-top"><span class="field-label">补洞细分</span><span class="field-unit">级</span></div>
            <input type="number" id="p-levels" value="2" step="1" min="0" max="4">
            <div class="field-help">补丁内部加点层数。0 = 平面补丁，2 适合大多数曲面，4 只给大洞用（面数增长很快）。</div>
          </div>
          <div class="field" data-param-for="fill_holes" ${has("fill_holes") ? "" : "hidden"}>
            <div class="field-top"><span class="field-label">补洞平滑</span><span class="field-unit">次</span></div>
            <input type="number" id="p-fillsmooth" value="12" step="2" min="0" max="50">
            <div class="field-help">固定边界的拉普拉斯平滑次数。0 = 保留细分折线，过高会把补丁拉塌。</div>
          </div>
          <div class="field" data-param-for="resolve_self_intersection" ${has("resolve_self_intersection") ? "" : "hidden"}>
            <div class="field-top"><span class="field-label">重网格精度</span><span class="field-unit">mm</span></div>
            <input type="number" id="p-pitch" value="0" step="0.05" min="0" max="20">
            <div class="field-help">体素边长。0 = 按模型尺寸自动取；越小越贴近原形，但内存与耗时按立方增长。</div>
          </div>
          <div class="field" data-param-for="remove_small" ${has("remove_small") ? "" : "hidden"}>
            <div class="field-top"><span class="field-label">碎屑阈值</span><span class="field-unit">占最大部件面积比</span></div>
            <input type="number" id="p-small" value="0.01" step="0.005" min="0.0001" max="0.5">
            <div class="field-help">表面积小于最大连通体此比例的部件会被删除。0.01 表示 1%。</div>
          </div>
          <div class="field" data-param-for="thicken" ${has("thicken") ? "" : "hidden"}>
            <div class="field-top"><span class="field-label">加厚量</span><span class="field-unit">mm</span></div>
            <input type="number" id="p-thicken" value="0.5" step="0.1" min="0.1" max="5">
            <div class="field-help">体素法，锐边会轻微圆化。</div>
          </div>
          <div class="field" data-param-for="rescale" ${has("rescale") ? "" : "hidden"}>
            <div class="field-top"><span class="field-label">单位换算系数</span><span class="field-unit">×</span></div>
            <input type="number" id="p-rescale" value="${S.rescaleFactor || 1}" step="0.001" min="0.001" max="1000">
            <div class="field-help">英寸模型填 25.4，厘米模型填 10。</div>
          </div>
        </div>

        <div class="row repair-actions-row">
          <button class="primary" id="btn-repair">执行修复（${S.repairActions.length} 个动作）</button>
          <span class="dim" style="font-size:11px">修完自动复检，不再重复提示已修好的问题</span>
        </div>
      </div>
    </div>`;

  const updateBtn = () => { $("#btn-repair").textContent = `执行修复（${S.repairActions.length} 个动作）`; };
  const updateParams = () => {
    $$("[data-param-for]", stage).forEach((el) => {
      el.hidden = !S.repairActions.includes(el.dataset.paramFor);
    });
    const cnt = $("#adv-count");
    if (cnt) cnt.textContent = `（${ADV.filter((x) => S.repairActions.includes(x)).length}/${ADV.length} 已选）`;
  };
  $$("[data-act]", stage).forEach((el) => el.onclick = (e) => {
    e.preventDefault();
    const a = el.dataset.act;
    const i = S.repairActions.indexOf(a);
    if (i >= 0) S.repairActions.splice(i, 1); else S.repairActions.push(a);
    el.classList.toggle("on", S.repairActions.includes(a));
    $("input", el).checked = S.repairActions.includes(a);
    updateBtn();
    updateParams();
  });
  const rec = $("#btn-repair-rec");
  if (rec) rec.onclick = async () => {
    if (!S.pid) return toast("先导入模型", "warn");
    rec.disabled = true;
    try {
      const plan = await api(`/api/projects/${S.pid}/repair-plan`);
      S.repairPlan = plan;
      S.repairActions = plan.has_analysis ? [...(plan.actions || [])] : [...CORE];
      if (plan.scale_factor) S.rescaleFactor = plan.scale_factor;
      render();
      if (!plan.has_analysis) toast("还没有检测报告，先套用通用三步；建议先运行检测", "warn");
      else if (plan.actions?.length) toast(`已按检测报告勾选 ${plan.actions.length} 项修复`, "ok");
      else if (plan.resolved) toast("检测未发现需要修复的问题", "ok");
      else toast("没有可自动执行的动作，请查看待确认项", "warn");
    } catch (e) {
      rec.disabled = false;
      toast("读取修复建议失败：" + e.message, "err");
    }
  };
  const clr = $("#btn-repair-clear");
  if (clr) clr.onclick = () => { S.lastRepair = null; render(); };
  $("#btn-repair").onclick = runRepair;
}

/* ---- 处理 */
function renderProcess(stage) {
  const p = S.proc;
  const lp = S.lastProcess;
  const procCard = lp ? `
    <div class="block repair-result">
      <div class="block-head">
        <div><div class="block-title">上次处理结果 <span class="dim">${lp.at}</span></div>
          <div class="block-sub">${(lp.msgs || []).join(" · ") || "无变化"}</div></div>
        <button class="ghost small" id="btn-proc-clear">清除</button>
      </div>
      <div class="block-body">
        ${(lp.warns || []).length
          ? (lp.warns || []).map((w) => `<div class="rr-note warn">${w}</div>`).join("")
          : `<div class="rr-note ok">没有回退或需要额外确认的项。</div>`}
      </div>
    </div>` : "";
  stage.innerHTML = `
    ${guideOf("process")}
    ${procCard}
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
        <div class="field-row">
          <div class="field">
            <div class="field-top"><span class="field-label">目标高度</span><span class="field-unit">mm</span></div>
            <input type="number" id="p-target-h" value="${p.target_height || 0}" step="1" min="0" max="2000">
            <div class="field-help">0 = 不用。填 180 会把模型缩放到总高 180mm，优先级高于缩放倍数。</div>
          </div>
          <div class="field">
            <div class="field-top"><span class="field-label">目标最长边</span><span class="field-unit">mm</span></div>
            <input type="number" id="p-target-max" value="${p.target_max_dim || 0}" step="1" min="0" max="2000">
            <div class="field-help">0 = 不用。适合「不管朝向，整体不超过平台」。</div>
          </div>
        </div>
        <div id="proc-preflight" class="field-help" style="margin-top:8px"></div>
        <div class="row" style="margin-top:8px">
          <button class="ghost small" id="btn-proc-preview">预检尺寸与用量</button>
          <span class="dim" id="proc-preview-status" style="font-size:11px">不生成文件，只估算摆正后尺寸与材料用量</span>
        </div>
        <div id="proc-preview" class="proc-preview" hidden></div>

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
  const pc = $("#btn-proc-clear");
  if (pc) pc.onclick = () => { S.lastProcess = null; render(); };
  bind("#p-wall", "wall_thickness");
  bind("#p-drain", "drain_holes", (v) => parseInt(v, 10));
  bind("#p-holed", "hole_diameter");
  bind("#p-over", "overhang_deg");
  bind("#p-supd", "support_diameter");
  bind("#p-sups", "support_spacing");
  bind("#p-scale", "scale");
  bind("#p-target-h", "target_height");
  bind("#p-target-max", "target_max_dim");
  const updateProcPreflight = () => {
    const box = $("#proc-preflight");
    if (!box) return;
    const st = S.project?.analysis?.stats || {};
    const ext = Array.isArray(st.extents) && st.extents.length === 3 ? st.extents : null;
    const build = S.printerDetail?.build;
    const msgs = [];
    if (S.proc.hollow && Number(S.proc.drain_holes) === 0) {
      msgs.push("<span class='warn-text'>⚠ 已选镂空但没有排液孔：树脂会封在空腔里，建议至少 2 个。</span>");
    }
    if (Number(S.proc.target_height) > 0 && Number(S.proc.target_max_dim) > 0) {
      msgs.push("<span class='dim'>目标高度优先于目标最长边；两者同时填时只按目标高度计算。</span>");
    }
    if (ext) {
      const curMax = Math.max(...ext);
      let factor = Number(S.proc.scale) || 1;
      if (Number(S.proc.target_height) > 0 && ext[2] > 0) factor = Number(S.proc.target_height) / ext[2];
      else if (Number(S.proc.target_max_dim) > 0 && curMax > 0) factor = Number(S.proc.target_max_dim) / curMax;
      const scaled = ext.map((x) => x * factor).sort((a, b) => b - a);
      if (build && build.length === 3) {
        const bed = build.map(Number).sort((a, b) => b - a);
        const over = scaled.map((x, i) => x > bed[i] + 0.01 ? `${x.toFixed(1)} > ${bed[i]}` : "").filter(Boolean);
        if (over.length) {
          msgs.push(`<span class='warn-text'>⚠ 缩放后 ${scaled.map((x) => x.toFixed(1)).join("×")}mm，超出 ${S.printerDetail.name || "当前机型"} 成型空间（${build.map((x) => Number(x).toFixed(0)).join("×")}mm）；可启用自动摆放或先分件。</span>`);
        } else {
          msgs.push(`<span class='ok-text'>✓ 缩放后 ${scaled.map((x) => x.toFixed(1)).join("×")}mm，可放入 ${S.printerDetail.name || "当前机型"}。</span>`);
        }
      } else {
        msgs.push(`<span class='dim'>缩放后约 ${scaled.map((x) => x.toFixed(1)).join("×")}mm；选择机型后可检查成型空间。</span>`);
      }
    }
    box.innerHTML = msgs.join("<br>");
  };
  ["#p-scale", "#p-target-h", "#p-target-max", "#p-drain", "#p-hollow"].forEach((sel) => {
    const el = $(sel);
    if (el) el.addEventListener("input", updateProcPreflight);
    if (el) el.addEventListener("change", updateProcPreflight);
  });
  updateProcPreflight();

  const procPreviewPayload = () => {
    const out = { ...S.proc };
    if (S.printerDetail?.build) out.build = S.printerDetail.build;
    if (S.printerKey) out.printer_key = S.printerKey;
    return out;
  };
  const procPreviewKey = () => JSON.stringify(procPreviewPayload());
  const setPreviewStatus = (text, kind = "dim") => {
    const el = $("#proc-preview-status");
    if (!el) return;
    el.textContent = text;
    el.className = kind === "dim" ? "dim" : kind === "err" ? "warn-text" : "ok-text";
    el.style.fontSize = "11px";
  };
  const renderProcPreview = (data, stale = false) => {
    const box = $("#proc-preview");
    if (!box || !data) return;
    box.hidden = false;
    const fit = data.fit;
    const ext = (data.extents || []).map((x) => Number(x).toFixed(1)).join(" × ");
    const factor = Number(data.scale?.factor || 1);
    const targetText = data.scale?.target === "height" ? "目标高度"
      : data.scale?.target === "max_dim" ? "目标最长边" : "缩放倍数";
    const volume = Number(data.volume_cm3 || 0).toFixed(2);
    const rows = [];
    rows.push(`<div class="pp-main"><b>${ext} mm</b><span class="dim">摆正后 · ${targetText}${Math.abs(factor - 1) > 1e-6 ? ` ×${factor.toFixed(3)}` : ""}</span></div>`);
    const usage = data.estimated_saved_pct != null
      ? `实体 ${volume} cm³ → 镂空后约 ${(Number(data.estimated_shell_volume_mm3 || 0) / 1000).toFixed(2)} cm³，省料 <b>${data.estimated_saved_pct}%</b>`
      : `实体体积 ${volume} cm³`;
    rows.push(`<div class="pp-row">${usage} <span class="dim">（估算，未含支撑）</span></div>`);
    if (!fit) {
      rows.push(`<div class="pp-row dim">未选择机型，跳过成型空间检查。</div>`);
    } else if (fit.ok) {
      rows.push(`<div class="pp-row ok-text">✓ 可放入 ${esc(data.printer?.name || S.printerDetail?.name || "当前机型")}（含 ${fit.margin_mm}mm 取件余量）</div>`);
    } else {
      const over = (fit.oversize || []).map((o) => `${o.need.toFixed(1)} > ${o.have.toFixed(1)}`).join("、");
      rows.push(`<div class="pp-row warn-text">✗ 超出成型空间：${over} mm</div>`);
      if (fit.scale_to_fit && fit.scale_to_fit < 1) {
        rows.push(`<div class="pp-row"><button class="ghost small" data-apply-fit="${fit.scale_to_fit}">按 ×${Number(fit.scale_to_fit).toFixed(3)} 缩放到可打印</button></div>`);
      }
    }
    for (const w of data.warnings || []) rows.push(`<div class="pp-row dim">· ${esc(w)}</div>`);
    for (const e of data.errors || []) rows.push(`<div class="pp-row warn-text">✗ ${esc(e)}</div>`);
    if (stale) rows.push(`<div class="pp-row dim">参数已修改，当前结果来自上一次预检。</div>`);
    box.innerHTML = rows.join("");
    const apply = $("[data-apply-fit]", box);
    if (apply) apply.onclick = () => {
      S.proc.scale = Number(apply.dataset.applyFit) || 1;
      S.proc.target_height = 0;
      S.proc.target_max_dim = 0;
      render();
    };
  };
  const runProcPreview = async (force = false) => {
    if (!S.pid || S.procPreviewBusy) return;
    const key = procPreviewKey();
    if (!force && S.procPreview?.pid === S.pid && S.procPreview.key === key) {
      renderProcPreview(S.procPreview.data, false);
      setPreviewStatus("预检完成 · 轻量估算，不生成文件", "ok");
      return;
    }
    S.procPreviewBusy = true;
    const btn = $("#btn-proc-preview");
    if (btn) btn.disabled = true;
    setPreviewStatus("预检中…（加载模型 + 摆正，不生成文件）");
    try {
      const data = await api(`/api/projects/${S.pid}/process-preview`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(procPreviewPayload()),
      });
      S.procPreview = { pid: S.pid, key, data };
      renderProcPreview(data, false);
      setPreviewStatus(
        data.errors?.length ? "预检发现问题，请先处理" : "预检完成 · 轻量估算，不生成文件",
        data.errors?.length ? "err" : "ok",
      );
    } catch (e) {
      setPreviewStatus("预检失败：" + e.message, "err");
    } finally {
      S.procPreviewBusy = false;
      if (btn) btn.disabled = false;
    }
  };
  const markPreviewStale = () => {
    if (!S.procPreview || S.procPreview.pid !== S.pid) return;
    if (S.procPreview.key !== procPreviewKey()) setPreviewStatus("参数已修改，点「预检尺寸与用量」刷新");
  };
  $$("input, select", stage).forEach((el) => {
    el.addEventListener("input", markPreviewStale);
    el.addEventListener("change", markPreviewStale);
  });
  $("#btn-proc-preview").onclick = () => runProcPreview(true);
  if (S.procPreview?.pid === S.pid) {
    renderProcPreview(S.procPreview.data, S.procPreview.key !== procPreviewKey());
    if (S.procPreview.key === procPreviewKey()) setPreviewStatus("预检完成 · 轻量估算，不生成文件", "ok");
    else setPreviewStatus("参数已修改，点「预检尺寸与用量」刷新");
  } else {
    setTimeout(() => runProcPreview(false), 120);
  }
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

/* ---- 机型预设 */
const PROCESS_LABEL = { SLA: "光固化", FDM: "FDM" };

async function loadPrinters() {
  if (S.printers.length) return;
  try {
    const d = await api("/api/printers");
    S.printers = d.printers || [];
    // 没选过就落到该工艺的默认机型（光固化默认 P1 Max）
    if (!S.printerKey) S.printerKey = d.defaults?.[S.printerProcess] || S.printers[0]?.key || null;
  } catch (e) {
    S.printers = [];
  }
}

async function loadPrinterDetail(key) {
  if (!key) { S.printerDetail = null; return; }
  try {
    S.printerDetail = await api(`/api/printers/${encodeURIComponent(key)}`);
  } catch {
    S.printerDetail = null;
  }
}

function printerOf(key) {
  return S.printers.find((x) => x.key === key) || null;
}

function pickPrinter(key) {
  S.printerKey = key;
  const p = printerOf(key);
  if (p) S.printerProcess = p.process;
  rememberPrinter(S.pid, key);
  S.advice = null;            // 换机型之后旧建议作废，避免张冠李戴
  S.appliedAdvice = false;
  toast(`已切换到 ${p ? p.brand + " " + p.model : key}`);
}

/** 顶部渐进引导条：把「现在该做什么」拆成看得见的三步。
 *
 * 状态从真实数据推出来，不写死——否则会出现「没做的事也打勾」，
 * 那种引导还不如没有。
 */
function flowBar() {
  const done = [!!S.printerKey, !!S.advice, !!S.appliedAdvice];
  const steps = ["选机型", "生成建议", "应用参数"];
  let current = done.findIndex((x) => !x);
  if (current < 0) current = steps.length - 1;
  const html = steps.map((label, i) => {
    const state = done[i] ? "done" : (i === current ? "now" : "");
    const mark = done[i] ? "✓" : i + 1;
    return `<div class="flow-step ${state}">
        <span class="flow-dot">${mark}</span>
        <span class="flow-label">${label}</span>
      </div>${i < steps.length - 1 ? '<span class="flow-line"></span>' : ""}`;
  }).join("");
  return `<div class="flowbar" id="flowbar">${html}</div>`;
}

/** 机型主卡：品牌、型号、四个关键规格、数据可信度 */
function printerHero() {
  const p = S.printerDetail || printerOf(S.printerKey);
  if (!p) {
    return `<div class="printer-hero">
      <div class="ph-top"><div>
        <div class="ph-brand">未选择机型</div>
        <div class="ph-model">先选一台打印机</div>
      </div><div class="ph-actions">
        <button class="primary" id="btn-pick-printer">选择机型</button>
      </div></div>
      <div class="ph-note">机型决定成型空间、层高区间、光源照度和抬升参数——这些直接改变建议值。</div>
    </div>`;
  }
  const isSla = (p.process || "").toUpperCase() === "SLA";
  const build = (p.build || []).map((x) => Math.round(x)).join(" × ");
  const chips = isSla
    ? [
        ["成型空间", `${build}<small> mm</small>`],
        ["XY 分辨率", p.xy_res ? `${(p.xy_res * 1000).toFixed(1)}<small> μm</small>` : "—"],
        ["层高区间", (p.layer_range || []).join(" – ") + "<small> mm</small>"],
        ["光源照度", p.irradiance ? `${p.irradiance}<small> mW/cm²</small>` : "—"],
      ]
    : [
        ["成型空间", `${build}<small> mm</small>`],
        ["喷嘴", `${p.nozzle || "—"}<small> mm</small>`],
        ["最大流量", `${p.max_flow || "—"}<small> mm³/s</small>`],
        ["加速度", `${Math.round(p.max_accel || p.accel || 0)}<small> mm/s²</small>`],
      ];
  const badge = p.verified
    ? '<span class="tag tag-official">官方规格</span>'
    : '<span class="tag tag-estimate">待校准</span>';
  const official = p.official_exposure && Object.keys(p.official_exposure).length
    ? `<div>厂家标定曝光：${Object.entries(p.official_exposure)
        .map(([m, s]) => `${materialName(m)} ${s}s @ 0.05mm`).join("；")}</div>`
    : "";
  const note = p.note ? `<div class="${p.verified ? "" : "ph-warn"}">${p.note}</div>` : "";
  const comp = computeLine(S.advice);
  return `<div class="printer-hero">
    <div class="ph-top">
      <div>
        <div class="ph-brand">${p.brand || ""} ${badge}</div>
        <div class="ph-model">${p.model || p.key}</div>
      </div>
      <div class="ph-actions">
        <button class="ghost" id="btn-pick-printer">更换机型</button>
      </div>
    </div>
    <div class="ph-chips">
      ${chips.map(([k, v]) => `<div class="chip"><div class="chip-k">${k}</div><div class="chip-v">${v}</div></div>`).join("")}
    </div>
    ${materialStrip()}
    ${(note || official || comp) ? `<div class="ph-note">${note}${official}${comp}</div>` : ""}
  </div>`;
}

/** 材料选择条：材料是整个建议链的源头，必须让用户能换 */
function materialStrip() {
  if (!S.materials.length) return "";
  const chips = S.materials.map((m) => {
    const on = m.key === S.materialKey;
    const sub = m.layer_range
      ? `${m.layer_range[0]}–${m.layer_range[1]}mm`
      : (m.nozzle_temp ? `${m.nozzle_temp}℃ / 床 ${m.bed_temp}℃` : "");
    return `<button class="mchip ${on ? "on" : ""}" data-mat="${m.key}" title="${m.note || ""}">
      <span class="mchip-name">${m.name}</span>
      <span class="mchip-sub">${sub}</span>
    </button>`;
  }).join("");
  return `<div class="ph-mat">
    <div class="ph-mat-label">材料</div>
    <div class="ph-mat-list">${chips}</div>
  </div>`;
}

/** 说明这次建议的计算落在 CPU 还是 GPU 上，避免「号称有卡其实全在跑 CPU」 */
function computeLine(a) {
  const c = a && a.compute;
  if (!c) return "";
  const ops = c.ops || {};
  const gpuOps = Object.entries(ops).filter(([, v]) => v && v !== "cpu");
  const parts = ["几何与切片：CPU"];
  if (gpuOps.length) {
    parts.push(`点边判定：${gpuOps.map(([k, v]) => k).join("/")}@GPU`);
  } else {
    parts.push("点边判定：CPU（规模未达 GPU 受益阈值）");
  }
  parts.push(c.device || "CPU");
  return `<div class="ph-compute">算力：${parts.join(" · ")}</div>`;
}

/** 材料名一律从后端 /api/materials 取，前端不再维护第二份表 */
function materialName(key) {
  const m = S.materials.find((x) => x.key === key);
  return m ? m.name : key;
}

function materialOf(key) {
  return S.materials.find((x) => x.key === key) || null;
}

/** 拉取当前工艺的材料清单，顺带定好默认值 */
async function loadMaterials(process, keepKey) {
  const proc = (process || "SLA").toUpperCase();
  try {
    const d = await api(`/api/materials?process=${encodeURIComponent(proc)}`);
    S.materials = d.materials || [];
    const want = keepKey || S.materialKey;
    const ok = S.materials.some((x) => x.key === want);
    S.materialKey = ok ? want : d.default;
  } catch {
    S.materials = [];
    S.materialKey = null;
  }
}

/** 机型选择器：工艺页签 + 搜索 + 卡片列表 */
function openPrinterPicker() {
  if ($("#printer-mask")) return;
  const all = S.printers;
  if (!all.length) { toast("机型列表还没加载好，稍后再试", "warn"); return; }
  if (S.printerKey) {
    const cur = printerOf(S.printerKey);
    if (cur) S.printerProcess = cur.process;
  }
  const mask = document.createElement("div");
  mask.id = "printer-mask";
  mask.className = "picker-mask";
  mask.innerHTML = `<div class="picker" role="dialog" aria-label="选择打印机型">
    <div class="picker-head">
      <div>
        <div class="picker-title">选择你的打印机</div>
        <div class="picker-sub">选完会按这台机器的真实规格重算参数，不是套模板</div>
      </div>
      <button class="picker-x" id="picker-x" aria-label="关闭">✕</button>
    </div>
    <div class="picker-tools">
      <div class="picker-tabs" id="picker-tabs">
        <button class="ptab" data-proc="SLA">光固化</button>
        <button class="ptab" data-proc="FDM">FDM</button>
      </div>
      <input class="picker-search" id="picker-search" placeholder="搜品牌或型号，例如 P1 Max / 拓竹 / Saturn" value="">
    </div>
    <div class="picker-list" id="picker-list"></div>
    <div class="picker-foot">
      <span>共 ${all.length} 款预设 · 标「待校准」的机型参数取自同级别常见值</span>
      <span>点卡片即切换</span>
    </div>
  </div>`;
  document.body.appendChild(mask);

  const draw = () => {
    $$("#picker-tabs .ptab").forEach((b) => b.classList.toggle("on", b.dataset.proc === S.printerProcess));
    const q = S.printerQuery.trim().toLowerCase().replace(/\s+/g, "");
    const list = all.filter((x) => {
      if ((x.process || "") !== S.printerProcess) return false;
      if (!q) return true;
      // 用后端拼好的 search 串，中文品牌名/简称都能命中；空格一并忽略
      const blob = (x.search || `${x.brand} ${x.model} ${x.key}`).toLowerCase().replace(/\s+/g, "");
      return blob.includes(q);
    });
    const box = $("#picker-list");
    if (!list.length) {
      box.innerHTML = `<div class="picker-empty">没有匹配的机型。换个关键词，或切到另一个工艺页签。</div>`;
      return;
    }
    box.innerHTML = list.map((x) => {
      const on = x.key === S.printerKey;
      const isSla = (x.process || "") === "SLA";
      const spec = isSla
        ? `${(x.build || []).map(Math.round).join("×")}mm · ${x.xy_res ? (x.xy_res * 1000).toFixed(0) + "μm" : "—"} · ${x.irradiance || "—"}mW/cm²`
        : `${(x.build || []).map(Math.round).join("×")}mm · 喷嘴 ${x.nozzle || "—"}mm`;
      const tag = x.verified
        ? '<span class="tag tag-official">官方</span>'
        : '<span class="tag tag-estimate">待校准</span>';
      return `<button class="pcard ${on ? "on" : ""}" data-key="${x.key}" tabindex="-1">
        <span class="pcard-main">
          <span class="pcard-model">${x.model} ${tag}</span>
          <span class="pcard-brand">${x.brand}</span>
          <span class="pcard-spec">${spec}</span>
        </span>
        <span class="pcard-go">${on ? "使用中" : "选用"}</span>
      </button>`;
    }).join("");
    $$("#picker-list .pcard").forEach((el) => {
      el.onclick = async () => {
        pickPrinter(el.dataset.key);
        await loadPrinterDetail(el.dataset.key);
        // 换了工艺，材料清单完全不同（树脂 vs 线材），必须重载并重置选择
        await loadMaterials(S.printerDetail?.process, null);
        close();
        render();
      };
    });
  };
  const close = () => { mask.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);

  $("#picker-x").onclick = close;
  mask.onclick = (e) => { if (e.target === mask) close(); };
  $$("#picker-tabs .ptab").forEach((b) => {
    b.onclick = () => { S.printerProcess = b.dataset.proc; draw(); };
  });
  const search = $("#picker-search");
  search.value = S.printerQuery;
  search.oninput = () => { S.printerQuery = search.value; cursor = -1; draw(); };
  // 键盘导航：↑↓ 在列表里移动光标，Enter 选中，Home/End 跳首尾。
  // 原来只支持「回车选第一条」，列表一长就完全不够用了。
  let cursor = -1;
  const cards = () => $$("#picker-list .pcard");
  const moveCursor = (delta) => {
    const list = cards();
    if (!list.length) return;
    cursor = Math.max(0, Math.min(list.length - 1, cursor + delta));
    list.forEach((el, i) => el.classList.toggle("cursor", i === cursor));
    list[cursor].scrollIntoView({ block: "nearest" });
  };
  const pickCursorOrFirst = () => {
    const list = cards();
    if (!list.length) return;
    (list[cursor] || list[0]).click();
  };
  search.onkeydown = (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); moveCursor(cursor < 0 ? 0 : 1); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); moveCursor(cursor < 0 ? 0 : -1); return; }
    if (e.key === "Home") { e.preventDefault(); cursor = -1; moveCursor(0); return; }
    if (e.key === "End") { e.preventDefault(); cursor = cards().length; moveCursor(-1); return; }
    if (e.key === "Enter") { e.preventDefault(); pickCursorOrFirst(); }
  };
  draw();
  search.focus();
}

/** 绑定机型相关的按钮。每次重渲染后都要重新绑，否则点击无响应。 */
function bindPrinterButtons() {
  const b = $("#btn-pick-printer");
  if (b) b.onclick = openPrinterPicker;
  $$(".mchip[data-mat]").forEach((el) => {
    el.onclick = () => {
      if (el.dataset.mat === S.materialKey) return;
      S.materialKey = el.dataset.mat;
      // 换材料等于换了一条建议链（层高上限/曝光/温度都变），旧结果必须作废
      S.advice = null;
      S.appliedAdvice = false;
      toast(`材料已切换：${materialName(S.materialKey)}`);
      render();
    };
  });
}

/* ---- 建议 */
function renderAdvice(stage) {
  const a = S.advice;
  if (!a) {
    stage.innerHTML = `${flowBar()}
      ${printerHero()}
      <div class="block"><div class="block-body" style="text-align:center;padding:30px">
        <button class="primary" id="btn-adv" ${S.printerKey ? "" : "disabled"}>生成参数建议</button>
        <div class="field-help" style="margin-top:10px">
          ${S.printerKey
            ? "依据检测报告里的几何量 + 这台机器的规格推算，不是套模板"
            : "先选机型：同样一个模型，换台机器层高、曝光、时长全都不一样"}
        </div>
      </div></div>`;
    bindPrinterButtons();
    const btn = $("#btn-adv");
    if (btn) btn.onclick = loadAdvice;
    return;
  }
  const g = a.geometry;
  // 当前建议的层高，层耗时曲线要按它算，不能写死 0.05
  const lhValue = (a.params || []).find((p) => p.key === "layer_height")?.value ?? 0.05;
  S.advicePlan = a.plan || S.advicePlan;
  const plans = a.plans || {};
  const planOrder = ["aggressive", "balanced", "conservative"];
  const planBtns = planOrder.filter((k) => plans[k]).map((k) => `
    <button class="plan-btn ${a.plan === k ? "on" : ""}" data-plan="${k}" title="${plans[k].desc}">${plans[k].label}</button>`).join("");
  const planDesc = (plans[a.plan] && plans[a.plan].desc) || "";
  const rationale = (a.rationale || []).map((r) => `<div class="rat-item">• ${r}</div>`).join("");
  const sources = (a.sources || []).map((s) => `
    <div class="src-group">
      <div class="src-title">${s.title}</div>
      ${(s.items || []).map((it) => `<div class="src-item">${it}</div>`).join("")}
    </div>`).join("");

  stage.innerHTML = `
    ${flowBar()}
    ${printerHero()}
    ${blockBanner(a)}
    ${fallbackBanner(a)}
    ${printabilityCard(a)}
    <div class="block">
      <div class="block-head"><div><div class="block-title">推荐结果 ${confBadge(a)}</div>
        <div class="block-sub">${a.summary}</div></div>
        <button class="ghost" id="btn-adv2">重新生成</button></div>
      <div class="block-body">
        ${confReasons(a)}
        <div class="plan-switch" id="plan-switch" role="group" aria-label="方案切换">
          <span class="plan-switch-label">方案</span>
          ${planBtns}
        </div>
        <p class="plan-hint" id="plan-hint">${planDesc}</p>
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
    ${rationale ? `<div class="rationale-box"><div class="rat-title">主要判断理由</div>${rationale}</div>` : ""}
    ${a.warnings.length ? `<div class="warn-box">${a.warnings.map((w) => `<div>⚠ ${w}</div>`).join("")}</div>` : ""}
    ${layerCompare(a)}
    <div class="block">
      <div class="block-head"><div class="block-title">层耗时曲线</div>
        <button class="ghost" id="btn-layer-times">计算</button></div>
      <div class="block-body" id="layer-times-body">
        <div class="queue-empty">点「计算」看每层耗时随高度的变化——截面积大的层明显更慢</div>
      </div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">推荐参数</div>
        <div class="block-sub">每条都附推导依据</div></div>
      ${a.params.map((p) => {
        const hero = ["layer_height", "exposure", "supports", "infill"].includes(p.key);
        const shown = typeof p.value === "boolean" ? (p.value ? "建议" : "不需要") : p.value;
        // 区间用全角括号包住并留出空隙，否则会和数值粘成「2.3 s1.8–2.8s」
        const rng = p.range && p.range.lo != null
          ? `<span class="advice-range">· P10–P90 ${p.range.lo}–${p.range.hi}${p.range.unit || ""}</span>` : "";
        return `<div class="advice-item ${hero ? "hero-param" : ""}">
          <div class="advice-head">
            <span class="advice-label">${p.label}</span>
            <span class="advice-value">${shown}${p.unit ? " " + p.unit : ""}${rng}</span>
          </div>
          <div class="advice-reason">${p.reason}</div>
        </div>`;
      }).join("")}
    </div>
    ${firstPrintTip(a)}
    <div class="cta-bar ${a.feasible === false ? "cta-disabled" : ""}">
      <div class="cta-text">
        <b>${a.feasible === false ? "先解决问题，再应用参数" : "下一步：把能落地的参数带过去"}</b>
        <span>${a.feasible === false
          ? "模型当前不满足可打印条件，占位参数不应写入处理页。"
          : "镂空 / 壁厚 / 支撑三项会写入「处理」页；层高与曝光属于切片器设置，照着填即可。"}</span>
      </div>
      <button class="primary" id="btn-apply-adv" ${a.feasible === false ? "disabled" : ""}>应用参数 →</button>
    </div>
    <div class="block source-panel">
      <button class="source-toggle" id="btn-sources" aria-expanded="false">
        <span class="src-caret">▸</span> 参数信息来源
        <span class="src-sub">数据来源 · 计算依据 · 参考取值</span>
      </button>
      <div class="source-body" id="sources-body" hidden>${sources}</div>
    </div>`;

  $("#btn-adv2").onclick = loadAdvice;
  bindPrinterButtons();
  const applyBtn = $("#btn-apply-adv");
  if (applyBtn) applyBtn.onclick = () => applyAdvice(a);
  const ltBtn = $("#btn-layer-times");
  if (ltBtn) ltBtn.onclick = () => loadLayerTimes(Number(lhValue) || 0.05);
  $$("#plan-switch [data-plan]").forEach((el) => el.onclick = () => loadAdvice(el.dataset.plan));
  const srcBtn = $("#btn-sources");
  if (srcBtn) srcBtn.onclick = () => {
    const body = $("#sources-body");
    const open = body.hidden;
    body.hidden = !open;
    srcBtn.setAttribute("aria-expanded", String(open));
    const caret = srcBtn.querySelector(".src-caret");
    if (caret) caret.textContent = open ? "▾" : "▸";
  };
}

/* ---- 置信度展示 */
const CONF_CLASS = { "高": "conf-high", "中": "conf-mid", "低": "conf-low" };

/** 阻断提示：模型放不下或已退化时，参数只是占位，不能直接拿去切 */
function blockBanner(a) {
  const bad = a.feasible === false;
  if (!bad) return "";
  const why = a.degenerate
    ? a.degenerate
    : (a.build_fit && a.build_fit.reason) || "模型不满足可打印条件";
  return `<div class="block-banner">
    <div class="bb-icon">!</div>
    <div class="bb-body">
      <b>这份参数不能直接用</b>
      <span>${why}。下面的数值是占位结果，请先分件 / 加厚 / 更换机型，再重新生成。</span>
    </div>
  </div>`;
}

/**
 * 输入被替换的提示。
 * 机型 key 没命中预设、材料 key 不属于当前工艺、方案名不认识时，
 * 后端会退回默认值并照常算出结果——数字自洽，但那不是用户选的东西。
 * 不显示出来的话，用户会拿着 Ender-3 的参数去开自己的机器。
 */
function fallbackBanner(a) {
  const f = a.fallbacks || {};
  const items = [];
  if (f.printer) items.push(`机型「${esc(f.printer)}」不在预设库里，已按 <b>${esc(a.printer_name || "")}</b> 计算`);
  if (f.material) items.push(`材料「${esc(f.material)}」不属于 ${esc(a.process)} 工艺，已按当前材料表默认值计算`);
  if (f.plan) items.push(`方案「${esc(f.plan)}」不存在，已回到「折中」`);
  if (!items.length) return "";
  return `<div class="block-banner soft">
    <div class="bb-icon">!</div>
    <div class="bb-body">
      <b>输入被替换过，参数不是按你选的算的</b>
      <span>${items.join("；")}。请重新选择后再生成一次。</span>
    </div>
  </div>`;
}

/** 一屏可打印性：阻断项 / 通过项 / 提醒数，先给结论再给细节 */
function printabilityCard(a) {
  const p = a.printability;
  if (!p) return "";
  const cls = p.status === "ready" ? "ok" : p.status === "caution" ? "warn" : "bad";
  const rows = (p.checks || []).map((c) => {
    const mark = c.ok === true ? "✓" : c.ok === false ? "✕" : "?";
    const tone = c.ok === true ? "ok" : c.ok === false ? "bad" : "unknown";
    return `<div class="pr-check ${tone}">
      <span class="pr-mark">${mark}</span><b>${esc(c.label)}</b><span>${esc(c.detail)}</span>
    </div>`;
  }).join("");
  const blockers = (p.blockers || []).map((b) => `<div>⚠ ${esc(b.text)}</div>`).join("");
  return `<div class="printability ${cls}">
    <div class="pr-head">
      <span class="pr-badge">${esc(p.label)}</span>
      <span class="dim" style="font-size:11px">${p.warning_count ? `${p.warning_count} 条风险提醒` : "无额外提醒"}</span>
    </div>
    <div class="pr-checks">${rows}</div>
    ${blockers ? `<div class="pr-blockers">${blockers}</div>` : ""}
  </div>`;
}

/** 置信度徽标：一眼看出这份建议有多可信 */
function confBadge(a) {
  const c = a.confidence;
  if (!c) return "";
  const cls = CONF_CLASS[c.grade] || "conf-mid";
  return `<span class="conf-badge ${cls}" title="置信度 ${c.score}">置信度 ${c.grade}</span>`;
}

/** 置信度的扣分项摊开给用户看，而不是藏一个分数 */
function confReasons(a) {
  const c = a.confidence;
  if (!c || !(c.reasons || []).length) return "";
  return `<div class="conf-box">
    <div class="conf-head">这份建议的可信度 ${c.score} · ${c.grade}</div>
    ${c.reasons.map((r) => `<div class="conf-item">· ${r}</div>`).join("")}
  </div>`;
}

/** 首件提醒：光固化第一次用某款料，先跑曝光测试片比信任何公式都靠谱 */
function firstPrintTip(a) {
  if ((a.process || "").toUpperCase() !== "SLA") return "";
  const exp = (a.params || []).find((p) => p.key === "exposure");
  const official = (S.printerDetail && S.printerDetail.official_exposure) || {};
  const off = Object.keys(official).length
    ? `厂家给这款机的标定参考是 ${Object.entries(official).map(([m, s]) => `${materialName(m)} ${s}s`).join("、")}（0.05mm）。`
    : "";
  return `<div class="tip-box">
    <div class="tip-title">首件建议</div>
    <div class="tip-body">
      换新料后的第一次打印，先跑一次<b>曝光测试片</b>：从 ${exp ? exp.range?.lo ?? "—" : "—"}s 到
      ${exp ? exp.range?.hi ?? "—" : "—"}s 分档，挑固化到位又不发胀的那一档。
      ${off}物理模型算的是起点，不是终点。
    </div>
  </div>`;
}

// 建议页的参数能映射到处理参数的只有这几项，其余（层高/曝光/温度）属于切片器设置
const ADVICE_TO_PROC = {
  hollow: (v) => ({ hollow: !!v }),
  wall_thickness: (v) => ({ wall_thickness: Number(v) || 2.0 }),
  supports: (v) => ({ supports: (v === "不需要" || v === "none" || v === false) ? "none" : "auto" }),
};

function applyAdvice(a) {
  if (a && a.feasible === false) {
    return toast("模型不满足可打印条件，请先分件或加厚", "warn");
  }
  let n = 0;
  const used = [];
  for (const x of a.params || []) {
    const fn = ADVICE_TO_PROC[x.key];
    if (!fn) continue;
    Object.assign(S.proc, fn(x.value));
    used.push(x.label);
    n++;
  }
  if (!n) return toast("这套建议里没有能直接映射到处理参数的项", "warn");
  S.appliedAdvice = true;
  toast(`已应用 ${n} 项：${used.join("、")}  → 已跳到「处理」页`);
  goto(3);
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
    <div class="block">
      <div class="block-head"><div class="block-title">当前模型</div>
        <div class="block-sub">拖动旋转；挖孔 / 装配预览都在这个视图上操作</div></div>
      <div class="block-body"><div class="viewer-wrap" id="export-view" style="height:300px"></div></div>
    </div>
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
          <input type="number" id="print-duration" min="0" step="1" placeholder="耗时 min"
                 style="width:100px" title="这次打印的实际耗时（分钟）">
          <input type="text" id="print-material" placeholder="材料（可选）" style="width:120px">
          <input type="text" id="print-note" placeholder="备注（可选，例如「支撑倒了」）" style="flex:1;max-width:280px">
        </div>
        <div class="row">
          <button class="ghost" data-print="success">打成功了</button>
          <button class="ghost" data-print="fail">打失败了</button>
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
                .map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("")}
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
        <div class="row" style="margin-top:8px">
          <select id="rot-axis" style="width:80px">
            <option value="x">X 轴</option><option value="y">Y 轴</option><option value="z">Z 轴</option>
          </select>
          <input type="number" id="rot-deg" value="15" step="1" min="-360" max="360" style="width:90px">
          <button class="ghost" id="btn-rot-free">任意角度旋转</button>
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
          <button class="ghost" id="btn-assembly">装配预览</button>
          <button class="ghost" id="btn-gap">间隙检查</button>
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
      <div class="block-head"><div class="block-title">参数变更对比</div>
        <button class="ghost" id="btn-param-hist">展开</button></div>
      <div class="block-body" id="param-hist-body">
        <div class="queue-empty">看每一步改了哪些参数，以及改成什么</div>
      </div>
    </div>
    <div class="block">
      <div class="block-head"><div class="block-title">操作历史</div>
        <div class="block-sub">${hist.length} 次 · 可回滚到任意一步</div></div>
      <div class="block-body">
        ${hist.length ? hist.map((h, i) => `<div class="metric-row">
          <span><span class="mono dim">${esc(String(h.at || "").slice(11, 19))}</span> &nbsp;${esc(h.action || "")}</span>
          <span class="dim" style="font-size:10.5px;margin-left:auto;margin-right:10px">${esc(h.note || "")}</span>
          ${h.out_removed
            ? '<span class="dim" style="font-size:10px">文件已清理</span>'
            : `<button class="ghost" style="font-size:10px;padding:2px 8px" data-revert="${(p.history || []).length - 1 - i}">回到这版</button>`}
        </div>`).join("")
        : '<div class="queue-empty">还没有操作记录</div>'}
        ${hist.length ? `<div class="row" style="margin-top:10px">
          <button class="ghost" id="btn-undo">撤销（Ctrl+Z）</button>
          <button class="ghost" id="btn-redo">重做（Ctrl+Y）</button>
          <button class="ghost" data-revert="-1">回到导入时</button>
          <button class="ghost" id="btn-clean-hist" title="只删中间文件，操作记录会保留">清理旧版本文件</button></div>` : ""}
      </div>
    </div>`;

  mountViewer("export-view", "current");
  if (changed) {
    mountViewer("view-orig", "original");
    mountViewer("view-now", "current");
  }
  const loadDemo = async (e) => {
    if (e && e.target) e.target.disabled = true;
    try {
      const d = await api("/api/demo", { method: "POST" });
      $("#tour-overlay")?.remove();
      await loadProjects();
      await loadProject(d.id);
      toast("示例模型已加载，可以直接点「开始检测」");
    } catch (err) { toast("加载失败：" + err.message, "err"); }
    finally { if (e && e.target) e.target.disabled = false; }
  };
  if ($("#btn-demo")) $("#btn-demo").onclick = loadDemo;
  if ($("#btn-demo-empty")) $("#btn-demo-empty").onclick = loadDemo;
  if ($("#btn-shortcut-empty")) $("#btn-shortcut-empty").onclick = (e) => createDesktopShortcut(e.currentTarget, null);
  $("#btn-split").onclick = runSplit;
  if ($("#btn-gap")) $("#btn-gap").onclick = async (e) => {
    try {
      const d = await api(`/api/projects/${S.pid}/check-assembly`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tolerance_mm3: 0.5 }),
      });
      e.target.classList.toggle("on", !d.ok);
      const box = $("#gap-result") || (() => {
        const div = document.createElement("div");
        div.id = "gap-result";
        div.className = "field-help";
        e.target.parentElement.after(div);
        return div;
      })();
      const bad = d.interferences || [];
      const unconfirmed = d.degraded || [];
      box.innerHTML = d.ok
        ? `✓ ${d.note}（容差 ${d.tolerance_mm3}mm³）`
        : `<span style="color:var(--error)">✗ ${d.note}</span><br>`
          + bad.map((x) => `第 ${x.a} 块 ↔ 第 ${x.b} 块：重叠 ${x.volume_mm3}mm³`).join("<br>")
          + (unconfirmed.length
            ? `<br><span class="dim">未确认：` + unconfirmed.map((x) => `第 ${x.a} 块 ↔ 第 ${x.b} 块`).join("、") + `</span>`
            : "");
      toast(d.ok ? "各块无干涉" : (bad.length ? `发现 ${bad.length} 处干涉` : `${unconfirmed.length} 对未确认`), d.ok ? "ok" : "err");
    } catch (err) { toast("间隙检查失败：" + err.message, "err"); }
  };
  if ($("#btn-assembly")) $("#btn-assembly").onclick = async (e) => {
    const v = S.viewers.find((x) => x.el.id === "export-view") || S.viewers.find((x) => x.el.id === "viewer");
    if (!v) return toast("预览还没准备好", "warn");
    try {
      const d = await api(`/api/projects/${S.pid}/assembly`);
      v.setGeometry(d.vertices, d.indices);
      v.setVertexColors(b64ToTyped(d.colors, Float32Array));
      e.target.classList.add("on");
      const box = $("#assembly-legend") || (() => {
        const div = document.createElement("div");
        div.id = "assembly-legend";
        div.className = "field-help";
        e.target.parentElement.after(div);
        return div;
      })();
      box.innerHTML = `装配预览：${d.parts.length} 块按切割位置还原，同色即同一块。`
        + d.parts.map((p, i) => `<span style="color:rgb(${p.color.map((c) => Math.round(c * 255)).join(",")});margin-left:8px">■ 第${i + 1}块</span>`).join("");
      toast(`装配预览：${d.parts.length} 块`);
    } catch (err) { toast("装配预览失败：" + err.message, "err"); }
  };
  if ($("#btn-diff")) $("#btn-diff").onclick = loadDiff;
  if ($("#btn-param-hist")) $("#btn-param-hist").onclick = loadParamHistory;
  if ($("#btn-undo")) $("#btn-undo").onclick = undoStep;
  if ($("#btn-redo")) $("#btn-redo").onclick = redoStep;
  if ($("#btn-clean-hist")) $("#btn-clean-hist").onclick = async (e) => {
    e.target.disabled = true;
    try {
      const r = await api(`/api/projects/${S.pid}/history/cleanup?keep=3`, { method: "POST" });
      S.project = await api(`/api/projects/${S.pid}`);
      await loadProjects();
      render();
      toast(r.removed
        ? `已清理 ${r.removed} 个旧版本，释放 ${(r.freed_bytes / 1048576).toFixed(1)} MB`
        : "没有可清理的旧版本");
    } catch (err) { toast("清理失败：" + err.message, "err"); e.target.disabled = false; }
  };
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
    const v = S.viewers.find((x) => x.el.id === "export-view");
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
      const v = S.viewers.find((x) => x.el.id === "export-view");
      if (v) v.clearMarkers();
      S.project = await api(`/api/projects/${S.pid}`);
      await loadProjects();
      render();
    } catch (e) { toast("挖孔失败：" + e.message, "err"); }
  };
  if ($("#btn-bore-clear")) $("#btn-bore-clear").onclick = () => {
    S.borePoints = [];
    const v = S.viewers.find((x) => x.el.id === "export-view");
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
      const dur = $("#print-duration")?.value;
      const r = await api(`/api/projects/${S.pid}/print-record`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          result: el.dataset.print,
          note: $("#print-note")?.value || "",
          duration_min: dur === "" || dur == null ? null : Number(dur),
          material: $("#print-material")?.value || "",
        }),
      });
      if ($("#print-note")) $("#print-note").value = "";
      if ($("#print-duration")) $("#print-duration").value = "";
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
  if ($("#btn-rot-free")) $("#btn-rot-free").onclick = async () => {
    const axis = $("#rot-axis").value;
    const deg = Number($("#rot-deg").value);
    if (!deg) return toast("角度不能为 0", "warn");
    try {
      const r = await api(`/api/projects/${S.pid}/rotate`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ axis, degrees: deg }),
      });
      toast(`绕 ${axis.toUpperCase()} 转 ${deg}°，新尺寸 ${r.extents.map((x) => x.toFixed(1)).join("×")}mm`);
      S.project = await api(`/api/projects/${S.pid}`);
      await loadProjects();
      render();
    } catch (e) { toast("旋转失败：" + e.message, "err"); }
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
    <div class="ec-head"><span class="ec-dot"></span><b>${esc(job.title)} 失败</b>
      <span class="ec-trace dim mono">trace ${esc(job.id)}</span></div>
    <div class="ec-detail">${esc(err)}</div>
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
  const switching = S.pid !== pid;
  if (switching) { S.lastRepair = null; S.lastProcess = null; S.repairPlan = null; }  // 换模型不要沿用上一个模型的结果
  S.pid = pid;
  const p = await api(`/api/projects/${pid}`);
  S.project = p;
  S.analysis = p.analysis || null;
  // 旧格式的建议没有 plans/sources，直接丢弃让用户重新生成，避免半新半旧
  S.advice = (p.advice && p.advice.plans) ? p.advice : null;
  // 这个项目上次用的是哪台机器
  const remembered = recallPrinter(pid);
  if (remembered) S.printerKey = remembered;
  // 同一项目刷新（任务完成/回滚）时保留当前步骤；换项目才回到检测页
  if (switching) S.step = 1;
  // 机型变了就同步材料清单（树脂/线材不通用）
  await loadPrinterDetail(S.printerKey);
  await loadMaterials(S.printerDetail?.process || S.printerProcess, S.materialKey);
  renderSteps(); render();
  bindHelp();
  // 等项目列表加载完再判断：已经有模型说明不是第一次用，别打扰
  setTimeout(() => {
    if (!S.projects.length) showTour(false);
  }, 700);
}

async function uploadFiles(files) {
  // 用户开始导入就说明不需要引导了，先关掉免得挡住拖拽区
  $("#tour-overlay")?.remove();
  if (!files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  toast(`正在导入 ${files.length} 个文件…`);
  try {
    const r = await api("/api/upload?dedupe=1", { method: "POST", body: fd });
    for (const e of r.errors || []) toast(`${e.name}：${e.error}`, "err");
    for (const d of r.duplicates || []) toast(`「${d.name}」已存在，已跳到原项目`, "warn");
    await loadProjects();
    const first = r.projects[0];
    const dup = (r.duplicates || [])[0]?.project;
    if (first) {
      toast(r.projects.length > 1 ? `已导入 ${r.projects.length} 个模型` : `已导入 ${first.name}`);
      await loadProject(first.id);
      if (files.length === 1) runAnalyze();
    } else if (dup) {
      await loadProject(dup.id);
    } else if (!(r.errors || []).length) {
      toast("没有可导入的文件", "warn");
    }
  } catch (e) {
    toast("导入失败：" + e.message, "err");
  }
}

function startJob(res, onDone) {
  S.job = res;
  setGauge(res);
  clearTimeout(S.jobTimer);
  const tick = async () => {
    let j;
    try {
      j = await api(`/api/jobs/${res.id}`);
    } catch {
      // 网络抖动不终止轮询；后台标签页降到 1.5s，避免 350ms 常驻请求
      S.jobTimer = setTimeout(tick, document.hidden ? 1500 : 350);
      return;
    }
    S.job = j;
    setGauge(j);
    refreshQueue();
    if (["done", "failed", "cancelled"].includes(j.status)) {
      clearTimeout(S.jobTimer);
      $("#btn-pause").disabled = true;
      $("#btn-cancel").disabled = true;
      if (j.status === "failed") { toast(j.error || "任务失败", "err"); showErrorCard(j); }
      if (j.status === "cancelled") toast("已中断，可点「恢复」从断点继续", "warn");
      if (j.status === "done") onDone && onDone(j);
      return;
    }
    S.jobTimer = setTimeout(tick, document.hidden ? 1500 : 350);
  };
  S.jobTimer = setTimeout(tick, 350);
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
  const quick = S.scanMode === "quick";
  try {
    const r = await api(`/api/projects/${S.pid}/analyze`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(quick
        ? { sample_faces: 500, check_self_intersection: false }
        : { sample_faces: 1500, check_self_intersection: true }),
    });
    startJob(r, afterAnalyze);
  } catch (e) { toast("检测启动失败：" + e.message, "err"); }
}

async function afterAnalyze(j) {
  const p = await api(`/api/projects/${S.pid}`);
  S.project = p;
  S.analysis = p.analysis;
  S.repairPlan = null;  // 检测更新后旧建议失效
  await loadProjects();
  renderSteps(); render();
  bindHelp();
  // 等项目列表加载完再判断：已经有模型说明不是第一次用，别打扰
  setTimeout(() => {
    if (!S.projects.length) showTour(false);
  }, 700);
  const s = S.analysis?.summary;
  if (s) toast(`检测完成：${s.counts.error} 项严重 / ${s.counts.warning} 项警告`, s.counts.error ? "warn" : "");
}

async function runRepair() {
  if (!S.repairActions.length) return toast("至少选一个修复动作", "warn");
  // 只提交当前选中动作真正用到的参数：把所有参数一股脑发过去，
  // 后端会照单全收并写进任务 params，排查时看不出哪些是用户真的设过的。
  const num = (id, dflt) => {
    const el = $(id);
    if (!el) return dflt;
    const v = parseFloat(el.value);
    return Number.isFinite(v) ? v : dflt;
  };
  const body = { actions: S.repairActions };
  if (S.repairActions.includes("remove_small")) body.small_ratio = num("#p-small", 0.01);
  if (S.repairActions.includes("fill_holes")) {
    body.fill_levels = Math.round(num("#p-levels", 2));
    body.fill_smooth = Math.round(num("#p-fillsmooth", 12));
  }
  if (S.repairActions.includes("resolve_self_intersection")) {
    const pitch = num("#p-pitch", 0);
    if (pitch > 0) body.remesh_pitch = pitch;   // 0 交给后端按模型尺寸自动取
  }
  if (S.repairActions.includes("thicken")) body.thicken_mm = num("#p-thicken", 0.5);
  if (S.repairActions.includes("rescale")) body.scale_factor = num("#p-rescale", 1);
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
  S.repairPlan = null;  // 修复后网格变了，旧建议不再适用
  S.advice = null; // 修复后几何变了，旧建议作废，去建议页会按新几何重新生成
  await loadProjects();
  const b = j.result?.before, a = j.result?.after;
  // 每个动作的 info 里藏着「到底修成了没有」：reverted（回退）、degraded（降级）、
  // pitch 被收敛、加厚量被收敛、补洞面与实体穿插……以前这些都只写进任务日志，
  // 界面上永远是一句「修复完成：面数 X → X」。用户以为修好了，实际什么都没变。
  const notes = [];
  const lines = [];
  for (const step of j.result?.actions || []) {
    const info = step.info || {};
    if (info.reverted) {
      notes.push({ level: "err", text: `${step.label}：未达验收标准，已回退 — ${info.revert_reason || "原因见任务日志"}` });
    } else if (info.degraded) {
      notes.push({ level: "warn", text: `${step.label}：${info.degrade_reason || "结果降级，需人工确认"}` });
    }
    if (info.patch_hits) {
      notes.push({ level: "warn", text: `${step.label}：补洞面与实体面穿插 ${info.patch_hits} 处` });
    }
    if (info.volume_warning) notes.push({ level: "warn", text: `${step.label}：${info.volume_warning}` });
    if (info.requested_mm && info.delta_mm) {
      notes.push({ level: "warn", text: `${step.label}：请求 ${info.requested_mm}mm，实际按 ${info.delta_mm}mm 执行（已收敛到模型尺度内）` });
    }
    if (info.pitch_requested && info.pitch && Math.abs(info.pitch_requested - info.pitch) > 1e-9) {
      notes.push({ level: "info", text: `${step.label}：请求精度 ${info.pitch_requested}mm，实际生效 ${info.pitch}mm（内存上限收敛）` });
    }
    if (info.removed_slivers) lines.push(`${step.label}：删除 ${info.removed_slivers} 个零厚度薄片面`);
    if (info.self_intersections_after !== undefined && info.self_intersections_after !== null) {
      lines.push(`${step.label}：自交 ${info.self_intersections_before ?? "?"} → ${info.self_intersections_after} 处`);
    }
  }
  S.lastRepair = {
    at: new Date().toLocaleTimeString(),
    before: b, after: a, notes, lines,
    actions: (j.result?.actions || []).map((s) => ({ label: s.label, ok: !s.info?.reverted })),
  };
  if (b && a) {
    toast(`修复完成：面数 ${b.faces} → ${a.faces}，水密 ${b.is_watertight ? "是" : "否"} → ${a.is_watertight ? "是" : "否"}`);
  }
  for (const n of notes.slice(0, 3)) {
    // toast 是短提示，完整原因在「上次修复结果」卡里
    toast(n.text.length > 110 ? n.text.slice(0, 108) + "…（详见修复结果）" : n.text, n.level);
  }
  // 有回退/降级时留在修复页，让结果卡直接可见；一切正常才跳到检测页看新报告。
  // 以前一律跳检测页，回退的修复会变成「页面说修好了、模型其实没变」。
  S.step = notes.length ? 2 : 1;
  renderSteps();
  if (j.result?.analysis) {
    // 后端已在修复后复检，直接用新报告，避免「修完还说不水密」以及重复检测
    S.analysis = j.result.analysis;
    render();
  } else {
    render();
    runAnalyze();
  }
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
  const warns = [];
  if (rep.hollow) msgs.push(`镂空省料 ${rep.hollow.resin_saved_pct}%`);
  if (rep.hollow_warning) warns.push(rep.hollow_warning);
  if (rep.drill) {
    msgs.push(`打了 ${rep.drill.holes} 个孔`);
    if (rep.drill.thin_fallback) {
      warns.push(`底面 ${rep.drill.skipped_thin || 0} 个候选位置壳厚不足，已在最薄处开孔，请确认孔是打穿而不是打成豁口`);
    }
  }
  if (rep.decimate?.reverted) {
    // 减面被回退是重要信息：用户以为减到 30%，实际还是原面数
    warns.push(`减面已回退：${rep.decimate.reason}（保留 ${rep.decimate.faces_kept} 面）`);
  } else if (rep.decimate) {
    msgs.push(`减面到 ${rep.decimate.faces} 面`);
  }
  if (rep.supports?.count) msgs.push(`${rep.supports.count} 根支撑`);
  if (rep.supports?.diameter_effective) {
    warns.push(`支撑直径随缩放变成 ${rep.supports.diameter_effective}mm（设定 ${rep.supports.diameter}mm）`);
  }
  if (rep.orient) msgs.push(`姿态：${rep.orient.chosen}`);
  for (const w of rep.warnings || []) warns.push(w);
  toast("处理完成：" + (msgs.join("，") || "无变化"));
  for (const w of warns.slice(0, 3)) {
    toast(w.length > 110 ? w.slice(0, 108) + "…" : w, "warn");
  }
  S.lastProcess = { at: new Date().toLocaleTimeString(), msgs, warns };
  // 有警告时留在处理页，让结果卡直接可见；一切正常才跳到建议页
  S.step = warns.length ? 3 : 4;
  renderSteps(); render();
  loadAdvice();
}

const PARAM_LABELS = {
  wall_thickness: "壳厚", hole_diameter: "孔径", drain_holes: "排液孔数",
  overhang_deg: "悬垂角", support_diameter: "支撑顶径", support_spacing: "支撑间距",
  decimate_ratio: "减面比例", scale: "缩放", target_height: "目标高",
  max_size: "成型上限", axis: "切割轴", op: "布尔操作", degrees: "角度",
  count: "数量", diameter: "直径", depth: "深度", lock: "锁定比例",
};
const paramLabel = (k) => PARAM_LABELS[k] || k;

async function loadParamHistory() {
  const box = $("#param-hist-body");
  if (!box) return;
  box.innerHTML = '<div class="queue-empty">读取中…</div>';
  try {
    const d = await api(`/api/projects/${S.pid}/param-history`);
    const rows = (d.rows || []).filter((r) => r.changed && Object.keys(r.changed).length);
    if (!rows.length) {
      box.innerHTML = `<div class="queue-empty">还没有参数变更记录。${d.note || ""}</div>`;
      return;
    }
    box.innerHTML = rows.slice().reverse().map((r) => `<div style="margin-bottom:10px">
      <div class="dim" style="font-size:10.5px"><span class="mono">${r.at.slice(5, 16).replace("T", " ")}</span>
        &nbsp;${r.action}${r.note ? " · " + r.note : ""}</div>
      ${Object.entries(r.changed).map(([k, v]) => `<div class="metric-row" style="padding:3px 0">
        <span style="font-size:11px">${paramLabel(k)}</span>
        <b class="dim" style="font-size:10.5px;font-weight:400">
          <span style="color:var(--muted)">${v.from}</span> → <span style="color:var(--accent)">${v.to}</span></b>
      </div>`).join("")}
    </div>`).join("");
    $("#btn-param-hist").classList.add("on");
  } catch (e) {
    box.innerHTML = `<div class="queue-empty">读取失败：${e.message}</div>`;
  }
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
      // append=true：三类问题要同时显示，不能被后一类的 clearMarkers 清掉
      v.markPoints(pts, color, true);
      total += pts.length;
    }
    const deg = d.degraded || [];
    const degTxt = deg.length ? `；${deg.length} 项定位失败（${deg.map((x) => x.check).join("、")}），结果未确认` : "";
    $("#heat-legend").textContent = total
      ? `已在模型上标出 ${total} 处：红=自交（${c.self_intersection || 0}）、橙=破洞（${c.holes || 0}）、紫=碎屑（${c.debris || 0}）${degTxt}`
      : (deg.length ? `问题定位未完成：${degTxt.slice(2)}` : "没找到可定位的问题点（模型可能是干净的，或问题太小定位不到）");
    $("#btn-issues").classList.toggle("on", total > 0);
    if (!total) toast(deg.length ? "问题定位未完成，结果未确认" : "没有可定位的问题点", deg.length ? "warn" : "ok");
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

async function loadLayerTimes(layerHeight) {
  const box = $("#layer-times-body");
  if (!box) return;
  box.innerHTML = '<div class="queue-empty">计算中…</div>';
  try {
    // 优先用当前建议的层高，拿不到再退回 0.05
    const lh = Number(layerHeight) || Number(($("#adv-params") || {}).value) || 0.05;
    const d = await api(`/api/projects/${S.pid}/layer-times?samples=40&layer_h=${lh}`);
    const rows = d.layers || [];
    if (!rows.length) { box.innerHTML = '<div class="queue-empty">没有数据</div>'; return; }
    const w = 660, h = 150, padL = 46, padR = 12, padT = 12, padB = 26;
    const maxS = Math.max(...rows.map((r) => r.seconds), 1);
    const minS = Math.min(...rows.map((r) => r.seconds));
    const x = (i) => padL + (w - padL - padR) * i / Math.max(1, rows.length - 1);
    const y = (v) => h - padB - (h - padT - padB) * ((v - minS) / Math.max(maxS - minS, 0.1));
    const line = rows.map((r, i) => `${x(i).toFixed(1)},${y(r.seconds).toFixed(1)}`).join(" ");
    box.innerHTML = `
      <svg viewBox="0 0 ${w} ${h}" class="profile-chart">
        <line x1="${padL}" y1="${h - padB}" x2="${w - padR}" y2="${h - padB}" stroke="#262b38"/>
        <polyline points="${line}" fill="none" stroke="#ef9f27" stroke-width="1.5"/>
        <text x="${padL - 6}" y="${padT + 4}" text-anchor="end" fill="#5f6675" font-size="9">${maxS.toFixed(0)}s</text>
        <text x="${padL - 6}" y="${h - padB}" text-anchor="end" fill="#5f6675" font-size="9">${minS.toFixed(0)}s</text>
        <text x="${padL}" y="${h - 8}" fill="#5f6675" font-size="9">${d.layers[0].z}mm</text>
        <text x="${w - padR}" y="${h - 8}" text-anchor="end" fill="#5f6675" font-size="9">${d.layers[d.layers.length - 1].z}mm</text>
      </svg>
      <div class="metric-row"><span class="dim">总层数 / 层高</span><b>${d.total_layers.toLocaleString()} 层 · ${d.layer_height}mm</b></div>
      <div class="metric-row"><span class="dim">平均每层 / 总时长</span><b>${d.avg_seconds}s · ${d.est_hours}h</b></div>
      <div class="field-help">${d.note}。曲线高处的层截面积大，扫描时间更长。</div>`;
  } catch (e) {
    box.innerHTML = `<div class="queue-empty">计算失败：${e.message}</div>`;
  }
}

async function loadAdvice(plan) {
  // 作为 onclick 直接绑定时第一个参数是事件对象，这里只认字符串方案名
  const p = (typeof plan === "string" && plan) ? plan : (S.advicePlan || "balanced");
  S.advicePlan = p;
  if (!S.pid) return toast("先导入一个模型", "warn");
  await loadPrinters();
  if (!S.printerKey) return toast("先在建议页选一台打印机", "warn");
  if (!S.materials.length) await loadMaterials(S.printerDetail?.process || S.printerProcess);
  const qs = new URLSearchParams({ plan: p, printer: S.printerKey });
  if (S.materialKey) qs.set("material", S.materialKey);
  try {
    S.advice = await api(`/api/projects/${S.pid}/advice?${qs}`);
    S.appliedAdvice = false;
    if (!S.printerDetail) await loadPrinterDetail(S.printerKey);
    if (S.step !== 4) { S.step = 4; renderSteps(); }
    render();
    // 生成后直接把「推荐参数」滚进视野，省得用户自己找
    requestAnimationFrame(() => {
      const el = $("#stage .block");
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    });
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
  const pruneBtn = $("#btn-queue-prune");
  if (pruneBtn) pruneBtn.onclick = async () => {
    try {
      const r = await api("/api/queue/prune", { method: "POST" });
      toast(r.removed ? `已清理 ${r.removed} 个失效项` : "没有失效项");
      loadQueue();
    } catch (e) { toast("清理失败：" + e.message, "err"); }
  };
  try {
    const d = await api("/api/queue");
    const items = d.items || [];
    stat.textContent = items.length ? `${items.length} 件待打` : "空队列";
    if (!items.length) { list.innerHTML = '<div class="queue-empty">队列为空</div>'; return; }
    list.innerHTML = items.map((q, i) => `<div class="metric-row">
      <span>${i + 1}. ${esc(q.name || "")}${q.exists ? "" : ' <span class="pill bad">已删除</span>'}</span>
      <span class="dim" style="font-size:10.5px;margin-left:auto;margin-right:8px">${esc(q.note || "")}</span>
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
    ? `共 ${d.total} 次，成功 ${d.success} 次（${d.rate}%）${d.total_minutes ? `，累计 ${Math.round(d.total_minutes)} 分钟` : ""}`
    : "还没记录过";
  if (!rows.length) { list.innerHTML = ""; return; }
  list.innerHTML = rows.slice().reverse().map((r) => {
    const meta = [r.material, r.printer, r.duration_min ? `${r.duration_min} 分钟` : ""].filter(Boolean).join(" · ");
    return `<div class="metric-row">
    <span>${r.result === "success" ? '<span class="pill good">成功</span>' : '<span class="pill bad">失败</span>'}
      <span class="mono dim" style="font-size:10px;margin-left:6px">${esc(String(r.at || "").slice(5, 16).replace("T", " "))}</span></span>
    ${meta ? `<span class="dim" style="font-size:10.5px">${esc(meta)}</span>` : ""}
    <b class="dim" style="font-size:10.5px">${esc(r.note || "")}</b>
  </div>`;
  }).join("");
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
  bindHelp();
  // 等项目列表加载完再判断：已经有模型说明不是第一次用，别打扰
  setTimeout(() => {
    if (!S.projects.length) showTour(false);
  }, 700);
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
  if (!box) return;
  if (!jobs.length) { box.className = "queue-empty"; box.textContent = "暂无任务"; return; }
  box.className = "";
  box.innerHTML = jobs.slice(0, 12).map((j) => `
    <div class="job-card">
      <div class="job-top">
        <span class="job-title" ${j.project_id ? `data-jopen="${esc(j.project_id)}" style="cursor:pointer" title="打开项目"` : ""}>${esc(j.title)}</span>
        <span class="job-state st-${esc(j.status)}">${j.interrupted ? "已中断" : statusText(j.status)}</span>
      </div>
      <div class="job-bar"><i style="width:${Number(j.percent) || 0}%"></i></div>
      <div class="job-msg">${esc(j.message || j.stage || "")}</div>
      <div class="job-ops">
        ${(j.status === "running" || j.status === "paused") && !j.interrupted
          ? `<button data-jcmd="pause" data-jid="${esc(j.id)}">${j.status === "paused" ? "恢复" : "暂停"}</button>` : ""}
        ${["failed", "cancelled"].includes(j.status) && !j.interrupted ? `<button data-jcmd="resume" data-jid="${esc(j.id)}">断点续跑</button>` : ""}
        ${["running", "paused", "queued"].includes(j.status) && !j.interrupted ? `<button data-jcmd="cancel" data-jid="${esc(j.id)}">中断</button>` : ""}
      </div>
    </div>`).join("");

  $$("[data-jopen]", box).forEach((el) => el.onclick = () => {
    if (S.pid === el.dataset.jopen) { S.step = 1; renderSteps(); render(); return; }
    loadProject(el.dataset.jopen).catch((e) => toast(e.message, "err"));
  });

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

/** 定时轮询：页面隐藏时停表，回来立即补一次。
 *  旧实现用 setInterval 常驻，切到后台仍按 1.5s/2s 请求接口；
 *  同时防止上一次请求没回来就叠加下一次（慢接口会排队）。 */
function pollEvery(ms, fn) {
  let timer = 0;
  let stopped = false;
  let busy = false;
  const tick = async () => {
    timer = 0;
    if (stopped || document.hidden) return;
    if (busy) { timer = setTimeout(tick, ms); return; }
    busy = true;
    try { await fn(); } catch {} finally { busy = false; }
    if (!stopped && !document.hidden) timer = setTimeout(tick, ms);
  };
  const onVisible = () => {
    if (!stopped && !document.hidden && !timer) timer = setTimeout(tick, ms);
  };
  document.addEventListener("visibilitychange", onVisible);
  timer = setTimeout(tick, ms);
  return () => {
    stopped = true;
    clearTimeout(timer);
    document.removeEventListener("visibilitychange", onVisible);
  };
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
    if (e.dataTransfer?.files?.length) pickFiles([...e.dataTransfer.files]);
  });
  $("#file-input").onchange = (e) => { pickFiles([...e.target.files]); e.target.value = ""; };
  $("#btn-pick")?.addEventListener("click", () => $("#file-input").click());

  function pickFiles(files) {
    const okExt = new Set(["stl", "obj", "ply", "3mf", "off", "glb", "gltf"]);
    const good = [], bad = [];
    const limit = 512 * 1024 * 1024;
    for (const f of files) {
      const ext = (f.name.split(".").pop() || "").toLowerCase();
      if (!okExt.has(ext)) bad.push(`${f.name}（不支持 ${ext || "无扩展名"}）`);
      else if (f.size === 0) bad.push(`${f.name}（空文件）`);
      else if (f.size > limit) bad.push(`${f.name}（超过 512MB）`);
      else good.push(f);
    }
    if (bad.length) toast(`已跳过 ${bad.length} 个文件：${bad.slice(0, 2).join("；")}`, "warn");
    if (good.length) uploadFiles(good);
  }
}

function bindHelp() {
  const btn = $("#btn-help");
  if (btn) btn.onclick = showShortcuts;
}

async function boot() {
  await loadWorkflow();
  await loadPrinters();
  if (S.printerKey) await loadPrinterDetail(S.printerKey);
  await loadMaterials(S.printerDetail?.process || S.printerProcess);
  await loadProjects();
  renderSteps();
  // 支持 #p=<id>&s=<step> 深链：刷新、书签、从任务通知打开都能回到原处
  const target = hashTarget();
  if (target.pid && S.projects.some((p) => p.id === target.pid)) {
    try {
      await loadProject(target.pid);
      if (target.step != null && target.step !== S.step) {
        S.step = target.step; renderSteps(); render();
      }
    } catch {
      toast("链接里的项目已不存在", "warn");
      render();
    }
  } else {
    render();
  }
  bindDrop();
  bindHelp();
  $("#btn-pause").onclick = () => jobCmd(S.job?.status === "paused" ? "resume" : "pause");
  $("#btn-cancel").onclick = () => jobCmd("cancel");
  pollResources();
  pollEvery(2000, pollResources);
  refreshQueue();
  // 队列列表不必 1.5s 刷一次：任务运行时 startJob 每 350ms 会顺带刷新，
  // 空闲时 5s 足够，后台标签页直接停表。
  pollEvery(5000, refreshQueue);
  window.addEventListener("resize", () => (S.viewers || []).forEach((v) => v.resize()));
  window.addEventListener("hashchange", async () => {
    const t = hashTarget();
    if (t.pid && t.pid !== S.pid && S.projects.some((p) => p.id === t.pid)) {
      try { await loadProject(t.pid); } catch { /* 项目可能刚被删 */ }
    }
    if (t.step != null && t.step !== S.step) { S.step = t.step; renderSteps(); render(); }
  });
  window.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea") return;
    if (e.key === "?") { e.preventDefault(); showShortcuts(); return; }
    // 数字键 1–6 直达步骤；输入框里不触发（上面已排除 input/select/textarea）
    if (/^[1-6]$/.test(e.key) && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault(); goto(Number(e.key) - 1); return;
    }
    // 建议页按 P 直接换机型，省得去点按钮
    if ((e.key === "p" || e.key === "P") && S.step === 4 && !e.ctrlKey && !e.metaKey) {
      e.preventDefault(); openPrinterPicker(); return;
    }
    if (e.key === "Escape") {
      ["#tour-overlay", "#help-overlay"].forEach((sel) => $(sel)?.remove());
      S.viewers.forEach((v) => v.disablePick && v.pickMode && v.disablePick());
      return;
    }
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
