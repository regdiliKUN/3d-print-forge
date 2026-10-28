# 模型修补 · 迭代记录

> 对象：`I:\PrinterApp` 的网格检测 / 修补管线（`app/analysis.py`、`app/repair.py`、`app/voxelops.py`）。
> 目标：1000 轮可验证迭代，优先修「补洞 / 非流形 / 自相交 / 法线 / 水密性 / 壁厚」。
> 当前进度：**183 / 1000 轮**（1–19、20–40、41–61、62–81、63–164 五段；本文件只记录已落地并有验证的轮次，不把计划算作完成）。
> 当前基线：`pytest tests -q` → **420 passed**；`bench/round_gate.py` → **39/39**；`tests/e2e.py` → **47/47**；`bench/probe_tritri.py` → **10/10**。
>
> 第 20–40 轮（2026-10-23）覆盖：检测漏检、补丁穿插、零厚度薄片、评分可信度、参数同口径、
> 动作顺序、异常隔离、接口校验、任务并发、缓存污染、派生数据共享、结果可见性。
> 第 41–61 轮（同日第二批）把范围从「检测/修补」扩到处理链、分件、拼盘、版本对比、批处理，
> 并继续修「补不上的洞不报原因」「整体反法线补完还是反的」「孤立顶点被平滑拽到原点」这类
> 会静默给出错误结果的问题。整体提升见第六节。

---

## 一、总览

| 轮次 | 缺陷编号 | 级别 | 一句话结果 | 主要文件 |
|---:|---|---|---|---|
| 1 | MESH-001 | S2 | 自触边界环拆成简单环，补洞不再制造非流形边 | `repair.py` |
| 2 | MESH-002 | S2 | 平滑补洞先清重复面/孤立薄片，不再补出零厚度双面 | `repair.py` |
| 3 | MESH-003 | S1 | 补洞验收改为零新增非流形/重复面，失败回退并给出原因 | `repair.py` |
| 4 | MESH-004 | S2 | 识别重复面；`edge_keys` 索引越界时自行收紧 | `analysis.py` |
| 5 | MESH-005 | S2 | 自相交改为精确三角-三角判定，覆盖共面重叠与共顶点折叠 | `analysis.py` |
| 6 | MESH-006 | S2 | 法线按连通分量 + 绕序冲突修复，不再只看总体积 | `analysis.py`、`repair.py` |
| 7 | MESH-007 | S2 | 内腔/碎屑按「负体积且在实体内」分类，修正量纲混用 | `analysis.py`、`repair.py` |
| 8 | MESH-008 | S3 | 射线索引缓存同时校验 verts/faces 身份，避免旧索引导致漏检 | `analysis.py` |
| 9 | MESH-009 | S2 | 壁厚全量 0 命中时不再无条件判安全，回退粗筛实测值 | `analysis.py` |
| 10 | TEST-001 | S3 | 建立修补回归入口，47 个用例覆盖几何量而不只是「不抛异常」 | `tests/test_repair.py` |
| 11 | MESH-010 | S2 | 耳切失败不再用凹多边形不安全的扇形兜底，并做面积校验 | `repair.py` |
| 12 | MESH-011 | S1 | 体素 pitch 上下界收敛，微小模型不再被固定 0.2mm floor 切没 | `voxelops.py` |
| 13 | MESH-012 | S2 | 自交重网格的 pitch 统一走安全收敛，非法输入不再直传 | `repair.py` |
| 14 | MESH-013 | S3 | 壁厚射线异常标记 degraded，不再用 KDTree 最近顶点伪造数值 | `analysis.py` |
| 15 | MESH-014 | S1 | 加厚量按模型尺度收敛，避免超大距离场撑爆内存 | `repair.py` |
| 16 | MESH-015 | S2 | 单层网格片补洞验收为「零厚度壳」并回退 | `repair.py` |
| 17 | MESH-016 | S2 | 缩放/迭代次数/碎屑比例等外部参数做合法区间校验 | `repair.py` |
| 18 | TEST-002 | S3 | 新增 9 项快速几何闸门，作为每轮续跑的入口 | `bench/round_gate.py` |
| 19 | API-001 | S2 | 修复接口按白名单透传动作参数，未知动作直接 422 | `main.py` |
| 20 | MESH-019 | S2 | 自交候选改为两层半径，细长大三角形不再漏检 | `analysis.py` |
| 21 | MESH-017 | S2 | 补洞新增「补丁与实体穿插」验收，穿墙补丁直接回退 | `repair.py` |
| 22 | MESH-018 | S2 | 多三角形零厚度薄片按连通分量识别并删除 | `repair.py` |
| 23 | MESH-020 | S2 | 重网格前后各数一次自交，未变好标降级 | `repair.py` |
| 24 | MESH-021 | S3 | 0.2~1.0 量级单位判定改为二义提示，不再盲推 ×1000 | `analysis.py` |
| 25 | SCORE-001 | S2 | 壁厚未测出/检测失败不得评 A，等级封顶 C 并给出原因 | `analysis.py` |
| 26 | CONSIST-001 | S2 | 碎屑阈值检测与动作同口径收敛，主件不再被判碎屑 | `analysis.py`、`repair.py` |
| 27 | ORDER-001 | S2 | 动作顺序固定 rescale 早于 thicken、thicken 最后 | `repair.py` |
| 28 | NORMAL-001 | S2 | 法线修复验收绕序冲突，消不掉则标降级 | `repair.py` |
| 29 | ROBUST-001 | S2 | 单项检测异常不再拖垮整份报告 | `analysis.py` |
| 30 | ROBUST-002 | S3 | `union_components` 越界索引自行收紧 | `analysis.py` |
| 31 | API-003 | S2 | 修复/检测入参在接口层做区间校验，越界 422 | `main.py` |
| 32 | API-004 | S2 | 等比缩放改用「用户填的那根轴」为基准 | `main.py` |
| 33 | JOBS-001 | S2 | 任务持久化串行化 + 独立临时文件，不再丢记录 | `jobs.py` |
| 34 | JOBS-002 | S2 | 排队中暂停的任务不再白占工作线程 | `jobs.py` |
| 35 | CACHE-001 | S1 | 缓存网格不再被调用方原地改坏（缩放/平移/合并顶点） | `meshio.py` + 9 处调用点 |
| 36 | PERF-001 | S3 | 七项检测共享边直方图/重复面/连通分量，串行耗时 −11.5% | `analysis.py` |
| 37 | UI-001 | S2 | 回退/降级/收敛原因显示到界面，不再只写日志 | `static/app.js`、`style.css` |
| 38 | UI-002 | S2 | 补洞细分/平滑、重网格精度补上界面入口 | `static/app.js` |
| 39 | TEST-003 | S3 | 闸门 9 → 18 项，新增回归 27 项 | `bench/round_gate.py`、`tests/test_rounds_20_40.py` |
| 40 | DOC-001 | S3 | README 的测试数/CI/覆盖率按实测改正 | `README.md` |
| 41 | MESH-022 | S3 | 边界环分解失败的边不再静默丢弃，报告「补不上是因为拓扑」 | `repair.py` |
| 42 | SCORE-002 | S2 | 壁厚粗筛分数按覆盖率打折，20 万面只测 600 面不再给满分 | `analysis.py` |
| 43 | MESH-023 | S2 | 整体反法线的模型补完洞按体积符号转正 | `repair.py` |
| 44 | PROC-001 | S2 | 打孔薄壁过滤从死代码改为按实测壳厚生效，全拒时降级而非失败 | `process.py` |
| 45 | PROC-002 | S2 | 支撑去重半径跟 spacing 挂钩，不再比设定密一倍 | `process.py` |
| 46 | PROC-003 | S3 | 缩放后支撑直径如实报告；导出面数统计含支撑 | `process.py` |
| 47 | PROC-004 | S2 | 减面破坏水密性当场回退并说明 | `process.py` |
| 48 | PROC-005 | S3 | 支撑跳过阈值参数化（注释 10mm / 代码 3mm 不一致） | `process.py` |
| 49 | SPLIT-001 | S2 | 分件桩位改取「实心区内」的点，不再落在空腔 | `split.py` |
| 50 | SPLIT-002 | S2 | 分件验收每块水密性，封盖失败明确报出 | `split.py` |
| 51 | PACK-001 | S2 | 平台参数校验（NaN/0 不再导出非法 STL）+ 转 90° 可放下的提示 | `pack.py` |
| 52 | CMP-001 | S2 | 版本对比改点到面距离，同一平面不同密度不再报「全部移动」 | `compare.py` |
| 53 | CMP-002 | S3 | 对比区分「整体缩放」与「形状变形」 | `compare.py` |
| 54 | GPU-001 | S3 | 补洞不再为报一句「用了什么」强制探测后端（首次 +330MB / +1s） | `repair.py`、`gpu.py` |
| 55 | GPU-002 | S2 | 孤立顶点不再被拉普拉斯平滑拽向原点 | `gpu.py` |
| 56 | API-006 | S2 | 处理/分件/拼盘/挖孔/旋转的枚举与范围校验（422） | `main.py` |
| 57 | UI-003 | S2 | 检测失败/降级项在报告里标「未确认」 | `static/app.js`、`style.css` |
| 58 | UI-004 | S2 | 处理警告（减面回退/薄处开孔/支撑直径）显示到界面 | `static/app.js` |
| 59 | BATCH-001 | S2 | 批量检测走同一套异常隔离；报告补质量评分；回退的修复不再记成功 | `batch.py` |
| 60 | TEST-004 | S3 | 闸门 18 → 28 项，新增回归 28 项 | `bench/round_gate.py`、`tests/test_rounds_41_61.py` |
| 61 | DOC-002 | S3 | 文档与实测同步 | `ROUNDS.md`、`README.md` |

级别：S1 = 可导致崩溃/内存爆炸/数据破坏；S2 = 修复结果错误或谎报成功；S3 = 检测误报/漏报、测试与可观测性。

---

## 二、逐轮记录

### 第 1 轮 · MESH-001（S2）边界环自触

- **现象**：两个洞只共享一个顶点时，边界走访返回 `[272,21,467,510,21,256]` 这类 8 字环。直接投影耳切会在捏合点生成重叠三角形，补完出现 4 个面共享同一条边。
- **改动**：`repair.boundary_loops` 新增 `_split_closed_walk`，用栈把重复顶点之间的片段切出来，保证返回的每个环都是简单环。
- **验证**：`test_boundary_loops_split_at_pinch_vertex`；`broken_ball` 补洞后开放边 96 → 0、非流形 0。
- **遗留**：边界图顶点度数 > 4 的非流形开口尚未专门建模，当前走「能拆则拆，拆不了跳过」。

### 第 2 轮 · MESH-002（S2）孤立薄片被补成双面

- **现象**：一个三角形失去所有邻居后，三条边都是开放边，看起来像三角洞。旧平滑补洞把它补上，得到两个完全重合的面，边计数「正常」但实际是零厚度壳。
- **改动**：新增 `_face_key`、`_dedupe_faces`、`_sliver_face_indices`；`fill_holes_smooth` / `fill_holes_manual` 先删重复面和孤立薄片，再重新找边界环。
- **验证**：`test_fill_removes_sliver_instead_of_duplicating_it`；`bench/probe_repair.py` 中 sliver 13 面 → 12 面、体积 1000、水密 `True`。
- **遗留**：多三角形组成的孤立薄片仍主要依赖后续验收拦截，未做专门的连通分量级薄片识别。

### 第 3 轮 · MESH-003（S1）补洞验收谎报成功

- **现象**：旧验收允许非流形边 +2，理由是「补洞难免碰坏两条边」。结果是补完仍非流形却报成功；两条候选路径失败时也没有可读原因。
- **改动**：新增 `_repair_health`、`_loop_summary`；`act_fill_holes` 改为零新增非流形边/重复面，平滑与平面两条路径都评分后择优；失败返回 `reverted`、`revert_reason`、`rejected_candidates`、`remaining_loops`。
- **验证**：`test_fill_never_increases_nonmanifold`、`test_fill_rejects_when_all_holes_are_too_large`；所有补洞用例断言开放边、非流形边、重复面、体积。
- **遗留**：验收尚未纳入「补丁自相交」计数，留待第 20 轮之后处理。

### 第 4 轮 · MESH-004（S2）重复面与非流形漏检

- **现象**：整张面被复制一遍时，每条边仍有面、开放边为 0，只数开放边的水密检测会判通过；`edge_keys` 在调用方传入过小 `nverts` 时可能让不同边撞 key。
- **改动**：新增 `analysis.duplicate_face_count`；`check_watertight`、`check_nonmanifold` 将重复面判为 error；`edge_keys` 用实际最大索引收紧 `nverts`。
- **验证**：`test_check_watertight_duplicate_faces_is_error`、`test_fill_dedupes_double_faces`、`test_edge_keys_are_collision_free_when_nverts_too_small`；重复面立方体 24 面 → 12 面、体积 1000、水密。
- **遗留**：重复面按「顶点集合相同」判定，顶点相同但绕序/法线不同的重叠面也能识别；几何重合但顶点不同的面仍依赖自相交检测。

### 第 5 轮 · MESH-005（S2）自相交检测误报/漏报

