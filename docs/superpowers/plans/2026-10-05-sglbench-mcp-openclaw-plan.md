# sglbench MCP 扩展 + OpenClaw 交付包实施计划（Plan 3：S7–S9）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** MCP tools 7→13（+report/rounds/campaign_report/trend/forensics/recover，recover 两段式）；OpenClaw 交付包生成式化（SKILL.md 从 scenarios 目录 + CLI 帮助 + 人工规范段自动生成，防漂移）+ install.sh 一键安装；主 README 与 agent-memory 文档收尾。

**Architecture:** mcp.py 按既有 TOOLS/HANDLERS 模式追加工具（handler 全部复用 Plan 1/2 的 Python API，零新逻辑）；新增 `tools/gen_openclaw_skill.py`（读 `cli.build_parser()` 与 `scenarios.BUILTIN_SCENARIOS` 渲染 Markdown，与 `integrations/openclaw/skills/sglbench/RULES.md` 拼接）；安装脚本 bash。

**Tech Stack:** Python 标准库 + duckdb；unittest；bash。

**Spec:** `docs/superpowers/specs/2026-10-05-sglbench-viz-agent-openclaw-design.md`（§3.4 MCP、§4 OpenClaw、§8 S7–S9）

## Global Constraints

- 回归红线：现有全部测试保持全绿（当前 265）。
- MCP 测试**不真连集群**：patch `sglbench.mcp._paths` 指向临时 home；recover 执行路径 mock `sglbench.recover.run_recovery`。
- recover MCP 语义（已批准）：`dry_run` 默认 **true**（agent 先拿计划，显式传 false 才执行）。
- campaign 执行器**不进 MCP**（长任务；CLI-only，文档说明）。
- SKILL.md 生成必须**确定性**（同一仓库状态生成同一文本）；同步测试断言"重新生成 == 落盘文件"。
- 提交尾注 `Co-Authored-By: Claude Code <noreply@anthropic.com>`。

---

### Task 1: MCP 快工具 ×5（report / rounds / campaign_report / trend / forensics）

**Files:**
- Modify: `sglbench/mcp.py`
- Test: `tests/test_mcp.py`（新建）

**Interfaces:**
- Consumes: `report.build_html/write_html`、`analyze.compare_runs/scenario_for_run/analyze_trend`、`rounds.extract_rounds/round_delta_rows/round_verdict`、`campaign.write_suite`、`forensics.collect_evidence`、`store.*_forensics`。
- Produces（TOOLS 追加 5 项 + HANDLERS 注册）：

| tool | 参数 | 行为 |
|---|---|---|
| `sglbench_report` | baseline_run, candidate_run, output? | 对比+HTML → 返回路径 |
| `sglbench_rounds` | run_id, candidate_run? | 逐轮序列 JSON；双 run 时附 Δ 表与判定 |
| `sglbench_campaign_report` | pair(labels), output_dir, title? | `write_suite` → index 路径 + 报告数 |
| `sglbench_trend` | scenario, labels[], metrics[], since?, output? | 序列 JSON + 可选 HTML |
| `sglbench_forensics` | run_id, collect(bool, 默认 false) | 已有摘要（返回 DB 行 + 解析后 signatures）；collect=true 现场补采 |

- [ ] **Step 1: Write the failing test**

