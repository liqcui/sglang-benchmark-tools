# GAP-ANALYSIS — 0731 PD 体系剩余缺口与优化机会（2026-10-15）

> 基座：三拓扑全网格（4P4D/3P5D/2P6D）、安全边界阶梯、L2 A/B、体感电池、修复战役、双 4P+4D 体感对照。
> 所有"未测"均未产生数据（勿与"已测出劣势"混淆）。

## A. 测试覆盖缺口（按优先级）

| # | 缺口 | 现状 | 预计价值 |
|---|---|---|---|
| A1 | **3P5D/2P6D 的 rate 阶梯与长上下文** | 3P5D 仅 r@c64 与 65K-c1；2P6D 无 rate 扫、无 longctx | 高——定 3P5D 容量点与 65K 表现 |
| A2 | **2P6D 边界上探（rate8、c256+）** | 只到 c240@r5 | 中（2P6D 已判批处理向） |
| A3 | **3P5D 官方默认参数变体**（已备未跑） | `pernode-3p5d-default/` 就绪 | 高——回答"对齐参数 vs 默认"在 3P5D |
| A4 | **L2 在 3P5D/2P6D** | L2 仅 pernode 4+4 测过 | 中（多轮冷热转换收益可否叠加） |
| A5 | **ib 双轨 / nospark A/B** | 未测（候选挂起） | 中 |
| A6 | **longctx 128K/200K @3P5D** | 仅 65K-c1 | 中（200K 在 3 prefill 的排队） |
| A7 | **pernode 200K context 档** | 仅官方默认档测过 ctx200k | 中 |
| A8 | **单阶段诊断（prefill/decode-only）@3P5D** | pp1x 上 prefill-only 三连败未解 | 中 |
| A9 | **长时间 soak（≥2h 连续混合负载）** | 无 | 高（发现慢漂移/泄漏/长尾累积） |
| A10 | **崩溃恢复 RTO/RPO 量化** | 未测（组重建耗时、在途丢失率） | 高（生产 SLA 必需） |
| A11 | **EPP 网关路径终版复测** | 未测（旧形态测过） | 低-中 |
| A12 | **保活器 27min 级长闲置验证** | §6.2 结论待守候窗实测 | 中 |
| A13 | **多 client/多 router 并发** | 未测 | 低 |

## B. 参数优化机会（sglang/引擎，均未做 A/B）

| # | 参数 | 现状 | 方向 | 依据/风险 |
|---|---|---|---|---|
| B1 | decode cap 48/引擎 | 4+4/3P5D/2P6D 全 48 | **3P5D 上试 56/64**（decode 侧变强） | cap 阶梯已证明 48 全包络最优于 4+4；3P5D decode 是强侧可再探 |
| B2 | prefill max-running 64 | 全形态 | **3P5D 上提 80/96**（prefill 是短板侧） | 直接对 A1/B 短板 |
| B3 | chunked-prefill 16384 | 全形态 | 8192 | 长输入 TTFT；官方默认测出 4096 时崩（参数重要性） |
| B4 | schedule_conservativeness 0.6 | 全形态 | 0.8 | 准入平滑，中收益 |
| B5 | **ib 双轨**（mlx5_0,1） | 单轨 | 双轨 | KV 传输带宽↑；包络报告 §6 老候选 |
| B6 | swa_full_tokens_ratio 0.1 | 全形态 | 0.05/0.15 微调 | 低 |
| B7 | DSPARK block 6 | 全形态 | 4/8 | accept 0.78-0.83，微调 |
| B8 | mem-frac prefill 0.82 | 4+4/3P5D/2P6D | 0.78 长ctx 档 | pp4full 上 D-fix 实证；pernode 4+4 上 decode 0.78 无收益（A/B-1）→ prefill 侧未验 |
| B9 | **graph-bs 64 vs 128** | 64 | 128（3P5D） | pp4full 上 graph-bs128 修过图捕获；pernode dp1 需 >cap 即可 |
| B10 | router cache-aware threshold 0.3 | 默认 | 0.2/0.5 | 前缀亲和 vs 均衡权重 |
| B11 | tokenizer/detokenizer worker 数 | 默认 | 2-4 | 短请求高并发小收益 |
| B12 | max-prefill-tokens / dynamic-chunking | 默认 | 扫 | 与 B3 相关 |

## C. 架构/拓扑机会

