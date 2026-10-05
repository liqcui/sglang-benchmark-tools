---
name: sglbench
description: PD 集群压测与 HiCache 收益对比。当用户要求压测/对比/查看集群测试数据（L2/L3 收益、崩溃边界、性能回归）时使用。工具仓库 ~/sglang-benchmark-tools。生成文件——勿手改；改 RULES.md 后重跑 tools/gen_openclaw_skill.py。
---

# sglbench —— PD 集群压测工具（OpenClaw Skill）

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
3. **label 约定**：`l2off-*`（非 HiCache）/ `l2pp1-*`（L2）/ `l3on-*`（L3）；
   批后缀（`-rt32`/`-bd256r16`…）区分场景批次，报告侧支持前缀通配配对（`--pair 'A*=B*'`）。
4. **数据不手搓**：分析一律 `compare`/`dashboard`/`campaign report`/`trend`/`sql`
   从 DuckDB 出，不手工汇总 CSV。

## 标准工作流（agent）

1. `status` → 集群健康？（router_ok / leaders）
2. 缺数据 → **战役执行器**补跑（内置跳过：已有 ok run 且 env 指纹一致的场景自动跳过）：
   ```bash
   python3 -m sglbench campaign run campaigns/l2-full.json --side baseline --dry-run  # 先看计划
   python3 -m sglbench campaign run campaigns/l2-full.json --side baseline \
     --report-dir reports/l2full                        # 执行 + 收尾自动完整报告
   ```
3. **崩溃自愈**（显式开启）：`run`/`multiturn` 加 `--auto-retry 3 --gpu-reset`
   （崩溃 → ssh 节点 GPU 热复位（-i 0/-i 4）→ 删 pod 重建 → 等健康 → 重跑）；
   不加 `--gpu-reset` 时只等健康重试，不动硬件。
4. **崩溃取证**：非 ok run 自动采集现场（pods/events/节点日志/GPU/签名）→
   `run_forensics` 表；手动补采 `forensics <run_id> [--json]`；agent 归因：
   `sql "SELECT run_id, signatures_json FROM run_forensics WHERE signatures_json LIKE '%xnack%'"`
5. 单场景对比 → `compare`；跨场景 → **战役完整报告**：
   ```bash
   python3 -m sglbench campaign report --pair 'l2off-opt*=l2pp1*' -o reports/l2full/
   # 产出 index.html（受控条件/全表/Δ 热图/断崖曲线/结论）+ SUMMARY.md + 每场景明细
   ```
6. 回归跟踪 → `trend --scenario K --label L --label L2 --metric mean_ttft_ms --html t.html`

> **MCP 优先**：若框架支持 MCP，注册 `python3 -m sglbench mcp`（13 个 tools），
> 直接调用 `sglbench_campaign_report`/`sglbench_rounds`/`sglbench_trend`/`sglbench_forensics`
> 等。`sglbench_recover` 为**两段式**：dry_run 默认 true 返回计划，确认后显式
> `dry_run=false` 才执行。campaign **执行器**不进 MCP（长任务）——用 CLI。

## 运维要点

- **崩溃恢复配方**：GPU 热复位 `mx-smi -y -r -i 0` + `-i 4`
  （"Resource temporarily unavailable" 等 30-60s 重试，≤3 次）。
- **L3 已知阻塞**：L3（Mooncake）写回路径触发平台级崩溃（见仓库 `docs/TEST-REPORT-*`），
  L1+L2 是当前最优可用层级。
- **判定阈值**：compare 噪声阈值 ±5%；逐轮断崖判定阈值 ±10%（round1 相对变化）。
- **显式触发原则**：`recover`（硬件级）与 `--gpu-reset` 必须显式给定，绝不默认开启。

## 场景目录（自动生成 · 来源 scenarios.py，key 稳定勿改）

