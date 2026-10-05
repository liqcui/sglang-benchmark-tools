# sglbench 可视化增强 + Agent 能力 + OpenClaw 交付包 —— 设计

> 2026-10-05 定稿（brainstorming 批准）。三域扩展：①可视化（逐轮曲线/战役完整报告/
> 双线叠加/趋势时间线）②Agent（GPU 复位自愈/战役执行器/崩溃取证/MCP 扩展）
> ③OpenClaw 生成式交付包。全部**增量式**，保持现有 191 测试语义不变。

## 0. 目标与非目标

**目标**

- 把 HiCache 最核心信号（多轮冷热断崖）从"已入库但无出口"变为 compare/report/dashboard/
  suite 四层可视化可达。
- 一条命令自动产出对标手工 `TEST-REPORT-2026-10-05-L2-full-comparison.md` 的完整战役
  报告（index.html + SUMMARY.md + 每场景报告）。
- Agent 闭环补齐：崩溃 → GPU 复位 → 重建 → 等健康 → 重跑（`--gpu-reset` 显式触发）；
  崩溃现场自动取证入库可 SQL 归因。
- OpenClaw 交付包消除 SKILL.md 漂移（生成式）、一键安装。

**非目标（YAGNI）**

- 不做部署切换自动化（kubectl apply/delete kustomize 属部署域，保持人工）。
- 不做浏览器实时看板/Web 服务（自包含 HTML 原则不变，零 CDN 零 JS）。
- 不改 bench_serving 执行协议（runner/scenarios 不动）。
- campaign 执行器不进 MCP（长任务，超时风险；CLI-only，skill 文档说明）。

**已批准决策记录**

| 决策 | 结论 | 理由 |
|---|---|---|
| GPU 复位触发 | **仅显式**：`--gpu-reset` 标志 / `sglbench recover` 命令 | 硬件级动作，行为可预期；audit 简单 |
| MCP recover 语义 | **两段式**：`dry_run` 默认 `true`，agent 确认后显式 `false` | 防 agent 误复位；与 harness "先计划" 原则一致 |
| campaign 执行器 MCP | 不进 MCP | 数小时长任务；MCP 只暴露快操作 |
| recover 通道 | ssh（本机 10.66.2.1 已验证免密直连全部节点 + sudo 免密 mx-smi） | 比 kubectl debug pod 稳；无需额外镜像 |
| 节点发现 | kubectl pod → hostIP 动态解析（不写死 IP）；`--node` 可显式指定 | 集群拓扑可变 |
| GPU 编号默认 | `-i 0` + `-i 4`（沿用验证配方）；`--gpus all` 扩 | 血泪规程，勿改默认 |
| 取证存储 | 文件 `data/runs/<id>/forensics/` + 摘要入库 `run_forensics` 表 | 证据可回溯 + agent 可 SQL 归因 |
| 汇总报告判定阈值 | 沿用 `analyze` 噪声阈值（±5%） | 与 compare 一致性 |

## 1. 模块划分

| 模块 | 状态 | 职责 |
|---|---|---|
| `sglbench/rounds.py` | 新增 | 逐轮指标提取/表/曲线数据（`round<N>_*` 解析） |
| `sglbench/remote.py` | 新增 | kubectl/ssh 执行封装（命令构建为纯函数，执行器可注入 mock） |
| `sglbench/recover.py` | 新增 | 节点发现、GPU 热复位、pod 重建、恢复编排 |
| `sglbench/forensics.py` | 新增 | 崩溃取证采集 + 签名提取 |
| `sglbench/campaign.py` | 新增 | 战役执行器 + 汇总报告（suite）生成 |
| `sglbench/svgchart.py` | 扩展 | + `heatmap_chart`（Δ 热图）、+ `trend_chart`（带状态点） |
| `sglbench/analyze.py` | 扩展 | sweep 加 label 过滤 + 双 label overlay 数据；trend 序列 |
| `sglbench/report.py` | 扩展 | 逐轮曲线卡（build_html）；双线 sweep 渲染；逐轮 console 表 |
| `sglbench/dashboard.py` | 扩展 | 多轮场景 → 逐轮断崖曲线区 |
| `sglbench/store.py` | 扩展 | `run_forensics` 表（SCHEMA_VERSION 1→2）+ 读写方法 |
| `sglbench/runner.py` | 扩展 | run 结束后 forensics 自动钩子（best-effort） |
| `sglbench/mcp.py` | 扩展 | 7 → 13 tools |
| `sglbench/cli.py` | 扩展 | + `campaign`/`trend`/`recover`/`forensics`；`--gpu-reset`/`--no-forensics`；sweep `--label` |
| `tools/gen_openclaw_skill.py` | 新增 | 生成 OpenClaw SKILL.md |
| `integrations/openclaw/` | 扩展 | RULES.md（规范源）、install.sh、README、生成 SKILL.md |

