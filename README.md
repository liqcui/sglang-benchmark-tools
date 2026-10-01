# sglbench —— SGLang PD 分离集群压测工具箱

面向 **8 节点纯 PD 分离集群**（4 Prefill + 4 Decode、沐曦 C500、SGLang 定制环境）的
压测**执行 → 归档 → 对比 → 可视化**一体化工具。把《SGLang PD 分离集群压测说明》里
那一堆手工 `nohup python3 -m sglang.bench_serving ...` 变成可复现、可对比、能自动
出结论的流程。

覆盖四件事：

1. **执行不同测试场景**，结果落到 **DuckDB + 不可变原始工件**（不用 Excel、不用手工截图）；
2. **同一场景的多次 test run 对比**（版本迭代、驱动升级、固件换版前后的 A/B）；
3. **分析对比结果**：指标 delta 方向判定、bootstrap 置信区间、KS / Mann-Whitney 显著性、
   SLA 门禁 pass/fail、并发梯度拐点与安全水位；
4. **可视化对比**：自包含 HTML 报告，内联 SVG 直方图 / CDF / 分位区间 / 分组柱状 / 折线图。

---

## 1. 为什么这样设计（数据管理最佳实践）

| 决策 | 原因 |
|---|---|
| **原始工件不可变**：每次 run 的 `stdout.log` / `command.txt` / `requests.jsonl` / `meta.json` 落在 `data/runs/<run_id>/`，永不就地改写 | 数据库只是工件的**可重建索引**。日志被轮转、集群被重装后，历史 run 依然完整可复现 |
| **DuckDB 作为分析库**（单文件 `data/bench.duckdb`） | 压测分析是"宽表 + 聚合 + 分布"，列存 + 向量化执行比 SQLite 快一个量级；零服务、可直接 `read_parquet`、可直接喂 pandas/Arrow |
| **长表存指标** `metrics(run_id, metric_key, value)` | 新增指标不需要改 schema —— 解析器看到任何 `标签: 数值` 都会入库，未登记指标只是不做"变好/变坏"判定 |
| **保留每请求明细** `request_samples` | 均值会被长尾骗。只有保留原始样本才能画直方图、CDF、算 P99、做显著性检验 |
| **配置即元数据**：场景参数 / 模型 / tokenizer / 端点 / 环境变量 / git sha / sglang 版本随 run 一起入库 | 才能回答"这两个 run 到底哪里不一样"，否则对比结论无效 |
| **内容指纹去重**：日志 sha256 入库，重复 `ingest` 直接跳过 | 幂等，避免同一份日志在库里出现五次 |
| **UTC + 可排序 run_id**：`<scenario>-<20260114T083000Z>-<4位随机>` | 不用猜哪个是新的 |
| **报告自包含**：内联 SVG、零 CDN、零 JS | 集群强制 `HF_HUB_OFFLINE=1`；报告能 scp 出来直接打开，也能贴进内网 wiki |

---

## 2. 安装

```bash
# 唯一硬依赖是 duckdb；其余全部标准库（含图表与统计），离线集群可直接用
pip install duckdb
# 可选：以包形式安装（提供 sglbench 命令）
pip install -e .
```

要求 Python ≥ 3.10。不用 `pip install` 也能跑：`python -m sglbench ...`（仓库根目录）。

> 集群侧只需要原来的 `sglang.bench_serving`。`sglbench run` 默认调用 `python3`；
> 想让本地 Windows/macOS 也生成命令，用 `--dry-run` 即可（不需要装 sglang）。

---

## 3. 三步上手

```bash
# ① 看场景目录（对应文档里的每一条命令）
sglbench scenarios

# ② 压测前预检：确认 PD 全链路通（比 curl 多测 TTFT 与流式完整性）
sglbench preflight --scenario single-standard

# ③ 在集群上跑（先 dry-run 看命令，再真跑）
sglbench run --scenario conc-64-standard --dry-run          # 打印可复制的 shell 片段
sglbench run --scenario conc-64-standard --label v2.3.1     # 直接执行并归档
```

已经有历史日志（集群上 `nohup` 跑出来的）？不需要重跑：

```bash
# 文件名约定 <scenario_key>__<label>.log，自动推断场景与标签
sglbench ingest /var/log/sglang-bench/ --infer
```

对比与出报告：

```bash
sglbench compare --scenario single-standard --last 2                  # 终端表格 + 结论
sglbench report  --scenario single-standard --last 2 -o compare.html --sweep
sglbench gate    --scenario single-standard                            # CI 卡点，失败返回 1
```

