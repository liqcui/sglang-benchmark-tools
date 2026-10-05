---
name: sglbench
description: PD 集群压测与 HiCache 收益对比。当用户要求压测/对比/查看集群测试数据（L2/L3 收益、崩溃边界、性能回归）时使用。工具仓库 ~/sglang-benchmark-tools。
---

# sglbench —— PD 集群压测工具

**工具位置**: `~/sglang-benchmark-tools`（python 模块 `sglbench`）。
所有压测/对比/查询都经它执行，结果自动落 DuckDB（`data/bench.duckdb`，长期记忆）。

## 决策入口

```bash
python3 -m sglbench status        # JSON: router/leaders 连通 + DB 概况（先跑这一步）
```

## 四条铁律

1. **同拓扑对照**：A/B 对比（如 L2 on/off）的 prefill/decode 拓扑必须一致；
   prefill 以最优解拓扑 pp1/tp8/dp4 为准（见 `envs/*.json` 的拓扑字段）。
2. **环境入库**：每次 `run`/`multiturn` 带 `--env-file envs/<deployment>.json`
   → 落 `run_environments` 表（对比时必须能回答"在什么拓扑/镜像下测的"）。
3. **label 约定**：`l2off-*`（非 HiCache）/ `l2pp1-*`（L2）/ `l3on-*`（L3）。
4. **数据不手搓**：分析一律 `compare`/`dashboard`/`sql` 从 DuckDB 出，
   不手工汇总 CSV。

## 标准场景（key 稳定，勿改参数；要变新增 key）

| 类别 | key | 说明 |
|---|---|---|
| 稳定边界 | `mini-l2-{32c,64c,128c}` | L2 收益, ≤12K tok/s 安全区 |
| 常规速率 | `conc-{32,64,128-deepseek-safe}-deepseek` | rate 4/6/5（+256 rate16/32 极限） |
| 多轮对话 | `hicache-multiturn-{20x10,80x10}` | 官方 A10/H800 协议 |
| **官方案例** | `hicache-multiturn-80x10-r16` + `--max-parallel 4` | 官方 H800 协议, L2/L2+L3 必含 |
| L3 专项 | `hicache-l3-*` / `hicache-l3-populated-*` | 需 L3 部署 |

## 常用命令

```bash
# 跑场景（docker 通道; --auto-retry 崩溃自愈）
python3 -m sglbench run --scenario mini-l2-64c --exec docker --label l2-on \
  --env-file envs/hicache-l2-pp1.json --auto-retry 3 --fail-on-bad

# 多轮对话（官方协议）
python3 -m sglbench multiturn --scenario hicache-multiturn-80x10-r16 \
  --max-parallel 4 --label l2-official --env-file envs/hicache-l2-pp1.json

# 对比（同 key 最近两次 / 指定 run）
python3 -m sglbench compare --scenario mini-l2-64c --last 2
python3 -m sglbench compare <off_run> <on_run> --baseline <off_run>

# 跨场景 dashboard（趋势图+表格 HTML）
python3 -m sglbench dashboard --pair l2off-opt=l2pp1-opt -o reports/x.html

# 长期记忆直查
python3 -m sglbench sql "SELECT ... FROM runs JOIN metrics USING(run_id) ..."

# 健康快照 / MCP server
python3 -m sglbench status
python3 -m sglbench mcp            # stdio MCP（若框架支持, 优先用 MCP tools）
```

## 已知结论速查（2026-10-05）

- **L2 收益场景相关**: multiturn round1（冷热转换）-51~-73% 大优; 64c/rt-32c@4 TTFT
  改善; mini-32c 劣化; TPOT 多数档改善。报告: `docs/TEST-REPORT-2026-10-05-L2-full-comparison.md`
- **L3 被 MACA 平台 bug 阻塞**（首次 batch_set 确定性崩溃, 与拓扑无关）:
  `docs/TEST-REPORT-2026-10-05-hicache-l3.md`
- **崩溃边界**: pp1 最优拓扑 + L2 稳定至 131K tok/s; 负载崩溃后需 GPU 热复位
  (`mx-smi -y -r -i 0`+`-i 4`, 重试) 再重建（--auto-retry 只等健康, 不复位 GPU）

## 完整规范

详细流程/拓扑硬约束/排障手册见 llm-d 仓库 skill `sglbench-hicache-testing`
（`~/llm-d/.claude/skills/`）与 `docs/agent-memory.md`。
