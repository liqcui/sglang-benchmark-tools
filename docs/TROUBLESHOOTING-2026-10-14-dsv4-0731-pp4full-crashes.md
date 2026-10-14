# TROUBLESHOOTING — 0731 pp4full decode 崩溃逐次分析（2026-10-13/14 战役）

- **用途**：本战役每次 decode/引擎崩溃的**逐次根因分析记录**（用户指令：每次崩溃分析原因并记录）。
  新增事件**追加**（最新在表前）；取证命令见 §5。
- **范围**：prod-suite 形态3 pp4full（官方 llm-launch；prefill pp4/tp8/dp1 + decode tp8/dp4/ep1+DSPARK；
  decode cap=16/rank×dp4=64；镜像 `-llm-retractfix` 46db2551）。
- **关联**：`TEST-REPORT-2026-10-13-safe-boundary-0731.md` §4/§5；巡检台账=llm-d/sglbench 记忆 next-actions。

---

## §1 崩溃分类框架（本战役实证确立）

| 类 | 名称 | 签名（首死方） | 触发条件 | 可参数修复？ |
|---|---|---|---|---|
| **A** | **KV 池饱和断言**（负载型） | `AssertionError: KV cache is full! Bug in memory estimation`（`region decode.py:1761 _pre_alloc`，`kv_loc is None`） | 并发 > decode 总 cap（≥64 档）+ 池近满（swa≈1.00）时新 transfer 预分配失败 | ✅ 概率可降（池增容/降 cap/平滑流入/准入保守） |
| **B** | **平台 GPU 故障** | `[MXC][E]memory violation(0x4)` / `xnack(0x8) atu`（设备侧，任意负载） | 任意负载（低至 running-req 4-6、池<43% 亦发）；.32/.34 为多发节点 | ❌ 不可软件修复 → 热复位（已自动化）/报修 + 拓扑隔离 |
| **C** | **sticky-context 症状**（次级） | `CUDA error: unrecognized error code` (10002) 出现在后续任意 CUDA 调用（含 graph 捕获 `decode_cuda_graph_runner.py:685`） | **非独立根因**——设备已故障后上下文永久报错；真实根因是其前的 B 类（或 f4 特例形状 bug） | ❌ 随根因消除 |
| **D** | **预填侧显存 OOM** | `torch.OutOfMemoryError: CUDA out of memory`（prefill rank） | 预填负载/显存配置边界被突破（高并发 + pp4 分片显存预算） | ✅ **潜在可参数**（prefill mem-frac / chunked 减小） |

**级联形态（三类共有）**：首死 rank 进程退出 → 同引擎其余 rank 在集合通信处报
`gloo ... pair.cc:547 Connection closed by peer [<首死方IP>]`（MLP-sync all_gather / broadcast_pyobj）
→ LWS `RecreateGroupOnPodRestart` 整组重建。**注意：此报错是"对端进程死亡"，不是通信超时**。

---

## §2 逐次崩溃记录

### [10-14 10:44] Phase3-pernode：.32 引擎 warm-reset（B 类）+ **pernode 隔离实证**
- **场景**：final-suite 战役 standard-c160 窗口（10:37:57 起）
- **首死（decode group-3 引擎，节点 .32）**：`[MCR][E] mcCtxGetCurrent/mcGetDevice: Returned
  **mcErrorDriverWarmReset**`（驱动级暖复位）+ `MCCL WARN: MACA shut down, proxy exit` → CUDA 10002
  → 该引擎全 rank 崩（10:44:39）
- **containment 实证（pernode vs pp4full 关键差异）**：**仅 group-3 单引擎重启**（LWS 单组重建），
  其余 3+4 引擎全程无感；c160 run 记 partial（**成功率 93.5%**、TTFT 3.79s、ITL 11.7ms）= 窗口内部分
  请求失败（侧写引擎死亡），auto_retry 将复跑 c160 取干净格
- **平台故障节点轮转**：.32（今日第 N 次：00:33 xnack/00:35 mem_viol/09:05 …）——**单引擎隔离下
  平台故障从"全站级联"降级为"1/8 算力闪断"** = pernode 拓扑的核心稳定性价值（报告重点论据）
- **分类**：B 类（无参数解）；router 无需动作（端点列表含全部引擎，router 自动剔除失败引擎）