## 2. 可视化设计

### 2.1 逐轮曲线（rounds.py）

**解析**：`^round(\d+)_(mean_ttft_ms|median_ttft_ms|cache_hit_rate)$`。
`extract_rounds(metrics) -> dict[int, dict[str, float]]`；轮次排序输出。

**判定语义**：

- 断崖轮 = `round1_mean_ttft_ms`（首次热，预期相对 round0 大幅下降或相对基线大幅改善）。
- 稳态 = round≥3 的 mean_ttft_ms 均值。
- 逐轮 Δ = (cand - base)/base；正=劣化（TTFT 越低越好）。

**四层出口**：

1. `compare`（multiturn run 对）：console 增加"逐轮 TTFT"表：
   `round | 基线 | 候选 | Δ%`，末行汇总"round1 / 稳态"两点；附注引擎限制
   （本集群 fork 不填 `usage.cached_tokens`，cache_hit_rate 恒 0，看 TTFT 断崖）。
2. `report`：build_html 当任一 run 含 round 指标时增加 "逐轮 TTFT 曲线" 卡
   （line_chart，x=轮次，双 series）+ 逐轮表卡。
3. `dashboard`：成对场景含 round 指标时，追加"多轮断崖"区（每场景一张曲线）。
4. `campaign` suite：§多轮汇总表（round0/round1/稳态/判定）+ 每场景曲线。

### 2.2 战役汇总报告（campaign report / suite）

```
sglbench campaign report --pair A=B [--pair C=D] [--run-pair off=on] \
    [--title T] [-o reports/<name>/] [--no-md]
```

**配对**：复用 `dashboard.resolve_pairs`（label→每场景最新 ok run；`--run-pair` 精确）。

**产出三件套**（自包含 HTML，零 CDN）：

1. `index.html` 章节（对标手工报告）：
   - ① 受控条件表：两侧 `run_environments` 快照差异（拓扑/镜像/HiCache 参数/MACA），
     差异行高亮 → "哪些变量不可消除"一目了然。
   - ② 压测场景汇总表：场景 | TTFT Δ | TPOT Δ | 判定（着色，±5% 噪声阈值）。
   - ③ 吞吐量完整宽表：输入/输出/总/峰值 tok-s 四组 × (基线|候选|Δ)。
   - ④ 多轮汇总表：场景 | round0 | round1 | 稳态 | 判定。
   - ⑤ 稳定性表：成功率/状态/崩溃标注（非 ok run 列明）。
   - ⑥ 图表区：Δ 热图（场景×指标，绿=改善红=劣化，|Δ| 阶梯着色）+ TTFT Δ 柱状 +
     总吞吐分组柱状 + 每多轮场景断崖曲线。
   - ⑦ 结论草稿：各指标 改善/劣化/持平 计数；|Δ|>10% 显著项清单；稳定性声明。
   - ⑧ 导航：每场景报告链接。
2. `NN-<scenario>.html`：每对场景 `report.build_html`（已含直方图/CDF/分位区间/表比较）。
3. `SUMMARY.md`：①②③④⑤⑦ 的 markdown 版（可直接 commit 进 `docs/`）。

**热图 API**：`heatmap_chart(row_labels, col_labels, values, *, title, better_dirs, subtitle)`。
着色：按指标方向归一（绿=改善），|Δ| 阶梯 {≤3% 浅, ≤10% 中, ≤30% 深, >30% 最深}，格内标值。

### 2.3 双部署曲线叠加（sweep --label）

- `sweep` / `report --sweep` 支持 `--label`（可重复）；无 label 保持现状（修正：现状
  latest-per-scenario 不区分 label，可能取错 run——加 label 过滤即修复）。
