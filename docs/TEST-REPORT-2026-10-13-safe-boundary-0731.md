# TEST-REPORT — 0731 安全边界（不崩溃）：pernode vs pp4full vs PP1-explicit（prefill/decode 分侧）

- **日期**：2026-10-13 起草 → **2026-10-14 终版**（含收官日：修复战役 + 三形态终版数据集 + 边界阶梯）
- **目的**：定义并记录「**安全边界 = 不崩溃**」——多大的并发（concurrency）与请求速率（request rate）
  下 **prefill / decode 两侧**均不崩溃，作为生产选型与运维判据。
- **口径**：一切数据来自 sglbench run 归档（DuckDB + `data/runs/` 全量已备份至
  `minio/cubestack-full-offline-files/sglbench-backup-20261014/`，供多角度后分析）；崩溃 = 引擎组重建 /
  pod restart / 进程 abort（非超时）。**超时（wrapper）单独标注，不混淆为崩溃**。
- **上游**：`TROUBLESHOOTING-2026-10-14-dsv4-0731-pp4full-crashes.md`（四类崩溃框架 + 逐次首死分析）、
  `TEST-REPORT-2026-10-12-stable-envelope-0731.md`、`TEST-REPORT-2026-10-10-0731-comprehensive.md`。

> **2026-10-14 收官日摘要**：① **修复战役闭环**——pp4full 两类崩溃被参数修复并验证（A 类 decode 池饱和
> 断言：mem-frac↑0.78 → std-c128 300/300 ✓；D 类 65K prefill OOM：prefill mem-frac↓0.78 → 65K 单流
> 20/20、TTFT **4.27s** ✓）；② **三形态终版数据集**——pernode 42 全 ok（3h10m）/ pp1x 33/34 ok /
> pp4full 战役 66 run；③ **L2 A/B**：pernode L2-off 与 L2-on 双双全绿；④ 边界阶梯：见 §8。

---

## §1 定义与口径

**安全边界（不崩溃）操作定义**（沿用 `docs/SCENARIO-REVIEW-4.1-standard-battery.md` §5）：

1. 请求成功率 **100%**（零 ClientPayloadError / 零 5xx — abort 降级产生的 500 亦计失败）
2. 零 wrapper timeout
3. **无引擎组重建 / 无 pod restart**（探针档除外）

**分侧口径**：

- **decode 侧安全边界** = decode 组零崩溃（无 LWS 重建、无 gloo 断连首发）
- **prefill 侧安全边界** = prefill 组零崩溃（无重建、无熔断首发）
- **级联崩溃**（gloo `Connection closed by peer` 传染）按**首故障侧**归属——证据链：router 错误码点名侧别
  （`No available prefill workers` / `No available decode workers`）+ 双侧 rank0 日志时序 + 节点 dmesg。

**测试方法 = 自适应阶梯（用户约束）**：不用盲测极端并发/速率——从已知安全点起步，**仅当该形态仍不崩才继续
上强度，首崩档即边界**。稳定形态（pernode）上探至首崩；不稳定形态（pp4full）就地记录，不硬推。

---

## §2 安全边界一览（当前判定）

| 形态 | decode 侧边界（0 崩溃） | prefill 侧边界（0 崩溃） | rate 边界 |
|---|---|---|---|
| **pernode**（最优解） | **conc ≤ 160 @ rate ≤ 5**（3354 tok/s、TTFT 2.0s、零失败）；conc > 192（=总 cap）超限（c256 全 partial） | 已测负载内**无独立崩溃线**；平台故障呈**单引擎隔离**（1/8 算力闪断，全站不崩） | **rate 2 = 容量点**（2903 tok/s）；rate 5 饱和档已验证；**rate ≥ 8 待阶梯**（§8.4） |
| **pp4full**（官方，**10-14 修复后**） | **conc64/128 ok（c128 300/300 ✓）**、65K 单流 ok（TTFT 4.27s ✓）；残余=**B 类平台故障**（.32/.33/.34/.35 轮转，无参数解）+ **D′**（chat rate13 MCCL-OOM） | 无独立负载线；65K OOM 已修复（mem-frac 0.78） | 官方路径税（单流 +~4s）仍在 |
| **PP1-explicit**（tp8/dp4 双侧，**10-14 终版**） | **conc ≤ 160 ok**（std 阶梯含 c160 ok）；crash-probe/conc-256 **竟然全过**；prefill-only stage 诊断失败（单引擎极限） | 无整组级联（引擎=4 节点整体，故障=全引擎） | rate/网格全 ok；prefill-only 为唯一非 ok |

> ⚠ pp4full 数据 **#14/25 进行中**；pernode 上探（conc192/224/256、rate8/10）待执行——见 §6。

---

## §3 pernode 安全边界（4+4 每节点独立 tp8 引擎；每引擎 cap48 ×4 = 192）

