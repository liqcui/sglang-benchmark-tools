# TEST-REPORT — 0731 (0.5.14) 全面对比文档：PP4(官方脚本) vs PP1(最优解)

> 多维度终版对比。数据来源：2026-10-10 全场战役（性能/稳定性/终扫）+ 2026-10-08~10 累积
> （cap 调优 / L2 / retract 修复 / rc3 历史）。
> 本文件为**总纲文档**；各专项细节见对应分报告（见 §10 索引）。**全部数据已回填。**

---

## §0 对比对象与口径

| 维度 | PP4 侧 | PP1 侧 |
|---|---|---|
| 定位 | **沐曦官方脚本推荐默认拓扑** | **本项目最优解**（生产候选） |
| 启动模式 | official（镜像内 llm-launch 脚本） | explicit（pd-launcher 直启） |
| prefill 拓扑 | **pp4 / tp8 / dp1**（tp=32/pp） | pp1 / tp8 / dp4 |
| decode 拓扑 | pp1 / tp8 / dp4 / ep1 + DSPARK block6 | 同左（对齐） |
| 并发语义 | decode specific=16 ×dp4=64 总 | PD_MAX_RUNNING_REQUESTS=64 直传 |
| 镜像 | `…-dsv4-0731-llm-retractfix`（46db2551） | `…-dsv4-0731-retractfix`（fb9cff47） |
| 模型/参数 | LLMC W8A8 / 400K / chunked 16384 / L2 off | 同左 |

⚠ **口径声明**：核心对比**捆绑了「启动模式 + prefill 拓扑」两个变量**（官方形态 vs 最优解形态）。
交叉控制组（PP4-explicit / PP1-official）未跑，列为后续项。凡「两侧差不多/差很多」的结论
均指「官方形态 vs 最优解形态」整体，不对模式/拓扑单独归因。

### §0.1 受控性审计：实测命令行逐项 diff（2026-10-10 复核）

四方实际生效命令（PP1 显式 prefill/decode + PP4 厂商 prefill/decode）已从节点日志拉出逐项比对：

| # | 项 | PP1-explicit | PP4-official | 评估 |
|---|---|---|---|---|
| 1 | prefill 拓扑 | pp1 / tp8 / dp4 (+dp-attn×2) | pp4 / tp8 / dp1 | **设计变量** |
| 2 | 启动模式 | explicit 直拼 | official 厂商脚本 | **设计变量**（与 #1 捆绑） |
| 3 | decode `chunked-prefill-size` | 16384 | **65536** | ⚠ 未对齐（厂商默认快照差异；decode 侧含义弱，量级影响待估） |
| 4 | `watchdog-timeout` | 300（默认） | **3600** | PP4 更宽容（若影响，方向对 PP4 有利） |
| 5 | prefill `disaggregation-ib-device` | mlx5_0 | **mlx5_0,mlx5_1** | ⚠ KV 传输单轨 vs 双轨 |
| 6 | `dist-timeout` | 300 | 30000 | 仅启动容忍度，服务期无影响 |
| 7 | `cuda-graph-max-bs` | 请求 40 | 请求 64 | **实际等价**：两引擎均按 max_running/4 自动调整为 **16**（cap64 实测） |
| 8 | 镜像 | `-retractfix` | `-llm-retractfix` | llm-launch 脚本补丁 + 休眠 modelslim 补丁；引擎热路径等价 |
| 9 | 其余 20+ 项 | — | — | **全部一致**：模型路径/tp8/context 400000/kv bf16/prefill chunked 16384/mem-fraction 0.82、0.75/max-running 64/ep1/DSPARK block6/lb round_robin/… |
| 10 | 运行态 | 常规 | 崩溃-重建冷组（对 PP4 不利）、时段差 | 噪声级 / 单向 |

**受控性结论**：
- 本对比 = **「官方形态 vs 最优解形态」的方案级对比**（2 个设计变量 + 4 处工程性未对齐项，
  其中 #4/#6 对 PP4 中性偏有利、#10 对 PP4 不利）；
- **不是纯拓扑 A/B**。纯拓扑归因需「对齐复测」（#3/#5 拉平后重跑核心格）——列为可选后续项；
- 稳定性主结论（16/24 尝试失败 vs 1/9）对上述未对齐项**不敏感**（#4 方向相反都救不回）。

---

## §1 维度一：单请求性能（single request）

> 场景：single-deepseek-standard 裁剪版（5 条 × 4K in / 2K out，串行）+ single-longctx
> 裁剪版（5 条 × 65K in / 8K out，串行）。

