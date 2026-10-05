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