### 3.1 逐格实测（prod-suite 形态1，37 run：22 ok / 9 partial / 6 failed）

| 场景组 | 结果 | 边界含义 |
|---|---|---|
| standard-c16 → c160（全档） | **全 ok** | 并发 16→160 零崩溃（含 c160） |
| chat/rag/code -c1 | 全 ok | 单流零崩溃 |
| chat-c16 / chat-c64 | ok | 画像常规档零崩溃 |
| **chat-c128** | **3 次全 partial**（0.95/0.13/0.13） | **画像边界**：高周转 chat@128（每请求短、迭代密度高） |
| rag/code -c16/64/128 | 全 ok | 画像 c128 崩仅限 chat 型流量 |
| longctx-65k-c1 / **c8** / 128k / 200k | 全 ok | 65K 8 并发零崩溃（长 prefill 无 PP，TTFT ~13s） |
| steady-standard-r2 | ok | rate2 稳态零崩溃（容量点） |
| **boundary-standard-c256** | 6 次全 partial（0.28-0.91） | **> 总 cap192 超限**（设计探针，预期捕获） |
| **boundary-longctx-c32** | 6 次 failed（**45m 超时 5 次 + warmup 断连 1 次**） | 非崩溃性超限：65K×32 ≈ 容量 ~12 → 单请求 36-80s 跑不完 |

### 3.2 decode 侧边界线

- **联合并发线**：conc ≤ 160 @ rate5 **零失败**（峰值 3354 tok/s）；conc 256（> cap192）engine 侧排队崩解
  → 边界位于 **160–192 之间**（精确档待 §6 上探钉死）。
- **KV 容量线（长上下文直接相关）**：4096/2048 负载下 swa 池水位与原 rc3 标定一致（64c≈0.64 → 安全；
  ≈0.99 → 崩）；**65K 级：8 并发已验证 100/100**（full 池每 rank ~3 请求、总量 ~12 为绝对上限）。
- **rate 线**：rate 2 = 服务容量点（2903 tok/s 稳态）；rate 5 = 饱和档（conc160 零失败）；**rate ≥ 8 从未通过
  （历史）→ pernode 下未复测**。

### 3.3 prefill 侧边界线

- 战役全程**无 prefill 侧独立崩溃**（对比 rc3 时代 prefill 健康检查熔断事件）；非 ok 均可归 decode/KV 或超时。
- 长 prefill（65K 单流 TTFT 13.0s）为 pernode 短板（无 PP 切分）——是**性能边界非崩溃边界**。

---

## §4 pp4full 安全边界（prefill pp4/tp8/dp1；decode tp8/dp4/ep1 + DSPARK；**数据进行中**）

### 4.1 decode 侧边界线：**conc ≥ 64 图扩展捕获崩**（本轮核心发现）

崩溃链（decode-rank0.log，节点 35）：

```
CUDA graph 捕获期 DSv4 c4_v2 压缩 JIT kernel 失败:
  tvm.error.InternalError: Runtime check failed at .../deepseek_v4/c4_v2.cuh:481:
  CUDA error: unrecognized error code
  → Capture cuda graph failed (decode_cuda_graph_runner.py:685)
  → scheduler 异常 → 引擎 abort → gloo "Connection closed by peer" 传染同组
  → LWS RecreateGroupOnPodRestart 整组重建
```

- **工作假设**：并发 > decode cap 总量（16×dp4=64）时需**运行时扩展 CUDA graph batch**，扩展路径在
  c4_v2 压缩 kernel 上捕获失败（与 pernode `graph-bs > max-running` 规避规则同族）。
- 今天 decode 组重建 ≥9 次（09:12-12:57 集中），每次伴随同款签名；decode-rank0.log 累计 40 次
  `Connection closed by peer`。
- **配置级修复 + 重测**见 §6（graph-bs 预捕获假设的验证）。

### 4.2 prefill 侧边界线

- 无独立负载线；prefill 故障出现在：① decode 崩后 KV 传输 peer 断连级联；② 平台故障（见 §5）；
  ③ **leader 单端点脆弱**——router `PD_PREFILL_ENDPOINTS` 仅 `{{IP_PREFILL_0}}:8000` 单点，组重建后
  leader 漂移即全站 503（本轮多次 `No available prefill workers`）。
- prefill 引擎侧同样出现 `CUDA error: unrecognized error code`（pp4 单流**非**崩溃强相关，待 nospark A/B）。

### 4.3 逐格实测【进行中】（#14/25，34 run：ok 10 / partial 12+ / failed 10+）