```python
"""MCP server 工具面测试（patch _paths 指向临时库; 零真实集群）。"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from sglbench import mcp as mcp_mod
from sglbench.models import RunRecord
from sglbench.store import Store

from .helpers import temp_home


def make_run(store, run_id, *, scenario="mini-l2-64c", label="A", metrics=None, env=None, status="ok", day=1):
    rec = RunRecord(run_id=run_id, scenario_key=scenario, label=label, status=status,
                    started_at=datetime(2026, 10, day, tzinfo=timezone.utc),
                    metrics=dict(metrics or {}))
    if env:
        rec.meta["deployment_env"] = env
    store.save_run(rec)
    return rec


class FakePaths:
    def __init__(self, home):
        self.home = home
        self.db = home / "bench.duckdb"
        self.runs_dir = home / "runs"
        self.reports_dir = home / "reports"


class McpToolsTest(unittest.TestCase):
    def setUp(self):
        self._th = temp_home()
        self.paths_ctx = self._th.__enter__()
        store = Store(self.paths_ctx.db)
        make_run(store, "b1", label="l2off", metrics={
            "mean_ttft_ms": 7.0, "round0_mean_ttft_ms": 606.0,
            "round1_mean_ttft_ms": 5178.0, "round3_mean_ttft_ms": 1700.0,
        })
        make_run(store, "c1", label="l2pp1", metrics={
            "mean_ttft_ms": 8.0, "round0_mean_ttft_ms": 656.0,
            "round1_mean_ttft_ms": 1410.0, "round3_mean_ttft_ms": 1720.0,
        })
        store.close()
        self.fake = FakePaths(self.paths_ctx.home)
        mcp_mod._paths = lambda: self.fake  # 测试期替换
        self.addCleanup(self._th.__exit__, None, None, None)

    def call(self, name, **args):
        payload = json.loads(mcp_mod.HANDLERS[name](args)["content"][0]["text"])
        return payload

    def test_tool_count_and_schemas(self):
        self.assertEqual(len(mcp_mod.TOOLS), 12)
        names = {t["name"] for t in mcp_mod.TOOLS}
        for n in ("sglbench_report", "sglbench_rounds", "sglbench_campaign_report",
                  "sglbench_trend", "sglbench_forensics"):
            self.assertIn(n, names)

    def test_rounds_tool(self):
        payload = self.call("sglbench_rounds", run_id="b1", candidate_run="c1")
        self.assertEqual(payload["rounds"]["1"]["mean_ttft_ms"], 5178.0)
        self.assertAlmostEqual(payload["delta"]["round1_pct"], (1410.0 - 5178.0) / 5178.0 * 100.0, places=3)
        self.assertEqual(payload["verdict"]["verdict"], "✅ round1 改善")

    def test_trend_tool(self):
        payload = self.call("sglbench_trend", scenario="mini-l2-64c")
        self.assertIn("mean_ttft_ms", payload)
        self.assertEqual(len(payload["mean_ttft_ms"]), 2)

    def test_campaign_report_tool(self):
        payload = self.call("sglbench_campaign_report", pair="l2off=l2pp1",
                            output_dir=str(self.fake.home / "suite"))
        self.assertIn("index", payload)

    def test_forensics_tool_no_collect(self):
        # 无采集记录 → 返回明确提示（不触发 kubectl/ssh）
        payload = self.call("sglbench_forensics", run_id="b1")
        self.assertFalse(payload.get("collected"))

    def test_report_tool(self):
        payload = self.call("sglbench_report", baseline_run="b1", candidate_run="c1",
                            output=str(self.fake.home / "r.html"))
        self.assertTrue(str(payload["output"]).endswith("r.html"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_mcp`
Expected: FAIL — len(TOOLS)==7

- [ ] **Step 3: Implement（mcp.py）**

TOOLS 追加（沿用既有 schema 风格）：