- `analyze.build_sweep_overlay(store, *, family|scenarios, labels) -> [(label, SweepReport)]`。
- console：每 label 一段现报告 + 并列对照表（并发 × label → TTFT/吞吐）。
- HTML：每指标一张 line_chart，每 label 一条线（x=并发），标注各自安全水位；
  拐点/注解沿用 SweepReport.notes。

### 2.4 趋势时间线（trend）

```
sglbench trend --scenario KEY [--label L]... [--metric M]... [--since YYYY-MM-DD] \
    [--html out.html] [--json]
```

- 默认指标：`mean_ttft_ms, total_token_throughput_tok_s, success_rate`。
- x = started_at（UTC），每 label 一条线；ok run 实心点，非 ok run 空心/红色点
  （`svgchart.trend_chart` 新函数，Frame 复用）。
- console 表：时间 | label | 各指标值 | 状态；`--json` 供 agent。
- 用途：镜像/驱动换版前后的回归跟踪（如 patch v2 前后、r4 镜像迭代）。

## 3. Agent 能力设计

### 3.1 recover.py —— GPU 复位自愈

**通道**：ssh `ubuntu@<hostIP>`（本机已配 key；`--ssh-user` 可改）。

**节点发现**（`--from-pods`，auto-retry 集成路径默认）：
kubectl pod json → 非 Ready / 重启>0 / terminated 非零退出的 PD pod → 去重 hostIP +
崩溃原因。pod→node→hostIP 全动态。

**复位**：对每个问题节点：
`sudo -n mx-smi -y -r -i <idx>`（idx 默认 {0,4}，`--gpus all`=0..7）。
输出含 "Resource temporarily unavailable" → 等 30-60s 重试，≤3 次/索引。
完成后 `sudo -n mx-smi -L` 验证全部 "Available"（返回校验结果，失败不抛）。

**重建**（`--no-rebuild` 可关）：删除问题 pod（`kubectl delete pod --wait=false`），
LWS 自动重建组。

**CLI**：

```
sglbench recover [--from-pods | --node mxgpu-3-29 ...] [--gpus 0,4|all] \
    [--dry-run] [--no-rebuild] [--ssh-user ubuntu] [--json]
```

`--dry-run` 打印完整计划（节点/命令/顺序）不执行。输出 JSON 含每节点每索引结果。

**集成**：`run`/`multiturn` `--auto-retry N` 重试路径中，若带 `--gpu-reset`：
失败后 → `recover(from_pods=True, rebuild=True)` → `_wait_healthy` → 重跑。
无 `--gpu-reset` 时行为与现状完全一致。

### 3.2 战役执行器（campaign run）

```
sglbench campaign run <spec.json> --side <side> [--only PATTERN]... [--force] \
    [--dry-run] [--json]
```