- **现象**：旧 11 轴 SAT 对共面三角形必然投影重叠，会把体素重网格出的大量相邻面误判；同时又会跳过共面重叠和共顶点折叠，真正的重复面/折面漏检。
- **改动**：新增 `_unit`、`_plane_cut_interval`、`_coplanar_overlap_2d`、`tri_tri_overlap`；`find_self_intersections` 对 `share>=3` 记重复面、`share==2` 跳过共边邻居、`share<2` 做精确判定；共面走 2D SAT，非共面走两平面交线区间。
- **验证**：`test_tri_tri_overlap`（10 组参数化）、`test_self_intersection_detects_duplicate_faces`、`test_self_intersection_detects_shared_vertex_fold`、`test_self_intersection_clean_sphere_has_no_false_positive`、`bench/probe_tritri.py` 10/10；两球穿插 76 对，干净球 0 对。
- **遗留**：候选对仍由「中心点 KD 树 + 99 分位半径」产生，极大三角形且中心相距很远时理论上可能漏候选；后续可改为 AABB 树查询。

### 第 6 轮 · MESH-006（S2）整体有向体积掩盖局部翻面

- **现象**：一个实体朝外、另一个朝内时总体积为 0，旧逻辑看不出来；同一连通体里少量面绕序翻转也无法定位。
- **改动**：新增 `winding_conflict_count`、`component_volumes`；`check_normals` 逐连通体判负体积并检查绕序冲突；`act_fix_normals` 先用 BFS 统一绕序，再逐分量翻转负体积。
- **验证**：`test_check_normals_detects_inverted_component`、`test_check_normals_detects_winding_conflict`、`test_fix_normals_makes_multi_body_positive`、`test_fix_normals_repairs_winding_only`；`multi_one_inverted` 体积 0 → 2000、`flipped_components=1`。
- **遗留**：开放曲面的「正确朝向」没有唯一解，当前只保证绕序一致；对开放面片不做强制翻正。

### 第 7 轮 · MESH-007（S2）内腔被当碎屑删掉 / 量纲混用

- **现象**：镂空件的内壳是负体积独立连通体，按面积比例会被当碎屑清掉；旧 `tiny_volume` 是 mm³，却直接和三角形面积和（mm²）比较。
- **改动**：新增 `classify_components`，负体积且位于正体积实体内才判内腔；`act_remove_small` 与 `check_components` 共用同一分类；碎屑阈值改为面积占比。
- **验证**：`test_remove_small_keeps_true_cavity`、`test_remove_small_removes_inverted_debris`、`test_component_check_uses_area_ratio_not_wrong_units`。
- **遗留**：`solid.contains` 采样对非常薄的实体可能不稳定，当前异常时保守判为内腔（保留）。

### 第 8 轮 · MESH-008（S3）射线索引缓存失效

- **现象**：缓存 key 只用 `id(verts)`；数组被回收后地址复用，或 `verts` 不变但 `faces` 被替换时，会拿到旧 rtree 索引，射线全部落空，壁厚检测误判为安全。
- **改动**：`_raycast_nearest` 的缓存 key 同时包含 `id(faces)` 与面数，命中后再次校验 `cached[1] is verts and cached[2] is faces`，并持有数组引用防止提前回收。
- **验证**：`bench/verify_raycast.py`；壁厚相关测试与 `test_wall_thickness_marks_degraded_on_raycast_failure`。
- **遗留**：同一数组原地修改仍无法通过身份校验发现；调用方目前都传新数组或不可变视图。

### 第 9 轮 · MESH-009（S2）壁厚 0 命中判安全

- **现象**：全量采样一条射线都没命中时，旧逻辑无条件返回 ok。粗筛可能已经量到 0.3mm 薄壁，却被全量阶段覆盖。
- **改动**：`check_thickness` 在全量 `samples==0` 时回退粗筛实测值，按 `wall_fail` / `wall_warn` 分级；质量评分支持 `lower_bound`，并对重复面、内腔、绕序扣分。
- **验证**：`test_check_thickness_uses_coarse_value_when_full_misses`（若测试名随重构变化，以 `pytest -k thickness` 为准）；`test_quality_score_penalises_nonmanifold`。
- **遗留**：`max_dist` 截断意味着「未命中」只代表「不小于阈值」，报告必须保留 `lower_bound` 语义。

### 第 10 轮 · TEST-001（S3）修补回归入口

- **现象**：原有测试只有打印机预设，修补逻辑没有任何回归保护；很多修复只能靠一次性探针观察。
- **改动**：新增 `tests/test_repair.py`，统一用开放边、非流形边、重复面、有向体积、水密性断言；本轮同时修正了两个测试夹具（单面洞体积、`edge_keys` 碰撞组合）。
- **验证**：`tests/test_repair.py` 当前 **47 passed**（38 个测试函数，其中三角-三角用例参数化为 10 组）；全量 `tests` **81 passed**。
- **遗留**：尚未覆盖 `main.py` 的任务调度/接口层；后续补 API 级回归。

### 第 11 轮 · MESH-010（S2）凹多边形扇形兜底

- **现象**：`mapbox_earcut` 缺失或失败时，旧兜底从 0 号点拉扇形。凹洞会把三角形补到轮廓外，形成自相交面片，而补洞验收只看开放边/非流形/重复面，可能放行。
- **改动**：新增 `_point_in_tri_2d`、`_ear_clip_2d`；`triangulate_loop` 失败时先耳切，耳切也失败才回退；补洞后做「三角形面积和 = 多边形面积」校验，自交/退化环直接返回空。
- **验证**：`test_triangulate_concave_loop_earcut_fallback`（L 形面积 3.0、4 个三角形、边界边各用一次）、`test_triangulate_self_intersecting_loop_is_rejected`；`bench/round_gate.py` 的 `ear_clip_concave` 9/9 通过。
- **遗留**：面积校验只覆盖投影平面内的重叠；非平面补丁的三维自交仍需第 3 轮提到的补丁自交验收。

### 第 12 轮 · MESH-011（S1）体素 pitch 失控 / 微小模型被切没

- **现象**：`to_grid` 直接使用外部 pitch。`1e-9` 会让 1000mm 模型产生 1e12 级体素；`1e9` 会让模型不足一个体素。`choose_pitch` 固定 `floor=0.2mm`，0.5mm 珠宝件只切 2~3 格，marching cubes 重建不出形状。
- **改动**：`voxelops` 新增 `MAX_AXIS_VOXELS=256`、`sanitize_pitch`；pitch 收敛到「每轴 ≤256 格」与「最长边至少 8 格、最薄非零边至少 1 格」；`choose_pitch` 的 floor 不超过最长边的 1/20。
- **验证**：`test_sanitize_pitch_bounds_and_empty_mesh`、`test_to_grid_tiny_model_and_huge_pitch`；0.5mm 立方体实际 pitch 0.025、grid 23³；20mm 立方体请求 1e9 → 2.5mm。
- **遗留**：极薄片（厚度 < 最长边/256）在内存上限内无法同时保留厚度，当前优先保证内存有界，会量化掉厚度。

### 第 13 轮 · MESH-012（S2）自交重网格 pitch 未校验

- **现象**：`act_resolve_self_intersection` 直接 `float(params.get("remesh_pitch"))`，负数/NaN/超大值会进入 `voxelops.remesh`，错误信息也不含实际生效 pitch。
- **改动**：解析失败或 ≤0 时退回 `sanitize_pitch` 默认值；返回 `pitch`（实际生效）与 `pitch_requested`（原始请求）。
- **验证**：`test_resolve_self_intersection_clamps_bad_pitch`；请求 1e9 时实际 pitch ≤20 且输出体积 >0。
- **遗留**：重网格后尚未复检自相交数量，只保证体素重建成功。

### 第 14 轮 · MESH-013（S3）壁厚异常兜底伪造数值

- **现象**：射线求交异常时，旧代码用 KDTree 最近顶点距离 ×2 冒充壁厚。最近顶点通常在同一个面上，量出来接近 0，健康模型会误报薄壁；也可能漏掉真正薄点。
- **改动**：`sample_wall_thickness` 异常时返回 `degraded=True`、`samples=0`、`error`，不再编造 min/median；`check_thickness` 粗筛或全量遇到 degraded 返回 `info` 级「无法可靠测量」，而不是 ok。
- **验证**：`test_wall_thickness_marks_degraded_on_raycast_failure`（monkeypatch 射线函数抛错，断言 severity 为 info）。
- **遗留**：degraded 时前端需要显式展示「检测失败」而不是「厚度安全」，属于 UI 待办。

### 第 15 轮 · MESH-014（S1）加厚量无上限

- **现象**：`thicken(amount)` 的距离场要为 amount 预留空余体素。外部传 1000mm 会 pad 出数千层，直接吃掉数 GB 内存；`int(params.get("thicken_smooth"))` 也可能抛 `ValueError`。
- **改动**：加厚量校验为有限正数，并收敛到 `max(0.5, 最长边×0.25)`；返回 `requested_mm` 与收敛提示；平滑次数走 `_bounded_int`。
- **验证**：`test_thicken_clamps_absurd_amount`（2mm 立方体请求 1e6mm，实际 0.5mm，输出体积 >0）。
- **遗留**：收敛上限是经验值，若将来支持「大件薄壳加厚」需要按打印机成型空间重定。

### 第 16 轮 · MESH-015（S2）单层网格片补成零厚度壳

- **现象**：5×5 单层三角网格片有 20 条开放边，但补洞会生成第二层皮，闭合后非流形 0、重复面 0、有向体积 0。旧验收只在「修复前体积非零」时检查塌缩，因此报告 `filled=True`。
- **改动**：`_repair_health` 增加 `abs_volume`：有向体积接近 0 时按连通分量取绝对体积和，区分「真零厚度」与「正负分量抵消」；`_judge` 只要补完 `abs_volume <= min_volume` 就判零厚度壳并回退。
- **验证**：`test_fill_open_sheet_rejected_as_zero_thickness`（`reverted=True`，回退后保留 50 个原始面）；`bench/round_gate.py` 的 `reject_zero_thickness_shell` 通过。
- **遗留**：附着在实体边缘的「鳍片」仍主要靠非流形边拦截；需要专门的薄片/鳍片连通分量检测。

### 第 17 轮 · MESH-016（S2）外部数值参数未校验

- **现象**：`act_rescale` 接受 0、负数、NaN；`fill_levels`、`fill_smooth`、`remesh_smooth`、`thicken_smooth`、`max_loop`、`small_ratio` 直接 `int()/float()`，非法值会抛异常或产生危险行为（缩放到点、碎屑阈值反转）。
- **改动**：新增 `_bounded_int`；补洞/重网格/加厚平滑次数与 `max_loop` 全部夹到安全区间；`small_ratio` 收敛到 `[0, 0.5]`；`act_rescale` 拒绝非有限、≤0、超出 `1e-6 ~ 1e6` 的系数。
- **验证**：`test_rescale_rejects_invalid_factor`、`test_fill_action_sanitizes_numeric_params`。
- **遗留**：`main.py` 的 API 层仍应在入参 schema 处做同样的范围校验，形成双保险。

### 第 18 轮 · TEST-002（S3）快速几何闸门

- **现象**：`pytest` 覆盖全，但每轮开始时要先跑 80+ 项、再看多个探针，反馈链路过长；探针输出是自由文本，不能直接判定通过/失败。
- **改动**：新增 `bench/round_gate.py`，9 项确定性检查：平面洞体积、破碎球补洞、薄片删除、重复面去重、逐分量法线、自相交检出/误报、零厚度壳回退、pitch 收敛、凹多边形耳切。支持 `--json`，失败退出码 1。
- **验证**：`.venv\Scripts\python.exe bench\round_gate.py` → **9/9 通过，约 0.5s**。
- **遗留**：闸门尚未纳入 `thicken`、`remove_small`、API 任务层；后续按轮次补充。

### 第 19 轮 · API-001（S2）修复动作参数被丢弃

- **现象**：`POST /api/projects/{pid}/repair` 只把 `small_ratio` 放进任务参数，前端传的 `thicken_mm`、`thicken_smooth`、`remesh_pitch`、`remesh_smooth`、`fill_levels`、`fill_smooth`、`max_loop`、`scale_factor` 全部被静默丢弃，后端一直用默认值。用户改「加厚 0.8mm」实际仍加厚 0.5mm。未知动作也会创建一条只写日志、什么也不做的任务。
- **改动**：`main.repair` 增加动作白名单校验（未知动作返回 422），按白名单透传 9 个动作参数，省略 `actions` 时保留默认的补洞 + 法线。
- **验证**：`test_repair_endpoint_forwards_action_params`：monkeypatch `_spawn` 捕获参数，断言 `thicken_mm=0.8`、`thicken_smooth=4`、`remesh_pitch=0.1` 原样到达，白名单外字段不进入任务；未知动作 422。
- **遗留**：接口层目前只做白名单，不做数值范围校验；具体数值仍由第 17 轮的 `repair.py` 内部收敛。后续可补 Pydantic schema，让 422 更早返回。

---

## 二之二、第 20–40 轮逐轮记录（2026-10-23）

### 第 20 轮 · MESH-019（S2）自交候选漏检