```python
    {
        "name": "sglbench_report",
        "description": "生成两个 run 的对比 HTML 报告（直方图/CDF/分位/逐轮曲线, 自包含）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "baseline_run": {"type": "string"},
                "candidate_run": {"type": "string"},
                "output": {"type": "string", "description": "输出 HTML 路径（默认 reports/）"},
            },
            "required": ["baseline_run", "candidate_run"],
        },
    },
    {
        "name": "sglbench_rounds",
        "description": "多轮对话逐轮指标（TTFT/cache-hit 序列）；给 candidate_run 时附 Δ 表与断崖判定。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "candidate_run": {"type": "string"},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "sglbench_campaign_report",
        "description": "战役完整报告（index.html+SUMMARY.md+每场景明细, 对标手工 TEST-REPORT）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pair": {"type": "string", "description": "labelA=labelB（支持 * 前缀通配）"},
                "output_dir": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["pair", "output_dir"],
        },
    },
    {
        "name": "sglbench_trend",
        "description": "同一场景跨 run 指标时间线（回归跟踪; 非 ok run 标记）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scenario": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
                "metrics": {"type": "array", "items": {"type": "string"}},
                "since": {"type": "string", "description": "YYYY-MM-DD"},
                "output": {"type": "string", "description": "可选 HTML 输出路径"},
            },
            "required": ["scenario"],
        },
    },
    {
        "name": "sglbench_forensics",
        "description": "崩溃取证摘要（签名/证据目录）。collect=true 时现场补采（kubectl/ssh）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "collect": {"type": "boolean", "default": False},
            },
            "required": ["run_id"],
        },
    },
```

handlers：

```python
def tool_report(a: dict[str, Any]) -> dict[str, Any]:
    from .analyze import compare_runs, scenario_for_run
    from .report import build_html, write_html

    paths = _paths()
    with Store(paths.db, read_only=True) as store:
        recs = [store.get_run(a["baseline_run"], with_samples=True),
                store.get_run(a["candidate_run"], with_samples=True)]
        recs = [r for r in recs if r is not None]
        if len(recs) < 2:
            return _err("run 不存在（用 sglbench_list_runs 查）")
        scenario = scenario_for_run(recs[0])
        report = compare_runs(store, [a["baseline_run"], a["candidate_run"]],
                              baseline=a["baseline_run"], scenario=scenario)
    out = Path(a.get("output") or (paths.reports_dir / f"mcp-{a['baseline_run']}-vs-{a['candidate_run']}.html"))
    write_html(out, build_html(report, runs=recs))
    return _text(json.dumps({"output": str(out)}, ensure_ascii=False))


def tool_rounds(a: dict[str, Any]) -> dict[str, Any]:
    from .rounds import extract_rounds, round_delta_rows, round_verdict

    paths = _paths()
    with Store(paths.db, read_only=True) as store:
        run = store.get_run(a["run_id"], with_samples=False)
        if run is None:
            return _err(f"未找到 run: {a['run_id']}")
        payload: dict[str, Any] = {"run_id": run.run_id, "rounds": extract_rounds(run.metrics)}
        if a.get("candidate_run"):
            cand = store.get_run(a["candidate_run"], with_samples=False)
            if cand is None:
                return _err(f"未找到 run: {a['candidate_run']}")
            payload["candidate_run"] = cand.run_id
            payload["delta"] = [
                {"round": r, "base": b, "cand": c, "pct": p}
                for r, b, c, p in round_delta_rows(run.metrics, cand.metrics)
            ]
            payload["verdict"] = round_verdict(run.metrics, cand.metrics)
    return _text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def tool_campaign_report(a: dict[str, Any]) -> dict[str, Any]:
    from .campaign import write_suite

    pair = (a.get("pair") or "").split("=", 1)
    if len(pair) != 2:
        return _err("pair 需形如 labelA=labelB")
    paths = _paths()
    with Store(paths.db, read_only=True) as store:
        result = write_suite(store, a["output_dir"], label_pairs=[pair], title=a.get("title"))
    return _text(json.dumps({
        "index": str(result["index"]),
        "summary_md": str(result["summary_md"]) if result["summary_md"] else None,
        "reports": len(result["reports"]),
    }, ensure_ascii=False, indent=2))


def tool_trend(a: dict[str, Any]) -> dict[str, Any]:
    from dataclasses import asdict

    from .analyze import analyze_trend

    paths = _paths()
    with Store(paths.db, read_only=True) as store:
        series = analyze_trend(
            store, a["scenario"],
            labels=a.get("labels") or None,
            metrics=a.get("metrics") or None,
            since=a.get("since"),
        )
    payload = {k: [asdict(p) for p in pts] for k, pts in series.items()}
    if a.get("output"):
        from .report import build_trend_html, write_html

        write_html(a["output"], build_trend_html(series, scenario_key=a["scenario"]))
        payload["html"] = a["output"]
    return _text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def tool_forensics(a: dict[str, Any]) -> dict[str, Any]:
    paths = _paths()
    with Store(paths.db, read_only=not a.get("collect")) as store:
        run = store.get_run(a["run_id"], with_samples=False)
        if run is None:
            return _err(f"未找到 run: {a['run_id']}")
        if a.get("collect"):
            from .forensics import collect_evidence

            report = collect_evidence(paths, run, trigger="manual")
            store.save_forensics(
                run.run_id, trigger="manual", nodes=report.nodes,
                signatures=report.signatures, evidence_dir=report.evidence_dir,
                ok=report.ok, notes="; ".join(report.notes),
            )
        row = store.get_forensics(run.run_id)
    if not row:
        return _text(json.dumps({"collected": False, "hint": "无取证记录; collect=true 现场补采"},
                                ensure_ascii=False))
    return _text(json.dumps({
        "collected": True, "run_id": row["run_id"], "trigger": row["trigger"],
        "ok": row["ok"], "collected_at": row["collected_at"],
        "evidence_dir": row["evidence_dir"], "notes": row["notes"],
        "signatures": json.loads(row["signatures_json"] or "[]"),
    }, ensure_ascii=False, indent=2, default=str))
```