### [10-14 09:05] Phase1-fixed4 收尾：**lc65k-c8 = partial 34/40（首次有效测量）**；B 类复发（.33 二次）
- **c8 结果**：**status=partial 成功 34/40（85%）**，MeanTTFT 42.84s（8×65K 排队）/ MeanTPOT 10.65ms
  —— 修复前 c8 从未跑出第一批；**85% 完成度 + 健康服务轨迹**（.33 崩溃前 running 1-2、池 0.74-0.79 无压力）
- **崩溃首死（decode .33，09:05:50）**：`.33 自引用 peer close`（pod 内 sibling rank 硬死，无异常行）
  → .32/.34 级联（:50-51）；**同 .33 08:08 事件模式 → .33 二次设备故障，B 类复发**；prefill 全程干净（98m）
- **Phase 1 收官判据（终版，两轮修复+四轮重测）**：
  | 类 | 修复 | 判据数据 |
  |---|---|---|
  | A（decode 池饱和） | ✅ mem-frac 0.78 | std-c128 = 300/300 ok |
  | D（65K prefill OOM） | ✅ mem-frac 0.78 | lc65k-c1 = **20/20 ok, TTFT 4.27s**；c8 = 34/40 partial |
  | B（平台设备故障） | ❌ 无解 | .32/.33/.34/.35 轮转（.33 二次）→ 报障 + 拓扑收敛 |
  | D′（chat rate13 MCCL-OOM） | ❌ 待镜像级/负载整形 | chat-c128 partial |
- **Phase 1 净结论**：**官方路径 64/128 并发 + 65K 单流已修复到零崩（在 B 类不发时）**；65K 性能
  TTFT 4.27s / TPOT 9.7ms 为 pp4full 新最优捕获。

### [10-14 08:54] 🎉 Phase1-fixed4：**lc65k-c1 = OK（20/20）——D 类修复完全确认**
- **fixed4 结果（label pp4full-fixed4-*）**：**lc65k-c1 = status ok 成功请求=20/20**，
  **MeanTTFT 4.27s / MeanTPOT 9.71ms** —— 本战役 pp4full 上**首次完全干净的 65K 单流测量**；
  TTFT 优于历史 PP4 强项（5.27s）；**零 OOM、零崩溃**（修复前：首批即 2.57GiB OOM 杀）
- **D 类结论（终版）**：`prefill PD_MEM_FRACTION_STATIC 0.82→0.78` **修复成立** ✓（fixed3 的 17min 零 OOM
  + fixed4 的 20/20 全绿双证）
- **lc65k-c8**：08:54:31 起运行（attempt 4，集群 83min 零崩窗口，首次有效测量的机会）——结果见下/后续追加
- **Phase 1 三判据终版**：A ✅（std-c128 300/300）/ **D ✅（20/20, TTFT 4.27s）** / 残余 B 类（.32/.33/.34/.35
  轮转，参数无解）+ D′（chat rate13 MCCL-OOM）

### [10-14 08:08] ★★ Phase1-fixed3 判据：**D 类修复成立（零 OOM 17min）**；残余=B 类设备故障（.33）
- **fixed3 结果（label pp4full-fixed3-*）**：lc65k-c1 = **partial 10/20 成功**（MeanTTFT **6.17s** ≈ PP4
  历史强项 5.27s 水平！MeanTPOT 9.70ms）；lc65k-c8 = failed（c1 崩后连带，第三次污染）
- **c1 崩溃首死（decode .33，08:08:30）**：`[MCR][E] mcCtxGetCurrent/mcGetDevice: Returned
  **mcErrorIllegalAddress**` → 进程硬死 → .33 自引用 peer close → .34/.32 级联（08:08:31-32）
  —— **B 类设备故障家族**（无 OOM、无 MXC xnack 行但有 MCR illegal-address）；崩溃前 .34 正健康服务
  （running 1、full 池 0.38-0.40、accept 5.5-6.5 正常）
- **判据汇总（Phase 1 三连）**：
  - **A 类（decode 池饱和断言）→ ✅ 修复**（R2 std-c128 300/300）
  - **D 类（prefill 65K OOM 2.57GiB）→ ✅ 修复**（R1 mem-frac 0.78；fixed3 实测 17min 65K 零 OOM，
    此前首批即杀；10/20 的断点是 B 类非 OOM）
  - **残余 = B 类**（.32/.33/.34/.35 轮转）+ **D′（chat rate13 MCCL-OOM）**——参数无解，明确报障/拓扑收敛
- **lc65k-c8 至今零有效测量**（三次均被污染）→ fixed4 再试（c1 复跑 + c8）；若再被 B 类打断则以
  "10/20 + TTFT 6.17s" 为 pp4full-65K 的现有最优干净捕获并入报告