| 指标 | PP4 | PP1 | 胜者 |
|---|---|---|---|
| 4K 单流 out tok/s | 86.6 | **104.0** | PP1（+20%） |
| 4K 单流 mean TTFT | 4.87s \* | **0.65s** | **PP1（7.5x）** |
| 4K 单流 mean TPOT | 9.2ms | 9.3ms | 持平（引擎单步能力相同） |
| 65K 单流 out tok/s | **100.7** | 90.2 | PP4（+12%） |
| 65K 单流 mean TTFT | **5.36s** | 14.09s | **PP4（2.6x）** |
| 65K 单流 mean TPOT | 9.3ms | 9.4ms | 持平 |

**结论：单流场景两者分工明显**——
- **PP1 短请求占优**：4K 单流 TTFT 0.65s（PP4 的 PP 流水线对短 prefill 有跨节点 bubble）；
- **PP4 长上下文占优**：65K 单流 TTFT 5.36s vs 14.09s（PP 流水线把长 prefill 切 4 段并行，
  收益显著）；两侧单流 TPOT 完全一致（9.2-9.4ms）→ **每步解码能力与拓扑无关**。
- ⚠ \* PP4 的 4K 读数 p99=17s（含冷启动成分，n=1）；即便取最乐观修正，短请求方向仍归 PP1。
  65K 读数两侧 p99 紧凑（6.7s vs 15.8s），结论稳健。

历史参照（PP1 @cap10 首轮 battery）：4K 单流 70.2 tok/s / TTFT 1.24s；65K 单流 70.0 tok/s /
TTFT 13.3s。本终扫 PP1 单流吞吐更高（104/90）——系裁剪版条数少 + 组长时间预热之故。

---

## §2 维度二：吞吐-时延曲线与最优请求速率（rate 阶梯）

> 场景：conc-64-deepseek 固定 200 prompts / 4K in / 2K out，**速率阶梯 rate {1, 2, 4}**
> （需求 = rate×4096 in-tok/s：4.1K / 8.2K / 16.4K；服务侧实测上限 ~7.3K）→ 找吞吐平台 +
> TTFT 膝点 = **最合理请求速率**。

| rate (req/s) | 需求 in-tok/s | PP4: out tok/s / TTFT / TPOT | PP1: out tok/s / TTFT / TPOT |
|---|---|---|---|
| 1 | 4.1K | 1780 / 1.64s / 17.9ms | 1835 / 1.73s / 16.7ms |
| 2 | 8.2K | **崩溃**（重试×2 均非 ok） | **2028 / 3.02s / 22.6ms** 🏆 |
| 4 | 16.4K | 1480 / **21.6s** / 22.2ms（队列膨胀） | 1996 / 5.29s / 22.5ms |

**结论（最合理请求速率）**：
- **PP1（4K/2K 档）= 2 req/s**：吞吐到达平台顶（1835→2028，之后再无提升：1996），
  TTFT 仍仅 3.0s（p99 6.2s）；rate4 起 TTFT 翻倍（5.3s）而吞吐反降 → 超配无益。
  该点即 **engine 服务容量**（≈8.2K in-tok/s ≈ 2 req/s），与 §0 预判一致。
- **PP4 = 1 req/s 都勉强**：rate2 直接崩溃（不稳定区），rate4 虽成功但 TTFT 21.6s
  （PP4 服务容量更低 + 调度更差）。

---

## §3 维度三：并发能力与最大安全并发（concurrency 阶梯）

> 场景：rate=5 饱和档（20.5K in-tok/s 需求，2.8x 超配——对齐历史崩溃边界协议的到达强度），
> 并发阶梯 conc {96, 128, 160}。

| conc | PP4: 状态 / out tok/s / TTFT | PP1: 状态 / out tok/s / TTFT |
|---|---|---|
| 64（战役参照） | 4/8 格干净；1675 / 11.15s | 100%；1877-2112 / 5.95-7.41s |
| 96 | ok（重试后）；1677 / 17.26s | ok（重试后）；1675 / 9.89s |
| 128 | **崩溃**（重试×2，256/300 失败） | **ok；2179 / 11.27s**（零失败，吞吐峰值） |
| 160 | ok（随机性）；1608 / 25.31s（p99 TPOT 230ms） | **ok；2111 / 13.35s / p99 27.9s（零失败）** |

**最大安全并发（饱和档 rate5 = 20.5K in-tok/s，2.8x 超配）**：
- **PP1 ≥ 160**（96/128/160 全零失败；**最大实测吞吐 2179 tok/s @128**，160 档仍 2111）；
  本轮未测 >160，安全边界留有余量。