HANDLERS 追加：

```python
    "sglbench_report": tool_report,
    "sglbench_rounds": tool_rounds,
    "sglbench_campaign_report": tool_campaign_report,
    "sglbench_trend": tool_trend,
    "sglbench_forensics": tool_forensics,
```

（`Store(..., read_only=True)` 需该库已存在——测试 setUp 已建。read_only 参数签名核对 store.py Store.__init__。）

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_mcp -v` → PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add sglbench/mcp.py tests/test_mcp.py
git commit -m "sglbench: MCP +5 快工具（report/rounds/campaign_report/trend/forensics）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: MCP `sglbench_recover`（两段式）

**Files:**
- Modify: `sglbench/mcp.py`
- Test: `tests/test_mcp.py`（追加）

**Interfaces:**
- Produces: `sglbench_recover {from_pods?, nodes[], gpus?(数组), rebuild?(默认 true), dry_run?(默认 **true**)}` → dry-run 返回计划 JSON（不动作）；`dry_run=false` 才执行。

- [ ] **Step 1: Write the failing test**（追加）

```python
    def test_recover_default_dry_run(self):
        payload = self.call("sglbench_recover", nodes=["mxgpu-3-29"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["nodes"][0]["host_ip"], "10.66.3.29")
        self.assertIn("计划", payload["plan_text"])

    def test_recover_execute_requires_explicit(self):
        from unittest import mock

        called = {}

        def fake(**kw):
            called.update(kw)

            class R:
                ok = True
                dry_run = False
                nodes = []
                notes = []

                def to_dict(self):
                    return {"ok": True, "dry_run": False, "nodes": []}

            return R()

        with mock.patch("sglbench.recover.run_recovery", side_effect=fake):
            payload = self.call("sglbench_recover", nodes=["mxgpu-3-29"], dry_run=False)
        self.assertFalse(payload["dry_run"])
        self.assertFalse(called.get("dry_run"))  # 传透
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_mcp -k recover` → FAIL

- [ ] **Step 3: Implement**

```python
def tool_recover(a: dict[str, Any]) -> dict[str, Any]:
    """GPU 热复位+重建。安全语义：dry_run 默认 true（先计划，确认后显式 false 执行）。"""
    from .recover import DEFAULT_GPU_INDICES, render_recovery_report, run_recovery

    dry_run = bool(a.get("dry_run", True))
    nodes = a.get("nodes") or []
    if not a.get("from_pods") and not nodes:
        return _err("至少给 from_pods=true 或 nodes=[...]")
    report = run_recovery(
        nodes=nodes or None,
        from_pods=bool(a.get("from_pods", False)),
        gpus=tuple(int(x) for x in (a.get("gpus") or DEFAULT_GPU_INDICES)),
        dry_run=dry_run,
        rebuild=bool(a.get("rebuild", True)),
    )
    payload = report.to_dict()
    payload["plan_text"] = render_recovery_report(report)
    return _text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
```

TOOLS 追加（描述里写明两段式）：

```python
    {
        "name": "sglbench_recover",
        "description": "GPU 热复位+pod 重建（硬件级, 破坏性）。两段式：dry_run 默认 true 返回计划；"
                       "确认后显式传 dry_run=false 执行。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_pods": {"type": "boolean", "description": "自动发现异常 pod 所在节点"},
                "nodes": {"type": "array", "items": {"type": "string"}, "description": "节点名/IP"},
                "gpus": {"type": "array", "items": {"type": "integer"}, "description": "默认 [0,4]"},
                "rebuild": {"type": "boolean", "default": True},
                "dry_run": {"type": "boolean", "default": True},
            },
        },
    },