仓库自带一套示例数据，克隆下来就能立刻看到全部效果：

```bash
sglbench ingest examples/logs --infer      # 递归导入（含 second-run 子目录）
sglbench list
sglbench compare --scenario single-standard --last 2
sglbench sweep   --family concurrency
sglbench report  --scenario single-standard --last 2 --sweep -o report.html
```

---

## 4. 内置场景（与文档逐条对应）

| scenario key | 族 | 并发 | 请求数 | 输入/输出 | 速率 | 说明 |
|---|---|---|---|---|---|---|
| `single-standard` | single | 1 | 50 | 4096/2048 | 1.0 | 单并发日常基线 |
| `single-deepseek-standard` | single | 1 | 50 | 4096/2048 | 1.0 | 同上，DeepSeek-V4-Flash |
| `single-longctx` | long-context | 1 | 20 | 65536/8192 | 0.5 | 约 200K 上下文单并发基线 |
| `conc-32-standard` | concurrency | 32 | 200 | 4096/2048 | 4.0 | Decode 满配并发 |
| `conc-64-standard` | concurrency | 64 | 200 | 4096/2048 | 6.0 | Prefill 满配并发 |
| `conc-128-standard` | concurrency | 128 | 300 | 4096/2048 | 8.0 | 极限承压（已知会打崩 Prefill） |
| `conc-128-safe` | concurrency | 128 | 300 | 4096/2048 | 5.0 | 128 并发安全边界 |
| `conc-128-deepseek-safe` | concurrency | 128 | 300 | 4096/2048 | 5.0 | DeepSeek 安全边界 |
| `conc-256-standard` | concurrency | 256 | 300 | 4096/2048 | 16.0 | 超高并发极限 |
| `conc-32-longctx` | long-context | 32 | 100 | 65536/8192 | 2.0 | 长上下文高并发压力 |
| `prefill-only` | single-stage | 64 | 100 | 8192/128 | 2.0 | 绕过网关直连 Prefill :8000 |
| `decode-only` | single-stage | 32 | 100 | 2048/2048 | 5.0 | 绕过网关直连 Decode :8000 |

**场景 key 是稳定标识**：对比的前提是同一个 key。改参数会让历史 run 失去可比性，
确需变更请**新增** key。现场参数（网关地址、模型路径、数据集路径）改
`sglbench/scenarios.py` 顶部常量，或用 `--scenario-file my-scenarios.json` 覆盖：

```json
{
  "scenarios": [
    {
      "key": "conc-64-standard",
      "name": "64 并发（现场参数覆盖）",
      "family": "concurrency",
      "base_url": "http://10.66.1.232:8081",
      "model": "/workspace/data/GLM-5.1-W8A8",
      "tokenizer": "/workspace/data/GLM-5.1-W8A8",
      "num_prompts": 200, "max_concurrency": 64,
      "random_input": 4096, "random_output": 2048,
      "request_rate": 6.0, "pd_separated": true,
      "sla": {"min_success_rate": 1.0, "max_p99_ttft_ms": 2500, "max_p99_tpot_ms": 35}
    }
  ]
}
```

临时改一两个参数不必写文件：

```bash
sglbench run --scenario conc-128-safe --set max_concurrency=96 --set request_rate=4.5
```

---

## 5. 命令参考

| 命令 | 作用 |
|---|---|
| `sglbench scenarios [list\|show KEY\|export]` | 场景目录；`show` 会连执行命令一起打印 |
| `sglbench preflight --scenario KEY [--url ...]` | 连通性预检（流式 TTFT、`[DONE]` 完整性、HTTP 状态），结果入库 |
| `sglbench run --scenario KEY [--label L] [--repeat N] [--dry-run]` | 执行压测并归档入库 |
| `sglbench ingest PATH... [--infer]` | 导入已有日志（幂等：内容相同则跳过） |
| `sglbench list [--scenario K] [--family F] [--status S]` | 历史 run 列表 |
| `sglbench show RUN` | 单个 run 的指标、分布、门禁、异常日志 |
| `sglbench compare RUN... \| --scenario K --last N` | 对比分析（`--format table\|markdown\|json`） |
| `sglbench sweep [--family concurrency]` | 并发梯度扫描：吞吐上限、拐点、安全水位 |
| `sglbench report ... -o out.html [--sweep]` | 生成自包含 HTML（可同时导出 Markdown / JSON） |
| `sglbench gate --scenario K [--set-sla max_p99_ttft_ms=2000]` | SLA 判定，失败退出码 1 |
| `sglbench export [-o DIR] [--format parquet\|csv\|json]` | 归档导出（parquet 便于丢进 BI） |
| `sglbench sql "SELECT ..."` | 直接查 DuckDB |
| `sglbench db info \| path` | 数据库信息 |

