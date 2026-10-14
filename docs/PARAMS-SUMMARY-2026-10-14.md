# PARAMS-SUMMARY — 0731 三形态参数汇总与修复记录（2026-10-14 终版）

> 配套：`TEST-REPORT-2026-10-13-safe-boundary-0731.md`（边界与战绩）、
> `TROUBLESHOOTING-2026-10-14-dsv4-0731-pp4full-crashes.md`（崩溃框架）。
> 值来源=各形态 `lws/base/*.yaml`（llm-d 仓库，10-14 提交后为准）。

## 1. 三形态参数对照（终版）

| 参数 | pernode（最优解） | PP1-explicit | pp4full（官方脚本） |
|---|---|---|---|
| 拓扑 prefill | 4× 节点内 tp8/dp1/pp1 | tp8/dp4/pp1 跨 4 节点（单引擎） | pp4/tp8/dp1 跨 4 节点 |
| 拓扑 decode | 4× tp8/dp4/pp1（同 prefill 节点内） | tp8/dp4/pp1 跨 4 节点（单引擎） | tp8/dp4/ep1 跨 4 节点 |
| 启动模式 | explicit | **explicit** | official（llm-launch 脚本） |
| prefill max-running | **64**/引擎 | 64（引擎级） | `prefill_max_running_requests_pre_batch=64` |
| decode max-running | **48**/引擎（×4=192 总量） | 64（引擎级） | `decode_max_running_requests_specific=16`×dp4=**64 总量** |
| graph-bs | **64**（>48 → adjusted 48 重建，dp1 唯一可行模式） | 80（adjusted=16 重建） | **128**（Phase1 E1；≠每rank cap16 规避 DSPARK bs=None） |
| prefill mem-frac | 0.82 | 0.82 | **0.78**（Phase1 D-fix：65K OOM） |
| decode mem-frac | **0.75** | **0.78**（Phase5 修复：c160 池风暴） | **0.78**（Phase1 R2：A 类池饱和） |
| DSPARK | on（block 6） | on（block 6） | on（block 6；ep 必须=1） |
| 镜像 | `-llm-retractfix`（46db2551） | prefill=官方原版；decode=`-retractfix`（fb9cff47） | `-llm-retractfix`（46db2551） |
| env 对齐（10-14） | — | — | 双侧 `SGLANG_ENABLE_TP_MEMORY_INBALANCE_CHECK=0`（废弃名修正） |
| L2 | `PD_ENABLE_HICACHE` 0/1 切换 | — | off |

公共参数：chunked 16384（官方侧）/4096（explicit 侧引擎归一）、page 64、swa_full_tokens_ratio 0.1、
context 400K、kv bf16、mem-frac 互见上表。模型 `/workspace/data/DeepSeek-V4-Flash-0731-W8A8`（LLMC W8A8）。

## 2. 10-14 修复记录（全部已回写 yaml + 已验证）

| # | 形态 | 变更 | 验证 |
|---|---|---|---|
| F1 | pp4full decode | `PD_CUDA_GRAPH_MAX_BS: 128`（原未设=厂商默认 8） | std-c64 ok（fixed1） |
| F2 | pp4full 双 | 废弃 env → `SGLANG_ENABLE_TP_MEMORY_INBALANCE_CHECK=0` | 随 F3/F4 生效 |
| F3 | pp4full decode | `mem-frac 0.75→0.78` | **std-c128 = 300/300 ok**（fixed2） |
| F4 | pp4full prefill | `mem-frac 0.82→0.78` | **lc65k-c1 = 20/20，TTFT 4.27s**（fixed4） |
| F5 | pp1 decode | `mem-frac 0.75→0.78` | c160 复赛 ok（pp1x） |

## 3. 历史标定依据（备查）

- cap 阶梯（rc3 时代 0.5.12）：40=稳/64=TTFT 好/160=boot 缺陷；0731 pernode 重标定 48=全包络最优。
- swa ratio：0.1 最优（0.4 → full 池 -70%；SWA 每 token 显存≈full 37.5×）。
- rate 容量点：pernode rate2=2903 tok/s；峰值 3354@conc160。
- graph-bs > max-running：**DSPARK bs=None 缺陷规避规则**（`_recompute` 仅在 adjusted≠requested 时重建）。
- 所有 run 的逐条环境快照见 DuckDB `run_environments` 表（含本文件的每个测点）。