| key | 说明 | 并发 | rate |
|---|---|---|---|
| `single-standard` | 单并发·常规文本（GLM） | 1 | 1.0 |
| `single-deepseek-standard` | 单并发·常规文本（DeepSeek-V4-Flash） | 1 | 1.0 |
| `single-longctx` | 单并发·超长上下文（GLM，约 200K） | 1 | 0.5 |
| `single-longctx-deepseek` | 单并发·超长上下文（DeepSeek，约 200K） | 1 | 0.5 |
| `conc-32-standard` | 32 并发·常规文本（GLM，Decode 满配） | 32 | 4.0 |
| `conc-32-deepseek` | 32 并发·常规文本（DeepSeek，Decode 满配） | 32 | 4.0 |
| `conc-64-standard` | 64 并发·常规文本（GLM，Prefill 满配） | 64 | 6.0 |
| `conc-64-deepseek` | 64 并发·常规文本（DeepSeek，Prefill 满配） | 64 | 6.0 |
| `conc-128-safe` | 128 并发·安全档（GLM） | 128 | 5.0 |
| `conc-128-deepseek-safe` | 128 并发·安全档（DeepSeek） | 128 | 5.0 |
| `conc-32-longctx` | 32 并发·超长上下文压力（GLM） | 32 | 2.0 |
| `conc-32-longctx-deepseek` | 32 并发·超长上下文压力（DeepSeek） | 32 | 2.0 |
| `conc-128-crash-probe` | 128 并发·崩溃探针（DeepSeek @ rate=8.0） | 128 | 8.0 |
| `conc-256-standard` | 256 并发·超高并发极限（GLM） | 256 | 16.0 |
| `conc-256-deepseek` | 256 并发·超高并发极限（DeepSeek） | 256 | 16.0 |
| `conc-64-sharegpt-deepseek` | 64 并发·ShareGPT 短上下文（DeepSeek） | 64 | 20.0 |
| `prefill-only` | Prefill 集群独立压测（GLM） | 64 | 2.0 |
| `prefill-only-deepseek` | Prefill 集群独立压测（DeepSeek） | 64 | 2.0 |
| `decode-only` | Decode 集群独立压测（GLM） | 32 | 5.0 |
| `decode-only-deepseek` | Decode 集群独立压测（DeepSeek） | 32 | 5.0 |
| `mini-l2-32c` | mini-l2: 32 并发·稳定边界（DeepSeek） | 32 | 3.0 |
| `mini-l2-64c` | mini-l2: 64 并发·稳定边界（DeepSeek） | 64 | 2.0 |
| `mini-l2-128c` | mini-l2: 128 并发·稳定边界（DeepSeek） | 128 | 1.0 |
| `hicache-multiturn-20x10` | HiCache 多轮对话 20 客户端×10 轮 | 20 | 2.0 |
| `hicache-multiturn-80x10` | HiCache 多轮对话 80 客户端×10 轮 | 80 | 1.0 |
| `hicache-multiturn-80x10-r16` | HiCache 多轮对话 80×10 官方 H800 参数（rate16+parallel4） | 80 | 16.0 |
| `hicache-l3-multiturn-20x10` | HiCache L3 多轮对话 20 客户端×10 轮（Mooncake） | 20 | 2.0 |
| `hicache-l3-multiturn-80x10` | HiCache L3 多轮对话 80 客户端×10 轮（Mooncake） | 80 | 1.0 |
| `hicache-l3-populated-20x10` | HiCache L3 预填充多轮对话 20 客户端×10 轮（Populated） | 20 | 2.0 |

## 命令参考（自动生成 · 来源 cli.py）

### `sglbench scenarios`
查看场景目录
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `-o/--output <值>` · `--python <值>` · `--module <值>` · `--json`

### `sglbench preflight`
压测前连通性预检
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `--scenario <值>` · `--url <值>` · `--model <值>` · `--prompt <值>` · `--max-tokens <值>` · `--timeout-s <值>` · `--no-stream` · `--no-nodes` · `--no-store` · `--json`

### `sglbench run`
执行场景压测
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `--scenario <值>` · `--label <值>` · `--notes <值>` · `--tag <值>` · `--run-id <值>` · `--repeat <值>` · `--auto-retry <值>` · `--gpu-reset` · `--no-forensics` · `--retry-wait <值>` · `--set <值>` · `--python <值>` · `--module <值>` · `--exec <值>` · `--detail-flag <值>` · `--no-dump-requests` · `--timeout <值>` · `--dry-run` · `--json` · `--no-store` · `--fail-on-bad` · `--verbose` · `--env-file <值>`