### [10-14 07:32] ★ Phase1-R2 重测：**std-c128 = ok（A 类修复生效！）**；chat-c128 新签名 MCCL-OOM
- **R2 配置**：R1 + decode `mem-frac 0.75→0.78`（07:2x boot，leader 漂移 .28→**.29** 已重同步）
- **R2 结果（label pp4full-fixed2-*）**：
  - **std-c128 = ok（300/300！）**：MeanTTFT 29.40s / MeanTPOT 37.63ms —— **A 类池饱和断言消失 =
    decode 增池容修复生效**（对比 R1：93 后崩）✓
  - **chat-c128 = partial**（136 成功）→ 本轮新根因（见下）
  - lc65k-c1/c8 = failed（**第二次被污染**——chat-c128 崩后连带；D 类判定仍悬，须 fixed3 重跑）
- **chat-c128 首死（prefill .31，07:32:43）新签名**：
  - 前置（07:32:30）：TileLang JIT 警告 ×5（**新 shape 触发 JIT 编译**）
  - **`MCCL WARN Maca failure 'out of memory'` / `Failed to MACA calloc 10485760 bytes`**（10MB 分配失败！）
    → `Proxy Setup failed` → `DistBackendError: NCCL internal check failed` → PP1 全 rank 崩 → 级联
  - **分类：D′（通信层内存耗尽）**——非 KV 池（mem-frac 已增）、非设备故障（无 MXC）；chat-c128
    rate13 极端压榨 + JIT 并发触发；**chat@128 为两形态共同的画像边界**（pernode 亦 3×partial）
- **待办（fixed3）**：prefill 重启完成后 → leader 重判 + router 重同步 → **纯跑 lc65k-c1/c8**（避开
  chat 污染；chat-c128 放最后可选）——D 类 mem-frac 0.78 修复的最终判据

### [10-14 07:04] ★ Phase1-R1 重测：std-c64 ok；**std-c128 复发 A 类池饱和断言**（→R2 增池容）
- **R1 配置**：graph-bs 128 + 双侧 env 对齐 + prefill mem-frac 0.78（06:4x 应用，07:02 链路复活验证 200）
- **R1 结果（label pp4full-fixed1-*）**：
  - **std-c64 = ok**（300/300；MeanTTFT 16.87s / MeanTPOT 22.79ms——TTFT 高属官方路径+冷启，专项分析）
  - **std-c128 = partial**（93 成功，07:04:42 崩）
  - chat-c128 / lc65k-c1 / lc65k-c8 = failed **快速失败（崩后连带污染，非有效测量，须重跑）**
- **std-c128 首死（decode .32 rank3，07:04:42）**：**`AssertionError: KV cache is full! Bug in memory
  estimation. available=0, evictable=0, protected=0, required_alloc=4096`** —— 与 20:02 c160 同款 **A 类**
  （07:04 窗口无 MXC/设备签名 → 非 B 类）
- **决策 R2**：decode `PD_MEM_FRACTION_STATIC 0.75→0.78`（增 KV 池容，治 A 类）→ 07:0x render+apply
  （decode 组重建中；prefill 不动）；待 boot + router 重同步后**重跑 4 格**（std-c128/chat-c128/lc65k×2，
  label `pp4full-fixed2-*`）
- **A 类深层候选**（报告 §3.1 新增）：`_pre_alloc` 路径也可做 graceful abort（类比 retractfix）→
  镜像级补丁候选（B 级优先级）

### [10-14 03:00] ★★ D 类复发且定性：**65K longctx 在 pp4full 上系统性 prefill OOM**
- **场景**：longctx-65k-c1 重试期（a6d4 @03:00:17 起）；此前后 65k-c1 三连败 + 65k-c8 首败
- **首死（.31 prefill-rank1）**：`torch.OutOfMemoryError: CUDA out of memory. Tried to allocate
  **2.57 GiB**. GPU 1 ... 324.33 MiB free. Of the allocated memory **54.01 GiB** is allocated by PyTorch`
  —— **与 01:53 OOM 同一分配量（2.57 GiB）** → 同一分配点，确定性复发（推测=16K chunk 的 prefill
  激活/KV 侧 buffer；chunked=16384）
- **失败构成（65k 段）**：65k-c1 = 4bea（warmup-503 假失败）+ a6d4（03:00 OOM 杀）+ 9f31（03:24 .31
  再死，.29 两次均等 [.31]）→ **3 attempts 全非 ok 定型**；65k-c8 = 55fd（03:25，撞 churn 早败）