- **PP4 ≈ 96**（128 档确定性崩溃）；且即使成功的档位 TTFT 也比 PP1 差 1.5-2x。

---

## §4 维度四：稳定性与故障画像

### 4.1 战役统计（10-10，直接来自 bench 结果，两侧同负载同套 8 格）

| | PP4-official | PP1 |
|---|---|---|
| 总尝试次数 / 非 ok | **24 / 16（67%）** | 9 / 1 |
| 从未取得干净数据的格 | mini-128c、conc-64r2 | — |
| 崩溃频率（相对） | **≈6-10x** | 1x |

### 4.2 故障族谱（两侧共有，PP4 放大）

```
进程异常退出（首死者签名：AttributeError: 'NoneType' object has no attribute 'server_args'）
 → gloo 集合通信级联（"Connection closed by peer"）
 → LWS RecreateGroupOnPodRestart 整组重建
 → （次生）leader 漂移 → router 陈旧 → 全站 503
```
- 该签名同样出现在 PP1-explicit 历史事件（10-09 01:28）→ **引擎广泛竞态，非 PP4 专属**；
  列为第 4 个引擎缺陷候选（前三个：retract→SWA offload【已修】、func_timer dedup【已修】、
  DSPARK cap160 boot【已定位未修】）。
- PP1 侧另有 KV-pool 断言类崩溃 2 次（cap64 重载下，`KV cache is full! Bug in memory
  estimation`）——第 5 个候选缺陷。

### 4.3 崩溃-拓扑相关性（证据）
PP4 侧每次尝试 ≈ 一次崩溃；PP1 侧 8 格仅 1 次。结合 rc3 时代 pp4 的崩溃史（128c 等），
**pp4-prefill + PD 的组合在本集群显著放大竞态触发**。根因未定位。`后续项`

---

## §5 维度五：运维与工程（官方形态 vs 最优解的工程性对比）

| 项 | PP4-official | PP1-explicit |
|---|---|---|
| 启动链路 | 厂商脚本 4 层（config.sh/spec/prefill.sh/decode.sh）+ launcher 回写 | 单层 launcher 直拼 argv（可审计） |
| 参数确定方式 | 厂商默认查表 + env 回写（改一项需理解回写链） | env → argv 一一对应 |
| 本轮踩坑 | ① `get_pp_layer_partition` 适配遗漏（待固化）② 官方并发语义（specific×dp）③ pp4 需双变量回写 | 无新增（参数面已全量验证） |
| leader 漂移 | 两侧通用（组重建后必同步 router，脚本已内置自愈） | 同左 |
| 升级/回切成本 | 切换 = `kubectl apply -k` 对方目录（同名资源就地滚动，~8min） | 同左 |
| 可观测性 | 日志经 tee 落 hostPath（同左） | 同左 |

---

## §6 维度六：测量方法论与场景审计（本轮沉淀）

1. **场景合理性审计**（详见附表）：
   - single 场景条数过重（50/20 条）→ 用 `--set num_prompts=5` 裁剪；
   - conc-* 设计为**超配探针**（2.2-3.4x），其 TTFT 反映排队而非服务时延（保留作高压档）；
   - `--set` 支持任意字段覆盖 → rate/conc 阶梯无需新增 scenario key。
2. **测量学铁律**（本系列实锤）：
   - 输出长度方差 ±25%（ignore_eos 端到端未生效）→ 吞吐必须多次取中位；
   - roll 后首跑有 JIT/冷启动伪影（冷 9.0s vs 热 2.8s）→ 先丢弃一轮；
   - mini-64c TTFT 同配置波动 ±15%；
   - **leader 漂移自愈**必须内置（缺省曾致 5h 假死）。
3. **工具缺陷待修**：sglbench forensics stale 捕获（不同事件快照 md5 相同）。

---

## §7 维度七：横向参照（同引擎历史维度一览）

| 维度 | 结论 | 报告 |
|---|---|---|
| cap 调优（10/40/64/80/160） | 40=稳 / 64=TTFT 优 / 160=引擎 boot 缺陷不可用 | battery + sweep |
| HiCache L2 | 随机 prompt 无效应；多轮稳态 +10-30%；早期尖峰=热身伪影 | hicache-l2-0731 |
| retract 修复 | retractfix 镜像闭环：1901.5 tok/s（2.88x vs cap10）、零崩溃 | battery + memory |
| vs rc3 历史 | 0.5.14 吞吐全面更高（+4~57%）；pp4 弃用判断再次验证 | l2-full-comparison + 本文件 |

