# OpenClaw 集成包 —— 把 sglbench 变成小龙虾（OpenClaw）的技能

> OpenClaw = 自托管 AI Agent 网关（Gateway 默认 :18789, Skill 生态 ClawdHub,
> Shell 工具支持 Docker 沙箱）。本目录提供三种把 sglbench（PD 集群压测工具）
> 接入 OpenClaw 的方式，按推荐度排序。

## 方式一（推荐）：MCP server —— 结构化工具调用

sglbench 自带 MCP server（`sglbench mcp`, stdio, 零依赖）。若你的 OpenClaw 版本
支持 MCP（Agents API / 插件体系），注册后 agent 获得 7 个原生工具：

| MCP tool | 用途 |
|---|---|
| `sglbench_status` | 集群+DB 健康快照（agent 决策入口, 只读） |
| `sglbench_list_runs` | 历史 run 列表（按 scenario/label/status 过滤） |
| `sglbench_show_run` | 单 run 完整记录（指标/参数/环境快照） |
| `sglbench_sql` | DuckDB 只读直查（长期记忆分析） |
| `sglbench_compare` | 两 run A/B 对比（Δ/显著性） |
| `sglbench_dashboard` | 跨场景对比 dashboard HTML（趋势图+表格） |
| `sglbench_run_scenario` | 执行压测场景并归档（支持 auto_retry 自愈） |

MCP 配置示例（stdio server）：

```json
{
  "mcpServers": {
    "sglbench": {
      "command": "python3",
      "args": ["-m", "sglbench", "mcp"],
      "cwd": "/home/supperadm/sglang-benchmark-tools"
    }
  }
}
```

## 方式二：Skill 包 —— 让 agent 学会压测流程与边界

OpenClaw 的 Skill（workspace `skills/<name>/SKILL.md`）适合沉淀"怎么做"的规范。
把本目录的 `skills/sglbench/SKILL.md` 复制到 OpenClaw workspace 的 skills 目录：

```bash
cp -r skills/sglbench <openclaw-workspace>/skills/
```

Skill 内容覆盖：标准场景矩阵（含官方案例）、同拓扑对照原则、env-file 环境入库、
崩溃自愈流程、拓扑/镜像硬约束——agent 依此规范驱动方式一/三的工具。

## 方式三：Shell 工具直调 —— 零改造兜底

OpenClaw 的 Shell 工具可直接执行（建议 Docker 沙箱）。全部命令 JSON 友好：

```bash
# 健康检查（agent 决策入口）
python3 -m sglbench status                      # JSON: db/runs/router/leaders

# 跑场景（崩溃自愈, docker 执行通道）
python3 -m sglbench run --scenario mini-l2-64c --exec docker \
  --label l2-on --env-file envs/hicache-l2-pp1.json --auto-retry 3 --fail-on-bad

# 多轮对话（官方协议; max-parallel 对齐官方）
python3 -m sglbench multiturn --scenario hicache-multiturn-80x10-r16 \
  --max-parallel 4 --label l3-official --env-file envs/hicache-l3-simple.json

# 对比与报告
python3 -m sglbench compare --scenario mini-l2-64c --last 2
python3 -m sglbench dashboard --pair l2off-opt=l2pp1-opt -o reports/l2.html

# 长期记忆直查
python3 -m sglbench sql "SELECT r.label, m.value FROM runs r JOIN metrics m USING(run_id) \
  WHERE r.scenario_key='mini-l2-64c' AND m.metric_key='mean_ttft_ms'"
```

## 典型 agent 工作流（编排示例）

1. `status` → 集群健康？（router_ok / leaders）
2. `list_runs`/`sql` → 基线数据在不在？（同 scenario_key 的对照 label）
3. `run_scenario`（auto_retry=3）→ 补缺失场景
4. `compare` → 单场景收益（Δ + 显著性）
5. `dashboard` → 跨场景总览 + 交付 HTML

## 注意事项（与项目 skill 一致）

- **崩溃恢复**：负载下 MACA 平台竞态会崩 prefill。崩溃后需 GPU 热复位
  （`mx-smi -y -r -i 0`+`-i 4`，重试 2-3 次）再重建；`--auto-retry` 只负责等健康重试，
  不负责 GPU 复位。
- **同拓扑对照**：A/B 对比的 prefill/decode 拓扑必须一致（见 skill §0）。
- **环境入库**：每次 run 带 `--env-file envs/<deployment>.json`（落 `run_environments`）。
- 沙箱建议：Shell 直调时用 OpenClaw Docker 沙箱；sglbench 本体只读宿主文件 +
  调 docker/kubectl 通道。