- **现象**：候选半径取 `min(99 分位 × 2.2, 对角线 × 0.25)`。只要一对三角形的圆心距超过封顶值，即使真实穿插也进不了候选集。夹具：200mm 长、2mm 宽的细长三角形，与 60mm 外一个真正穿过它的小三角形——精确判定 `tri_tri_overlap` 为 `True`，`find_self_intersections` 返回 **0**。
- **改动**：新增 `_candidate_pairs`，改成两层且**可证明完备**：批量层用 `p = min(99.9 分位, 上限/2)`，覆盖「两个半径都 ≤ p」的对；补查层对半径 > p 的少数大三角形逐个按 `2·r_i` 查询。判据是 `|c_i − c_j| ≤ r_i + r_j ≤ 2·max(r_i, r_j)`，没有分位数近似。
- **附带**：`max_pairs` 采样上限移到「剔除共边邻居之后」，不再把采样预算浪费在必然被排除的拓扑相邻对上；新增 `count_is_lower_bound` 标记。
- **验证**：`test_r20_candidate_pairs_do_not_miss_large_triangles`（0 → 1）；干净球体仍为 0 误报；闸门 `candidate_completeness`。
- **代价（实测）**：`ball_82k/analyze` 1004ms → 1218ms（+21%）。原因是以前 40 万候选对被截断后只精确判定 28.5 万对（`sampled=True`，`count` 其实是下限），现在判定 36.9 万对且不再采样。用 21% 的耗时换掉「静默采样导致的漏检」，仍在 5000ms 阈值内。

### 第 21 轮 · MESH-017（S2）补丁从实体里穿过去

- **现象**：补洞只验收开放边/非流形/重复面/体积。洞口正后方立着一片穿出顶面的墙时，补出来的面会直接切进墙里：开放边归零、体积正常、水密 `True`，切片器却读到互相矛盾的截面。
- **改动**：新增 `_patch_hits`，只对补丁面做邻域查询（几十到几百个面），与原网格共享 ≥2 顶点的缝合边跳过，其余做精确三角-三角判定。两条补洞路径都把 `patch_hits` 写进 info，`_judge` 只要 > 0 就回退。
- **验证**：`test_r21_patch_crossing_a_wall_is_rejected`（3 处穿插 → `reverted=True`，原模型面数不变）；正常补洞 `patch_hits=0`，闸门 `reject_patch_through_wall`。

### 第 22 轮 · MESH-018（S2）多三角形零厚度薄片

- **现象**：`_sliver_face_indices` 只认「3 条边全是开放边」的单个三角形。4 个三角形拼成的薄片识别不到，补洞会把它缝成双层皮（体积 0、边计数却正常）。旧实现的结果是**回退**——安全但什么都没修，用户看到「补洞未达验收标准」却不知道原因。
- **改动**：新增 `_sheet_face_indices`：按连通分量判「有开放边且 |有向体积| ≤ 容差」；删薄片后**重算边界环**（否则会照着薄片轮廓再补一层）。整张网格都是薄片时不删，改为给出明确原因。
- **验证**：`test_r22_multi_triangle_sheet_is_removed_not_filled`（16 面 → 12 面、体积 1000、水密、`removed_slivers=4`）；`test_r22_sheet_only_model_is_reverted_with_clear_reason`；闸门 `remove_multi_tri_sheet`。

### 第 23 轮 · MESH-020（S2）重网格后不复检

- **现象**：`act_resolve_self_intersection` 只要体素重建成功就返回，不数自交有没有减少。pitch 太大时重网格可能把两处穿插压成一处甚至不变，界面照样显示成功。
- **改动**：重网格前后各数一次自交（面数 > 20 万时只数后测，避免双倍开销），并记录水密、非流形、开放边、体积变化率。后测仍有自交且没变好 → `degraded` + 可执行的原因；体积变化 > 15% → 提示「原本可能是薄壳/中空」。
- **验证**：两球穿插 76 → 0、水密、体积 −0.32%；闸门 `remesh_verified`。

### 第 24 轮 · MESH-021（S3）单位推断把微缩件判成米

- **现象**：最大尺寸 < 1.0 一律判米并建议 ×1000。0.5mm 的微缩件会被放大成 500mm——比不提示更糟。
- **改动**：0.2~1.0 判为**二义**，`unit="m?"`、`ambiguous=True`，同时给出「按米 = 500mm」和「按毫米 = 0.5mm」两种解释；< 0.2 才判米。`check_units` 对二义降为 info，修复建议带 `needs_confirmation`。
- **验证**：`test_r24_units_are_ambiguous_between_micro_part_and_meters`。

### 第 25 轮 · SCORE-001（S2）没测出来也评 A

- **现象**：壁厚 `degraded` 时给 12/20 分，总分 92 仍判 A「可以直接打印」。等于用「没测到」冒充「没问题」——和之前空模型评 A 是同一类错误。
- **改动**：未测出/降级/检测失败一律 0 分，并记入 `degraded` 列表；只要有降级项，等级封顶 C，结论改为「有项目未能确认（…），建议先补测再打」。自交被采样时文案改为「≥ N 处（已采样）」。
- **验证**：`test_r25_unmeasured_thickness_cannot_earn_grade_a`、`test_r25_healthy_model_still_gets_grade_a`（不能矫枉过正）、闸门 `no_grade_a_when_unmeasured`。

### 第 26 轮 · CONSIST-001（S2）检测与动作对碎屑阈值不同口径

- **现象**：`check_components` 直接用 `float(params["small_ratio"])` 不收敛，文案里写死「1%」；`act_remove_small` 收敛到 [0,0.5]。传 5.0 时报告说「2 个部件里 2 个是碎屑」（主件也算），动作只删 1 个——同一份报告自相矛盾。传 `"abc"` 则整项检测抛异常。
- **改动**：新增 `sanitize_small_ratio`（非数/非有限回默认、夹到 [0,0.5]），检测项、`classify_components`、`act_remove_small` 共用；文案用实际阈值；补「所有分量都判碎屑时保留最大块」的兜底。
- **验证**：`test_r26_small_ratio_is_sanitized_identically_everywhere`（四种输入下 tiny 与 removed 完全一致）、`test_r26_message_uses_the_actual_ratio`；闸门 `small_ratio_consistent`。

### 第 27 轮 · ORDER-001（S2）动作顺序把加厚放在单位换算前

- **现象**：`ORDER_HINT` 只固定了五个动作，`rescale`/`thicken` 落在「保持原序」。用户勾成 `[thicken, rescale]` 就变成「先按旧单位加厚 0.8mm，再换算」——英寸模型差 25 倍。
- **改动**：把 `rescale` 提到 `merge_vertices` 之后、其余动作之前，`thicken` 固定最后（体素膨胀会吃掉细节）。
- **验证**：`test_r27_action_order_respects_units_and_voxel_ops`；闸门 `action_order`。

### 第 28 轮 · NORMAL-001（S2）绕序冲突修不掉也报成功

- **现象**：`orient_consistent` 只做一次 BFS 传播，同一面被两条路径要求相反翻转、或三面共边时必然留下冲突；`act_fix_normals` 无论结果如何都返回成功。重复面立方体实测冲突 18 → 18，界面显示「法线已统一」。
- **改动**：返回前比对 `winding_conflicts_before/after`，没变好标 `degraded` 并给出下一步（先移除碎屑或体素重网格）；整体体积仍为负也标降级（开放曲面没有唯一朝向）。
- **验证**：`test_r28_fix_normals_reports_unresolvable_conflicts`、`test_r28_fix_normals_clean_model_is_not_degraded`。

### 第 29 轮 · ROBUST-001（S2）一项检测抛异常，整份报告消失

- **现象**：七项检测在线程池里跑，`fut.result()` 一旦抛异常整个任务就 failed。用户拿不到其余六项结论，也看不出是哪一项坏了。
- **改动**：新增 `_safe_check`，把异常收成一条 info 级结论（带 `value.error`），其余六项照常；报告新增 `failed_checks`，摘要新增 `degraded`/`confidence`，评分据此封顶。
- **验证**：`test_r29_one_failing_check_does_not_kill_the_report`（7 项齐全、1 项失败、等级不是 A）；闸门 `check_failure_isolated`。

### 第 30 轮 · ROBUST-002（S3）越界顶点索引

- **现象**：`edge_keys` 早就会自行收紧 `nverts`，`union_components` 没有。外部文件出现越界索引时 `coo_matrix` 抛「axis 1 index 5 exceeds matrix dimension 3」，法线与孤立面片两项一起失败。
- **改动**：`union_components` 按实际最大索引收紧；`sample_wall_thickness` 的采样数收敛为合法正整数（负数会让 `rng.choice` 抛 `negative dimensions`）。
- **验证**：`test_r30_out_of_range_vertex_index_does_not_raise`、`test_r30_negative_sample_faces_is_clamped`。

### 第 31 轮 · API-003（S2）接口层不校验数值

- **现象**：第 17/19 轮把收敛放在 `repair.py` 内部，接口层照单全收。`sample_faces=-5` 会让检测任务在 `rng.choice` 处崩掉；`thicken_mm=1e9` 要等任务跑起来才被内部收敛，用户看到的是「任务失败」而不是「你填的数不合法」。
- **改动**：新增 `_PARAM_RANGES` 区间表与 `_checked_params`，检测/修复接口先校验再入队，非数字、非有限、越界一律 422 并说明范围；内部收敛保留作第二道防线。
- **验证**：`test_r31_api_rejects_out_of_range_numbers`（4 种越界全 422，合法值 200）。

### 第 32 轮 · API-004（S2）等比缩放用错了基准轴

- **现象**：`lock_ratio=True` 时用 `max(targets) / max(cur)` 当系数。模型 100×80×200、用户只填「宽度 50」，算成 50/200 = 0.25，宽度变成 25——填的数字和结果对不上。
- **改动**：抽出 `_resize_factors`，等比时以**第一个填了的轴**为基准；非等比时逐轴独立；目标尺寸做 0.01~10000mm 校验。
- **验证**：`test_r32_lock_ratio_uses_the_axis_the_user_filled`；接口实测 width=50 → extents[0]=50.0。

### 第 33 轮 · JOBS-001（S2）并发写 jobs.json 丢记录

- **现象**：`_persist` 是「读整个文件 → 改一条 → 写回」，多线程同时写会互相覆盖；临时文件名还都叫 `jobs.tmp`。实测 6 个任务各写 30 次，最终只剩 **4** 条记录。
- **改动**：加 `_PERSIST_LOCK` 串行化读改写，临时文件带任务 id；写入失败仍不影响任务本身。
- **验证**：`test_r33_concurrent_persist_does_not_lose_jobs`（6/6）。

### 第 34 轮 · JOBS-002（S2）暂停的任务白占工作线程

- **现象**：`_next_job` 接受 `queued` 和 `paused` 两种状态。排队中被暂停的任务仍会被取走，工作线程随即卡在 `tick` 的暂停循环里——暂停两个任务就能占满 2 线程池。
- **改动**：`_next_job` 只取 `queued`；`pause` 时若还没被取走就从队列摘出并置 `_paused_queued`；`resume` 分「重新排队」与「直接放行」两条路径；`cancel` 对排队中/排队暂停的任务直接标记取消。
- **验证**：`test_r34_paused_queued_job_does_not_hold_a_worker`（单线程池：暂停后 `ran` 里没有它，恢复后才执行）。

### 第 35 轮 · CACHE-001（S1）缓存网格被调用方原地改坏

- **现象**：`meshio.load_mesh` 直接返回缓存对象，而 9 处调用方会 `apply_scale` / `apply_translation` / `apply_transform` / `merge_vertices`。实测：加载 60mm 球 → `apply_scale(2)` → 再加载同路径拿到 **120mm**，且污染跨请求保留。回退到历史版本、装配预览、批量处理都会读到被改过的几何。
- **改动**：`load_mesh(..., mutable=True)` 返回缓存副本，默认只读；修复/处理/批量/拼盘/缩放/居中/旋转/装配共 9 处调用点改为 `mutable=True`。
- **验证**：`test_r35_cached_mesh_is_not_mutated_by_callers`；闸门 `cache_not_mutated`。

### 第 36 轮 · PERF-001（S3）七项检测重复算同一批派生量

- **现象**：边直方图（水密/非流形）、重复面（水密/非流形）、连通分量（法线/孤立面片）各被算 2~3 遍。
- **改动**：新增 `_mesh_memo`，把派生量挂在网格对象上共享。**不能按 `id(faces)` 缓存**——trimesh 的 `faces` 属性每次访问都返回新数组，按身份做键永远命中不了（第一版就是这么写的，实测零收益）。`classify_components` 增加 `labels` 参数复用标签。
- **验证**：18 万面模型串行 3.00s → **2.65s（−11.5%）**，结果完全一致；`test_r36_derived_data_is_shared_between_checks` 断言只算一次。
- **说明**：任务里是 4 线程并行，部分收益被并行掩盖，但省掉的是重复 CPU 与临时内存。

### 第 37 轮 · UI-001（S2）修复结果只有一句「面数 X → X」

- **现象**：`reverted`、`revert_reason`、`degraded`、`patch_hits`、加厚量被收敛、pitch 被收敛——全部只写进任务日志。界面永远显示「修复完成」，回退的修复也读起来像成功。
- **改动**：`afterRepair` 逐动作汇总成 `S.lastRepair`，修复页顶部新增「上次修复结果」卡：面数/水密前后、删除薄片数、自交前后、以及按 err/warn/info 着色的说明；没有回退或降级时明确写「修复结果可信」。
- **验证**：`test_r37_ui_surfaces_revert_and_degrade_reasons`（关键字段必须出现在界面代码里）。

### 第 38 轮 · UI-002（S2）进阶参数在界面上无法设置