- **归类**：**D 类（prefill OOM）**；**与 10-10 结论矛盾**（当年 PP4 65K 单流 5.27s 为强项）——当前
  pp4full 配置（prefill mem-frac 0.82 + chunked 16384 + pp4 分片）在 65K 输入下 OOM 越界；
  pernode 同 0.82 但单节点 tp8（无 PP 分片）65K 场景 ok → **分片显存布局是关键差异**
- **复发记录（第 3 次）**：`03:48:02` .31 `OutOfMemoryError: Tried to allocate **2.57 GiB**`（GPU 0，
  642 MiB free，**54.01 GiB 已分配**＝与 03:00 完全相同数值）→ 杀 65k-c8 attempt2（b0b1，跑中全失败
  → bench IndexError on empty）。**三连同签名 = 确定性**（65K 输入 → 该分配点必 OOM）
- **修复验证（Phase 1 矩阵扩展）**：#7 预填 mem-frac 0.82→0.78 或 #8 chunked 16384→8192 后，
  **重测 longctx-65k-c1/c8 作为 D 类判据**（加入 E3/E8 验证格）
- **数据质量备注**：65k 段 4 次非 ok 中 2 次为假失败（warmup 503）→ 报告剔除/标注

### [10-14 01:53] ★ 新类 D：prefill(.31) 真实显存 OOM → 级联 + 二次 churn
- **场景**：code-c64 e335（01:52:25 起，attempt3）运行期
- **首死（.31 prefill-rank1）**：`01:53:53 PP1 TP5/TP6/TP2/TP1 Scheduler hit an exception` →
  **`torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.57 GiB. GPU 5 has a total
  capacity of 63.59 GiB of which 1.36 GiB is free. Of the allocated memory 52.90 GiB is allocated
  by PyTorch, and 3.02 GiB is reserved...`** ← **本战役首例预填侧真实 OOM（D 类）**
- **级联**：01:53:54 .29 等 [.31] `Connection closed by peer`；01:54:03 decode 侧批量
  `KVTransferError: Lost connection with prefill instance (10.66.3.29:8998)`（本批请求受影响）
- **二次 churn**：01:55:46 .29 二次异常 `CUDA error: unrecognized error code`（C 类症状）；01:55:47
  PP1 全线异常；code-c128 32fe（01:55:19 起）撞上 churn → failed；01:58 rebuild
- **归类**：**D 类（prefill OOM）**——与 A（decode 池饱和断言）/B（设备故障）区分；**潜在参数可修**：
  prefill `PD_MEM_FRACTION_STATIC` 0.82→0.78 或 `PD_CHUNKED_PREFILL_SIZE` 16384→8192（§3.1 #7/#8）
- **待定位**：是稳态负载越界还是 churn 叠加（上一代残留 + 新负载）；后续复现则 A/B 验证
- **后续（02:19-02:38，非崩溃事件链 + 假失败台账）**：02:19:24 decode `KVTransferError: Aborted by AbortReq`
  （=retractfix KV 压力优雅降级，**设计内行为**）；**假失败台账（warmup 503 `server_selection_failed`，
  撞集群恢复窗口，烧重试基数，报告须剔除/标注）**：42ec（code-c128 @02:20，17s）、**4bea（longctx-65k-c1
  @02:38，19s）**；另 10-13 boundary-c32 R2-R6 同族（5 次）。02:24/02:48 两组重建=恢复动作（.31 见 .29
  close、"0 requests affected"）。**未再出现设备故障/OOM 签名**。
- **工具改进点（累积）**：campaign 重试在集群恢复窗口内开跑（wait_healthy 与实际就绪竞态）→ 建议重试前
  加 E2E 探测双确认；恢复链不含 router 端点同步（10-13 chat 路径先例）→ 重试前 sync_router。

### [10-14 06:26] ★★ 三段定位：404 真因（非 leader 端点）+ leader :8000 僵死 + 修复执行
- **修复执行**：用户执行 router 同步三连（set env .28 + scale 0→1）✓；router 新 pod 就绪、env=.28/.35
- **但生成请求仍挂起（60s HTTP 000）→ 逐层定位**：
  1. **leader 判定更正**：bootstrap :8998 **只在 .28 响应（404=服务在听）**，.29/.31/.36 均无监听
     → **.28（prefill-dsv4-0731-0）= 引擎 leader**（此前"pod 名 -0 落点"假设实为正确）
  2. **.28:8000 HTTP 僵死**：连 /health 都不响应（code 000）、pod 已 **0/1 未就绪**（04:59 时还是
     1/1 → 05:00-06:26 间 HTTP 死亡）→ **半僵尸**（bootstrap 活、HTTP 死）
  3. **404 `prefill_not_found` 真因**：03:53 重建后 leader .29→.28 漂移；**非 leader rank 对 /health
     回 200、对 /generate 回 404**（stub 行为）→ router 指向 .29 时"假健康 + 404"