```

HANDLERS 注册 `"sglbench_recover": tool_recover`。测试计数断言同步改为 13。

- [ ] **Step 4: Run tests + 全量回归**

Run: `python3 -m unittest tests.test_mcp -q`；`python3 -m unittest discover -s tests -t . -q` → OK

- [ ] **Step 5: Commit**

```bash
git add sglbench/mcp.py tests/test_mcp.py
git commit -m "sglbench: MCP sglbench_recover（两段式, dry_run 默认 true）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: OpenClaw SKILL.md 生成器 + 同步测试

**Files:**
- Create: `tools/gen_openclaw_skill.py`
- Create: `integrations/openclaw/skills/sglbench/RULES.md`（人工规范段——由现有 SKILL.md 迁移并更新新命令）
- Modify: `integrations/openclaw/skills/sglbench/SKILL.md`（生成产物）
- Test: `tests/test_openclaw_skill.py`

**Interfaces:**
- Produces: `generate_skill() -> str`（确定性）；CLI `python3 tools/gen_openclaw_skill.py [--check]`（check 模式：不同步则 exit 1）。
- SKILL.md = frontmatter（name/description 固定）+ `RULES.md` 全文 + "## 场景目录"（BUILTIN_SCENARIOS 渲染表）+ "## 命令参考"（build_parser 遍历）。

- [ ] **Step 1: Write RULES.md**（人工维护段；语义与 llm-d skill / agent-memory 一致）

```markdown
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
   批后缀（-rt32/-bd256r16…）区分场景批次，报告侧支持前缀通配配对。
4. **数据不手搓**：分析一律 `compare`/`dashboard`/`campaign report`/`trend`/`sql`
   从 DuckDB 出，不手工汇总 CSV。

## 标准工作流（agent）

1. `status` → 集群健康？（router_ok / leaders）
2. 缺数据 → `campaign run campaigns/l2-full.json --side <baseline|candidate>`
   （内置跳过：已有 ok run 且 env 指纹一致的场景自动跳过；`--dry-run` 先看计划）
3. 崩溃自愈（显式开启）：`run/multiturn --auto-retry 3 --gpu-reset`
   （崩溃 → ssh 节点 GPU 热复位 → 删 pod 重建 → 等健康 → 重跑）
4. 非 ok run 自动取证（`run_environments` 同库 `run_forensics` 表：签名/证据目录）；
   手动补采 `forensics <run_id>`；agent 归因：`sql "SELECT ... FROM run_forensics"`
5. 单场景对比 → `compare`；跨场景 → `campaign report --pair 'A*=B*' -o dir/`
   （产出 index.html + SUMMARY.md + 每场景明细, 完整测试报告）
6. 回归跟踪 → `trend --scenario K --label ...`

## 铁律之外的运维要点

- **崩溃恢复**：负载下 MACA 平台竞态会崩 prefill。GPU 热复位配方
  `mx-smi -y -r -i 0` + `-i 4`（"Resource temporarily unavailable" 等 30-60s 重试）。
- **L3 已知阻塞**：L3（Mooncake）写回路径触发平台级崩溃（详见仓库 docs/TEST-REPORT-*），
  L1+L2 是当前最优可用层级。
- **判定阈值**：compare 噪声阈值 ±5%；逐轮断崖判定阈值 ±10%（round1 相对变化）。
- **运行时**：`recover`（硬件级）与 `--gpu-reset` 必须显式触发，绝不默认开启。
```