- **现象**：接口从第 19 轮起已透传 9 个动作参数，但界面只有碎屑阈值、加厚量、缩放系数三个输入框，补洞细分/平滑、重网格精度只能吃默认值。另外碎屑阈值的标签写的是「占最大部件**体积**比」，而判据是**面积**比。
- **改动**：补 `p-levels`/`p-fillsmooth`/`p-pitch` 三个输入并按动作显隐；`runRepair` 只提交当前动作真正用到的参数；标签改为「占最大部件面积比」。
- **验证**：`test_r38_ui_exposes_fill_and_remesh_parameters`。

### 第 39 轮 · TEST-003（S3）闸门与回归

- **改动**：`bench/round_gate.py` 从 9 项扩到 **18 项**（新增：候选完整性、补丁穿插回退、多三角薄片、重网格复检、未测出不评 A、碎屑同口径、动作顺序、缓存隔离、检测异常隔离）；新增 `tests/test_rounds_20_40.py` **27 项**，每项对应一轮的可证伪目标。
- **验证**：闸门 18/18、约 1.15s；全量 `pytest` 108 passed（47 + 34 + 27）。

### 第 40 轮 · DOC-001（S3）README 与仓库不符

- **现象**：README 声称 223 项单元测试、47 项端到端、`tests/e2e.py`、`tests/ui_smoke.py`、`tests/bench.py`、GitHub Actions 双版本 CI、覆盖率 78%——实测测试 81→108 项，其余文件与工作流**都不存在**。优化记录里还把「99 分位 + 包围盒封顶」写成收益，而它正是第 20 轮修掉的漏检来源。
- **改动**：测试章节改为实测数字与按文件的用例分布，明确写出「没有的东西不写在这里」；给那条历史优化补上「后来发现引入漏检」的注脚。
- **同时**：`bench/expected.json` 按本轮实测重新录制（10 处指纹偏差全部可解释：新增 `large_triangles` / `count_is_lower_bound` / `small_ratio` / `ambiguous` 四个字段，候选对与判定对计数变化，`ball_82k` 的 `sampled` 由 `True` 变 `False`，`inch_254` 的 `ambiguous` 变 `True`）。全量基准 **0 项超阈值**。
- **验证**：`pytest tests -q` 108、逐文件 47/34/27 与文档一致。

---

## 二之三、第 41–61 轮逐轮记录（2026-10-23 第二批）

### 第 41 轮 · MESH-022（S3）补不上的洞不说原因

- **现象**：`boundary_loops` 只在走回起点时产出环，走不回去的走线被**静默丢弃**。实测「三面共边」的开放片有 3 条开放边，返回 **0 个环**，上层于是认为没有洞，补洞报告只说「开放边没有减少」——用户看不出是拓扑太烂还是算法不行。立方体挂一片只共享一条边的鳍（2 条开放边）同样补不上。
- **改动**：新增 `boundary_loops_ex`，统计 `boundary_edges / resolved_edges / unresolved_edges / open_walks / pinch_vertices`；`boundary_loops` 保持原签名做薄包装。`_loop_summary` 与回退原因都带上「另有 N 条边界边无法串成闭合环（边界图有 M 个分叉顶点 / K 段走不回起点）」。
- **关键口径**：一开始用「未使用的边」当 unresolved，结果三面共边报 0（边走完了但没成环）。改成 `boundary_edges − Σ环长` 才对。
- **验证**：`test_r41_unresolved_boundary_edges_are_reported`；闸门 `unresolved_loops_reported`。

### 第 42 轮 · SCORE-002（S2）粗筛的「安全」结论没有打折

- **现象**：壁厚粗筛抽 600 面、未发现薄壁就直接返回 `ok` + `lower_bound`，评分无条件给 20/20。20 万面模型只覆盖 0.3% 表面，一块占 0.1% 面积的局部薄壁有一半概率被漏掉，结论却写「整件厚度安全」。文案里还把「600 面」写死。
- **改动**：`sample_wall_thickness` 返回实际采样面数；`check_thickness` 按覆盖率分档措辞（≥2% 说安全，0.5–2% 说「局部仍可能漏检」，<0.5% 说「漏检风险较高，请调大采样面数」）；`quality_score` 按覆盖率给 20/16/12 分，并把 <0.5% 记入新增的 `caveats`（只提醒、不封顶等级）。
- **验证**：`test_r42_coarse_thickness_score_scales_with_coverage`、`test_r42_coarse_message_states_actual_coverage`；闸门 `thickness_coverage_scoring`。

### 第 43 轮 · MESH-023（S2）整体反法线补完还是反的

- **现象**：整体朝内的立方体挖掉一个面后补洞，结果有向体积 **−8000**、水密、绕序一致、无开放边——所有验收项都过，`filled=True`。切片器会把实体当空腔。
- **改动**：补洞验收后若 `boundary==0` 且 `signed_volume < 0`，直接 `invert()`（闭合体朝向没有二义性），并回报 `orientation_fixed`。
- **验证**：`test_r43_fill_fixes_inside_out_body`（−8000 → +8000）、`test_r43_normal_body_is_not_inverted`；闸门 `fill_fixes_orientation`。

### 第 44 轮 · PROC-001（S2）打孔薄壁过滤是死代码

- **现象**：`drill_holes(..., min_wall=0.0 if p["hollow"] else None)` 写在 `if p["hollow"]` 分支里，所以恒为 0.0 —— 薄壁过滤从来没生效过。而 `_filter_thin_spots` 对镂空件量的正是「外表面到第一个内表面」，也就是真实壳厚，本来就该用。
- **改动**：阈值改为「实测壳厚的 30%，不低于 0.25mm」；全部候选被拒时**不再抛错**，改为在最薄处开孔并回报 `thin_fallback`，处理报告里给出警告。
- **验证**：`test_r44_drill_thin_wall_filter_is_active`（`min_wall_checked=0.48`）、`test_r44_all_rejected_positions_fall_back_instead_of_failing`；闸门 `drill_thin_filter_active`。

### 第 45 轮 · PROC-002（S2）支撑比设定密一倍

- **现象**：支撑点去重半径是 `max(0.5, diameter×1.5)`，与 `spacing` 无关。用户设 3mm 间距，实际柱间距只有 1.5mm——支撑数量、拆支撑工作量、表面疤痕全部翻倍。
- **改动**：去重半径改为 `max(diameter×1.5, spacing×0.8)`。
- **验证**：`test_r45_support_density_follows_spacing`（间距 2 → 21 根，间距 8 → 3 根）；闸门 `support_spacing_respected`。

### 第 46 轮 · PROC-003（S3）缩放后的支撑直径与面数口径

- **现象**：① 支撑直径是工艺参数（mm），整体缩放会把它一起放大，但报告仍写用户填的 1.0mm（实际 2.0mm）；② `after = mesh_stats(mesh)` 统计的是不含支撑的网格，而导出文件里包含支撑——界面面数与 STL 对不上。
- **改动**：缩放后写 `diameter_effective` 与说明；`after` 改为统计导出用的 `final`，并附 `mesh_only_faces`。
- **验证**：`test_r46_support_diameter_reported_after_scaling`、`test_r46_run_job_stats_include_supports`（回读导出文件面数必须等于报告）。

### 第 47 轮 · PROC-004（S2）减面破坏水密却继续用

- **现象**：`decimate_ratio=0.02` 时 `fast_simplification` 直接把 26 面减到 **0 面**，随后镂空报「模型不水密」——用户完全想不到是减面干的。
- **改动**：减面后当场验收水密性（减面前水密、减面后不水密 → 回退原网格并说明），并记录体积变化率与保留面数。
- **验证**：`test_r47_decimate_breaking_watertight_is_reverted`、`test_r47_mild_decimate_is_kept`（0.6 倍正常保留）；闸门 `decimate_reverted`。

### 第 48 轮 · PROC-005（S3）注释 10mm、代码 3mm

- **现象**：判断「悬垂点下方有实体就跳过」的射线探测写死 `max_dist=3.0`，注释却写 10mm；异常兜底分支用的又是 10。三个数字互不一致，报告里也看不到判据。
- **改动**：参数化 `min_support_h=3.0`，探测距离与兜底共用它，并把 `min_support_h_mm`、`spacing` 写进报告。
- **验证**：`test_r48_support_threshold_is_reported_and_consistent`。

### 第 49 轮 · SPLIT-001（S2）桩位提示落在空腔里

- **现象**：`section_centroid` 用切面顶点均值。U 形截面（30×30 去掉中间 10×20 槽）算出的点是 (15, 17.5)，`mesh.contains` 判 **False**——按这个提示打桩，桩悬在空气里。环形、双腔件同理。另外 `run_job` 直接在切面上取截面，截面与封盖重合，圆柱件全部返回 `None`。
- **改动**：改为「最大轮廓面积质心 → 代表点 → 向内收缩一圈」三级，每级都用 `mesh.contains` 验证；兜底退到「离均值最近的、确实在实体内的顶点」。调用方把探测面往内让 0.5mm（最多 2% 高度），XY 取内截面、Z 回到切面。
- **验证**：`test_r49_peg_hint_lands_inside_material`（U 形 / 环形）、`test_r49_peg_hint_convex_case_is_centroid`；闸门 `peg_hint_inside`。

### 第 50 轮 · SPLIT-002（S2）分件不验收

- **现象**：每块都记了 `watertight` 字段，但没人看：封盖失败（细碎轮廓上 shapely 三角化退化并不罕见）的块照样打进 zip，用户打开才发现是破的。
- **改动**：切完立即逐块验收，`not_watertight` 列表 + 日志 + `note` 三处都报；一块都没有时直接报错。
- **验证**：`test_r50_split_reports_not_watertight_pieces`（120mm 柱切 3 段、全部水密、桩位给出）。

### 第 51 轮 · PACK-001（S2）平台参数不校验 + 没有旋转提示

- **现象**：`pw = float(params.get("platform_w") or 192)`。传 NaN 时所有比较都是 False，件被排到 NaN 坐标并导出成非法 STL；传 0 则全部判「超出平台」。另外 100×150 的件在 192×120 平台上明明转 90° 就能放，只回一句「超出平台」。
- **改动**：`run_job` 用区间校验（1~2000mm / gap 0~100）并给出可读错误；`pack` 对放不下的件计算 `rotate_fits` 并在原因与 `note` 里提示；读取失败的项目单独列 `load_skipped`（以前静默跳过）。
- **验证**：`test_r51_pack_hints_rotation_when_it_would_fit`、`test_r51_pack_rejects_bad_platform_params`；闸门 `pack_rotation_hint`。

### 第 52 轮 · CMP-001（S2）版本对比的位移口径错了

- **现象**：用「新顶点到最近旧顶点」的距离。同一张 10×10 平面，只把网格从 4×4 加密到 8×8（表面完全相同），旧口径报 **56/81 个顶点移动、最大 1.77mm**。拓扑一变（减面/重网格/合并顶点）结论就失真。
- **改动**：改用 `trimesh.proximity.closest_point` 的**点到表面**距离，失败时退回顶点-顶点并标注口径（`distance_metric`）。
- **验证**：`test_r52_compare_uses_point_to_surface`（moved_ratio 0、最大位移 0、`identical=True`）；闸门 `compare_surface_metric`。

### 第 53 轮 · CMP-002（S3）缩放与变形不分

- **现象**：整体放大 1.5 倍会让 100% 顶点「位移」，报告只写「拓扑未变，位移可以定位改动区域」，用户判断不出是尺寸变了还是形状变了。
- **改动**：新增 `size` 段（逐轴比例、`uniform_scale`、是否等比），等比且变化 >1% 时在 `note` 里说明「位移主要来自尺寸变化」。
- **验证**：`test_r53_compare_separates_scale_from_deformation`。

### 第 54 轮 · GPU-001（S3）为报一句话强制探测后端

- **现象**：`act_fill_holes` 的返回里塞了 `gpu.info()`，这会强制 `backend()` 探测——首次调用要 `import cupy`，实测 **+330MB 常驻、+1s**。而小模型的补洞根本达不到 GPU 启用门槛（15000 顶点），这笔开销纯属为了一句「用了什么」。基准里它还被记在「第一次调用 GPU 的阶段」头上，看起来像那个阶段内存回归了。
- **改动**：补洞改报 `gpu.last_backend()`（这次实际用了什么，不探测）；`_probe_cupy/_probe_torch` 先查驱动库（`nvcuda.dll` / `libcuda.so`），没驱动的机器连 import 都不做。
- **验证**：小模型补洞 `cupy` 不再进入 `sys.modules`，耗时 0.59s → 0.02s、内存 +330MB → +2MB；`test_r54_repair_does_not_force_gpu_probe`；闸门未单列（用 `test_r54_cuda_driver_check_exists` 兜住接口）。
- **说明**：本机有 RTX 4090 且 cupy 可用，所以「有驱动」路径下 GPU 仍会被真正启用；省下的是**不需要 GPU 的小任务**和**没有驱动的机器**。

### 第 55 轮 · GPU-002（S2）孤立顶点被拽向原点

- **现象**：`_smooth_numpy` 把顶点度数夹到 1，孤立顶点（没有边）的邻居平均是零向量，每轮变成 `v×(1−λ)`。实测 (999,999,999) 迭代 10 次后变成 **(0.98, 0.98, 0.98)**——游离顶点被拽到原点。
- **改动**：把「度数为 0」并入固定点掩码；cupy / torch 两条后端同步改，否则同一输入两条后端结果不同。
- **验证**：`test_r55_isolated_vertex_is_not_dragged_to_origin`；闸门 `isolated_vertex_kept`。