- **教训（铁律强化）**：**探测 leader 用 bootstrap :8998（或 LWS 状态），勿以 /health 判**（非 leader
  也 200）；**router env 校正后仍须发真实 /generate 探测**（本次 env 修对但 HTTP 死，仍需复活）
- **待处置**：.28 僵尸 pod 需重启复活（delete pod → LWS 重建组，~10-15min）；或提前收官直接进
  Phase 1（重建会顺带复活）——**由用户决策**；campaign 剩余（boundary×2 探针）继续烧中
### [10-14 04:08-04:28] ★★ 根因实锤：**router prefill 端点陈旧（leader 漂移未同步）→ 404 假失败批量烧尝试**
- **失败签名**：warmup `ValueError ... Not Found: {"code":"prefill_not_found","message":"Prefill server
  error (404 Not Found)"}`（bba6=65k-c8 attempt3 / 0b17+55ff=128k attempt1-2，3 次连败 17-19s 即退）
- **根因核实（04:28 实况）**：router env `PD_PREFILL_ENDPOINTS=10.66.3.29:8000`（**已死**），
  实际 prefill leader pod `prefill-dsv4-0731-0` 已漂移落点 **10.66.3.28**；decode 端点 .35 正常；
  **真实请求黑洞**（POST /generate 无响应；直连 .29:8000 /health 空）；而 router `/health` 仍 200
  （**假健康**——只证明 router 活着，不证明上游可用）→ campaign wait_healthy 因此通过 → 连续烧尝试
- **重要澄清**：04:08-04:24 的 3 连败**非** D 类 OOM（该窗口引擎侧无 OOM 记录）——**longctx 段非 ok
  构成修正**：真 OOM 杀 2 次（a6d4/b0b1）+ 9f31（03:24 .31 死）+ warmup 假失败 5 次
- **修复动作（铁律 #5：漂移必同步 router + scale 0→1）**：`kubectl set env deploy/…-router
  PD_PREFILL_ENDPOINTS=10.66.3.28:8000 PD_DECODE_ENDPOINTS=10.66.3.35:8200` + scale 0→1
  —— **10-14 04:3x 由值守执行时被权限拦截（两次），待用户执行**（命令已备）
- **教训（新增铁律候选）**：**组重建后 prefill pod 落点会变（不止 rank0 漂移）——router env 的 IP
  必须每次重建后核对**；watchdog 巡检应加一步「router env vs 实况 podIP 比对」

### [10-14 01:17] ★ B 类实锤：node35 GPU xnack（DSPARK draft 前向中）→ 整组级联
- **场景**：code-c64 b102（01:16:53 起）运行中；decode 四节点 01:17:42 同时异常
- **首死链（.35 rank0，节点侧）**：
  - `01:17:40.786 [MXC][E]xnack(0x8): kernel causes atu address translation error`
  - `01:17:41.020 mcCtxGetCurrent / mcGetDevice: Returned mcErrorIllegalAddress`（上下文毒化）
  - `terminate called after throwing 'c10::AcceleratorError: CUDA error: an illegal memory access'`
    → `Fatal Python error: Aborted`
- **py-spy dump（崩溃诊断机制有效）**：崩溃瞬间当前线程位于 **DSPARK draft 前向**：
  `dspark_draft_proposer.py:202 _run_forward ← dspark_worker_v2.py:814 _forward_decode`
  → **xnack 发生于 draft 前向执行中**。**nospark A/B 动机增强**（draft 内核路径 = 暴露面候选）
- **级联**：01:17:42 `.34/.32/.33` 全线 `Connection closed by peer [10.66.3.35]`；.34 拆除期出现
  `AttributeError: 'NoneType' object has no attribute 'server_args'`（已知第 4 候选签名；此处=启动/
  拆除竞态**次生症状**，非独立根因）
- **次生 churn**：01:19:53 重建 boot 失败 `ValueError: TP rank 2 could finish the model loading, but
  there are other ranks that didn't`（慢/失败节点）→ 01:28 再重建完成