- [ ] **Step 2: Write the failing sync test**

```python
"""OpenClaw SKILL.md 生成同步测试（防漂移）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

from gen_openclaw_skill import SKILL_PATH, generate_skill  # noqa: E402


class SkillSyncTest(unittest.TestCase):
    def test_generated_matches_committed(self):
        self.assertTrue(SKILL_PATH.exists(), f"缺失: {SKILL_PATH}（跑 tools/gen_openclaw_skill.py）")
        self.assertEqual(
            generate_skill(), SKILL_PATH.read_text(encoding="utf-8"),
            "SKILL.md 与生成器不同步——运行 python3 tools/gen_openclaw_skill.py 后提交",
        )

    def test_contains_autogen_sections(self):
        text = generate_skill()
        self.assertIn("## 场景目录", text)
        self.assertIn("## 命令参考", text)
        self.assertIn("mini-l2-64c", text)      # 场景表来自 scenarios 目录
        self.assertIn("campaign", text)          # 命令参考来自 CLI parser
        self.assertIn("--gpu-reset", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m unittest tests.test_openclaw_skill` → FAIL（模块不存在）

- [ ] **Step 4: Implement `tools/gen_openclaw_skill.py`**

```python
#!/usr/bin/env python3
"""生成 OpenClaw Skill 包里的 SKILL.md（防漂移：场景目录 / 命令参考自动渲染）。

用法::

    python3 tools/gen_openclaw_skill.py          # 重新生成并写盘
    python3 tools/gen_openclaw_skill.py --check  # 校验同步（CI/测试用, 不同步 exit 1）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SKILL_DIR = REPO / "integrations" / "openclaw" / "skills" / "sglbench"
RULES_PATH = SKILL_DIR / "RULES.md"
SKILL_PATH = SKILL_DIR / "SKILL.md"

FRONTMATTER = """---
name: sglbench
description: PD 集群压测与 HiCache 收益对比。当用户要求压测/对比/查看集群测试数据（L2/L3 收益、崩溃边界、性能回归）时使用。工具仓库 ~/sglang-benchmark-tools。生成文件——勿手改，改 RULES.md 后重跑 tools/gen_openclaw_skill.py。
---
"""


def _scenario_table() -> str:
    from sglbench.scenarios import BUILTIN_SCENARIOS

    lines = ["## 场景目录（自动生成）", "", "| key | 说明 | 并发 | rate |", "|---|---|---|---|"]
    for s in BUILTIN_SCENARIOS.values():
        lines.append(f"| `{s.key}` | {s.name} | {s.max_concurrency} | {s.request_rate} |")
    return "\n".join(lines)


def _cli_reference() -> str:
    from sglbench.cli import build_parser

    parser = build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    lines = ["## 命令参考（自动生成）", ""]
    for choice in sub.choices.values():
        help_ = choice.description or ""
        lines.append(f"### `{choice.prog}`")
        lines.append(f"{help_}")
        opts = []
        for action in choice._actions:
            if action.dest in ("help",):
                continue
            if action.option_strings:
                opts.append("`" + " ".join(action.option_strings) + (" <值>" if action.nargs != 0 and action.const is None else "") + "`")
        if opts:
            lines.append("参数: " + " · ".join(opts))
        lines.append("")
    return "\n".join(lines)


def generate_skill() -> str:
    rules = RULES_PATH.read_text(encoding="utf-8").strip()
    return (
        FRONTMATTER
        + "\n"
        + rules
        + "\n\n"
        + _scenario_table()
        + "\n\n"
        + _cli_reference()
        + ""
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    text = generate_skill()
    if args.check:
        current = SKILL_PATH.read_text(encoding="utf-8") if SKILL_PATH.exists() else ""
        if current != text:
            print(f"❌ {SKILL_PATH} 与生成器不同步", file=sys.stderr)
            return 1
        print("✅ 同步")
        return 0
    SKILL_PATH.write_text(text, encoding="utf-8", newline="\n")
    print(f"已生成 {SKILL_PATH}（{len(text)} 字符）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

（`choice.description`：argparse subparser help 存于 `sub._choices_actions` ——实现时用 `{a.dest: a.help for a in sub._choices_actions}` 映射取 help，避免私有 API 混用。）

- [ ] **Step 5: 生成 + 测试 + Commit**

Run: `python3 tools/gen_openclaw_skill.py && python3 -m unittest tests.test_openclaw_skill -q` → PASS

```bash
git add tools/gen_openclaw_skill.py tests/test_openclaw_skill.py integrations/openclaw/skills/sglbench/
git commit -m "sglbench: OpenClaw SKILL.md 生成式化（RULES.md + 场景/命令自动段 + 同步测试）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: `install.sh` + OpenClaw README 更新