### 第 56 轮 · API-006（S2）处理链入参没有校验

- **现象**：只有修复/检测接口做了区间校验。`supports: "automatic"` 会被静默当成默认值；`wall_thickness: 99`、`overhang_deg: 5` 直接进队列；`platform_w: "nan"` 能把拼盘导成非法 STL；`rotate.axis: "w"` 报 500。
- **改动**：`_PARAM_RANGES` 扩到处理/分件/拼盘/挖孔/旋转共 12 个键，新增 `_checked_enum`（大小写不敏感、拼错 422、支持按接口覆盖可选值——`rotate` 不接受 `auto`）。
- **验证**：`test_r56_api_validates_process_split_pack_rotate`（8 种非法值全 422，合法值 200）。

### 第 57 轮 · UI-003（S2）检测没跑成也显示「通过」

- **现象**：后端早已产出 `summary.degraded` 与 `value.error`，界面一个都没用。七项里有一项崩了，报告页仍是一片绿色。
- **改动**：报告顶部加「有 N 项没能给出可信结论」横幅，逐项加「未确认」标签（橙色，与「通过」区分）；`ITEM_NAME` 把 id 映射成中文。
- **验证**：`test_r57_ui_marks_unconfirmed_checks`。

### 第 58 轮 · UI-004（S2）处理警告只写日志

- **现象**：`hollow_warning`（壳厚被自动降）、`drill.thin_fallback`（薄处开孔）、`decimate.reverted`（减面回退）、`supports.diameter_effective`（支撑被放大）——全部只在任务日志里。用户看到「处理完成」就以为一切按设定执行了。
- **改动**：`afterProcess` 汇总成 warn 级 toast，并保存 `S.lastProcess` 在「处理」页顶部渲染结果卡（含「没有回退或需要额外确认的项」的正向结论）。
- **验证**：`test_r58_ui_surfaces_process_warnings`。

### 第 59 轮 · BATCH-001（S2）批量处理的三处不一致

- **现象**：① 批量检测直接 `[fn(mesh,…) for fn in CHECKS]`，绕过第 29 轮的异常隔离——一项崩了整个批处理项就失败；② 存进项目的报告没有 `quality`，批完打开详情质量评分是空的；③ 修复动作无论是否回退都记进 `repaired`，报告写着「补洞已完成」而模型原样返回。
- **改动**：改用 `analysis._safe_check`；报告补 `quality` 与 `failed_checks`；新增 `reverted` 列表（含原因），回退的动作不再进 `repaired`。
- **验证**：`test_r59_batch_uses_safe_checks_and_marks_reverts`。

### 第 60 轮 · TEST-004（S3）闸门与回归

- **改动**：`bench/round_gate.py` 18 → **28 项**（新增：未成环边上报、壁厚覆盖率打分、补洞纠朝向、打孔过滤生效、支撑间距、减面回退、桩位在实体内、点到面口径、孤立顶点、拼盘旋转提示）；新增 `tests/test_rounds_41_61.py` **28 项**。
- **验证**：闸门 28/28、约 1.3s；全量 `pytest` **136 passed**（47 + 34 + 27 + 28）。

### 第 61 轮 · DOC-002（S3）文档同步

- **改动**：本文件补第 41–61 轮；README 测试章节更新为 136 项与四个文件的用例分布；第六节整体提升表更新到 R61。
- **验证**：`pytest tests -q` 136、逐文件 47/34/27/28 与文档一致。

### 第 62 轮 · PERF-004（S3）整轮性能与资源优化

- **现象**：OPTIMIZATION.md 的优先级清单里还压着几处「不报错但一直慢」的点：支撑密度用逐点 `query_ball_point` 建 Python 列表再取长度；镂空先跑一次体素化算壳，又单独跑一次 `cavity_volume()` 再体素化一遍；多个尺寸的预览会重复解析同一个 STL；装配干涉逐对做几何求交，没有 AABB 预筛；布尔求交失败时被当成「无干涉」；前端每次打开项目都把 Three.js 打到 CDN，且渲染循环常驻、切换标记/网格时材质和几何不释放。
- **改动**：
  - `app/main.py`：支撑密度改用 `cKDTree.query_ball_point(..., return_length=True)` 一次取计数；装配改为 AABB 预筛 + 逐对精判，并回报 `pairs_geometry_checked` / `pairs_skipped_by_aabb`；布尔失败置 `degraded` 而不是「无干涉」；`project_issues()` 用 `np.bincount` 一次聚合面积。
  - `app/voxelops.py` / `app/process.py`：抽出 `_inside_distance()`，镂空在一次 EDT 里同时产出壳体和 `cavity_mm3` / `pitch_mm`，删掉独立的 `cavity_volume()` 调用。
  - `app/meshio.py`：两级缓存（原始网格 + 按预览尺寸的派生结果），40k/50k/60k/80k/120k 预览共用一次 STL 解析；`clear_cache()` 与内存预算同时清两级。
  - `app/jobs.py`：`Job` 构造时固化 `_job_file`，避免后台线程/用例之间串写任务文件。
  - `static/viewer.js`：本地 `three.module.min.js` 优先、CDN 兜底；改为按需渲染（`_invalidate` + 单帧 `requestAnimationFrame`）；`clearMarkers` / `clearSegments` / `clearNormals` / `clearSection` / `clearGrid` / `clearBox` / `toggleAxes` 全部释放 geometry/material；`markPoints(..., append)` 支持多颜色问题标记。
  - `static/app.js`：三角面积直方图改用 `Float64Array.sort()` + 单遍计数；轮询在页面隐藏时暂停且不重叠；队列 5s、资源 2s；任务轮询改递归 `setTimeout`；装配/检测的 `degraded` 状态在界面明示；三类问题标记都显示。
- **收益**：`support_density` ball_20k 125 → **88ms**、ball_82k 415 → **276ms**；`hollow` 少一次完整体素化（一次 `cavity_volume` 实测约 0.8s）；预览缓存让多尺寸预览只解析一次；空闲时浏览器几乎不再产生 `requestAnimationFrame`（实测 1 秒 1 次，之前约 60 次）。
- **回归风险**：镂空统计量改为同一次体素化产出，浮点累加顺序变化可能带来 ulp 级差异；已用 `test_optimizations.py` 覆盖（`cavity_mm3` 与独立计算一致、预览指纹不变）。
- **验证**：`pytest tests -q` **143 passed**（新增 7 项）；`bench/round_gate.py` 28/28；`bench/run.py --quick` 44 阶段 **0 超阈值、0 指纹差异**；浏览器实测本地 three.js r160 加载、检测 7 项出结果、154 处问题标记、控制台无报错。

---

## 二之四、第 63–164 轮逐轮记录（2026-10-29 端到端优化）

> 目标：只优化用户真实走过的路径——导入 → 检测 → 修复 → 处理 → 建议 → 导出，外加项目、批量、队列、历史和界面可靠性。
> 本轮共 **102 轮**；每轮至少有一个可复现的行为变化。统一验证基线：
> `pytest tests -q` **420 passed**、`bench/round_gate.py` **39/39**、`node --check static/app.js` 通过。
> 新增回归用例集中在 `tests/test_rounds_62_162.py`（31 项）。

### 63–72 · 导入与文件校验

| 轮次 | 改动 | 可验证结果 |
|---|---|---|
| 63 | 上传按扩展名白名单逐文件校验 | 传 `.exe`/无扩展名只进 `errors[]`，不会创建项目 |
| 64 | 单文件 512MB 上限 + 空文件拒绝 | 超限/0 字节文件当场报错，已成功的其它文件保留 |
| 65 | 先写 `.part` 再原子替换 | 中途断开不留下半个模型；失败时 `.part` 与目标一起清理 |
| 66 | 上传时计算 SHA-256，`dedupe=1` 才查重 | 默认导入行为不变；开启后重复文件跳回原项目 |
| 67 | 前端上传固定带 `dedupe=1` | 同一文件重复拖入不再生成第二个项目 |
| 68 | 每个文件的错误独立返回 | 第 3 个文件损坏不会让前 2 个白传，错误逐条 toast |
| 69 | 重复文件返回原项目信息 | 提示「已存在，已跳到原项目」并直接打开 |
| 70 | 文件名去路径、去控制字符、非法字符替换 | `../../x.stl`、带换行的文件名都落成安全名称 |
| 71 | 示例模型按「示例模型」名称/标签去重 | 重复点「加载示例」不再堆项目；`force=true` 才强制新建 |
| 72 | 拖拽入口先做扩展名/空文件/大小校验 | 不合格文件在发请求前就被拦下并提示原因 |

### 73–84 · 下载、批量打包与项目包

| 轮次 | 改动 | 可验证结果 |
|---|---|---|
| 73 | 下载主名按最后一个路径段取 | 项目名含 `/` 时不再导出成 `_report.json` 这类怪名 |
| 74 | `_download_headers()` 统一生成下载头 | CR/LF 与裸引号被清除；ASCII 回退名 + RFC 5987 UTF-8 名 |
| 75 | STL/3MF/OBJ/PLY/slicer/params/report/log 全部改走该函数 | 中文项目名在各下载入口都不再变问号 |
| 76 | HTML 报告使用 `inline` + 同一套头 | 浏览器直接预览报告，中文名正常 |
| 77 | 批量 ZIP 成员名用 `_display_stem` 清洗 | 包内不存在 `/`、`..`、反斜杠条目 |
| 78 | 批量 ZIP 同名成员自动加 `_2` 后缀 | 清单里的 `file` 与实际成员一一对应，不覆盖 |
| 79 | 批量 CSV 单元格做公式注入转义 | `= + - @` 开头的内容前置单引号，Excel 不执行 |
| 80 | 项目包导入加 zip 炸弹限制 | 成员 >500、解压总量 >800MB、单文件 >120MB 直接 400 |
| 81 | 项目包拒绝绝对路径、`..`、反斜杠、盘符 | 路径穿越包返回「包内有非法路径」 |
| 82 | 项目包恢复 `history/000.ext` 并重写 `out` | 换机器导入后历史版本仍可回滚（`test_bundle_roundtrip_restores_history`） |
| 83 | 导入项目包后面板提示恢复了多少历史版本 | 没恢复时明确说明「只导入了模型本体」 |
| 84 | 导出页始终挂载 `export-view` | 挖孔点选、装配预览不再因视图 id 不存在而失效 |

### 85–100 · 项目、历史、任务与队列

| 轮次 | 改动 | 可验证结果 |
|---|---|---|
| 85 | 删除项目只清理 `data/` 内且无人引用的文件 | 共享源文件不会被误删，项目记录删除成功 |
| 86 | `find_by_hash()` 按内容哈希找项目 | 上传查重不再依赖文件名 |
| 87 | `_referenced_paths()` 汇总所有项目的引用 | 历史清理/删除前能识别共享文件 |
| 88 | `cleanup_history()` 只删旧中间文件、保留审计记录 | 最近 N 版与当前文件保留，旧记录标记 `out_removed` |
| 89 | `POST /history/cleanup?keep=` 路由 | `keep` 限制 1–50；项目不存在返回 404 |
| 90 | 操作历史面板加「清理旧版本文件」 | 返回删除数量与释放 MB，记录仍可查看 |
| 91 | 已清理版本的「回到这版」按钮禁用 | 不再点出一个必然 404 的回滚 |
| 92 | 服务启动时读取 `jobs.json` | 重启后任务历史不再一片空白 |
| 93 | 重启时把未完成任标记为中断失败 | 不再永远显示「running 47%」，原因写明服务重启 |
| 94 | 任务历史上限 200/300 条 | 长期使用后 `jobs.json` 不会无限膨胀 |
| 95 | `list()` 合并实时任务与历史快照 | 历史任务仍出现在任务列表 |
| 96 | `get_snapshot()` 与任务查询路由回退历史 | 任务结束后继续轮询/刷新仍能拿到结果 |
| 97 | 界面从任务历史恢复检查器状态 | 关闭再打开页面仍能看到上次任务输出 |
| 98 | 中断任务显示「已中断」，任务标题可打开项目 | 中断原因与项目入口都可达 |
| 99 | 队列备注去控制字符、队列上限 200 | 备注不再能塞换行；超限时明确提示先移出 |
| 100 | 队列写入原子化 + `prune` + 重排去重 | 重复 id 不会复制条目，已删项目可一键清理 |

### 101–113 · 检测、报告与修复计划

| 轮次 | 改动 | 可验证结果 |
|---|---|---|
| 101 | `quality_score()` 区分「没有 stats」与「0 面」 | 健康网格不再因为缺 stats 被判 D 级 |
| 102 | 每次检测追加轻量 `analysis_history` | 只存分数/等级/计数/面数，限 12 条 |
| 103 | 同一文件同分数重复检测只更新时间戳 | 连点检测不会把趋势图刷成一排重复点 |
| 104 | 报告页显示分数趋势 sparkline | 改前/改后能看出质量是否真的提升 |
| 105 | 快速/完整扫描模式 | 快速跳过自相交并粗筛壁厚；完整扫描给出可靠结论 |
| 106 | 后端真正尊重 `check_self_intersection=false` | 关掉后不再偷偷跑完整自交检测 |
| 107 | 报告严重度筛选（全部/问题/错误） | 长报告可只看需要处理的项目 |
| 108 | 一键复制纯文本报告 | 无剪贴板权限时自动回退到文本框 |
| 109 | 报告显示各项检测耗时合计 | 能判断慢在壁厚、自交还是补洞 |
| 110 | `GET /repair-plan` 从报告生成修复计划 | 自交/碎屑/法线/壁厚不再需要用户猜勾选项 |
| 111 | 碎屑统计兼容 `components` / `tiny` 字段 | 旧报告也能正确推荐「去碎屑」 |
| 112 | 单位有歧义时绝不自动 `rescale` | 只给警告；避免把模型误放大 1000 倍 |
| 113 | 计划返回逐动作原因、警告、`resolved`、`scale_factor` | 前端能解释「为什么修」并判断是否已无需修复 |

