/**
 * 3D 预览：Three.js 场景、轨道控制、热力图着色、剖切、点选。
 *
 * 单独成模块是因为它有一整套自己的生命周期（初始化/销毁/重建几何），
 * 和界面状态机混在一起会互相干扰——切换步骤时最容易漏掉 dispose。
 */

const round = (v, d = 2) => Math.round(v * 10 ** d) / 10 ** d;

function b64ToTyped(b64, T) {
  const bin = atob(b64);
  const buf = new ArrayBuffer(bin.length);
  const u8 = new Uint8Array(buf);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  return new T(buf);
}

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
    this.labels = [];

    this.theta = 0.9; this.phi = 1.15; this.radius = 3; this.target = new T.Vector3();
    this._bind();
    this._loop();
    this.ok = true;
    this.resize();
  }

  pick(ev) {
    if (!this.ok || !this.mesh) return null;
    const T = this.THREE;
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new T.Vector2(
      ((ev.clientX - rect.left) / rect.width) * 2 - 1,
      -((ev.clientY - rect.top) / rect.height) * 2 + 1,
    );
    const rc = new T.Raycaster();
    rc.setFromCamera(ndc, this.camera);
    const hits = rc.intersectObject(this.mesh, false);
    if (!hits.length) return null;
    // group 有 -90° 旋转，世界坐标要转回模型坐标才能和后端对齐
    const local = this.group.worldToLocal(hits[0].point.clone());
    return [round(local.x, 2), round(local.y, 2), round(local.z, 2)];
  }

  enablePick(cb) {
    this.pickMode = true;
    this.onPick = cb;
    if (this.renderer) this.renderer.domElement.style.cursor = "crosshair";
  }

  disablePick() {
    this.pickMode = false;
    this.onPick = null;
    if (this.renderer) this.renderer.domElement.style.cursor = "";
  }

  _bind() {
    const c = this.renderer.domElement;
    let drag = null;
    c.addEventListener("pointerdown", (e) => {
      if (this.pickMode && this.onPick) {
        const p = this.pick(e);
        if (p) this.onPick(p);
        return;
      }
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

  setVertexColors(colors) {
    if (!this.ok || !this.mesh) return;
    const T = this.THREE;
    this.mesh.geometry.setAttribute("color", new T.BufferAttribute(colors, 3));
    this.mat.vertexColors = true;
    this.mat.color.set(0xffffff);
    this.mat.needsUpdate = true;
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

  drawSection(paths) {
    if (!this.ok) return 0;
    const T = this.THREE;
    this.clearSection();
    if (!paths || !paths.length) return 0;
    const pts = [];
    for (const path of paths) {
      for (let i = 1; i < path.length; i++) {
        pts.push(path[i - 1][0], path[i - 1][1], path[i - 1][2],
                 path[i][0], path[i][1], path[i][2]);
      }
    }
    if (!pts.length) return 0;
    const g = new T.BufferGeometry();
    g.setAttribute("position", new T.Float32BufferAttribute(pts, 3));
    // depthTest 关掉：截面轮廓在实体内部，不关会被模型表面完全挡住
    this.section = new T.LineSegments(g, new T.LineBasicMaterial({
      color: 0x5dcaa5, depthTest: false, transparent: true, opacity: 0.95,
    }));
    this.section.renderOrder = 10;
    this.group.add(this.section);
    return paths.length;
  }

  showDimensions() {
    if (!this.ok || !this.box) return 0;
    const T = this.THREE;
    this.clearLabels();
    const size = this.box.getSize(new T.Vector3());
    const min = this.box.min, max = this.box.max;
    const cx = (min.x + max.x) / 2, cy = (min.y + max.y) / 2, cz = (min.z + max.z) / 2;
    const span = Math.max(size.x, size.y, size.z) || 1;
    const mk = (text, pos, color) => {
      const cv = document.createElement("canvas");
      cv.width = 320; cv.height = 80;
      const ctx = cv.getContext("2d");
      ctx.fillStyle = "rgba(14,16,20,0.88)";
      ctx.fillRect(0, 0, 320, 80);
      ctx.strokeStyle = color; ctx.lineWidth = 3;
      ctx.strokeRect(1.5, 1.5, 317, 77);
      ctx.fillStyle = color;
      ctx.font = "500 40px Consolas, monospace";
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(text, 160, 42);
      const tex = new T.CanvasTexture(cv);
      tex.anisotropy = 4;
      const sp = new T.Sprite(new T.SpriteMaterial({ map: tex, depthTest: false, transparent: true }));
      sp.position.set(pos[0], pos[1], pos[2]);
      const w = span * 0.26;
      sp.scale.set(w, w / 4, 1);
      sp.renderOrder = 12;
      this.group.add(sp);
      this.labels.push(sp);
    };
    // 注意：group 旋转了 -90°，这里的坐标是**模型坐标**（Z 朝上）
    mk(`X ${size.x.toFixed(1)}`, [cx, min.y, min.z], "#378add");
    mk(`Y ${size.y.toFixed(1)}`, [max.x, cy, min.z], "#5dcaa5");
    mk(`Z ${size.z.toFixed(1)}`, [max.x, min.y, cz], "#ef9f27");
    return this.labels.length;
  }

  clearLabels() {
    (this.labels || []).forEach((l) => {
      this.group.remove(l);
      l.material?.map?.dispose?.();
      l.material?.dispose?.();
    });
    this.labels = [];
  }

  clearSection() {
    if (this.section) {
      this.group.remove(this.section);
      this.section.geometry?.dispose?.();
      this.section = null;
    }
  }

  toggleAxes() {
    if (!this.ok) return false;
    const T = this.THREE;
    if (this.axes) {
      this.scene.remove(this.axes);
      this.axes = null;
      return false;
    }
    const size = this.box ? Math.max(this.box.getSize(new T.Vector3()).length() * 0.18, 5) : 20;
    this.axes = new T.AxesHelper(size);
    this.axes.material.depthTest = false;
    this.axes.renderOrder = 8;
    this.scene.add(this.axes);
    return true;
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

export { Viewer, b64ToTyped, round };