### `sglbench multiturn`
HiCache 多轮对话基准（N 客户端 × R 轮，逐轮 TTFT/cache-hit）
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `--scenario <值>` · `--url <值>` · `--clients <值>` · `--rounds <值>` · `--request-length <值>` · `--output-length <值>` · `--request-rate <值>` · `--max-parallel <值>` · `--auto-retry <值>` · `--gpu-reset` · `--no-forensics` · `--timeout-s <值>` · `--label <值>` · `--notes <值>` · `--env-file <值>` · `--no-store` · `--json`

### `sglbench dashboard`
跨场景对比 dashboard（趋势图+表格, 直读 DuckDB）
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `--pair <值>` · `--run-pair <值>` · `--title <值>` · `--notes <值>` · `-o/--output <值>`

### `sglbench campaign`
战役：完整测试报告（index + SUMMARY.md + 每场景明细）

#### `sglbench campaign report`
跨场景完整测试报告（对标手工 TEST-REPORT-*.md）
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `--pair <值>` · `--run-pair <值>` · `--title <值>` · `-o/--output <值>` · `--no-md`

#### `sglbench campaign run`
执行战役一侧（当前部署下全部场景, 带自愈）
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `--side <值>` · `--only <值>` · `--force` · `--dry-run` · `--report-dir <值>` · `--json`

### `sglbench recover`
GPU 热复位+pod 重建（ssh mx-smi; --dry-run 先看计划）
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `--from-pods` · `--node <值>` · `--gpus <值>` · `--dry-run` · `--no-rebuild` · `--ssh-user <值>` · `--json`

### `sglbench forensics`
崩溃取证（采集现场+签名提取; 非 ok run 自动触发）
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `--json`

### `sglbench status`
集群+DB 健康快照（JSON; agent 决策入口, 只读非侵入）
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `--router <值>`

### `sglbench mcp`
MCP server（stdio）—— 供 agent 框架直接调用 list/run/compare/sql 等
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet`

### `sglbench ingest`
导入已有日志
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `--scenario <值>` · `--infer` · `--pattern <值>` · `--requests <值>` · `--label <值>` · `--notes <值>` · `--tag <值>` · `--run-id <值>` · `--operator <值>` · `--force` · `--no-store`

### `sglbench list`
列出历史 run
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `--scenario <值>` · `--family <值>` · `--status <值>` · `--limit <值>` · `--json`

### `sglbench show`
查看单个 run
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `--json`

### `sglbench compare`
对比同一场景的多次 test run
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `--scenario <值>` · `--last <值>` · `--baseline <值>` · `--threshold-pct <值>` · `--all-metrics` · `--no-distributions` · `--resamples <值>` · `--set-sla <值>` · `--format <值>` · `--fail-on-gate`

### `sglbench report`
生成可视化报告
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `--scenario <值>` · `--last <值>` · `--baseline <值>` · `--threshold-pct <值>` · `--all-metrics` · `--no-distributions` · `--resamples <值>` · `--set-sla <值>` · `-o/--output <值>` · `--title <值>` · `--markdown <值>` · `--json <值>` · `--sweep` · `--sweep-family <值>` · `--sweep-label <值>` · `--all-runs`

### `sglbench sweep`
并发梯度扫描分析
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `--scenario <值>` · `--family <值>` · `--label <值>` · `--all-runs` · `--json`

### `sglbench trend`
指标趋势时间线（同一场景跨 run 回归跟踪）
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `--scenario <值>` · `--label <值>` · `--metric <值>` · `--since <值>` · `--html <值>` · `--json`

### `sglbench gate`
按 SLA 判定通过/失败
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `--scenario <值>` · `--run <值>` · `--set-sla <值>` · `--json`

### `sglbench export`
导出数据归档
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `-o/--output <值>` · `--format <值>` · `--sql <值>`

### `sglbench sql`
对 DuckDB 执行 SQL
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet` · `--json`

### `sglbench db`
数据库管理
参数: `--home <值>` · `--db <值>` · `--scenario-file <值>` · `--quiet`