---

## §8 最终推荐（操作点标定）

> **2026-10-12 晚修订**：pernode PD（见 §10）全面超越本节的 PP1-explicit 推荐——
> **最佳 PD 配置已更新为 `deepseek-v4-flash-0731-pernode` + 每引擎 cap48 + DSPARK on**。
> 本节保留作为 pernode 之前的基线标定（仍适用于显式/官方两种旧形态）。

**总体（旧形态基线）**：**PP1-explicit = 旧形态中的主推荐路径**；PP4-official 按场景分工保留
（65K 单流 / ≤32c 短请求轻载）。

### PP1 最优操作点（4K/2K 工作负载档，cap64）
| 目标 | 参数选择 | 达成值 |
|---|---|---|
| **稳态最优（时延/吞吐平衡点）** | **rate = 2 req/s**（conc ≤64） | **TTFT 3.02s / TPOT 22.6ms / ITL 17.5ms / 2028 tok/s** |
| 极限吞吐 | rate 5 + conc 128 | **2179 tok/s**（TTFT 11.3s / p99 21.5s）；conc160：2111 tok/s / TTFT 13.4s / p99 27.9s |
| 最大安全并发 | **≥160**（rate5 饱和档零失败；>160 未测，留余量） | |
| 单流 4K | — | TTFT **0.65s** / 104 tok/s / TPOT 9.3ms |
| 单流 65K | — | TTFT 14.1s / 90 tok/s / TPOT 9.4ms |
| cap 档位 | 稳定优先 40 / 时延优先 64（§7） | |

### PP4-official 适用边界
- **适用**：65K 级长上下文单流（TTFT -62%）；≤32c 短请求轻载（mini-32c TTFT 2.88s）。
- **不建议**：≥64c 或 rate≥2（崩溃频率 6-10x，128c 确定性崩溃；rate2 直接崩）；
  64c 档 TTFT（11-18s vs 4-7s）与吞吐（-15~25%）双双落后。
- 若必须启用：rate ≤1 req/s、conc ≤96、并接受「崩溃-重建-漂移自愈」的运维方式。

### 一句话答复
**短请求为主、需要高并发与稳定 → PP1（本集群最优解）；长上下文（65K 级）单流为主 → PP4 官方
脚本形态有独特优势；混合业务 → 以 PP1 为主。**

---

## §9 pernode PD —— 终版最佳配置（2026-10-12）

### 9.1 动机与结构
单机探测（官方镜像 tp8 节点内、非 PD）实测单流 **210-226 tok/s / ITL 4.5ms**，而跨节点 PD
（每节点仅 2 卡）单流 104 / ITL 9.3ms → **跨节点张量并行是 PD 单流的系统性瓶颈**。
`deepseek-v4-flash-0731-pernode/`：**4+4 台每节点自包含 tp8/dp1 引擎**（LWS replicas=4 / size=1），
prefill↔decode 逐请求配对（router 端点各 4 项）。

### 9.2 终版标定（P1→P4 战役，40+ run 全部 sglbench 落库）
| 参数 | 终版值 | 依据 |
|---|---|---|
| 每引擎 `max-running` | **48**（×4=192 总） | P4：160 并发零失败 TTFT 2.0s；16→TTFT 崩（队列），32→conc64 略优但 160 无余量 |
| `cuda-graph-max-bs` | **64**（须 > max-running 触发 bs 重建） | §10.4 引擎缺陷定位 |
| DSPARK | **on**（block 6） | P2 实测收益巨大：单流 TPOT 5.5 vs 16.6ms（3x）、吞吐 2.5x、b128 TTFT 5.9x |
| 其余 | 同基线（retractfix 镜像 / L2 off / chunked 16384 / 4K 档） | — |