- **归类**：**B 类**。可疑设备扩至 **.32/.34/.35**（今日均出现 mem_viol/xnack）→ **分布多节点 =
  偏向"平台/拓扑暴露面"**（跨节点 tp8 高网/卡交互）而非单卡；与 pernode 零 B 类级联对照成立
- **结论**：参数无解；运维対策=热复位（已自动化）+报修评估 + 拓扑收敛。

### [10-14 00:58] ★ B 类新签名：prefill(.29) `mcErrorDriverWarmReset` → CUDA 失效 → 重启循环
- **场景**：code-c64 7a64（00:56:23 起）运行中失败；prefill 组 00:58 起重建（48s 时 0/1 boot 中）
- **首死链（.29 prefill-rank0.log）**：`00:57:56.563 [MCR][E]mc_runtime_api.cpp:215 mcGetDevice:
  Returned mcErrorDriverWarmReset`（**驱动级暖复位窗口**）→ 其后任意 CUDA 调用即崩：
  `torch.AcceleratorError: CUDA error: unrecognized error code`（栈=get_device_capability→current_device）
- **重启循环**：`[LWS] group start` 00:58:09 / 00:58:13 / 00:59:34 / 00:59:39 / 00:59:53 ×5；
  期间 `cudaGetDeviceCount ... Error 10002` + `RuntimeError: Unexpected error from cudaGetDeviceCount()`
  —— 设备复位完成前引擎无法初始化
- **归类**：**B 类（驱动/平台）**，新签名 **mcErrorDriverWarmReset**（引擎视角：GPU 处于暖复位窗口，
  CUDA 全不可用）；与 00:38-00:44 .32/.34 自动复位（ResetType=1）同族 = campaign gpu_reset 自愈链
- **结论**：参数无解；等复位完成后 LWS 自动恢复。**待观察**：warm reset 触发源（设备自愈 vs campaign
  自动化）——下轮巡检核 recovery 日志。

### [10-14 00:35] ★ B 类实锤：node32 GPU mem_viol（低负载）
- **场景**：rag-c128 attempt3（e0c0，00:22 起）运行中；**负载极低**（.34 视角 running-req 4-6、
  full 池 0.07-0.11、swa 0.13-0.43、accept rate 0.84-0.92 正常服务中）
- **首死链（.32 自身日志，node32 pod rank2）**：
  - `00:33:44 [MXC][E]xnack(0x8): kernel causes atu address translation error`（前兆）
  - `00:35:07.484 [MXC][E]memory violation(0x4)` ← **首死根因**
  - `00:35:09 DP2 TP4 Scheduler hit an exception`（request_receiver broadcast 处挂）
- **级联（.34 视角）**：`00:35:10 DP1 TP3/TP2 ... all_gather → Connection closed by peer [10.66.3.32]`
- **收尾**：campaign 自愈触发 **GPU 复位**（.34 日志 `00:38:30-00:40:01 MX_EVENTTYPE_DRIVER ResetType=1,
  ResetCause=1 ×7`）→ 组重建
- **结论**：**纯平台故障，参数无解**；.32 加入可疑卡名单（与 .34 并列）。

### [10-13 20:02] ★ A 类实锤：c160 池饱和 → KV-full 断言
- **场景**：standard-c160 attempt2（3e8c，20:00 起）；offer 160 vs cap 64 → 深度排队
- **饱和过程（.34 rank1 日志）**：20:01:56-20:02:07 running 8→16、full 池 0.87-0.90、
  **swa 池 0.74→1.00**、prealloc 14 单、transfer 25 单、retract 风暴
  （`KV cache pool is full. Retract requests. #retracted_reqs: 0` ×连续，new_token 256/512 持续释放）
- **首死（.34）**：`20:02:07 DP1 TP2 Scheduler hit an exception` →
  `decode.py:1761 _pre_alloc → assert kv_loc is not None`：
  `available=0, evictable=0, protected=0, required_alloc=4096, page_size=256`
- **级联（.35 视角）**：20:02:08 MLP-sync all_gather `Connection closed by peer [10.66.3.34]`
- **结论**：**负载型可缓解**——池无余量时新请求预分配 4096 token 失败即崩（"Bug in memory estimation"=
  引擎未预留安全余量）。**非 retract 死锁**：ratio 持续上升，说明完成请求在正常释放；
  `#retracted_reqs: 0`= 无**可回收**候选（运行请求均在生成中无可 retract）。
- **参数杠杆**：见 §3。