**run 引用写法**：完整 `run_id`、唯一前缀、`latest`、`<scenario_key>:<n>`（n=1 是最新一次）。

常用选项：`--home DIR`（默认 `./data`，或 `SGLBENCH_HOME`）、`--db FILE`、
`--scenario-file FILE`、`--threshold-pct 5`（噪声阈值）、`--all-metrics`、
`--timeout 30m`、`--no-dump-requests`、`--fail-on-bad`、`--fail-on-gate`。

---

## 6. 数据模型

```
data/
├── bench.duckdb                    # 分析库（可从工件重建）
└── runs/<run_id>/                  # 不可变工件
    ├── run.json                    # 归一化记录
    ├── command.txt                 # 完整命令行
    ├── stdout.log                  # 压测原始输出（含 stderr）
    ├── requests.jsonl              # 每请求明细（分布分析原始数据）
    └── meta.json                   # 环境快照（python/sglang/git/平台/内容指纹）
```

DuckDB 表：

| 表 | 内容 |
|---|---|
| `scenarios` | 场景定义快照 + 参数指纹 |
| `runs` | 每次 test run 的元数据、参数、环境、工件索引 |
| `metrics` | 长表 `(run_id, metric_key) -> value`，约 52 项指标 |
| `request_samples` | 每请求 `ttft/tpot/itl/e2e/input_len/output_len/status/error` |
| `preflight_checks` | 预检记录 |
| `v_run_metrics` | 便于手写 SQL 的宽表视图 |

手写 SQL 例子：

```bash
# 每个场景最近一次的输出吞吐与 P99 TTFT
sglbench sql "SELECT scenario_key, label, value FROM v_run_metrics WHERE metric_key='output_token_throughput_tok_s' ORDER BY started_at DESC LIMIT 10"

# 直接读 parquet 与外部数据联查（DuckDB 原生能力）
sglbench sql "SELECT * FROM read_parquet('exports/v_run_metrics.parquet') LIMIT 5"
```

---

## 7. 对比分析看什么

`sglbench compare` 分三层给结论：

**① 指标级** —— 关键指标逐项 delta + 方向判定（延迟类越低越好、吞吐类越高越好，
`min/max/std` 这类噪声敏感量默认不参与，避免假告警）：

```
| 指标                  |   基线    |   候选    |         变化         |  判定  |
| 首 token 延迟 均值    |  84.00 ms |  92.80 ms |    +8.80 ms (+10.5%) | ❌回归 |
| 输出 token 吞吐       |   5,153.5 |   4,834.8 |      -318.7 (-6.2%)  | ❌回归 |
| 请求成功率            |   100.00% |   100.00% |        0.00% (0.00%) | ➖持平 |
```

**② 分布级** —— 有每请求明细时回答"这点差异是真的还是抖动"：中位数差 + bootstrap
95% 置信区间、KS 距离、Mann-Whitney p、Cliff's δ 效应量。

```
| 指标      | 基线中位 | 候选中位 | Δ中位(ms) |      95% CI       | KS D  | MWU p  |  结论  |
| TTFT (ms) |    80.37 |    90.12 |     +9.75 |     [+6.20, +13.4] | 0.284 | 0.0008 | 显著   |
```

**③ 门禁级** —— 按场景 SLA 判 pass/fail，并叠加日志异常信号（OOM / pod 重启 /
HTTP 5xx / NXLI 传输失败 / 超时）。**只要日志里有严重异常，即使指标好看也判失败** ——
这正是文档里"128 并发 request-rate=8.0 打崩 Prefill"必须被自动抓到的地方。

单并发合格标准（全量成功、TTFT/TPOT 平稳、无 KV 传输异常）已经编码进
`single-standard` 的 SLA；`conc-128-standard` 的示例数据会被判 FAIL 并给出原因。

`sglbench sweep` 再把并发梯度串成曲线，回答"最优并发水位在哪"：

```
吞吐峰值出现在 conc-32-standard（并发 32，总吞吐 30,454 tok/s）。
零失败的最高并发为 64（conc-64-standard），建议作为线上安全水位。
以下配置未通过成功率检查，不建议上线：conc-128-standard(并发 128)
检测到吞吐拐点：conc-64-standard（并发 64）之后吞吐增益明显放缓而延迟继续抬升。
```