### 114–126 · 处理与预检

| 轮次 | 改动 | 可验证结果 |
|---|---|---|
| 114 | 目标高度与目标最大尺寸冲突时以高度为准 | 两个都填时不再互相覆盖，结果可预测 |
| 115 | `hollow`/`orient` 严格布尔校验 | `"false"` 不会被当成 True，非法值 422 |
| 116 | 打孔薄壁过滤使用「实际生效的镂空壁厚」 | 改壳厚后过滤阈值同步变化 |
| 117 | 镂空但排液孔为 0 时给出警告 | 不会让树脂/粉末封在空腔里还一声不吭 |
| 118 | 支撑采样先过采样再裁剪 | 小模型也能得到足够采样点，支撑密度更稳定 |
| 119 | 薄片打孔硬门槛 | 壁厚不足时拒绝打孔，而不是打穿成破面 |
| 120 | 修复 `preview_pipeline` 参数被默认值覆盖 | 目标高度 40mm 的预检现在真按 40mm 算（新增用例） |
| 121 | 预检按摆正后尺寸检查成型空间 | 自动摆正后放不下的件会提前报出超限轴 |
| 122 | 预检估算镂空壳体/省料比例 | 开镂空前先看到大概省多少料 |
| 123 | `POST /process-preview` 路由 | 不建任务、不写文件即可预检 |
| 124 | 预检校验 `build` 必须是 3 个正数 | 少一个数、0、字符串、布尔都 422 |
| 125 | 处理页自动预检 + 参数变更标记过期 | 改参数后旧结果标灰，不冒充当前结果 |
| 126 | 「按 ×N 缩放到可打印」按钮 | 超限时可把 `scale_to_fit` 一键带回参数 |

### 127–140 · 参数建议与可打印性

| 轮次 | 改动 | 可验证结果 |
|---|---|---|
| 127 | `calibrate` 支持 `"false"/"0"/"no"` | 用户明确关闭历史校正时真的不校正 |
| 128 | 报告过期时丢弃旧 stats，按当前文件重测 | 缩放后耗材估算不再沿用上一版几何 |
| 129 | 过期报告补测最薄壁厚 | 层高/细节判断不会因缺一项而整条失效 |
| 130 | 过期报告置信度扣 0.10 并列出原因 | 用户知道这份建议为什么不够可信 |
| 131 | 过期报告在 warnings 中明确提示重检 | 建议页不再对旧的水密/自交结论背书 |
| 132 | 本机样本 1–2 条标为 `sparse` | 不再写成「已按实测偏差校正」 |
| 133 | 校正因子为 1 时说明「未做历史修正」 | 不会出现「修正 ×1.0」这种假动作 |
| 134 | 机型 key 未命中时给出 fallback 警告 | 明确说明用的是默认机型，而非用户那台 |
| 135 | 材料 key 与工艺不匹配时给出 fallback 警告 | 不再静默换成另一种材料 |
| 136 | 返回 `fallbacks` 结构 | 界面可把被替换的输入标黄 |
| 137 | 支撑未判定时输出「未评估」而非「不需要」 | 没测出来不会被当成自支撑良好 |
| 138 | 支撑未判定时置信度扣 0.12 | 建议可信度反映真实信息缺口 |
| 139 | 新增 `printability` 总览 | 阻断项/通过项/提醒数一屏可见 |
| 140 | 建议页渲染可打印性卡片与 fallback 横幅 | 用户先看结论，再看参数推导 |

### 141–146 · 打印记录与 CSV

| 轮次 | 改动 | 可验证结果 |
|---|---|---|
| 141 | 打印结果归一化（`ok/pass/成功/failed/失败`） | 不同前端/脚本传值都能正确统计 |
| 142 | 打印记录校验时长/材料/打印机 | 负数、非数字、超范围时长返回 422 |
| 143 | 打印记录返回 `total_minutes` | 多次打印累计耗时可直接展示 |
| 144 | 打印面板增加耗时/材料输入 | 记录一次成功不再只有一句备注 |
| 145 | 打印记录列表转义备注/材料/机型 | 备注里的 HTML 不会被执行 |
| 146 | 批量报告 CSV 公式注入防护 | `=HYPERLINK(...)` 等单元格按文本打开 |

### 147–164 · 列表、深链、缓存、无障碍与查看器

| 轮次 | 改动 | 可验证结果 |
|---|---|---|
| 147 | 项目搜索重渲染后恢复焦点与光标 | 边输入边筛选不再每敲一个字就丢焦点 |
| 148 | 项目列表分页（每页 20，可调） | 上百个项目不再一次性渲染 |
| 149 | 按更新时间/创建时间/名称/大小/问题数排序 | 找最近改过的、最大的、问题最多的都能排 |
| 150 | 项目行增加删除/复制按钮 | 常用操作不必再进详情页 |
| 151 | 项目名/标签/下拉选项/错误卡统一转义 | 文件名里的 `<img onerror>` 不会执行 |
| 152 | 批量选择与任务输出文本转义 | 批量结果、输出文件名不会注入 HTML |
| 153 | 深链 `#p=<id>&s=<step>` | 刷新/书签/分享后回到同一项目同一步骤 |
| 154 | 同项目刷新保留当前步骤，换项目才回检测页 | 处理/导出过程中刷新不再被打回第一步 |
| 155 | 数字键 1–6 直达六个步骤 | 键盘用户不必逐次点顶部步骤条 |
| 156 | Toast 增加 `role/aria-live`，最多保留 6 条 | 屏幕阅读器会播报；批量报错不刷屏 |
| 157 | 引导/帮助弹层加 `role=dialog`、焦点陷阱与焦点恢复 | Tab 不会跑出弹层，关闭后回到原按钮 |
| 158 | `prefers-reduced-motion` 下关闭动画 | 对动态效果敏感的用户不被动画干扰 |
| 159 | 顶栏 900/760/560px 响应式 + 步骤条横向滚动 | 窄窗口不再把按钮挤出屏幕 |
| 160 | 查看器双指缩放/平移 | 触控板/触屏可以捏合缩放并双指移动 |
| 161 | 触摸点选加 8px 位移阈值 | 想旋转时不会误挖孔/误选点 |
| 162 | 双击复位视角，载入时保存 `_homeR` | 转丢视角后双击即可回到刚载入的角度 |
| 163 | 静态资源按 mtime+大小版本化，HTML/JS 回源校验，API 不缓存 | 服务升级后刷新立即加载新 app.js；深链、修复方案等新按钮不会再因旧缓存失效 |
| 164 | 数字键 1–6 改调存在的 `goto()` | 按 2 直接到「模型检测」；控制台不再抛 `ReferenceError: goStep is not defined` |

### 本轮关键修复（值得单独说明）

1. **预检参数被默认值覆盖（R120）**：`preview_pipeline(mesh, p)` 先把参数命名为 `p`，下一行又 `p = dict(DEFAULTS)`，随后从 `p` 里读用户参数——等于永远只预检默认值。用户把目标高度改成 40mm，预检仍显示 20mm 并提示可打印。改成独立 `params` 入参后，新增 `test_process_preview_conflicting_targets_prefers_height` 锁定行为。
2. **单位歧义不再自动换算（R112）**：`m?` 这类两种解释都合理的单位只给警告，`scale_factor=None`；只有明确的 `m` 才推荐 `rescale`。避免一次误判把 20mm 模型放大到 20m。
3. **支撑「没测出来」不再等于「不需要」（R137/R138）**：射线/截面判定失败时输出「未评估」，同时扣置信度并给 warning。旧行为会把采样失败当成自支撑良好。
4. **过期报告全链路处理（R128–R131）**：几何改动后 `analysis_stale=True`；建议层丢弃旧 stats、补测壁厚、扣置信度、界面显示「报告已过期」并可一键重检。
5. **历史中间文件可回收（R88–R91）**：只删 `data/` 内、未被其它项目引用、且被新版本取代的 STL；审计记录保留并禁用对应回滚按钮。实测 4 个旧版本 `keep=2` 删除 2 个，当前文件与最近两版保留。
6. **队列持久化与清理（R99/R100）**：写入改临时文件 + `os.replace`；重排对重复 id 去重、未知 id 返回 `ignored`；`/api/queue/prune` 移除已删项目。
7. **升级后仍跑旧前端（R163）**：`/` 和 `/static/*` 之前没有 `Cache-Control`，浏览器按启发式缓存继续用旧 `app.js`，深链、修复方案等新按钮看起来「没生效」。现在资源 URL 带 mtime+大小版本号，HTML/JS 回源校验，API `no-store`；本地重启后刷新即加载新脚本。
8. **数字键调用不存在的函数（R164）**：R155 的快捷键写成了 `goStep()`，实际函数叫 `goto()`，按 1–6 直接抛 `ReferenceError`。改为 `goto()` 后实测按 2 进入「模型检测」，控制台无报错。

### 本轮量化结果

| 指标 | R62 | R164 | 变化 |
|---|---|---|---|
| 单元测试 | 143 | **420** | +277（含合并进来的既有用例与新增 31 项） |
| 几何闸门 | 28/28 | **39/39** | +11（截断 STL、过期重算、回退归因等） |
| 下载头注入 | 原始字符串拼接 | CRLF/引号清理 + RFC 5987 | 消除响应拆分/文件名截断 |
| 批量 ZIP 路径 | 项目名直接拼成员名 | 清洗 + 同名去重 | 消除 zip slip/覆盖 |
| 预检参数 | 永远默认值 | 按用户参数 | 修复误导性预检 |
| 支撑未判定 | 当成「不需要」 | 「未评估」+ 扣分 | 消除虚假结论 |
| 历史产出 | 只增不减 | 按项目保留最近 N 版 | 可控磁盘占用 |
| 任务重启 | 历史空白/永远 running | 恢复 + 标中断 | 可解释、可继续 |

---


## 二之五、第 62–81 轮逐轮记录（2026-10-29 第三批：状态一致性与结论可信度）

> 编号说明：本批与「二之四」是**并行批次**，各自独立编号，互不覆盖。
> 前两批集中在「几何算得对不对」，这批集中在**状态与结论**：
> 界面/建议用的是不是当前那份文件、没测出来的项有没有被当成「没问题」、
> 写回存储时会不会把别人的改动抹掉。这类缺陷共同点是**不报错、结果自洽、但结论是错的**。
>
> 统一验证基线：`pytest tests -q` **418 passed**（新增 35 项）；
> `bench/round_gate.py` **39/39**（28 → 39）；`tests/e2e.py` 47/47；`node --check static/app.js` 通过。

### 62 · STATE-001（S1）几何改了，旧报告还当真的用

- **目标**：模型被缩放/旋转/打孔/处理之后，检测报告与参数建议不能继续用上一版文件的数字。
- **现象**：`store.add_history` 只把 `current` 指向新文件，不动 `analysis`。于是报告里存着旧的
  `extents/volume_mm3`，建议层 `_geometry` 优先读报告 stats。实测 20mm 立方体缩放到 60mm 后，
  建议仍按 20mm 给出 **9.9g / 0.56h**，真实值约 **78.6g / 4.5h**——差 8 倍，界面无任何提示。
- **改动**：`add_history` 打 `analysis_stale=True` 并清空 `advice`；`update(analysis=...)` 写入新报告时
  自动清标记；`advisor._geometry` 见到标记就**丢弃旧报告、从当前文件重测**（stats、壁厚粗筛、连通分量），
  并在 warnings 与置信度里各记一笔（−0.10）。前端已有「这份检测报告已过期」横幅，现在它终于有真实触发源。
- **自检**：`test_r62_geometry_change_marks_report_stale` / `_advisor_ignores_stale_stats`
  （体积 6000 → 162000）/ `_fresh_analysis_clears_stale`；闸门 `stale_analysis_recomputed`。

### 63 · FILE-001（S1）被截断的 STL 变成「空模型」

- **目标**：文件只拷了一半时，报「文件被截断」，而不是报「模型没有任何三角面」。
- **现象**：`is_binary_stl` 要求 `84 + n×50 == 文件长度` 严格相等。截断时等式不成立 → 掉进 ASCII 分支
  → 正则一个顶点都匹配不到 → **安静返回 0 个三角面**。12 面立方体截掉一半，上传、检测、建议全流程不报错。
  尾部多 20 字节的正常文件同样被误判成 ASCII，加载结果也是 0 面。
- **改动**：新增 `meshio.probe_stl()`（只看文件头与长度：declared/expected/truncated/missing_bytes/trailing_bytes）
  与 `human_size()`；`is_binary_stl` 改为委托 probe，允许尾部多余字节；`load_vertices_faces` 先 `_assert_stl_complete`；
  上传接口在落盘后立即校验并**连目标文件一起删掉**（否则留下进不了库的孤儿文件）。
  ASCII 缺 `endsolid` 只标 `truncated_suspect`，不拒收——不少导出器不写。