### [10-13 17:01 / 16:19] B 类：.34 GPU0 平台故障（既有记录）
- 17:01:55 standard-c16 attempt2 崩；**node34 dmesg 实锤 GPU shader exception：B900@09:00.0 mem_viol(0x4)**
  （同卡 10-06 曾 xnack 0x8）→ 首故障设备=.34 GPU0；16:19 崩溃同族（8 decode ranks 2s 内 gloo 全灭）。
- 备注：两次均在**低并发档**（c16@rate2）发生 → 与负载无关，B 类定性依据之一。

### [10-13 20:57 / 22:59] B 类：.34 二次故障
- `20:57:34 mem_viol(0x4)` / `22:59:25 xnack(0x8)`（.34 rank1 MXC 日志，chat/rag 时段）→ .34 全天多发。

### [10-13 09:12-12:57] decode 组 ≥9 次重建（汇总）
- 集中期每次伴随 `Capture cuda graph failed: CUDA error: unrecognized error code`
  （`decode_cuda_graph_runner.py:685`）——**按 C 类定性：症状**（前置于其的 B 类设备故障毒化上下文，
  或捕获期的大 shape kernel 触发故障）。**图捕获预捕（graph-bs）实验价值待验证**（见 §3）。

### [10-13 14:50-15:10] ⚠ 非本形态：f4（tp32）形状崩溃（记录以正视听）
- `Capture cuda graph failed: shape '[336, 0, -1]' is invalid`（deepseek_v4.py:1190）——
  f4 全量形态的 `o_groups=8 < tp=32` 硬约束（已终止，**与 pp4full 无关**；.34 rank1 累积日志中的
  此签名属 f4 时代，勿误归因）。

---

## §3 参数优化评估（对 decode 崩溃次数的作用）

### 3.1 可用杠杆（全部为**现存**配置项；逐项 A/B 验证）

| # | 杠杆 | 现值 | 候选值 | 目标类 | 机理 |
|---|---|---|---|---|---|
| 1 | `PD_CUDA_GRAPH_MAX_BS`（decode） | **未设**（厂商默认 8） | ≥128 | C 子类 | 预捕获大图，绕开运行期扩展捕获路径（pernode 已验证 graph-bs>cap 规则可行） |
| 2 | `SGLANG_DISAGGREGATION_THREAD_POOL_SIZE` | 24 | 12 | A | **现存最接近"限制传输并发"的旋钮**（平滑 KV 流入，防 prealloc 瞬时堆积） |
| 3 | `PD_MEM_FRACTION_STATIC`（decode） | 0.75 | 0.78 | A | 池增容 → 饱和门槛后移（需核对权重加载余量） |
| 4 | `PD_SCHEDULE_CONSERVATIVENESS` | 0.6 | 0.8 | A | 准入更保守，削池尖峰 |
| 5 | `decode_max_running_requests_specific` | 16/rank（总 64） | 12/rank（总 48） | A | 降池水位（代价：吞吐/TTFT；与本形态 c64+ 边界一致方向） |
| 6 | `PD_SPECULATIVE_ENABLE=0`（nospark） | 1 | 0 | A/C | 去 DSPARK 图/显存开销，隔离其贡献（变体目录已备） |
| 7 | `PD_MEM_FRACTION_STATIC`（**prefill**） | 0.82 | 0.78 | **D** | 预填显存预算下调 → 防 OOM（代价：KV 池略小） |
| 8 | `PD_CHUNKED_PREFILL_SIZE`（**prefill**） | 16384 | 8192 | **D** | 削单批 prefill 激活峰值（0.5.12 时代 4096 最快；本形态需 A/B） |

### 3.2 对既有诊断意见的校正（10-14 用户提供的日志分析）

| 意见 | 评估 |
|---|---|
| prealloc/transfer 堆积、池 0.95+、MLP-sync 级联、.34 反复出现 | ✅ 观察正确（本报告 §2 复现） |
| "Retract 死锁" | ❌ 更正：retract 在推进（ratio 持续上升）；`#retracted_reqs:0`=无候选，真正断点是 **prealloc 无余量断言** |
| "Gloo 超时被 retract 阻塞" → 建议 `dist-timeout=180000` | ❌ **无效**：报错为 `Connection closed by peer`=**对端进程死亡**（pair.cc:547），非超时。放宽 dist/watchdog 不解决进程死亡 |
| `--disaggregation-decode-max-transfer-concurrency=8` | ❌ **本 build 无此旗标**（server_args 检索无果）→ 用杠杆 #2 等效替代 |
| `--max-running-requests=32` | ⚠️ 语义映射：#5（per-rank 16→12/8）；方向认可（降水位），代价明确 |
| `--mem-fraction-static=0.70` | ❌ **方向相反**：A 类=池被耗尽 → 应**增大**池（#3 ↑0.78），0.70 会更早饱和（本案无硬 OOM 证据） |
| `--enable-overlap-schedule=False` | ⚠️ 低优先级；崩溃证据不支持 overlap 特定交互；可列为后期 A/B |
| "排查 Prefill 节点 .34" | ✅ 已执行并升级为**双侧首死判定**（.34/.32 均为 B 类多发节点） |

