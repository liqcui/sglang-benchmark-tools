# HiCache L2 vs 非 HiCache PD 分离 —— 同拓扑对照测试报告

**测试时间**：2026-10-04 ~ 2026-10-05
**测试人**：supperadm（sglbench 工具驱动，数据全部落 DuckDB `~/sglang-benchmark-tools/data/bench.duckdb`）
**测试目标**：在**相同拓扑**（prefill pp4/tp8/dp1 + decode pp1/tp8/dp4）下，量化 HiCache L2 对 PD 分离推理的收益与代价

---

## 1. 测试环境

| 项 | 非 HiCache（l2-off-pp4） | HiCache L2（l2-on-mini） |
|---|---|---|
| 部署目录 | `deepseek-v4-rc3-pp4/`（新增变体，原最优解不动） | `deepseek-hicache/lws/` |
| prefill 镜像 | `0.5.12-maca.ai3.8.2.7-...-llm`（含 10-02 双缺陷修复） | `0.5.13-dsv4-adapter-r4-hicache-l2` |
| decode 镜像 | 同上（两者 decode 完全一致） | `0.5.12-maca.ai3.8.2.7-...-llm`（同左） |
| **prefill 拓扑** | **tp8/pp4/dp1** | **tp8/pp4/dp1**（原生合法拓扑） |
| decode 拓扑 | tp8/dp4/pp1 | tp8/dp4/pp1 |
| HiCache 配置 | 无 | `--enable-hierarchical-cache --hicache-io-backend=direct --hicache-ratio 0.5 --hicache-mem-layout page_first`（storage_backend=None=纯 L2） |
| 集群 | metax-c500-10.66.3，8 节点 × 8 卡（64 卡），MACA 3.8.1.3 / 驱动 3.9.6 | 同左 |
| 环境记录 | `envs/pd-non-hicache-pp4.json` | `envs/hicache-l2.json`（已入 `run_environments` 表） |

> 拓扑背景：r4-hicache-l2 厂商 `prefill.sh` 硬约束 `tp=卡数/pp 且 tp|o_groups(=8)` → 4 节点下
> 唯一合法拓扑 pp4/tp8/dp1。非 HiCache 基线为此**新建** `deepseek-v4-rc3-pp4` 变体（独立目录，
> 原最优解 pp1/tp8/dp4 目录不动）。

## 2. 测试协议

- **场景**（sglbench 场景目录，key 稳定标识）：
  - `mini-l2-{32c,64c,128c}`：4096/2048，速率 3.0/2.0/1.0（输入 token 速率 ≤8K/s，远低于实测崩溃边界 ~12K/s）
  - `hicache-multiturn-20x10`：20 客户端 × 10 轮多轮对话（对齐官方 bench_multiturn 协议，
    第 2 轮起输入 = 上轮输入+输出；流式测量逐轮 TTFT）
- **同 key 双部署各跑一遍**，label `l2-off-pp4` / `l2-on-mini` 区分，环境快照自动入库
- 每场景最多 3 次尝试（集群曾观测到概率性 MACA 平台故障）

## 3. 结果

### 3.1 mini-l2 稳定边界基准（同拓扑）

| 场景 | 指标 | **l2-off-pp4** | **l2-on** | 变化 | 判定 |
|---|---|---|---|---|---|
| **32c@3.0** | 成功率 | 100% (200/200) | 100% (200/200) | — | ➖ |
| | TTFT 均值 | 5.55 s | **2.63 s** | **-52.6%** | ✅ 改善 |
| | 输出吞吐 | 1018.7 | 1060.6 | +4.1% | ➖ |
| **64c@2.0** | 成功率 | 100% (200/200) | 100% (200/200) | — | ➖ |
| | TTFT 均值 | **833 ms** | 951 ms | +14.2% | ❌ 回归 |
| | 输出吞吐 | 1727.9 | 1761.8 | +2.0% | ➖ |
| **128c@1.0** | 成功率 | **100% (300/300)** | **70.3% (211/300)** | -29.7pp | ❌ 回归 |
| | TTFT 均值 | 930 ms | 933 ms | 持平 | ➖ |
| | 输出吞吐 | **1713.1** | 1396.4 | -18.5% | ❌ 回归 |

**结论**：
1. **L2 收益仅在 32c 档显现**（TTFT -52.6%）；64c 反而慢 14%，128c 稳定性与吞吐双回归。
2. 此前（不同拓扑对比）观察到的 64c "TTFT -71%" 主要是 **拓扑差异**（pp4 vs pp1/dp4）而非 HiCache 收益——**同拓扑对照的必要性得到证实**。
3. 128c 下 L2 部署在 211/300 时崩溃（MACA xnack 平台故障，见 §4）。

