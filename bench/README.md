# bench —— 3D Print Forge 性能与正确性基准套件

这套基准存在的理由只有一条：**所有「优化了多少」的结论都必须由它说了算，禁止凭感觉或编造数字下结论。** 它同时守住两件事——性能（耗时/内存不超阈值）和正确性（结果指纹没变，即「没悄悄算错」）。

## 一条命令重建整套环境

```bash
.venv/Scripts/python.exe bench/make_models.py     # 生成 13 个确定性模型 + model_manifest.json
.venv/Scripts/python.exe bench/run.py --save-expected   # 跑全量基准，并把指纹存为回归基线
```

模型由固定脚本生成，**无随机数、跨机器逐字节相同**。改了生成器必须重跑上面两步，否则历史数据不可比。

## 日常怎么用

| 你想做的事 | 命令 |
|---|---|
| 跑全量基准（3 次重复取中位） | `.venv/Scripts/python.exe bench/run.py` |
| 快速跑（1 次，迭代时用） | `.venv/Scripts/python.exe bench/run.py --quick` |
| 只测某几个模型/阶段 | `.venv/Scripts/python.exe bench/run.py --models ball_20k,needle --stages analyze,thick_field` |
| 存回归基线（优化前做一次） | `.venv/Scripts/python.exe bench/run.py --save-expected` |
| 两次结果对比（耗时/内存/指纹） | `.venv/Scripts/python.exe bench/compare.py bench/results/A.json bench/results/B.json` |
| 校验射线求交新旧一致 | `.venv/Scripts/python.exe bench/verify_raycast.py` |
| 审计阈值表 | `.venv/Scripts/python.exe bench/audit_thresholds.py` |
| 校验模型集 ↔ 清单 ↔ 磁盘 ↔ 阈值 | `.venv/Scripts/python.exe bench/check_models.py` |
| **CI 一键门禁（上面三道闸门全跑）** | `.venv/Scripts/python.exe bench/ci.py` |

## 每个脚本负责什么

- **make_models.py** — 用确定性参数生成 13 个模型，覆盖常规件、高面数、薄壁、退化面、单位错误（inch）、多实体、自交、悬垂测试、破损、长宽比 1000:1 的针、0 面空模型、ASCII 大文件。同时产出 `model_manifest.json`（面数/体积/表面积/包围盒）。
- **run.py** — 总控。每个模型起一个独立子进程跑 worker，汇总后判过/不过。关键设计：阶段之间**清空全部模块级缓存再 gc**，否则测的是「缓存命中后的第二次」，优化红利会被自己的缓存假象吃掉；内存用**独立采样进程**按 4ms 轮询 RSS（同进程内采样线程会被 GIL 饿死，采出来的峰值是假的）；每个阶段额外记录**结果指纹**。
- **worker.py** — 单模型执行器。每个阶段重复 N 次取中位；网格加载不计入被测阶段；网格加载失败会按阶段记录错误而不杀死整个 worker。
- **stages.py** — 被测阶段定义（加载/预览/导出/检测/场/修复/处理）。每个阶段返回「紧凑领域摘要」给指纹用，剔除耗时、时间戳这类噪声。
- **digest.py** — 把任意返回值压成可比较的指纹，并用 `diff` 逐项比差异。这是「变快了但算错了」的克星。
- **mem_sampler.py** — 独立内存采样进程。
- **verify_raycast.py** — 把射线求交的旧实现原样抄一份当参考，在 10 个模型 × 3 组参数下逐条射线比对，命中/未命中不一致直接判失败。
- **compare.py** — 两次结果的三方对比（耗时/内存/指纹），并**校验环境指纹**（换机器/升库版本会报警，因为绝对值不可比）。
- **audit_thresholds.py** — 阈值表审计：缺定义（假 PASS）和过紧（换机假报警）算失败；过松只作调优建议。优先读 `expected.json`。
- **check_models.py** — 模型集 vs 清单一致性 + 四源一致性（矩阵/清单/磁盘/阈值）。
- **ci.py** — 串起 run（阈值+指纹回归）+ verify + audit，聚合退出码。任一不过即红。

## 两个必须知道的坑

1. **并行（`--parallel N`）测出的耗时与串行不可比**——各 worker 争抢 CPU。结果文件里记了 `env.parallel`，compare.py 会在跨并行度对比时报错。平时建立基线用串行。
2. **环境指纹**（`env` 字段：CPU 核数、内存、Python/numpy/trimesh/rtree 版本）。阈值是在「32 核 / 64GB」这台开发机上定的，换机器跑出来的数字如果混在一起比较，结论就是错的。compare.py 与 audit 都会据此报警。

## 阈值在 `thresholds.json`

改这个文件等于改验收标准，**必须走版本控制并说明原因**。重操作/导出的宽松上限是刻意留的**安全天花板**，不是回归检测线——它们的退化由结果指纹回归闸门捕捉。