- **自检**：`test_r63_truncated_binary_stl_is_rejected`（报「缺 342 字节，仅够 5 个完整三角面」）、
  `_trailing_bytes_still_parse`（0 → 12 面）、`_ascii_without_endsolid_is_only_suspicious`、
  `_upload_rejects_truncated`；闸门 `truncated_stl_rejected`、`trailing_bytes_still_binary`。

### 64 · TRUST-001（S2）截面切不出来，结论却是「自支撑性良好」

- **目标**：没判定出来的支撑，不能输出成「不需要支撑」。
- **现象**：`support_analysis` 在三种失败路径上都给 `ok=True`：截面为空、采样点为 0、`total_area<=0`。
  上层 `sup.get("ok")` 为真 → 走「悬垂 0.0%，模型自支撑性良好」分支。零厚度薄片面片实测得到
  `ok=True, samples=0`，即「一个采样点都没测，却给出了通过结论」。
- **改动**：失败路径全部 `ok=False` 并带 `reason`（「截面切片失败」「悬垂面采样失败」「模型没有三角面」）；
  `advisor` 新增第四条分支输出「**未评估**」+ warning「不要据此认为模型不需要支撑」；
  置信度 −0.12；`printability.checks` 里支撑项改为 `ok=None`（界面画「?」而不是「✕」——
  「没测」和「测了不行」是两件事）。
- **自检**：`test_r64_support_unassessed_when_sections_missing` / `_advisor_says_unassessed_not_safe`
  （置信度 0.5 → 0.38）/ `_printability_marks_support_unknown`；闸门 `support_unassessed_is_not_safe`。

### 65 · TRUST-002（S2）机型 key 不认识，静默换成另一台机器

- **目标**：用户选的机型不存在时，必须说明「参数不是给你那台算的」。
- **现象**：`printers.resolve` 对未知 key 退回该工艺默认机型（Ender-3 V3 KE），`resolved_from="fallback"`，
  但 `recommend` 只用这个字段判断工艺，**从不提示**。成型空间 256³ 与 220×220×240 的差异直接体现在
  「放不放得进去」的结论上。
- **改动**：新增 `printer_fallback`，命中时写 warning（含实际使用的机型与成型空间）+ rationale；
  返回值新增 `fallbacks.printer`；前端新增 `fallbackBanner()` 把三项回退统一显示在建议页顶部。
- **自检**：`test_r65_unknown_printer_is_reported`；闸门 `input_fallback_is_reported`。

### 66 · TRUST-003（S2）工艺与材料不匹配，静默换成 PLA

- **目标**：材料 key 不属于当前工艺时，明确说换了什么、有哪些可选。
- **现象**：`FDM_MATERIALS.get("standard", PLA)` —— 从光固化切到 FDM 后材料 key 还停在 `standard`，
  于是「材料」一栏显示 PLA，数字自洽，用户以为在用自己选的那种料；标定还按错误的 key 去查历史。
- **改动**：新增 `material_fallback`，回退时写 warning（列出该工艺可选材料）+ rationale + `fallbacks.material`；
  同时把「方案名不认识」也纳入 `fallbacks.plan`。
- **自检**：`test_r66_process_material_mismatch_is_reported`、`_matching_inputs_have_no_fallback`
  （正常输入三项全为 None，不能误报）。

### 67 · INPUT-001（S3）`calibrate: "false"` 被当成真

- **目标**：字符串/数字形式的布尔，语义与字面一致。
- **现象**：`use_cal = ov.get("calibrate", True)`。前端传布尔没问题，但批量模板、导入的 JSON、
  脚本调用传的是字符串：`bool("false")` 是 `True`、`bool("0")` 也是 `True`。用户明确关掉了
  「按本机历史校正」，算出来仍是校正后的数。
- **改动**：新增 `_truthy()`：布尔直通、数字按 0/NaN 为假、字符串认 `1/true/yes/on` 与 `0/false/no/off/""`，
  其余回落默认值。
- **自检**：`test_r67_calibrate_truthiness`（8 组参数化）、`_calibrate_false_disables_correction`；
  闸门 `calibrate_string_false_honored`。

### 68 · TRUST-004（S3）标定来源标成 `global`，但根本没做池化

- **目标**：标签必须描述算法真实做的事。
- **现象**：`src = "group" if n_g>=3 else ("global" if n_g else "prior")`。`gvals` 只来自本组样本，
  没有任何跨机型池化。1~2 条记录时标 `global`，上层据此在「数据来源」里写「本机历史修正……按实测偏差校正」，
  实际只是 1 条记录被 1/(1+8) 权重缩了一下。`cost.basis` 还会写「各 0/0 条记录」。
- **改动**：新增 `sparse` 档；`_sources` 对 `sparse` 明确写「样本不足，只做极小幅度校正（×factor，权重 n/9）」；
  `cost.basis` 只在真的校正了（factor≠1）时才提历史修正，否则写「本机暂无可用记录」。
- **自检**：`test_r68_calibration_source_is_honest`（且断言不再是 `global`）；闸门 `calibration_source_label_honest`。

### 69 · REPORT-001（S2）报告里「没测成」看不出来

- **目标**：导出的 HTML 报告必须把未确认项单独列出来。
- **现象**：检测项失败只写成一行 info，而整页最醒目的是「严重项 0」和大号 A/B 级评分。
  实测一份 `failed_checks=["self_intersection"]` + 壁厚只采样 0.3% 的报告，页面上完全看不出异常。
- **改动**：新增「未确认项（不要当成「通过」）」区块，汇总：失败检测项、`value.degraded`、
  `count_is_lower_bound`/`lower_bound`、`quality.caveats`、`quality.degraded`、以及 `analysis_stale`；
  去重后逐条列出。
- **自检**：`test_r69_report_lists_unconfirmed_checks`、`test_r70_report_flags_stale_analysis`；
  闸门 `report_shows_unconfirmed`。

### 70 · REPORT-002（S3）报告只给单点数字

- **目标**：报告里的参数要带区间和置信度——单点估算最容易骗人。
- **现象**：`params` 表只有 value/unit/reason。`range`（P10–P90）与 `mass_range` 后端早就算好了，
  报告里一个都没用；`confidence`（含扣分原因）也完全没出现。
- **改动**：参数行下方增加区间小字（`0.16–0.24mm` 形式）；新增置信度卡片，列出每条扣分原因。
- **自检**：`test_r70_report_shows_ranges_and_confidence`。

### 71 · STORE-001（S2）`save()` 把并发改动写回旧值

- **目标**：写回一条记录时，只覆盖本次真正改过的字段。
- **现象**：`save(proj)` 整条覆盖。两个请求各改不同字段时，后写的把先写的抹掉。实测：
  另一个请求刚打了 `tags=["新标签"] + favorite=True`，紧接着一次「只改名字」的 save 就把两者清空——
  不报错、不告警、事后查不出来。
- **改动**：`get()` 返回 `Record(dict)`，带一份读取时的快照；`save()` 用快照算出 changed 字段集，
  只把这几项合并进磁盘上的最新记录；没有快照（`create` 刚造出来的 dict）时退回整条写入；
  记录新增 `rev` 计数器，每次写入自增。
- **自检**：`test_r71_save_does_not_clobber_concurrent_fields`、`_save_without_snapshot_still_writes`；
  闸门 `save_merges_only_changed_fields`。

### 72 · STORE-002（S2）记录坏了，项目就从界面上消失

- **目标**：损坏的记录要可见、可删，而不是无声无息地不见。
- **现象**：`list_all` 解析失败 `continue`。文件还在磁盘上，但列表里没有它，用户既看不到也删不掉，
  只能去翻 `data/state/projects`；`get` 也返回 None，接口一律答「项目不存在」。
- **改动**：解析失败时产出一条 `damaged=True` 的条目（名字标「（记录损坏）」、带 `damaged_reason`、
  `severity=error`），`delete()` 本来就能删解析失败的文件，于是「列表可见 → 可删」闭环成立。
- **自检**：`test_r72_damaged_record_is_listed`（含 `store.delete` 成功）；闸门 `damaged_record_visible`。

### 73 · STORE-003（S3）列表里的 `issues` 恒等于 7

- **目标**：这个字段要表达「有几个问题」，不是「跑了几项检测」。
- **现象**：`issues` 取 `analysis.summary.total`，那是检测项数量，恒为 7。前端拿它排序（`b.issues - a.issues`），
  等于所有项目并列。
- **改动**：新增 `_issue_count()`：统计 severity 为 `warning`/`error` 的项数；没检测过返回 None。
- **自检**：`test_r73_issue_count_is_not_check_count`（3 项里 2 项有问题 → 2）；闸门 `issue_count_is_not_check_count`。

### 74 · STORAGE-001（S2）中间产物目录只增不减

- **目标**：断点续跑要留的中间文件保留，确定没用的回收掉。
- **现象**：修复任务把每步结果存成 `state/tmp/<job_id>/NN_action.stl` 用于续跑，所以取消/失败时**不能删**。
  但没有任何回收：删项目、删任务都不碰它们。200MB 的模型跑一次修复就留 200MB，反复调参数能堆到几 GB。
- **改动**：新增 `jobs.cleanup_orphan_tmp()`，启动时执行，只删三类：任务记录已不存在、
  任务已 `done`/`cancelled`、目录超过 7 天没动。`failed` 任务保留（还要续跑）。
- **自检**：`test_r74_orphan_tmp_cleanup`（3 个目录 → 删 2 留 1，留下的正是 `failed`）。

### 75 · UI-004（S2）输入被替换了，界面不说

- **目标**：机型/材料/方案被替换时，建议页顶部要有明确提示。
- **现象**：第 65/66 轮后端已经产出 `fallbacks`，但前端没有任何地方读它。
  用户看到的是一份「正常」的建议，只是机型名不太对。
- **改动**：新增 `fallbackBanner(a)`：分别提示机型不在预设库（并写出实际用的是哪台）、
  材料不属于当前工艺、方案名不存在；`printability` 里支撑项改为 `ok=None` 让界面画「?」。
- **自检**：`test_r75_ui_renders_fallback_banner`（断言函数与文案存在于 app.js）。

### 76 · API-006（S3）建议接口的工艺/方案不校验

- **目标**：枚举参数拼错要 422，不能悄悄退回默认值。
- **现象**：`process=fdm2` 会被 `process_hint or printer["process"]` 吞掉，`plan=fast` 会被
  `if ov.get("plan") in PLANS` 吞掉，两次都静默退回默认。用户以为拿到的是自己选的那组参数。
- **改动**：`/api/projects/{pid}/advice` 先校验：`process` ∈ {FDM, SLA}，`plan` ∈ PLANS，否则 422 并给出可选值。
- **自检**：`test_r76_advice_rejects_unknown_process_and_plan`（两个 422 + 一个 200）。

### 77 · READ-001（S3）退化模型的摘要像一份正常估算

- **目标**：打不出来的模型，摘要第一句就要说「打不出来」。
- **现象**：空模型摘要为「预估 0.0g / 0.0h（P10–P90：0.0–0.0h）……2 条风险提示」，
  读起来只是数字小；真正原因埋在后面的 warnings 列表里。超尺寸模型同理。
- **改动**：摘要按 `degenerate` / `not fits` / 正常 三路分支，前两种直接把原因写在第一句，
  并说明「下面的估算不能作为打印依据」。
- **自检**：`test_r77_degenerate_summary_leads_with_reason`（断言含「不可打印」且不含「0.0g」）。

### 78 · TRUST-005（S3）报告过期时连通分量按缺省值 1 算

- **目标**：「1 个连通体」是结论，不是缺省值。
- **现象**：`_geometry` 的 components 只从 `items.components` 取，取不到就 `default=1`。
  报告过期后 items 被清空 → 多部件模型被当成单体，漏掉「切片时会各自生成支撑」的提醒。
- **改动**：取不到且有网格时，直接对当前文件跑 `union_components`；返回值新增 `components_known`
  区分「真的是 1 个」与「没测出来」。
- **自检**：`test_r78_components_recomputed_when_report_stale`（两件分离体 → 2）。

### 79 · TEST-004（S3）本批回归

- **改动**：新增 `tests/test_rounds_62_78.py` **35 项**，每项对应一轮的可证伪目标
  （过期标记与重测、截断/尾部字节/ASCII 可疑、支撑未评估、机型材料回退、calibrate 8 组真值、
  标定标签、报告两个区块、save 合并、损坏记录、issues 口径、tmp 回收、界面提示、接口枚举、
  摘要文案、分量重算）。
- **自检**：`pytest tests/test_rounds_62_78.py -q` → 35 passed。

### 80 · TEST-005（S3）闸门 28 → 39 项

- **改动**：`bench/round_gate.py` 新增 11 项：`truncated_stl_rejected`、`trailing_bytes_still_binary`、
  `stale_analysis_recomputed`、`save_merges_only_changed_fields`、`damaged_record_visible`、
  `issue_count_is_not_check_count`、`support_unassessed_is_not_safe`、`input_fallback_is_reported`、
  `calibrate_string_false_honored`、`calibration_source_label_honest`、`report_shows_unconfirmed`。
- **自检**：39/39，1.96s（原 28 项 1.10s，新增项含一次真实文件缩放与三份报告渲染）。