| # | 方向 | 说明 | 价值 |
|---|---|---|---|
| C1 | **每节点多小引擎（2×tp4/节点）** | 8 节点 × 2 引擎 = 16 引擎；故障隔离粒度更细（1/16 vs 1/8）+ tp4 的 A2B 带宽可能更稳 | **高**（未探索的形态空间） |
| C2 | 动态 P:D 配比 | 负载驱动切 3P5D↔2P6D（批处理高峰切 2P6D） | 中（运维复杂度） |
| C3 | 异步 prefill / overlap-schedule | 未验证 | 中 |
| C4 | LMCache（MP 缓存） | 被 hybrid-SWA 门禁阻塞（sglang#38652） | 解锁后值得 |
| C5 | 多 router / 一致性哈希 | 集群级水平扩展 | 低（单 router 已够） |

## D. 平台/引擎层（非参数可解）

| # | 项 | 状态 |
|---|---|---|
| D1 | **B 类平台故障**（mem_viol/xnack/warm-reset；.32/.33/.34/.35 轮转） | 报 MACA 证据链已全；pernode 单引擎隔离=当前缓解；**建议同时做节点级运行统计（按卡/节点聚合故障率）定位热卡** |
| D2 | warm-reset 触发源（ResetType=1） | 未定位（自愈链 vs 外部） |
| D3 | NoneType.server_args 竞态 | 未修（上游候选） |
| D4 | 长尾 hitch（p99 3.2s） | §6.4 引擎侧采样未做 |
| D5 | 0.5.12-decode 跨版本配对 | 时间盒放弃（模型/协议/DSPARK 三重未知） |

## E. 工程/工具（低风险高收益）

| # | 项 | 建议 |
|---|---|---|
| E1 | **router 滚动卡壳**（deploy env 改后必 scale 0→1） | 固化进 `deploy.sh`（脚本内自动 scale+等 ready） |
| E2 | **leader 漂移自动同步**（pp4full 单端点场景） | sglbench 或脚本加 bootstrap 探测 → set env |
| E3 | MinIO 备份协议（新目录） | 已定，可脚本化 `backup.sh` |
| E4 | forensics stale 捕获 bug | 修（两次事件 md5 相同） |
| E5 | campaign 重试与恢复窗口竞态（假失败烧尝试） | 重试前加 E2E 双确认 + sync_router |
| E6 | **一页速查表**（三形态×边界×最优×体感） | 出 `QUICKREF` 文档供交接 |

## 综合建议（若继续优化，按此序）

1. **A3（3P5D 默认参数对照）** — 资产已就绪，一轮部署即可
2. **B1+B2（3P5D cap/prefill 上探）** — 直接对冲 3P5D 唯一短板（prefill TTFT）
3. **A9 soak + A10 RTO/RPO** — 生产 SLA 必答
4. **C1（2×tp4/节点）** — 新形态空间，若上线前有时间做探针
5. **B5 ib 双轨** — 低成本高确定
6. **E1/E2 固化** — 工程收敛

> 全部数据（三拓扑网格/边界/体感）已在 DuckDB + MinIO `sglbench-backup-20261015-1120/`。

---

## 附录 A：DSPARK 投机解码启用情况（权威配置，2026-10-15 核验）

**结论：除 nospark 对照变体外，所有测试形态默认开启 DSPARK**（block 6）；三拓扑同构同参 → 对比口径公平。
**跨节点实证**：官方 pp4full 形态 decode 运行期 server_args 抓取 = `nnodes=4, tp_size=8, dp_size=4,
enable_dp_attention=True, speculative_algorithm='DSPARK', dspark_block_size=6` → **DSPARK 跨节点正常
工作**（限制非"单节点"而是：① `ep_size` 必须=1（>1 图捕获必崩）② `tp≤8`（o_groups=8 模型硬约束，
f4 终止之因）。

| 形态 | DSPARK | 配置机制 | 备注 |
|---|---|---|---|
| **pernode-best**（4+4 最优解） | ✅ on | `PD_ENABLE_DSPARK=1` · `PD_DSPARK_BLOCK_SIZE=6` | 显式开关 |
| **3P5D** | ✅ on | `PD_ENABLE_DSPARK=1` · block 6 | 同 pernode 家族 |
| **2P6D** | ✅ on | `PD_ENABLE_DSPARK=1` · block 6 | 同 pernode 家族 |
| **pp4full**（官方脚本） | ✅ on | 厂商 decode.sh 的 DSPARK 分支（`PD_DSPARK_BLOCK_SIZE=6`，`PD_EP_SIZE=1` 强制） | ep>1 时 draft verify graph 捕获必崩 |
| **pp4full-nospark**（对照） | ❌ off | `PD_SPECULATIVE_ENABLE=0`（与 pp4full 唯一差异） | 用于隔离 DSPARK 贡献 |

