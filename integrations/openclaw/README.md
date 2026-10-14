# OpenClaw 集成包 —— 把 sglbench 变成小龙虾（OpenClaw）的技能

> OpenClaw = 自托管 AI Agent 网关（Gateway 默认 :18789, Skill 生态 ClawdHub,
> Shell 工具支持 Docker 沙箱）。本目录提供三种把 sglbench（PD 集群压测工具）
> 接入 OpenClaw 的方式 + 一键安装脚本。

## 一键安装（推荐入口）

```bash
bash integrations/openclaw/install.sh --workspace <OpenClaw workspace>
# → 生成/安装 Skill 包（skills/sglbench/{SKILL.md,RULES.md}）
# → 生成 <workspace>/mcp-config.json（绝对路径, 13 个 MCP tools）
# → 打印接入步骤; 幂等可重复执行
bash integrations/openclaw/install.sh --check   # CI: 校验 SKILL.md 与生成器同步
```

`SKILL.md` 是**生成文件**（场景目录表 + 命令参考自动渲染自 `scenarios.py` /
`cli.py`，规范段维护在 `RULES.md`）——改规范请改 `RULES.md` 后重跑
`tools/gen_openclaw_skill.py`，同步测试会拦截手改漂移。

## 方式一（推荐）：MCP server —— 结构化工具调用（13 tools）

sglbench 自带 MCP server（`sglbench mcp`, stdio, 零依赖）。注册后 agent 获得：

| MCP tool | 用途 |
|---|---|
| `sglbench_status` | 集群+DB 健康快照（agent 决策入口, 只读） |
| `sglbench_list_runs` / `sglbench_show_run` | 历史 run 列表 / 单 run 完整记录 |
| `sglbench_sql` | DuckDB 只读直查（长期记忆分析） |
| `sglbench_compare` | 两 run A/B 对比（Δ/显著性） |
| `sglbench_dashboard` | 跨场景对比 dashboard HTML |
| `sglbench_report` | 单场景对比 HTML（直方图/CDF/分位/**逐轮曲线**） |
| `sglbench_rounds` | 多轮逐轮序列 + 断崖判定（JSON） |
| `sglbench_campaign_report` | **战役完整报告**（index+SUMMARY.md+每场景明细） |
| `sglbench_trend` | 跨 run 指标时间线（回归跟踪） |
| `sglbench_forensics` | 崩溃取证摘要（签名/证据目录; collect=true 现场补采） |
| `sglbench_run_scenario` | 执行压测场景并归档（auto_retry 自愈） |
| `sglbench_recover` | GPU 热复位+重建（**两段式**: dry_run 默认 true 先给计划, 确认后显式 false 执行） |

MCP 配置（stdio server；install.sh 会自动生成带绝对路径的版本）：

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

> **campaign 执行器（长任务）不进 MCP** —— 用 CLI：
> `python3 -m sglbench campaign run campaigns/l2-full.json --side baseline`

## 方式二：Skill 包 —— 让 agent 学会压测流程与边界

`skills/sglbench/`（SKILL.md 生成 + RULES.md 规范源）覆盖：标准场景矩阵（含官方案例）、
同拓扑对照原则、env-file 环境入库、**战役执行器/自愈/取证**工作流、拓扑/镜像硬约束。
复制到 OpenClaw workspace：

```bash
cp -r skills/sglbench <openclaw-workspace>/skills/   # 或直接用 install.sh
```

## 方式三：Shell 工具直调 —— 零改造兜底

```bash
# 健康检查（agent 决策入口）
python3 -m sglbench status

# 战役：先看计划 → 执行（已有 ok run 且 env 指纹一致自动跳过）→ 收尾完整报告
python3 -m sglbench campaign run campaigns/l2-full.json --side baseline --dry-run
python3 -m sglbench campaign run campaigns/l2-full.json --side baseline \
  --report-dir reports/l2full

# 单场景跑（崩溃自愈: 等健康重试; --gpu-reset 显式开启硬件复位闭环）
python3 -m sglbench run --scenario mini-l2-64c --exec docker \
  --label l2-on --env-file envs/hicache-l2-pp1.json --auto-retry 3 --gpu-reset

# 多轮对话（官方协议）
python3 -m sglbench multiturn --scenario hicache-multiturn-80x10-r16 \
  --max-parallel 4 --label l3-official --env-file envs/hicache-l3-simple.json

# 完整战役报告（跨批后缀通配配对一个命令搞定）
python3 -m sglbench campaign report --pair 'l2off-opt*=l2pp1*' -o reports/l2full-v2/

# 崩溃取证 / GPU 复位（显式）
python3 -m sglbench forensics <run_id> --json
python3 -m sglbench recover --from-pods --dry-run     # 先计划
python3 -m sglbench recover --from-pods               # 执行（复位+重建）

# 趋势 / 对比 / 长期记忆直查
python3 -m sglbench trend --scenario mini-l2-64c --label l2off-opt --label l2pp1 --html t.html
python3 -m sglbench compare --scenario mini-l2-64c --last 2
python3 -m sglbench sql "SELECT r.label, m.value FROM runs r JOIN metrics m USING(run_id) \
  WHERE r.scenario_key='mini-l2-64c' AND m.metric_key='mean_ttft_ms'"
```

## 典型 agent 工作流（编排示例）

1. `status` → 集群健康？（router_ok / leaders）
2. `campaign run --dry-run` → 缺哪些数据（跳过决策一览）
3. `campaign run --side <S>`（CLI, 带自愈）→ 补数据；失败 run 自动取证
4. `sql` 查 `run_forensics` → 崩溃归因（xnack / memory violation 签名）
5. `compare` / `rounds` → 单场景收益（Δ + 断崖判定）
6. `campaign report` → 完整测试报告（index+SUMMARY.md+明细）交付
7. `trend` → 跨版本回归跟踪

## 注意事项

- **崩溃恢复**：负载下 MACA 平台竞态会崩 prefill。GPU 热复位配方
  `mx-smi -y -r -i 0`+`-i 4`（"Resource temporarily unavailable" 等 30-60s 重试）；
  `--gpu-reset`/`recover` 必须显式触发（硬件级动作）。
- **L3 已知阻塞**：Mooncake 写回路径触发平台级崩溃（见仓库 `docs/TEST-REPORT-*`），
  L1+L2 是当前最优可用层级。
- **同拓扑对照**：A/B 对比的 prefill/decode 拓扑必须一致（见 skill §铁律）。
- **环境入库**：每次 run 带 `--env-file envs/<deployment>.json`（落 `run_environments`）。
- 沙箱建议：Shell 直调时用 OpenClaw Docker 沙箱；sglbench 本体只读宿主文件 +
  调 docker/kubectl/ssh 通道（recover/forensics 需要节点 ssh 免密）。