**Files:**
- Create: `integrations/openclaw/install.sh`
- Modify: `integrations/openclaw/README.md`
- Test: 手工冒烟（`--check` + 临时 workspace 安装）

**Interfaces:**
- `install.sh --workspace DIR [--repo REPO] [--check]`：生成 SKILL.md（或校验）→ 拷贝到 `<workspace>/skills/sglbench/` → 生成 `<workspace>/mcp-config.json`（绝对路径）→ 打印接入步骤。

- [ ] **Step 1: 写 install.sh**

```bash
#!/usr/bin/env bash
# sglbench → OpenClaw 一键安装：Skill 包 + MCP 配置（幂等, 可重复执行）。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKSPACE=""
CHECK=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --check) CHECK=1; shift ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$WORKSPACE" ]]; then
  echo "用法: install.sh --workspace <OpenClaw workspace> [--repo <sglbench 仓库>] [--check]" >&2
  exit 2
fi

PY="python3"
# 1) 生成/校验 SKILL.md（防漂移）
if [[ "$CHECK" == "1" ]]; then
  (cd "$REPO" && "$PY" tools/gen_openclaw_skill.py --check)
  echo "✅ check 通过（未做任何安装动作）"
  exit 0
fi
(cd "$REPO" && "$PY" tools/gen_openclaw_skill.py)

# 2) 安装 Skill
mkdir -p "$WORKSPACE/skills"
rm -rf "$WORKSPACE/skills/sglbench"
cp -r "$REPO/integrations/openclaw/skills/sglbench" "$WORKSPACE/skills/sglbench"
echo "skill  → $WORKSPACE/skills/sglbench/SKILL.md"

# 3) 生成 MCP 配置（绝对路径）
cat > "$WORKSPACE/mcp-config.json" <<EOF
{
  "mcpServers": {
    "sglbench": {
      "command": "$PY",
      "args": ["-m", "sglbench", "mcp"],
      "cwd": "$REPO"
    }
  }
}
EOF
echo "mcp    → $WORKSPACE/mcp-config.json"

cat <<'TIP'

接入步骤:
  1) 将 mcp-config.json 的内容并入 OpenClaw 的 MCP 配置（Agents API / 插件体系）
  2) 冒烟: python3 -m sglbench status   （JSON 输出即链路通）
  3) 13 个 MCP tools 生效（含两段式 recover: dry_run 默认 true）
TIP
```