### 3.3 参数**无法**解决的部分（必须明示）

- **B 类平台故障（mem_viol/xnack）占比最大且任意负载触发** → 参数无解。对策：
  ① 热复位（campaign gpu_reset 已自动化，10-14 00:38 实测生效）；② .32/.34 报修评估；
  ③ **拓扑选择**——pernode（每节点独立引擎）当日**零 B 类级联**（跨节点 tp8 放大暴露面）。
- 因此本战役对 decode 崩溃的最终答案：**A/C 类可压降（实验矩阵见 §4），B 类靠运维+拓扑收敛**。

---

## §4 修复实验矩阵（pp4full 收官后 Phase 1 执行）

| 步骤 | 变更 | 重测 | 判据 |
|---|---|---|---|
| E1 | #1 graph-bs≥128 + 废弃 env 对齐 + decode_ep 对齐 | standard-c64/c128 + chat-c128 | 图捕获类崩溃消失/减少 |
| E2 | E1 + #2 thread-pool 24→12 | 同上 + c160 | A 类断言减少 |
| E3 | E2 + #3 mem-frac 0.78 | c160 ×2 | 池饱和推迟 |
| E4 | E1 + #5 cap 16→12/rank | c128/c160 | 边界档存活率 |
| E5 | nospark A/B（`--side pp4full-nospark` @c128） | chat-c128 | DSPARK 贡献隔离 |
| — | 每步间复位 .32/.34（热复位协议=10-13 17:30 版；先停 campaign 防烧重试） | — | — |

**记录义务（用户指令）**：本战役此后**每次崩溃**按 §2 格式追加至本文档（时间/场景/首死方/签名/
证据行号/归类/结论），并在 next-actions 巡检记录中引用。

---

## §4.5 部署切换陷阱（10-14 实测发现，重要运维知识）

**现象**：pernode↔pp4full 形态切换（LWS `replicas/size` 拓扑变更）时，`kubectl apply -k` 后
**滚动更新死锁**：LWS RollingUpdate 按组逐个更新——新组（1/2/3）先创建但 **Pending**（旧组 0 的
size=4 遗留 pod 占着全部 GPU），旧组却要等新组 ready 才删 → 互锁 21 分钟零进展；
**footprint 脚本的 wait_ready 被旧 pod 的 ready 状态骗过**（误报 8/8 就绪），footprint 实测打到了旧
pp4full 部署上（签名：`graph adjusted to 16` = pp4full-fixed 专属；pernode 应为 adjusted 48）。
**判据**：切换后 25 秒就"ready" = 必然没切换成功（真切换冷启动 ≥10min）。
**解法**：`kubectl delete pod` 两侧旧 group-0 全部成员（8 个）→ GPU 释放 → pending 新组秒调度 + group 0
按新 spec 重建 → 正常 boot。
**预防（脚本改进项）**：切换脚本应在 apply 后比对 LWS `observedGeneration`/`updatedReplicas` 或
检查 pod age，而非只看 ready 计数。

## §5 取证命令速查

```bash
# 首死判定（双视角：谁等谁 → 被等方=首死）
kubectl -n metax-ai-pd exec decode-dsv4-0731-0-{1,2,3} -- bash -c \
  'cd /var/log/sglang-dsv4-0731/; grep -nE "Connection closed by peer \[|Scheduler hit an exception" decode-rank*.log | tail'
# 设备故障签名（B 类）
kubectl -n metax-ai-pd exec <pod> -- bash -c \
  'grep -nE "\[MXC\]\[E\]|memory violation|xnack|ResetType" /var/log/sglang-dsv4-0731/decode-rank*.log | tail -20'
# 节点侧 dmesg 实锤（需 ssh 跳板）: ssh ubuntu@<node-ip> 'sudo dmesg | grep -iE "mem_viol|xnack|B900" | tail'
# 池压力轨迹（A 类）：grep "KV cache pool is full\|full token usage\|swa token usage"
```
节点映射：decode pod `-0`=.35 / `-0-1`=.34 / `-0-2`=.32 / `-0-3`=.33。