### 9.3 终版对照（pernode-c48 vs 老跨节点 PD，同场景同负荷）
| 场景 | pernode c48 | 老 PD | 优势 |
|---|---|---|---|
| mini-32c | 2.06s / 10.2ms | 4.68s / 16.5ms | TTFT 2.3x / TPOT 1.6x |
| mini-128c | **0.95s** / 9.6ms | 1.70s / 16.1ms | 1.8x / 1.7x |
| conc-64 | **2.16-2.38s** / 21.6ms | 5.95-7.41s / 20.1ms | **TTFT ~3x** |
| conc-128 | **1.48s** / 18.2ms | 12.84s / 17.3ms | **TTFT 8.7x** |
| conc-160 @r5 | **2.00s** / 18.9ms / **3354 tok/s** | 13.35s / 15.6ms / 2111 | **6.7x / +59%** |
| rate 2（最优稳态） | **1.05s** / 14.8ms / 2903 | 3.02s / 22.6ms / 2028 | 2.9x / 1.5x / +43% |
| **吞吐峰值** | **3354 tok/s** | 2179 tok/s | **+54%** |
| 单流 TPOT/ITL | **5.5ms**（DSPARK） | 9.3ms | **1.7x**（节点内 tp8 红利） |
| 单流 TTFT | ~0.8s（nospark 基准；3.71s 读数为热身伪影*） | 0.65s | ≈ 持平 |

\* 遵循测量学铁律：battery 首格受 roll 后 JIT 冷启动影响（同组晚测 nospark 版 0.82s），
以暖态为准。

### 9.4 收益与缺点（用户要求专项）

**收益（Pros）**：
1. **吞吐峰值 3354 tok/s（+54%）**；160 并发零失败（TTFT 2.0s / p99 8.6s）。
2. **TTFT 全面碾压**：并行场景 2-9x 改善（100+ 并发档差距最大）。
3. **每步解码 1.5-2x**：ITL 5.5ms vs 9.3ms——每请求底层速度质变（单机同源验证）。
4. 4+4 引擎**互为热备**：单引擎重启影响面 1/4（无整组连带）。
5. 启动/重启无需跨节点 rendezvous（各引擎自包含）。

**缺点（Cons）**：
1. **长上下文（65K）单流 prefill 退化**：无 PP 流水线 → 13.0s vs pp4-official 的 5.3s
   （纯长单流场景 pp4 仍为特解）。
2. **引擎数 ×2（8 台）**：运维面变大；per-engine 参数语义（max-running 需 < graph-bs）有陡峭陷阱（见下）。
3. 单引擎故障时在途请求丢失（router 重试兜底）；配对质量依赖 router（本集群实测分配均衡 ✓）。
4. 参数标定非平凡：cap16→队列崩、cap32/48→稳（P1-P4 才收敛）。

### 9.5 顺带钉死的引擎缺陷（第 3 缺陷完整定位）
```
--cuda-graph-max-bs 一旦给定即清空 decode bs 列表, 指望 _recompute 重建;
而 _recompute 仅在 adjusted ≠ requested 时重建
  (adjusted = min(requested, max_running[/dp]))
→ dp1 下 min(req,mr) ≡ req → 永不重建 → bs=None → DSPARK draft sampler 崩 (TypeError)
```
- 解释两起事故：cap160 boot 崩（40==min(40,160/4)）；pernode cap16/dp1 boot 崩（40==min(40,16)…实际触发同模式）。
- **规避规则（已写入本部署）**：`cuda-graph-max-bs > max-running`，强制触发列表重建。
- 修复候选：`_recompute` 在 `bs is None` 时也重建；建议上游。

### 9.6 终版推荐结论（取代 §8）
| 场景 | 最佳配置 |
|---|---|
| **常规/短请求/高并发（默认）** | **pernode + cap48 + DSPARK on** |
| 65K 级长上下文单流为主 | pp4-official（TTFT 5.3s）或 pernode（13s，若可接受） |
| 极简/低并发 | 单机（无 PD 开销时最快，224 tok/s 单流） |
| 旧形态保留 | PP1-explicit（cap64）为已知稳定基线/备选 |

## §10 分报告索引

1. `TEST-REPORT-2026-10-08-dsv4-0731-first-battery.md`（首轮 battery + retract 修复）
2. `TEST-REPORT-2026-10-08-hicache-l2-0731.md`（L2）
3. `TEST-REPORT-2026-10-10-pp4-vs-pp1.md`（PP4 vs PP1 主对比）
4. 本文件（多维度总纲）

## 附录 A：终扫执行

```bash
# rate 阶梯
python3 -m sglbench run --scenario conc-64-deepseek --exec kubectl --label dsv4-0731-final-pp1-rate2 \
  --env-file envs/dsv4-0731-pp1.json --set model=DeepSeek-V4-Flash --set tokenizer=/workspace/data/DeepSeek-V4-Flash-0731-W8A8 \
  --set request_rate=2.0
# 并发阶梯 (rate5 饱和档)
python3 -m sglbench run --scenario conc-64-deepseek --exec kubectl --label …. conc96 \
  … --set max_concurrency=96 --set request_rate=5.0
```