`chmod +x`。

- [ ] **Step 2: 更新 README**（tools 表 7→13 + 新工作流 + install.sh 用法 + campaign 执行器 CLI-only 说明 + L3/自愈注意）

按现有 README 表格逐行追补：tools 表加 6 行（report/rounds/campaign_report/trend/forensics/recover）；"典型 agent 工作流"改为：status → campaign run（skip/自愈）→ forensics 归因 → compare → campaign report → trend；"注意事项"补：recover 两段式语义、campaign 执行器不进 MCP（CLI: `campaign run`）。

- [ ] **Step 3: 冒烟**

```bash
bash integrations/openclaw/install.sh --check
bash integrations/openclaw/install.sh --workspace /tmp/openclaw-ws-test
cat /tmp/openclaw-ws-test/mcp-config.json
ls /tmp/openclaw-ws-test/skills/sglbench/
rm -rf /tmp/openclaw-ws-test
```

- [ ] **Step 4: Commit**

```bash
git add integrations/openclaw/
git commit -m "sglbench: OpenClaw install.sh 一键安装 + README 更新（13 tools/新工作流）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: 文档收尾 + 全量回归

**Files:**
- Modify: `README.md`（主 README：命令面更新——campaign/trend/recover/forensics/--gpu-reset/--no-forensics/sweep --label）
- Modify: `docs/agent-memory.md`（路线图勾销 + run_forensics 表 + 新基元）
- Modify: `~/.claude/...`? **否**——llm-d 仓库 skill 更新作为可选后续（跨仓库，另行）。

- [ ] **Step 1: 主 README**

在"三步上手"后追加"新能力速览"小节（6 条：逐轮曲线/完整战役报告/趋势/双线叠加/崩溃自愈/取证），命令面表格补：`campaign report|run`、`trend`、`recover`、`forensics`。

- [ ] **Step 2: agent-memory.md**

- §5 "后续方向" 更新：崩溃自愈内嵌 ✅、环境捕获扩展（部分）、MCP 13 tools ✅、OpenClaw 生成式包 ✅。
- §2 表结构加 `run_forensics` 行。
- 新增 §6 "崩溃自愈与取证（2026-10-06）"简述流程与安全语义。

- [ ] **Step 3: 全量回归 + 提交**

Run: `python3 -m unittest discover -s tests -t . -q` → OK；`python3 tools/gen_openclaw_skill.py --check` → ✅

```bash
git add README.md docs/agent-memory.md
git commit -m "docs: 三域扩展收尾（README 新能力速览 + agent-memory 路线图勾销）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

- [ ] **Step 4: 分支收尾**

完成后调用 superpowers:finishing-a-development-branch：全量验证 → 合并回 main（用户确认）。

---

## 自审记录

1. **Spec 覆盖**：§3.4 MCP 6 工具（T1×5 + T2×1, 含两段式 recover）✓；§4 OpenClaw 生成式（T3）+ install.sh（T4）+ README（T4）✓；§8 S7–S9 ✓。"campaign 执行器不进 MCP" 在 T4 文档中显式声明 ✓。
2. **占位符扫描**：T3 Step 4 有一个实现标注（argparse `_choices_actions` 取 help 的方式），指出了替代 API 路径；其余无 TBD。
3. **类型一致性**：`_paths()` patch 方式与现有 `mcp._paths` 一致；`store.get_run(with_samples=)`/`get_forensics`/`save_forensics`/`write_suite`/`analyze_trend`/`round_*`/`collect_evidence` 全部沿用 Plan 1/2 已交付签名；工具计数断言 12→13 在两处同步（T1 Step1 断言 12、T2 改为 13——实现时 T2 修改该断言）。