**spec（JSON，风格同 envs/*.json）**：

```json
{
  "name": "l2-full",
  "title": "HiCache L2 全面对比",
  "exec": "docker",
  "timeout": "30m",
  "auto_retry": 3,
  "gpu_reset": true,
  "sides": {
    "baseline":  {"label": "l2off-opt", "env_file": "envs/pd-non-hicache-pp1.json"},
    "candidate": {"label": "l2pp1-opt", "env_file": "envs/hicache-l2-pp1.json"}
  },
  "scenarios": [
    "mini-l2-32c",
    {"key": "hicache-multiturn-80x10-r16", "mode": "multiturn", "max_parallel": 4}
  ]
}
```

**语义**：

- **一次跑一侧**（`--side`）：部署切换是人工步骤（非目标），执行器只负责"当前部署下
  这一侧的全部场景"。两侧都跑完后 `campaign report` 出汇总。
- **跳过已有**：默认对 (scenario, label) 已有 ok run 且 env_file 指纹一致则跳过；
  `--force` 全部重跑。
- `--only` glob 过滤场景；`--dry-run` 打印计划表（含每个场景 跳过/执行 决策）。
- 结束：若对侧全场景已有 ok run → 提示（不自动）执行 `campaign report` 命令；
  `--report-dir DIR` 给出时自动生成 suite。
- multiturn 条目走 multiturn 执行路径（max_parallel 等）。
- 每个场景失败（重试后仍非 ok）→ 记录该场景 partial/failed 继续下一场景，
  最终汇总报告里体现；全部完成返回码反映是否有失败（`--fail-on-bad` 语义默认开）。

### 3.3 崩溃取证（forensics.py）

**触发**：① run/multiturn 结束状态非 ok → 自动（best-effort，不阻塞主流程，
`--no-forensics` 关）；② 手动 `sglbench forensics <run_id> [--json]`。

**采集**（各步独立容错，单项失败记 partial 不中断）：

| 证据 | 来源 | 存放 |
|---|---|---|
| pod 状态/退出码/重启数 | kubectl get pods -o json（PD 组） | `forensics/pods.json` |
| namespace 事件 | kubectl get events（按 pod 过滤，时间窗=run 前后） | `forensics/events.txt` |
| 崩溃节点日志尾 | ssh `sudo tail -n 2000 /var/log/sglang-dsv4rc3/<最新>` | `forensics/logs/<node>.tail` |
| GPU 状态 | ssh `sudo -n mx-smi -L` | `forensics/gpu.txt` |
| 签名提取 | 对以上文本正则匹配 | `forensics/signatures.json` |

**签名表**（可扩展常量）：`xnack(0x8)`、`memory violation(0x4)`、`mcErrorIllegalAddress`、
`MCCL wrong-magic`、`Connection reset by peer`、`AssertionError`、`Subprocess .* crashed`、
`NotImplementedError`、`AttributeError: kv_cache_cpu`、`address already in use` 等。
每条命中：模式名、节点、首现行号、行片段。

**入库**：`run_forensics` 表（SCHEMA_VERSION 1→2）：

```sql
CREATE TABLE IF NOT EXISTS run_forensics (
    run_id VARCHAR PRIMARY KEY, collected_at VARCHAR, trigger VARCHAR,
    nodes_json VARCHAR, signatures_json VARCHAR, evidence_dir VARCHAR, ok BOOLEAN
)
```

**呈现**：`sglbench show <run_id>` 追加取证摘要；`--json` 全量；agent SQL 直查签名归因
（如 `SELECT run_id, signatures_json FROM run_forensics WHERE signatures_json LIKE '%xnack%'`）。

**总超时**：默认 90s（`--forensics-timeout`），保证不拖累压测主流程。

### 3.4 MCP 扩展（mcp.py，7 → 13 tools）

| 新 tool | 参数 | 返回 |
|---|---|---|
| `sglbench_report` | baseline_run, candidate_run, output | HTML 路径 |
| `sglbench_rounds` | run_id（可选 candidate_run） | 逐轮表 JSON（+可选 html） |
| `sglbench_campaign_report` | pair, output_dir, title? | index 路径 |
| `sglbench_trend` | scenario, labels[], metrics[], output? | 序列 JSON + 可选 html |
| `sglbench_forensics` | run_id, collect(bool) | 摘要 JSON |
| `sglbench_recover` | from_pods?, nodes[], gpus?, dry_run=true, rebuild? | 计划/执行报告 JSON |

`sglbench_recover` 默认 `dry_run=true`（两段式：agent 先拿计划，确认后传 `false`）。
campaign 执行器不暴露（CLI-only，见非目标）。

## 4. OpenClaw 交付包

### 4.1 生成式 SKILL.md

`tools/gen_openclaw_skill.py` 生成 `integrations/openclaw/skills/sglbench/SKILL.md`：

- **自动段①：场景目录** ← `scenarios.py` 目录（key/名称/关键参数）实时渲染。
- **自动段②：命令参考** ← `cli.build_parser()` 遍历子命令 + 关键 flags。
- **人工段③：操作规范** ← `integrations/openclaw/skills/sglbench/RULES.md`
  （铁律/标签约定/崩溃自愈流程/拓扑限制/已知结论——现有 SKILL.md 的规范内容迁移至此）。
- **同步测试**：`tests/test_openclaw_skill.py` 断言重新生成结果 == 落盘文件（防漂移）。

### 4.2 一键安装 `integrations/openclaw/install.sh`

```
install.sh --workspace <OpenClaw workspace> [--repo /path/to/sglang-benchmark-tools]
           [--check]
```

- 生成 SKILL.md（或 `--check` 仅校验同步）→ 拷贝到 `<workspace>/skills/sglbench/` →
  生成含绝对路径的 `mcp-config.json`（command=python3, args=["-m","sglbench","mcp"],
  cwd=repo）→ 打印接入步骤（MCP 注册 + skill 生效 + 冒烟命令 `sglbench status`）。

### 4.3 README 更新

tools 表 7→13；新工作流示例（自愈闭环 / campaign 执行+汇总 / 取证归因 / trend）。

## 5. 接口面汇总（新增 CLI）

```
sglbench campaign report --pair A=B [-o DIR] [--no-md]        # 战役汇总（完整测试报告）
sglbench campaign run spec.json --side baseline [--only ...]   # 战役执行器
sglbench trend --scenario K [--label L]... [--metric M]...     # 趋势时间线
sglbench recover [--from-pods|--node X] [--gpus 0,4|all]       # GPU 复位（--dry-run 计划）
sglbench forensics <run_id>                                    # 崩溃取证（手动补采）
sglbench run/multiturn ... --auto-retry N --gpu-reset          # 自愈闭环（显式触发）
sglbench run/multiturn ... --no-forensics                      # 关闭非 ok 自动取证
sglbench sweep --family concurrency --label A --label B        # 双线叠加
sglbench compare（multiturn）                                  # 自动附加逐轮表
sglbench report/dashboard（multiturn）                          # 自动附加逐轮曲线
```

## 6. 错误处理原则

- 所有 ssh/kubectl 子进程：超时 + BatchMode(ssh) + 非零退出不抛异常，转结构化错误。
- recover：单节点失败不中断其余节点；报告逐节点逐索引结果；整体失败 → 退出码非 0 + 明确提示。
- forensics：best-effort；任何采集步骤失败标 partial；总超时硬上限。
- campaign：单场景失败继续；final 汇总含失败清单；`--fail-on-bad` 默认开。
- suite 报告：缺失指标显示 "—"；非 ok run 在稳定性表显式标注（不静默）。

## 7. 测试计划（TDD，unittest 风格，`-t .` 运行）

| 模块 | 测试要点 |
|---|---|
| rounds | 解析边界（缺轮/乱序/仅 median）；Δ 表；断崖/稳态判定 |
| svgchart | heatmap 方向语义/阶梯色/空数据；trend_chart 状态点 |
| analyze | sweep label 过滤；overlay 数据；trend 序列（含非 ok 点） |
| campaign | spec 解析（含非法）；跳过决策；suite 表/结论/MD 生成；pair 解析 |
| recover | 计划构建（mock kubectl/ssh）；"Resource temporarily unavailable"重试序列；gpu all 扩展 |
| forensics | 签名提取（fixture 日志片段）；采集容错；DB 读写往返 |
| mcp | 13 tools 清单/schema；recover dry_run 默认 true；新 tool 冒烟（mock store） |
| openclaw | 生成==落盘同步断言；install.sh --check 退出码 |

回归：现有 191 测试全绿不破坏。

## 8. 实施分期（每期独立可交付 + 提交）

| 期 | 内容 | 依赖 |
|---|---|---|
| S1 | rounds.py + compare/report/dashboard 逐轮出口 | — |
| S2 | heatmap + trend_chart + trend 命令 + sweep 双 label | — |
| S3 | campaign suite 报告（index/SUMMARY/每场景） | S1/S2（曲线/热图） |
| S4 | remote.py + recover.py + CLI + `--gpu-reset` 集成 | — |
| S5 | forensics.py + run_forensics 表（schema v2）+ 自动钩子 | S4（remote） |
| S6 | campaign 执行器 | S3/S4/S5（报告+自愈+取证） |
| S7 | MCP 7→13 tools | S1–S6 |
| S8 | OpenClaw 生成器 + install.sh + README | S7（tools 清单） |
| S9 | 文档（README/agent-memory）+ 全量回归 + 最终提交 | 全部 |

物理验证（非单元）：S4 用 `--dry-run` + 单节点真实复位（谨慎，集群当前有 L2 部署在跑，
复位会崩组——验证放在无压测窗口）；S5 用真实历史 run（如 L3 崩溃 run）跑取证。