---

## 8. 图

`sglbench report` 生成的 HTML 里包含（全部内联 SVG，可另存、可打印）：

- **分组柱状图** —— TTFT 与 TPOT 分两张（量级差 10 倍，画在一起小指标会被压没）、吞吐、成功率；
- **直方图** —— TTFT / TPOT 分布按"样本占比"归一化，不同 `num_prompts` 也能公平比形状；
- **CDF** —— 曲线越靠左越好，交叉说明各有优势区间；
- **分位区间图** —— P1~P99 全尾 + P25~P75 箱体 + 中位数 + 均值，比箱线图信息更明确；
- **折线图** —— 吞吐 / TTFT / TPOT 随并发变化（对数 X 轴），带并发拐点标注。

想看全部图表长什么样：`python tools/chart_gallery.py -o reports/chart-gallery.html`。

图表代码在 `sglbench/svgchart.py`，是纯函数（数据进、SVG 字符串出），可单测、可复用。

---

## 9. 典型工作流

**版本迭代 A/B（最常见）**

```bash
sglbench run --scenario single-standard --label v2.3.0-baseline
# ... 升级固件 / 换驱动 / 换 model 路径 ...
sglbench run --scenario single-standard --label v2.3.1-new
sglbench compare --scenario single-standard --last 2 --format markdown > ab.md
sglbench gate    --scenario single-standard --fail-on-gate   # 或 gate --run <新run>
```

**回归卡点（CI / 发版门禁）**

```bash
sglbench report --scenario conc-64-standard --last 2 -o qa.html --json qa.json
sglbench gate   --scenario conc-64-standard        # 退出码非 0 即阻断
```

**定位瓶颈在 Prefill 还是 Decode**（文档第五节）

```bash
sglbench run --scenario prefill-only --label diag
sglbench run --scenario decode-only  --label diag
sglbench show <run_id>            # 对比两个单阶段 run 的分位与异常
```

**失败留证**（打完就崩的那次也要入库）

```bash
sglbench ingest /var/log/128-request.log --scenario conc-128-standard --label crash-8.0
sglbench show <run_id>            # 直接看到 OOM / pod 重启 / 成功率
```

---

## 10. 目录结构

```
sglbench/
├── cli.py          # 命令行入口（子命令与参数）
├── scenarios.py    # 内置场景目录（现场常量在这里）
├── models.py       # 领域模型 + 指标注册表（方向/单位/族）
├── parse.py        # bench_serving 输出宽容解析 + 异常信号识别
├── runner.py       # 命令构造、进程执行、工件落盘、ingest
├── store.py        # DuckDB schema 与仓储
├── stats.py        # 分位数 / 直方图 / ECDF / bootstrap / KS / MWU / 效应量
├── analyze.py      # 对比分析、门禁、并发梯度扫描
├── svgchart.py     # 零依赖 SVG 图表原语
├── report.py       # 终端表格 / Markdown / 自包含 HTML 报告
└── preflight.py    # 连通性预检
tests/              # 171 个 unittest（解析/统计/存储/分析/报告/CLI 端到端）
tools/
├── make_examples.py   # 生成 examples/ 示例数据（确定性）
└── chart_gallery.py   # 生成图表总览 HTML
examples/logs/       # 示例压测日志 + 每请求明细
```

---

## 11. 开发

```bash
python -m unittest discover -s tests -t .     # 或 python -m pytest tests
python tools/make_examples.py                 # 重新生成示例数据（幂等）
python tools/chart_gallery.py                 # 目视校验全部图表
```

测试覆盖：解析器对真实格式的容忍度、示例日志汇总值与每请求明细的**逐项对账**
（相对误差 < 0.5%）、DuckDB 读写与幂等、统计函数与 scipy 语义一致性、
对比判定方向、门禁、SVG XML 合法性、报告无外部依赖、CLI 端到端退出码。

## 12. 已知边界

- `sglbench run` 默认走 `python3 -m sglang.bench_serving`；不同 SGLang 版本的
  每请求明细参数名可能不同，用 `--detail-flag` 指定或用 `--no-dump-requests` 关闭。
  探测失败只影响"能不能画直方图"，不影响压测本身。
- 图表的分布分析需要每请求明细；没有明细时报告只给汇总指标并明确标注
  "已跳过分布级检验"，不会用汇总值反推伪造分布。
- DuckDB 同一文件只允许一个写入进程。并发跑多个 `sglbench` 时请用 `--db` 分开。
