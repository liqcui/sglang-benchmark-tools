# HiCache L2 @ 最优解拓扑（pp1/tp8/dp4）—— 收益对比测试报告

**测试时间**：2026-10-05
**测试人**：supperadm（sglbench 驱动，数据落 DuckDB `~/sglang-benchmark-tools/data/bench.duckdb`）
**目的**：在**生产最优解拓扑**（prefill pp1/tp8/dp4）下，严格同拓扑对比 HiCache L2 与
非 HiCache 的收益与代价（消除此前 pp4 对照的拓扑混淆）。

---

## 1. 环境

| 项 | 非 HiCache（l2off-opt） | HiCache L2（l2pp1-opt） |
|---|---|---|
| 部署 | `deepseek-v4-rc3-maca/lws/`（生产最优解） | **`deepseek-hicache-l2-pp1/`**（新目录） |
| prefill 镜像 | 0.5.12-maca.ai3.8.2.7-...-llm | **0.5.13-dsv4-adapter-r4-hicache-l2-pp1**（新补丁镜像） |
| decode 镜像 | 0.5.12-maca.ai3.8.2.7-...-llm | 同左（两测 decode 完全一致） |
| **拓扑** | **pp1/tp8/dp4** | **pp1/tp8/dp4**（严格相同） |
| HiCache 参数 | 无 | direct / ratio 0.5 / page_first / storage=None |
| env 记录 | envs/pd-non-hicache.json | envs/hicache-l2-pp1.json |

**L2-PP1 补丁镜像**（本报告核心交付）：给 r4-hicache-l2 的 `prefill.sh` 打补丁
（非 auto 路径 tp 优先取 `prefill_tp_size`=8），并修复 launcher 对
`prefill_pp_size_default` 的回写（0.5.13 血统变量名差异——config.sh 的
`prefill_pp_size` 与 models 的 `prefill_pp_size_default` 是两个变量，前者回写无效）。
验证：server_args `tp_size=8/pp_size=1/dp_size=4` + `--enable-dp-attention` ✓

## 2. 结果（同拓扑，全场景 100% 成功率）

### 2.1 mini-l2 稳定边界基准

| 场景 | 指标 | l2off-opt | l2pp1-opt | 变化 | 判定 |
|---|---|---|---|---|---|
| **32c@3.0** | TTFT 均值 | **7.66 s** | 11.02 s | +43.9% | ❌ 回归 |
| | 输出吞吐 | **980.9** | 920.6 | -6.1% | ❌ |
| **64c@2.0** | TTFT 均值 | 3.38 s | **2.72 s** | **-19.6%** | ✅ 改善 |
| | 输出吞吐 | 1676.6 | 1717.1 | +2.4% | ➖ |
| **128c@1.0** | TTFT 均值 | **1.38 s** | 1.56 s | +12.8% | ❌ |
| | 输出吞吐 | 1715.1 | 1718.1 | +0.2% | ➖ |
| | 成功率 | 100% | **100%** | — | ✅ 稳定 |

### 2.2 多轮对话（multiturn）

| 轮次 | 20×10 l2off | 20×10 l2pp1 | 80×10 l2off | 80×10 l2pp1 |
|---|---|---|---|---|
| 0（冷） | 606 ms | 656 ms | 225 ms | 258 ms |
| 1 | 5178 ms | **1410 ms (-73%)** | 7154 ms | **3526 ms (-51%)** |
| 2+ | 1.7 s 稳态 | 1.6 s 稳态 | 2.5 s 稳态 | 2.6 s 稳态 |
| 成功率 | 100% | 100% | **100%** | **100%** |

### 2.3 稳定性（关键差异）

- **最优解拓扑下 L2 完全稳定**：5/5 场景全部 100%（128c 300/300、80×10 800/800）——
  与此前 **pp4 拓扑**下 L2 的 128c 70.3% 崩溃形成对比，确认当时崩溃是 pp4 拓扑+L2 组合
  的（或概率性）问题，**最优解拓扑下无此问题**。
- 80×10 大工作集：两部署均 100% 完成（此前 pp4 对照中非 HiCache 侧 38% 崩溃——拓扑差异）。

## 3. 结论

1. **同拓扑（最优解 pp1/tp8/dp4）下 L2 收益有限且不一致**：
   - 64c TTFT -19.6% 是主要正面点；32c/128c 反而 +44%/+13% 劣化
   - 吞吐无实质差异（±6% 内）
   - **multiturn 的冷→热转换轮（round1）改善显著（-51~-73%）**，稳态持平
2. **L2 的容量价值在工作集超 L1 时显现**（round1 场景），但稳态吞吐/延迟无优势——
   官方声称的"大工作集收益"在本集群（32 卡 prefill）上未转化为端到端优势。
3. **最优解拓扑是 L2 的正确部署形态**（稳定 + 64c/round1 收益）；pp4 拓扑+L2 应弃用。
4. **总体建议**：L2 收益不足以支持生产切换（32c/128c 劣化 + 无吞吐收益）；若要用，
   面向"多轮对话冷热转换"场景（64c 档 + multiturn round1 改善）。平台故障（MACA
   竞态）与 HiCache 无因果（非 HiCache 也触发过），报障另行。

## 4. 交付

- llm-d：`deepseek-hicache-l2-pp1/`（部署目录+镜像补丁资产+README）、`9873f218`、
  `de35acce`（pp_size_default 回写修复）
- sglbench：`envs/hicache-l2-pp1.json`；runs 全量入库（label `l2off-opt*` / `l2pp1-opt*`）
- HTML：`reports/l2opt-mini{32c,64c,128c}.html`
- 4 步计划全部完成：80×10 双部署 ✓ / 生产最优解恢复 ✓ / L2-PP1 镜像 ✓ / 最优解拓扑对比 ✓
