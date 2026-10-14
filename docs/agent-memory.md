# DuckDB 作为长期记忆 —— 设计与 Agent 化路线

> 2026-10-04 定稿。本文说明 sglbench 的数据落盘设计，以及未来改造为 agent 的接口面。

## 1. 为什么 DuckDB 是记忆

压测产生的是"事件流 + 环境快照 + 指标"的宽表型数据。DuckDB 列存 + 单文件 +
零服务 + 可被 pandas/Arrow 直接消费，天然适合作为**可查询的长期记忆**：

- **不可变工件在磁盘**（`data/runs/<run_id>/`：stdout/requests.jsonl/meta.json/run.json）
  —— 证据永远可回溯，DB 只是索引（可 `rebuild` 重建）。
- **结构化索引在 DB**（`data/bench.duckdb`）—— 一切比较/报告都从 DB 出发，而不是
  靠人记忆或散落的 CSV。

## 2. 表结构（记忆的 schema）

| 表 | 记忆内容 | 关键列 |
|---|---|---|
| `scenarios` | **测试用例定义快照**（key 是稳定标识，跨环境可比） | scenario_key, family, spec_json |
| `runs` | 每次执行（含环境标签、参数、状态、指纹） | run_id, scenario_key, label, status, params_json, meta_json |
| `metrics` | 长表指标（TTFT/TPOT/吞吐/成功率/**逐轮 TTFT**…） | run_id, metric_key, value |
| `request_samples` | 每请求明细（分布分析原始数据） | run_id, seq, ttft_ms, tpot_ms, status |
| **`run_environments`** | **部署环境快照（2026-10-04 新增）** | 节点数/GPU 数/TP/DP/PP/镜像/launch_mode/**HiCache 全套参数**/MACA 版本 |
| **`run_forensics`** | **崩溃取证摘要（2026-10-06 新增, schema v2）** | run_id, trigger, signatures_json, evidence_dir, ok |
| `preflight_checks` | 压测前连通性预检 | target, ok, ttft_ms |
| `v_run_metrics` | 视图：run × metric 拉平 | — |

**环境快照的意义**：同一 `scenario_key` 在不同部署（GPU only / L1+L2 / L1+L2+L3 /
Populated L3）下的 run 通过 `label` 区分，环境差异用结构化列而不是备注文字记录，
比较时可直接 GROUP BY 拓扑/镜像/HiCache 参数。

## 3. 测试协议（对齐 SGLang 官方 HiCache 基准）

官方 4 配置矩阵 ↔ 本工具场景/label：

| 官方配置 | 部署 | 场景 key | label 约定 |
|---|---|---|---|
| GPU only（仅 GPU KV） | 非 HiCache PD（dsv4rc3） | `mini-l2-*` / `hicache-multiturn-*` | `l2-off*` |
| L1+L2（Host DRAM） | hicache-l2 部署 | `mini-l2-*` / `hicache-multiturn-*` | `l2-on*` |
| L1+L2+L3（Mooncake） | hicache-l3 部署 | `hicache-l3-multiturn-*` | `l3-on*` |
| Populated Mooncake | hicache-l3 部署（预加载 KV） | `hicache-l3-populated-*` | `l3-pre*` |

多轮对话协议（`sglbench multiturn`，对齐 `bench_multiturn.py`）：
N 客户端 × R 轮，第 2 轮起输入 = 上一轮输入 + 输出；关注**逐轮 TTFT**（prefill 指标）
与**输入 token 吞吐**。逐轮指标入库为 `round<N>_mean_ttft_ms` / `round<N>_cache_hit_rate`。

⚠ 本集群 sglang fork 的 `/v1/completions` 响应**不填 `usage.cached_tokens`**
（命中判据应看逐轮 TTFT 断崖 + 引擎日志 `#cached-token`）——`cache_hit_rate` 指标
在当前引擎上恒为 0，属测量限制，非无命中。

## 4. 比较与报告

```bash
# 同 key 跨环境对比（收益对比的标准入口）
sglbench compare --scenario mini-l2-64c --last 2            # 最近两次 run
sglbench compare mini-l2-64c:1 mini-l2-64c:2 --baseline ... # 指定 run（l2-on vs l2-off）
sglbench report --scenario mini-l2-64c --last 2 -o l2.html  # 自包含 HTML 报告

# 环境维度的直接 SQL（DuckDB 既是记忆也是分析引擎）
sglbench sql "SELECT r.scenario_key, r.label, e.deployment, e.hicache_ratio,
                     m.metric_key, m.value
              FROM runs r JOIN run_environments e USING (run_id)
              JOIN metrics m USING (run_id)
              WHERE r.scenario_key LIKE 'mini-l2-%' AND m.metric_key='mean_ttft_ms'"
```