| 场景 | 结果 | 备注 |
|---|---|---|
| chat/rag/code -c1 | 全 ok | 单流零崩溃 |
| standard-c16 | 2 partial（0.44/0.145）+ 1 failed | **0 干净**；两崩均平台故障（node34 mem_viol，§5） |
| standard-c32 | **1 ok（d218，GPU 复位后）** + 2 failed | 复位前 2/2 崩 |
| standard-c64 | 1 ok + 1 failed | 图捕获假设首发档 |
| standard-c96 | 1 ok + 1 partial（0.27） | |
| standard-c128 | 1 ok + 1 partial（0.65）+ 1 failed | |
| **standard-c160** | **3 次全 partial**（0.10/0.07/0.21） | 与 decode cap64 偏差最大档 |
| chat-c16 | 2 partial（0.93/0.905）+ 1 failed | |
| chat-c64 | 2 partial（0.107/0.26）+ 1 failed（+1 尝试） | |
| chat-c128 | 3 failed（进行中→定型） | |
| rag-c16 | 2 failed + 1 partial（进行中） | |
| rag-c64 | 1 failed + 1 partial（进行中） | |
| rag-c128 / code×3 / longctx×4 / steady / boundary×2 | **待跑** | 收官后回填 |

**模式小结**：≥64 档几乎每格都需 2-3 次尝试才可能拿到 1 次 ok；160 档从未干净。**与 decode cap=64 的
"扩展捕获崩"完全吻合**。

---

## §5 崩溃归属与签名表（本轮 + 历史继承）

| 签名 | 首故障侧 | 类别 | 可修性/状态 |
|---|---|---|---|
| `c4_v2.cuh:481 CUDA error: unrecognized error code`（graph 捕获） | **decode** | 引擎缺陷（图扩展路径） | **配置级候选**（graph-bs 预捕获，§6） |
| `node34 GPU0 mem_viol(0x4)`（B900@09:00.0；同卡 10-06 xnack 0x8） | decode 节点卡 | **平台故障（MACA）** | **不可软件修** → 报 MACA；复发考虑复位/报修 |
| gloo `Connection closed by peer` / MCCL reset | 级联（次生） | 放大器 | 随首故障消灭 |
| router `No available prefill/decode workers`（熔断） | 次生 | 运维面 | leader 漂移自愈 + 多端点化（改进项） |
| 客户端 `ClientPayloadError / TransferEncodingError` | 次生 | 观测面 | — |
| （历史）retract→SWA offload `NotImplementedError` | decode | 引擎缺陷 | ✅ 已修（retractfix，两侧镜像内置，46db2551） |
| （历史）func_timer `DuplicateTimeseries` | 恢复路径 | 引擎缺陷 | ✅ 已修（BH-patch3 内置） |
| （历史）`NoneType.server_args` 竞态 | 两侧均现 | 引擎缺陷（未定位） | ❌ 未修（上游候选） |
| （历史）cap160 boot `bs=None` DSPARK 崩 | decode | 引擎缺陷（已定位） | 规避：`graph-bs > max-running` |

**结论**：pp4full 当前崩溃**不含 retract 链**（该修复已在镜像内生效并经战役验证）；主因 = ① decode 图扩展捕获
（本任务修复对象）② node34 平台故障（不可修）③ 官方路径竞态放大（NoneType，未修）。

---

## §6 修复与上探计划（结果回填节）

1. **pp4full 配置级修复 + 重测**（campaign 收官后执行，保持 pp4full 部署）：
   - `official-pp4/lws/base/decode.yaml`：`PD_CUDA_GRAPH_MAX_BS` ≥128（预捕获大图，消除运行期扩展捕获）
   - 废弃 env 对齐：`SGLANG_DISABLE_TP_MEMORY_INBALANCE_CHECK` → `SGLANG_ENABLE_TP_MEMORY_INBALANCE_CHECK=0`
   - env `decode_ep:8` → `1` 对齐 yaml（防误部署）
   - 重测 standard-c64 / c128 / chat-c128（label `pp4full-fixed-*`）→ 边界是否扩张入档
   - 可选：nospark A/B @conc128（隔离 DSPARK 触发面）
2. **pernode 边界上探**（恢复 pernode 后，自适应阶梯）：conc192 → 224 → 256 @rate5 →（若全绿）rate8/10
   @conc64；首崩即停；label `boundary-probe-pernode-*`。
3. **超时 vs 崩溃分离**：longctx-65k-c32 探针的 45m 超时档记录"完成边界"（非崩溃边界）。
4. 平台故障（node34）：出 MACA 报障补充件（证据链：dmesg mem_viol + 复现负载 + 同卡历史 xnack）。

## §7 数据来源

- pernode：`data/runs/prod-*` label `prod-2026-10-pernode`（37 run，10-12 22:50Z → 10-13 06:46Z）
- pp4full：label `prod-2026-10-pp4full`（进行中，10-13 07:27Z →）
- 引擎日志：节点 hostPath `/var/log/sglang-dsv4-0731/{prefill,decode}-rank*.log`
- 战役编排：`campaigns/production-suite.json`（25 场景，auto_retry=2，gpu_reset，45m timeout）
- env 快照：`envs/dsv4-0731-pernode-c48.json` / `envs/dsv4-0731-pp4full.json`（已入库 run_environments）