### 3.2 多轮对话（multiturn 20×10，同拓扑）

| 轮次 | l2-off-pp4 TTFT | l2-on TTFT |
|---|---|---|
| 0（冷） | 44.5 s | 65.0 s |
| 1 | 3.55 s | 2.94 s |
| 2 | 2.32 s | 2.50 s |
| 3+ | 0.67-0.80 s | 0.71-0.96 s |

**结论**：两部署收敛到相同的稳态 TTFT（~0.7-0.8s）——20 客户端 × 2K token 的工作集（~40K tokens）
**完全装得下 GPU L1 池**，轮间前缀复用由 L1 兜住，**L2 未被真正触发**（两部署 L1 行为一致）。
要逼出 L2 收益需 **>GPU 池的工作集**（80×10 场景已就绪，官方文档同样指出 L2 容量优势在
工作集超 L1 后显现）。
⚠ 本引擎 `/v1/completions` 不填 `usage.cached_tokens`，cache_hit_rate 恒 0 属测量限制。

### 3.3 稳定性

- l2-off-pp4：mini-l2 三档 3/3 全绿；multiturn 3 次尝试 1 次通过（2 次失败均因平台故障/瞬时失败）
- l2-on：mini-l2 32c/64c 通过、128c 崩溃 1 次（70.3%）；multiturn 1 次通过
- **两者都观测到 MACA 平台级故障**（详见 §4），L2 部署概率更高

## 4. 平台级故障（MACA，非 HiCache 专属）

| 时间 | 部署 | 故障 |
|---|---|---|
| 10-03 全天 | HiCache L2 | `xnack(0x8) atu address translation error`（配置矩阵全排：backend/ratio/layout/dyn 全崩） |
| 10-05 05:01:59 | **非 HiCache（pp4）** | `mcErrorIllegalAddress`（multiturn 流式负载，prefill 组崩，无 xnack 前置 trap） |

- 预测因子（HiCache 侧实测）：**输入 token 速率** ≤~12K tok/s 稳定 / ≥~20K 概率崩 / ≥24K 必崩；
  65K 长上下文必崩。但 10-05 显示**非 HiCache 部署同样可触发**（multiturn 流式模式）——
  故障本质是 MACA 平台 host 内存/DMA 路径的竞态缺陷，HiCache 只是提高触发概率。
- 运维规程：崩溃后 `mx-smi -y -r -i {0,4}` GPU 热复位（需重试 2-3 次）再重建；
  全链路干净重启可破 MCCL rendezvous 死锁；leader 漂移频繁需同步 router 端点。

## 5. 工具交付（本次一并完成）

- **sglbench 增强**（4 commits）：`run_environments` 表（31 列结构化环境）、`env.py` 自动捕获、
  `mini-l2-*` 稳定边界场景、`hicache-multiturn-*`/`hicache-l3-*`（L3 场景就绪待驱动解锁）、
  `sglbench multiturn` 子命令、`envs/*.json` 三套环境描述、`docs/agent-memory.md`（DuckDB 长期记忆设计）
- **llm-d**：`deepseek-v4-rc3-pp4/` 同拓扑变体（独立目录，kustomize 对 LWS CRD 无法合并容器的
  限制已记录于注释；原最优解不动）；`lws-pp4` 实验目录保留提交态
- HTML 报告：`reports/l2-sametopo-mini{32c,64c,128c}.html`

## 6. 结论与建议

1. **同拓扑下 L2 的收益有限且不稳定**：32c TTFT -52.6% 是真收益；64c 无收益（略回归）；
   128c 稳定性/吞吐双回归。**不建议在当前形态下把 HiCache L2 用于生产**。
2. **L2 的容量价值需要大工作集验证**：80×10（80 客户端）或更长对话场景（已就绪）才能逼出
   L1 淘汰后的 L2 命中收益——这是 L2 立项价值的最终判定实验。
3. **平台故障需报障 MACA**：4 种签名（xnack 0x8 / memory violation 0x4 / cudaHostRegister 712 /
   mcErrorIllegalAddress）+ 同拓扑对照矩阵是完整证据链；非 HiCache 也触发证明与 HiCache 无因果。
4. 65K 长上下文在 L2 下不可用（token 速率必然超崩溃边界）；非 HiCache 基线（含修复补丁）稳定。
