# 模型修补 · 迭代记录

> 对象：`I:\PrinterApp` 的网格检测 / 修补管线（`app/analysis.py`、`app/repair.py`、`app/voxelops.py`）。
> 目标：1000 轮可验证迭代，优先修「补洞 / 非流形 / 自相交 / 法线 / 水密性 / 壁厚」。
> 当前进度：**18 / 1000 轮**（本文件只记录已落地并有验证的轮次，不把计划算作完成）。
> 当前基线：`pytest tests -q` → **80 passed**；`bench/round_gate.py` → **9/9**；`bench/probe_tritri.py` → **10/10**。

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
| 10 | TEST-001 | S3 | 建立修补回归入口，46 个用例覆盖几何量而不只是「不抛异常」 | `tests/test_repair.py` |
| 11 | MESH-010 | S2 | 耳切失败不再用凹多边形不安全的扇形兜底，并做面积校验 | `repair.py` |
| 12 | MESH-011 | S1 | 体素 pitch 上下界收敛，微小模型不再被固定 0.2mm floor 切没 | `voxelops.py` |
| 13 | MESH-012 | S2 | 自交重网格的 pitch 统一走安全收敛，非法输入不再直传 | `repair.py` |
| 14 | MESH-013 | S3 | 壁厚射线异常标记 degraded，不再用 KDTree 最近顶点伪造数值 | `analysis.py` |
| 15 | MESH-014 | S1 | 加厚量按模型尺度收敛，避免超大距离场撑爆内存 | `repair.py` |
| 16 | MESH-015 | S2 | 单层网格片补洞验收为「零厚度壳」并回退 | `repair.py` |
| 17 | MESH-016 | S2 | 缩放/迭代次数/碎屑比例等外部参数做合法区间校验 | `repair.py` |
| 18 | TEST-002 | S3 | 新增 9 项快速几何闸门，作为每轮续跑的入口 | `bench/round_gate.py` |

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
- **遗留**：验收尚未纳入「补丁自相交」计数，留待第 19 轮之后处理。

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
- **验证**：`tests/test_repair.py` 当前 **46 passed**（37 个测试函数，其中三角-三角用例参数化为 10 组）；全量 `tests` **80 passed**。
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

- **现象**：`pytest` 覆盖全，但每轮开始时要先跑 80 项、再看多个探针，反馈链路过长；探针输出是自由文本，不能直接判定通过/失败。
- **改动**：新增 `bench/round_gate.py`，9 项确定性检查：平面洞体积、破碎球补洞、薄片删除、重复面去重、逐分量法线、自相交检出/误报、零厚度壳回退、pitch 收敛、凹多边形耳切。支持 `--json`，失败退出码 1。
- **验证**：`.venv\Scripts\python.exe bench\round_gate.py` → **9/9 通过，约 0.5s**。
- **遗留**：闸门尚未纳入 `thicken`、`remove_small`、API 任务层；后续按轮次补充。

---

## 三、当前验证结果（可复现）

```powershell
cd I:\PrinterApp

# 全量回归
.venv\Scripts\python.exe -m pytest tests -q
# 80 passed

# 快速几何闸门（每轮开始/结束各跑一次）
.venv\Scripts\python.exe bench\round_gate.py
# 9/9 项通过

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

---

## 四、遗留问题与下一轮计划

按优先级排序，下一轮从第 19 轮开始，每轮只做一个可证伪的改动：

1. **MESH-017（S2）补丁自相交验收**：`act_fill_holes` 目前只查开放边/非流形/重复面/体积，不查补出来的面是否与周围面穿插。计划：只对新增面与邻域面做 `tri_tri_overlap`，失败则回退。
2. **MESH-018（S2）多三角形孤立薄片**：`_sliver_face_indices` 只处理 3 边环。计划：按连通分量找「所有边都是开放边」的薄片组，整组删除。
3. **MESH-019（S2）自相交候选完整性**：KD 树中心半径可能漏掉大三角形。计划：改用三角形 AABB 树/扫描线生成候选对，并用大三角形夹具验证。
4. **MESH-020（S2）重网格后复检**：`act_resolve_self_intersection` 返回前重新计数自相交、非流形、水密性，未达标则标记 `degraded`。
5. **MESH-021（S3）单位推断歧义**：最大尺寸 <1.0 时一律判米会误伤 0.5mm 微件。计划：增加「微件/米制」二义性提示，而不是直接给 ×1000 建议。
6. **UI-001（S2）degraded 展示**：壁厚 `degraded`、补洞 `reverted`、pitch 被收敛都应在进度/结果面板显示原因，而不是只写日志。
7. **API-001（S3）入参 schema**：在 `main.py` 层校验 `scale_factor`、`thicken_mm`、`remesh_pitch` 等，和修补层形成双保险。
8. **PERF-001（S3）`boundary_loops` 大模型性能**：当前 Python 字典走访，10 万开放边时耗时明显；计划用 numpy 向量化边表 + 邻接数组。

> 1000 轮是长期目标。当前只完成 18 轮，剩余 982 轮必须继续按「先写失败用例 → 修复 → 跑 `pytest` + `round_gate` → 记录本轮」的流程推进；本文件不会预填未完成的轮次。

---

## 五、每轮续跑清单

1. 跑基线：`pytest tests -q` 与 `bench\round_gate.py`，确认全绿。
2. 从第四节取最高优先级问题，先写一个会失败的测试（几何量断言）。
3. 改代码，只动与问题相关的最小范围。
4. 重跑测试与闸门；记录修复前后数值。
5. 在本文追加「第 N 轮」小节：缺陷编号/级别、改动文件、验证命令与结果、遗留问题、下一轮计划。
6. 若修改了 `bench/round_gate.py` 的检查项，同步更新本文第三节的表格。