---

## §8 收官日全记录（2026-10-14）

### 8.1 修复战役（pp4full，Phase 1）——A/D 两类修复闭环

| 类 | 修复 | 验证数据（label） |
|---|---|---|
| **A** decode 池饱和断言（`kv_loc=None`） | decode `mem-frac 0.75→0.78`（+graph-bs128/env 对齐） | **std-c128 = 300/300 ok**（`pp4full-fixed2-std-c128`）✓ |
| **D** 65K prefill OOM（2.57GiB 三连复现） | prefill `mem-frac 0.82→0.78` | **lc65k-c1 = 20/20 ok，MeanTTFT 4.27s**（`pp4full-fixed4-lc65k-c1`）；c8 = 34/40 ✓ |
| **B** 平台设备故障（mem_viol/xnack/warm-reset） | ❌ 参数无解 | .32/.33/.34/.35 轮转；**pernode 单引擎隔离实证**（同故障仅 1/8 算力闪断） |
| **D′** chat rate13 MCCL-proxy OOM | ❌ 待镜像级/负载整形 | chat-c128 136/800 后崩（`pp4full-fixed2-chat-c128`） |

pp4full 终台账：prod-suite campaign **66 run（7 ok/21 partial/38 failed，含大量假失败污染——假失败台账见
troubleshooting §2）**；修复重测 4 轮（fixed1-4）。

### 8.2 三形态终版数据集（同一 42 场景套件）

| 形态 | label | 结果 | 耗时 |
|---|---|---|---|
| **pernode**（4+4 节点内 tp8） | `final-2026-10-14-pernode` | **42/42 ok**（c160 1 次重试；.32 引擎故障=单引擎隔离） | 3h10m |
| **pp1x**（4P+4D 跨节点 tp8/dp4） | `final-2026-10-14-pp1x` | **33/34 ok**（唯一 failed=prefill-only stage；crash-probe/conc-256 竟然通过；c160 修复后复跑 ok） | ~2h |
| **pp4full**（官方脚本 pp4/tp8/dp1） | `prod-2026-10-pp4full` + `pp4full-fixed*` | 战役 66 run + 修复重测（见 §8.1） | — |

### 8.3 L2 A/B（pernode，严格同拓扑）

| 侧 | label | 结果 |
|---|---|---|
| L2-off（基线，主套件内） | `final-2026-10-14-pernode`（mini-l2×3 + multiturn×2 同键） | 全 ok |
| L2-on（`PD_ENABLE_HICACHE=1`） | `final-2026-10-14-pernode-l2` | **6/6 ok**（含 80x10-r16 官参档） |

数值级 A/B（TTFT/吞吐差）→ 从备份数据后分析（两组均已入 DuckDB + MinIO 备份）。

### 8.4 边界阶梯（pernode，自适应首崩即停）——终版结果

| 探针格 | 结果 | 形态 |
|---|---|---|
| c192@r5 | **partial（成功 3.3%）** | **router 熔断**（`No available prefill workers (all circuits open)`；引擎全健康、无 forensics 签名、probe 200） |
| c176@r5（二分细化） | **partial（200/300，TTFT 29s）** | 服务层崩解（无签名；队列/熔断） |
| **r8@c64** | **failed（8 节点 connection_reset）** | **引擎级级联崩溃**（历史"rate≥8 从未通过"复现） |

**pernode 安全边界终版**：**conc ≤ 160 @ rate ≤ 5 = 零崩溃包络**（3354 tok/s 峰值 / TTFT 2.0s）；
**conc ≥ 176 = 服务层崩解**（router 熔断型，引擎不崩可恢复）；**rate ≥ 8 = 引擎级崩溃**（跨引擎级联）——
**"边界崩"与"崩溃"分两层**：并发超限 = 服务拒绝（可恢复）；速率超限 = 真崩溃（须避免）。label
`boundary-probe-pernode-*`（4 格数据全部入 DuckDB + MinIO 备份）。

### 8.5 交付物清单

- 本报告（安全边界终版）+ `TROUBLESHOOTING-2026-10-14-*.md`（四类框架+逐次首死）
- 套件：`campaigns/final-suite.json`（42×双形态）+ `final-pernode-l2-suite.json`
- 数据：`data/runs/` 全量 + `bench.duckdb`（MinIO 备份 `cubestack-full-offline-files/sglbench-backup-20261014/`）
- 部署资产：llm-d 五形态目录（pernode/pp1x/pp4full/nospark/f4-终止）+ 修复 yaml（Phase 1/5）