**数据口径影响**：
- 本体系全部性能/边界/体感数据（三拓扑对照 §8.7、chat-feel §7、体感电池）**均在 DSPARK-on 下测得** —— 跨形态对比公平 ✓
- **DSPARK 自身贡献（on vs off）A/B 基线**（4+4 时代，10-12）：单流 **TPOT 5.5 vs 16.6ms（3x）**、吞吐 **2.5x**、batch128 **5.9x** → 收益巨大必留
- **未测**：3P5D/2P6D 下的 DSPARK on/off 对照（若需，可复用 `PD_ENABLE_DSPARK=0` 显式关；列为候选 G4）
- accept rate（DSPARK 质量指标）：实测 0.78-0.83（近饱和）

### 附录 A 辨析：DSPARK「PP=1」≠「不支持跨节点」

**结论：DSPARK 限制的是 PP（流水线并行），不是跨节点本身**。跨节点扩展有三条路，DSPARK 兼容 TP/DP 跨节点，仅禁 PP：

| 并行维度 | DSPARK 支持？ | 本体系实测证据 |
|---|---|---|
| **TP 跨节点**（tp8 分 4 节点，每节点 2 卡） | ✅ | **pp4full 官方形态全程实测**：decode 引擎运行期 server_args =
  `nnodes=4, node_rank=1, tp_size=8, pp_size=1, dp_size=4, enable_dp_attention=True,
  speculative_algorithm='DSPARK', speculative_dspark_block_size=6`（10-06/10-13 两代日志均实锤）；
  c128 300/300、65K 等全部 DSPARK-on 数据即此形态产出 |
| **DP 跨节点**（dp4 跨 4 节点） | ✅ | 同上（dp_size=4 + dp_attention=True + dp_lm_head=True） |
| **PP 跨节点**（切层流水线） | ❌ **pp_size 必须=1** | DSPARK draft/verify 的 graph 捕获与跨 stage 通信不兼容；PP>1 时无合法配置 |

**重要澄清（工程权衡，非限制）**：
- DSPARK **允许**跨节点，但跨节点 tp8（每节点 2 卡）性能结构性劣势——KV/A2B 走网络：
  **ITL 9.3ms / 单流 104 tok/s**（pp4full）vs 节点内 tp8（8 卡）**ITL 4.5ms / 210-226 tok/s（2.2x）**
- 因此最优解（pernode）把每个引擎**收进单节点内**（tp8 占满 8 卡），DP 交由 router 层逐请求配对——
  **是"性能最优"的选择，不是 DSPARK 的边界**
- 模型硬约束另有一条：**tp ≤ 8**（attention 仅 8 个 output groups，`o_groups % tp == 0`；tp32 → 
  `n_local_groups=0` 图捕获崩）——所以"单大引擎吞多节点"（tp32/tp64）任何形态都不可行

## 附录 B：Gap 补测实测结果（2026-10-15）

### B1 G1 完成 ✅ — 3P5D 长上下文补齐（全 ok，零崩）
| 格 | TTFT | TPOT | 对照（pernode 4+4） |
|---|---|---|---|
| 65k-c8 | 15.40s | **6.08ms** | 9.4ms（pernode 记录） |
| 128k-c1 | 25.35s | **5.87ms** | — |
| 200k-c1 | 46.03s | **5.71ms** | — |
→ 3P5D 长上下文 TPOT 全面 <6.1ms（decode 5 引擎收益），TTFT 随长度线性排队（prefill 3 台短板），
全程零崩。label `gap-3p5d-lc*`。

### B2 G2 完成 ✅ — prefill cap 64→96（`pernode-3p5d-p96`）
| 格 | cap64 基线 | cap96 有效值（×2 中位） | 判定 |
|---|---|---|---|
| c128@r4 | 2.17s / 11.45ms | **1.74s（-20%） / 11.0ms** | ✅ 改善 |
| c160@r5 | 2.42s / 12.10ms | 2.50s（+3%，噪声内） / 12.5ms | 中性 |
- ⚠ 测量学：首对 run（12:29/12:31，同 label）为**滚动后冷启动伪影**（TTFT 5845/3181ms），
  按铁律**丢弃**；另发现**同名 label 双 run**（另一执行已提前跑过 G2 协议）——分析以 12:54-13:04
  三对数据为准。label `g2-p96-*`。
- **结论**：prefill cap 96 = **c128 档明确改善候选**（-20% TTFT，优于"稳定优先"阈值），c160 无损。

### B3 G3 状态（官方默认参数对照）
- `pernode-3p5d-default`（撤全部对齐 env）→ 已 apply（gen=19=observed）→ LWS 滚动中 → 进行时。