### 81 · DOC-002（S3）文档与仓库再次对齐

- **现象**：README 的「没有的东西不写在这里」写着仓库不含 `tests/e2e.py` / `tests/ui_smoke.py` /
  `tests/bench.py`——这三个文件现在**存在且能跑**（`tests/e2e.py` 47/47）。R40 的结论过期了。
- **改动**：README 测试章节按实测更新（418 passed / 闸门 39 项 / e2e 47 项 / 新增本批回归文件），
  删掉过期的「不存在」断言，改成「这些是独立脚本，不进 pytest 收集」；本文件补上本批 20 轮记录。
- **自检**：逐项跑过并记录真实输出；数字与命令一一对应。

### 本批整体提升（R61 → R81）

| 维度 | R61 | R81 | 变化 |
|---|---|---|---|
| 单元测试 | 383 passed | **418 passed** | +35 |
| 几何闸门 | 28 项 | **39 项**，1.96s | +11 |
| 几何变更后的建议 | 按旧几何算（9.9g vs 78.6g） | 按当前文件重测 | 8 倍偏差消除 |
| 截断 STL | 静默 0 面 | 明确报「缺 N 字节」并拒收 | 消除静默空模型 |
| 尾部多余字节的 STL | 0 面 | 正常加载 12 面 | 误判消除 |
| 支撑判定失败 | 「自支撑性良好」 | 「未评估」+ 扣分 + 阻断项 | 虚假结论消除 |
| 未知机型/材料 | 静默替换 | warning + 界面横幅 | 可追溯 |
| `calibrate:"false"` | 当真 | 真关掉 | 语义一致 |
| 标定来源标签 | `global`（未池化） | `sparse` + 权重写明 | 名实相符 |
| 导出报告 | 看不出「没测成」 | 未确认项区块 + 区间 + 置信度 | 可复核 |
| 并发写回 | 抹掉别人的改动 | 只合并改过的字段 | 丢数据消除 |
| 损坏记录 | 从列表消失 | 可见可删 | 可恢复 |
| 中间产物 | 只增不减 | 启动时按规则回收 | 磁盘可控 |

**这一批的共同点**：修的全是「不报错、数字自洽、但结论是错的」那类问题。
它们的危害比崩溃大——崩溃会让人去查，错误结论会被直接拿去打件。
所以本批的验收标准统一成一条：**每个结论字段都要能回答「这是测出来的，还是没测」**，
答不上来就加一个字段（`stale` / `components_known` / `ok` / `reason` / `fallbacks` / `damaged`）。

## 三、当前验证结果（可复现）

```powershell
cd I:\PrinterApp

# 全量回归
.venv\Scripts\python.exe -m pytest tests -q
# 420 passed（含合并后的既有用例与 tests/test_rounds_62_162.py 新增 31 项）

# 快速几何闸门（每轮开始/结束各跑一次）
.venv\Scripts\python.exe bench\round_gate.py
# 39/39 项通过，约 1.7s

# 三角-三角精确判定探针
.venv\Scripts\python.exe bench\probe_tritri.py
# 10/10
```

关键几何量（来自 `bench/round_gate.py`）：

| 夹具 | 修复前 | 修复后 |
|---|---|---|
| 20mm 立方体单面洞 | 开放边 4，体积 6666.67 | 开放边 0，体积 8000.00，水密 |
| 破碎球 + 碎屑 | 开放边 96 | 开放边 0，非流形 0，水密 |
| 孤立薄片 + 立方体 | 13 面 | 12 面，体积 1000，水密 |
| 重复面立方体 | 24 面，非流形 18 | 12 面，非流形 0，水密 |
| 一正一反两个立方体 | 体积 0（水密但抵消） | 体积 2000，水密 |
| 两球穿插 / 干净球 | — | 76 对 / 0 对 |
| 5×5 单层网格片 | 开放边 20，体积 0 | 回退，保留原始 50 面 |
| 0.5mm 立方体 | pitch 0.2，约 2~3 格 | pitch 0.025，23³ 栅格 |
| 细长大三角形 + 远处小三角形（真实穿插） | 检出 0（漏检） | 检出 1 |
| 洞口后立着穿墙的板 | 报成功，补丁穿墙 | 回退，`patch_hits=3` |
| 立方体 + 4 三角薄片（16 面） | 回退，薄片仍在 | 12 面，体积 1000，水密 |
| 两球穿插体素重网格 | 只报「面数变了」 | 自交 76 → 0，水密，体积 −0.32% |
| 壁厚测量降级 | 92 分 / A / 可直接打印 | 80 分 / C / 建议先补测 |
| `small_ratio=5` | 报告「2/2 是碎屑」，动作删 1 | 报告 1/2，动作删 1 |
| 缓存网格被 `apply_scale(2)` | 再加载 60mm → 120mm | 仍为 60mm |
| 6 线程并发写 jobs.json | 6 条只剩 4 条 | 6 条全在 |
| 18 万面七项检测（串行） | 3.00s | 2.65s（−11.5%） |
| 三面共边的开放片（3 条开放边） | 返回 0 个环，静默 | 返回 0 个环 + 明确原因 |
| 整体反法线立方体补洞 | 体积 −8000，`filled=True` | 体积 +8000，`orientation_fixed` |
| 20 万面壁厚粗筛（600 面） | 壁厚 20/20 分 | 12/20 分 + 覆盖率提醒 |
| 同一平面 4×4 vs 8×8 网格 | 56/81 顶点判「移动」 | 0 个，`identical=True` |
| 小模型补洞（首次） | 0.59s / +330MB（import cupy） | 0.02s / +2MB |
| 孤立顶点 (999,999,999) 平滑 10 次 | (0.98,0.98,0.98) | 原样保持 |
| 120mm 柱分 3 段 | 桩位 `None` | 桩位在实体内，逐块验收水密 |

---

## 四、遗留问题与下一轮计划

第 19 轮列的 8 项已全部处理；第 41–61 轮又把处理链、分件、拼盘、对比、批处理各扫了一遍。
剩下的按优先级：

1. **MESH-024（S2）补洞跨多个洞的拓扑**：`boundary_loops_ex` 现在会**报出**走不成环的边，
   但仍不会尝试修复（例如按「度数最高的顶点切开边界图」重建环）。第 41 轮只解决了「不静默」。
2. **MESH-025（S3）曲面薄片判据**：当前判据是「有开放边且 |有向体积| ≤ 容差」，
   一张弯曲的零厚度薄片（半圆柱面）有非零有向体积，仍识别不到；需要「沿法线打一条射线看有没有第二层」的厚度探测。
3. **PERF-002（S3）自交候选规模**：18 万面平面片仍会产生 > 40 万候选对。
   注意：候选集已经是「AABB 重叠」的超集，改成网格哈希**不会**减少真正需要精确判定的对数，
   真正的出路是给 `tri_tri_overlap` 加空间划分或降低单对成本。
4. **PERF-003（S3）`orient_consistent`**：18 万面 0.64s，字典 BFS。可改 CSR + 并查集带奇偶，
   但收益有限（只在绕序不一致时调用），优先级低于上一条。
5. **UI-005（S3）批量结果页**：`reverted` / `quality` 已经进 `process_one` 的返回，前端批量页还没展示。
6. **STORE-001（S3）产出文件无上限**：已在 R88–R91 处理——按项目保留最近 N 版，删除前校验 `data/` 与跨项目引用，历史记录保留并禁用已清理版本的回滚。后续可加全局磁盘占用统计。

> 1000 轮是长期目标。当前完成 162 轮，剩余 838 轮继续按「先写失败用例 → 修复 → 跑 pytest + round_gate → 记录本轮」推进。

## 五、每轮续跑清单

1. 跑基线：`pytest tests -q` 与 `bench\round_gate.py`，确认全绿。
2. 从第四节取最高优先级问题，先写一个会失败的测试（几何量断言）。
3. 改代码，只动与问题相关的最小范围。
4. 重跑测试与闸门；记录修复前后数值。
5. 在本文追加「第 N 轮」小节：缺陷编号/级别、改动文件、验证命令与结果、遗留问题、下一轮计划。
6. 若修改了 `bench/round_gate.py` 的检查项，同步更新本文第三节的表格。

---

## 六、整体提升（R19 → R61）

### 6.1 量化对比

| 维度 | R19（两批迭代前） | R61（最终版） | 变化 |
|---|---|---|---|
| 单元测试 | 81 passed | **136 passed** | +55 |
| 几何闸门 | 9 项 | **28 项**，1.3s | +19 |
| 自交漏检（大/细长三角形） | 检出 0 | 检出 1 | 漏检消除 |
| 补丁穿墙 | 报成功 | 回退 + 原因 | 谎报消除 |
| 多三角零厚度薄片 | 识别不到 | 删除并说明 | 能力新增 |
| 补不上的洞 | 静默丢弃 | 报「N 条边无法成环 + 原因」 | 可解释 |
| 整体反法线补洞 | 体积 −8000 仍报成功 | 自动转正 + `orientation_fixed` | 谎报消除 |
| 重网格结果 | 不验收 | 自交前后计数 + 降级判定 | 谎报消除 |
| 未测出项的评分 | 92 / A / 可直接打印 | 80 / C / 建议补测 | 误判消除 |
| 壁厚粗筛（0.3% 覆盖） | 20/20 满分 | 12/20 + 覆盖率提醒 | 过度自信消除 |
| 打孔薄壁过滤 | 死代码（恒不过滤） | 按实测壳厚生效 + 全拒降级 | 能力恢复 |
| 支撑间距 | 比设定密一倍 | 跟 spacing 走 | 工艺一致 |
| 减面破坏水密 | 继续用，下游报错 | 当场回退并说明 | 归因正确 |
| 分件桩位 | 落在空腔 / `None` | 落在实体内 | 可执行 |
| 版本对比位移 | 同一平面不同密度报 56/81 移动 | 0 个，`identical=True` | 口径修正 |
| 孤立顶点平滑 | (999,999,999) → (0.98,0.98,0.98) | 原样保持 | 数据破坏消除 |
| 小模型补洞（首次） | 0.59s / +330MB | 0.02s / +2MB | −97% 耗时 / −99% 内存 |
| 接口非法入参 | 入队后失败或 500 | 422 + 范围/枚举提示 | 反馈提前 |
| 批量处理 | 一项崩全项失败 / 无评分 / 回退记成功 | 隔离 + 评分 + `reverted` | 三处一致 |
| 并发任务持久化 | 6 条丢 2 条 | 6 条全在 | 数据完整 |
| 缓存网格污染 | 60mm → 120mm | 不再污染 | 数据破坏消除 |
| README 可信度 | 223 项测试 / 不存在的 CI | 136 项 / 无 CI 明说 | 文档与仓库一致 |

### 6.2 结构性提升（不只是修 bug）

1. **「修好了没有」变成可验收的**：补洞（穿插 + 朝向）、法线（绕序冲突）、重网格（自交前后）、
   碎屑（同口径）、减面（水密）、分件（逐块水密）、打孔（薄壁过滤）、评分（可信度）
   都有了显式验收字段，而不是靠「函数没抛异常」当作成功。
2. **失败可见**：`reverted` / `degraded` / `caveats` / `patch_hits` / `count_is_lower_bound` /
   `confidence` / `failed_checks` / `orientation_fixed` / `thin_fallback` / `not_watertight` /
   `rotate_fits` 这套字段从检测层一路传到界面卡片与报告横幅。
3. **参数一条链路收敛**：接口区间表 + 枚举校验（422）→ 各模块 `_bounded_int` / `sanitize_*` /
   `_num` → 动作内二次收敛。三层同一套边界，`small_ratio`、`platform_w`、`max_size` 不再各写一套。
4. **数据破坏类的静默错误被清掉**：缓存网格写时复制、`jobs.json` 串行化、孤立顶点固定、
   减面回退、打孔降级。这几处以前都不会报错，只会安静地给出错误数据。

### 6.3 本轮放弃或否决的优化（记录原因，避免重复试探）

- **`find_self_intersections` 改 AABB 网格哈希**：候选集已经是「AABB 重叠」的超集，
  换成网格哈希**不会**减少真正需要精确判定的对数（36.9 万对），收益接近零，故不做。
- **`orient_consistent` 向量化**：18 万面 0.64s，只在绕序不一致时调用；收益小于风险，留待后续。
- **`hollow_shell` 的 `_coarsen_grid` 变换偏移**：一度怀疑少了一个 pitch 的平移，
  实测腔体中心与外壳中心偏差 0.000mm、半径偏差等于半个粗体素（0.8mm），属粗化固有量化，不是 bug。
- **`boundary_loops` 向量化**：实测 3 万面全碎模型（9 万条边界边）0.14s，不是瓶颈。
- **曲面薄片识别**：正确判据需要「沿法线探测第二层」的厚度射线，属于新能力而非修 bug，
  且误删真实薄壳的代价高，单独排到 MESH-025。
- **镂空省料预估与实际差 0.2%**：`cavity_volume` 与 `hollow_shell(coarse=2)` 的口径差异实测
  81.0% vs 80.9%/81.2%，在容差内，不改。
- **`broken_1k/fill_holes` 的 +330MB「回归」是测量归属问题**：单模型跑基准时首次
  `gpu.info()` 会 `import cupy`。用 `git worktree` 检出改动前代码，同样 +327MB——不是回归。
  第 54 轮顺带把「为报一句话强制探测」这个根因去掉了。