## 5. Agent 化接口面（roadmap）

当前已就绪的 agent 基元：

1. **稳定场景 key**（`scenarios` 表）——agent 说"跑 mini-l2-64c 对比 l2-on/l2-off"即可。
2. **结构化环境快照**（`env.py` + `run_environments`）——agent 能回答"这批数据是在什么
   拓扑/镜像/参数下测的"，并能校验"对照组环境是否一致"。
3. **统一 run 生命周期**（run/ingest/compare/report/gate 全走同一 DB）——agent 的
   观察-行动-记录循环天然闭环；`--json` 输出面可直接被工具调用消费。
4. **不可变工件 + 内容指纹**——agent 的每个结论可回溯到原始日志。

**2026-10-05 已落地（agent 能力三件套 + MCP）**：

- **`sglbench status`**——集群+DB 健康快照（JSON；router/leaders 自动探测，只读非侵入）→
  agent 的决策入口（先 status 再行动）。
- **`sglbench dashboard`**——跨场景对比仪表盘（label 自动配对/`--run-pair`；每指标
  分组柱状趋势图 + 全指标 Δ 表格，自包含 HTML）→ 例行 A/B 分析固化为单命令。
- **`run/multiturn --auto-retry N`**——崩溃自愈（非 ok 自动等健康重试；配合外部
  GPU 复位流程）→ 把此前 ladder 脚本的手工 retry 模式内置。
- **`sglbench mcp`**——MCP server（stdio，零依赖）：7 个 tools（status/list/show/sql/
  compare/dashboard/run_scenario）→ 任何 MCP 兼容 agent 框架直接调用。
- **OpenClaw 集成包**（`integrations/openclaw/`）：MCP 注册示例 + Skill 包
  （`skills/sglbench/SKILL.md`）+ Shell 直调规范 → 小龙虾（OpenClaw）三种接入方式。

**2026-10-06 已落地（三域扩展：可视化 / agent 闭环 / MCP 13 tools / OpenClaw 生成式）**：

- **可视化**：`rounds.py` 逐轮曲线（compare/report/dashboard 三层自动附加，补上
  HiCache 最核心信号断崖）；`campaign report` 完整战役报告（index.html 受控条件/
  Δ 热图/吞吐宽表/断崖曲线/结论 + SUMMARY.md + 每场景明细，对标手工 TEST-REPORT）；
  `trend` 跨 run 趋势时间线；`sweep --label A --label B` 双部署叠加；label 前缀
  通配配对（`--pair 'A*=B*'`，批后缀不对称一键全配）。
- **崩溃自愈闭环**：`recover`（ssh 节点 → `mx-smi -y -r -i 0/-i 4` 热复位 → 删 pod
  重建 → 验证）+ `run/multiturn --auto-retry N --gpu-reset`（**显式开启**，未开启
  行为不变）。通道实测：本机免密 ssh 全部节点 + sudo 免密 mx-smi。
- **崩溃取证**：非 ok run 自动采集（pods/events/节点日志 tail/GPU 状态）+ 10 类
  签名提取（xnack(0x8)/memory violation(0x4)/MCCL wrong-magic…）→ `run_forensics`
  表（schema v2）；`forensics <run_id>` 手动补采；`show` 显示摘要。
- **战役执行器**：`campaign run <spec.json> --side S`——spec 驱动、已有 ok run 且
  env 指纹一致自动跳过（label 前缀匹配 + deployment 指纹守卫）、`--dry-run` 先看
  计划、收尾自动出完整报告。campaign 执行器**不进 MCP**（长任务，CLI-only）。
- **MCP 7 → 13 tools**：+report/rounds/campaign_report/trend/forensics/recover；
  `recover` 两段式（`dry_run` 默认 true 先计划，显式 false 才执行）。
- **OpenClaw 生成式包**：`tools/gen_openclaw_skill.py`（场景目录 + 命令参考自动
  渲染，规范段在 `RULES.md`，同步测试防漂移）+ `install.sh --workspace` 一键安装。

后续方向（未实现）：自动环境捕获扩展（mx-smi/kubectl 驱动版本、GPU 健康纳入
快照）；llm-d 侧 skill（`sglbench-hicache-testing`）同步新命令面。
