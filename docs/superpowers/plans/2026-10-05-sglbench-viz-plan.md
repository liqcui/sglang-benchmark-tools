# sglbench 可视化增强实施计划（Plan 1：S1–S3）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 HiCache 多轮断崖信号接入 compare/report/dashboard 四层出口，新增趋势时间线、双 label sweep 叠加、Δ 热图，并以 `sglbench campaign report` 一键产出对标手工 L2 全量报告的完整战役报告（index.html + SUMMARY.md + 每场景报告）。

**Architecture:** 全部增量模块：新增纯数据模块 `rounds.py`；`svgchart.py` 增 `heatmap_chart`/`trend_chart`；`analyze.py` 增 `analyze_trend`、sweep label 过滤 + `build_sweep_overlay`；`report.py`/`dashboard.py` 增渲染出口；新增 `campaign.py` 做 suite 数据构建 + HTML/MD 渲染 + 写盘。复用既有 `dashboard.resolve_pairs`（label→最新 ok run 配对）与 `report.build_html`（每场景明细）。

**Tech Stack:** Python ≥3.10 标准库 + duckdb；自包含内联 SVG（零 CDN/零 JS）；unittest（`python3 -m unittest discover -s tests -t .`，仓库根目录运行）。

**Spec:** `docs/superpowers/specs/2026-10-05-sglbench-viz-agent-openclaw-design.md`（§2 可视化设计、§7 测试计划、§8 分期 S1–S3）

## Global Constraints

- 所有测试命令在仓库根 `/home/supperadm/sglang-benchmark-tools` 运行：`python3 -m unittest tests.test_xxx -v`（tests 含 `__init__.py`，支持点路径）。
- 回归红线：现有 191 个测试必须保持全绿（`python3 -m unittest discover -s tests -t . -q`）。
- 中文注释/文案风格与现有模块一致；HTML 报告自包含（内联 SVG、零 CDN、零 JS）。
- 判定阈值统一沿用 `analyze.DEFAULT_THRESHOLD_PCT`（5.0）语义；逐轮断崖判定用 `rounds` 模块内 `noise_pct=10.0`。
- 指标方向：TTFT/TPOT 越低越好（`higher_is_better=False`）；吞吐/成功率越高越好（`True`）。
- 已知引擎限制文案（逐轮图注必须带）："本集群 fork 版 `/v1/completions` 不填 `usage.cached_tokens`，`cache_hit_rate` 恒 0——命中判据看 round1 起的 TTFT 断崖"。
- 提交信息尾部统一 `Co-Authored-By: Claude Code <noreply@anthropic.com>`（本仓库惯例）。

---

### Task 1: `rounds.py` —— 逐轮指标提取

**Files:**
- Create: `sglbench/rounds.py`
- Test: `tests/test_rounds.py`

**Interfaces:**
- Consumes: 无（纯函数，仅标准库）。
- Produces: `extract_rounds(metrics: Mapping[str, Any]) -> dict[int, dict[str, float]]`（后续所有任务依赖此签名）。

- [ ] **Step 1: Write the failing test**

```python
"""逐轮指标提取与判定的单测。"""
from __future__ import annotations

import unittest

from sglbench.rounds import extract_rounds


class ExtractRoundsTest(unittest.TestCase):
    def test_parses_round_metrics(self):
        metrics = {
            "round0_mean_ttft_ms": 606.0,
            "round1_mean_ttft_ms": 5178.0,
            "round1_median_ttft_ms": 5100.0,
            "round3_cache_hit_rate": 0.0,
            "success_rate": 1.0,          # 非逐轮键忽略
            "mean_ttft_ms": 123.0,        # 非逐轮键忽略
            "roundX_mean_ttft_ms": 1.0,   # 非法轮次忽略
            "round2_mean_ttft_ms": "bad", # 非数值忽略
        }
        out = extract_rounds(metrics)
        self.assertEqual(sorted(out), [0, 1, 3])
        self.assertEqual(out[0], {"mean_ttft_ms": 606.0})
        self.assertEqual(out[1]["mean_ttft_ms"], 5178.0)
        self.assertEqual(out[1]["median_ttft_ms"], 5100.0)
        self.assertEqual(out[3], {"cache_hit_rate": 0.0})

    def test_empty_and_none(self):
        self.assertEqual(extract_rounds({}), {})
        self.assertEqual(extract_rounds(None), {})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_rounds -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sglbench.rounds'`

- [ ] **Step 3: Write minimal implementation**

```python
"""逐轮（多轮对话）指标提取与呈现数据。

HiCache 多轮基准把每轮的 TTFT/命中率存成 ``round<N>_<field>`` 长表指标，
本模块把这些散键还原成"逐轮序列"，供 compare / report / dashboard / suite 四层复用。

判读要点（本集群引擎限制）：fork 版 ``/v1/completions`` 不填 usage.cached_tokens，
``cache_hit_rate`` 恒为 0 —— 命中看的是 round1 起的 TTFT 断崖，不是命中率。
"""
from __future__ import annotations

import re
from typing import Any, Mapping

ROUND_RE = re.compile(r"^round(\d+)_(mean_ttft_ms|median_ttft_ms|cache_hit_rate)$")
ROUND_FIELDS = ("mean_ttft_ms", "median_ttft_ms", "cache_hit_rate")
STEADY_FROM = 3  # round>=3 视为稳态（round1 首次热、round2 个别残留冷）


def extract_rounds(metrics: Mapping[str, Any] | None) -> dict[int, dict[str, float]]:
    """解析 ``round<N>_<field>`` 指标为 ``{轮次: {字段: 值}}``，轮次升序。"""
    out: dict[int, dict[str, float]] = {}
    for key, value in (metrics or {}).items():
        m = ROUND_RE.match(str(key))
        if not m:
            continue
        try:
            fv = float(value)
        except (TypeError, ValueError):
            continue
        out.setdefault(int(m.group(1)), {})[m.group(2)] = fv
    return dict(sorted(out.items()))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_rounds -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add sglbench/rounds.py tests/test_rounds.py
git commit -m "sglbench: rounds.py 逐轮指标提取（round<N>_* 解析）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: `rounds.py` —— Δ 表 / 判定 / 曲线序列

**Files:**
- Modify: `sglbench/rounds.py`（追加函数）
- Test: `tests/test_rounds.py`（追加用例）

**Interfaces:**
- Consumes: Task 1 的 `extract_rounds`。
- Produces:
  - `round_delta_rows(base, cand, field="mean_ttft_ms") -> list[tuple[int, float, float, float]]`（round, base, cand, delta_pct）
  - `round_verdict(base, cand, *, field="mean_ttft_ms", noise_pct=10.0) -> dict`（键：`round0_pct`/`round1_pct`/`steady_pct`/`verdict`）
  - `round_series(base, cand, field="mean_ttft_ms", base_name="基线", cand_name="候选") -> tuple[list[int], list[dict]]`（`line_chart` 直接可用）

- [ ] **Step 1: Write the failing test**（追加到 `tests/test_rounds.py`）

```python
from sglbench.rounds import round_delta_rows, round_series, round_verdict

BASE = {f"round{r}_mean_ttft_ms": v for r, v in {0: 606.0, 1: 5178.0, 2: 1600.0, 3: 1700.0, 4: 1700.0}.items()}
CAND = {f"round{r}_mean_ttft_ms": v for r, v in {0: 656.0, 1: 1410.0, 2: 1650.0, 3: 1720.0, 4: 1690.0}.items()}


class RoundDeltaTest(unittest.TestCase):
    def test_delta_rows_values_and_pct(self):
        rows = round_delta_rows(BASE, CAND)
        self.assertEqual([r for r, *_ in rows], [0, 1, 2, 3, 4])
        r1 = next(row for row in rows if row[0] == 1)
        self.assertAlmostEqual(r1[1], 5178.0)
        self.assertAlmostEqual(r1[2], 1410.0)
        self.assertAlmostEqual(r1[3], (1410.0 - 5178.0) / 5178.0 * 100.0)

    def test_delta_rows_skips_missing_side(self):
        rows = round_delta_rows({"round1_mean_ttft_ms": 100.0}, {"round2_mean_ttft_ms": 90.0})
        self.assertEqual(rows, [])

    def test_verdict_improvement(self):
        v = round_verdict(BASE, CAND)
        self.assertAlmostEqual(v["round0_pct"], (656.0 - 606.0) / 606.0 * 100.0)
        self.assertAlmostEqual(v["round1_pct"], (1410.0 - 5178.0) / 5178.0 * 100.0)
        self.assertAlmostEqual(v["steady_pct"], ( (1720.0 + 1690.0) / 2 - (1700.0 + 1700.0) / 2 ) / 1700.0 * 100.0)
        self.assertEqual(v["verdict"], "✅ round1 改善")

    def test_verdict_regression_and_no_data(self):
        bad = {f"round{r}_mean_ttft_ms": BASE[f"round{r}_mean_ttft_ms"] * 1.2 for r in range(5)}
        self.assertEqual(round_verdict(BASE, bad)["verdict"], "❌ round1 劣化")
        self.assertEqual(round_verdict({}, {})["verdict"], "无逐轮数据")

    def test_series_aligned(self):
        rounds, series = round_series(BASE, CAND, base_name="A", cand_name="B")
        self.assertEqual(rounds, [0, 1, 2, 3, 4])
        self.assertEqual([s["name"] for s in series], ["A", "B"])
        self.assertEqual(series[0]["values"], [606.0, 5178.0, 1600.0, 1700.0, 1700.0])
        self.assertEqual(series[1]["values"], [656.0, 1410.0, 1650.0, 1720.0, 1690.0])

    def test_series_fills_missing_side(self):
        rounds, series = round_series({"round1_mean_ttft_ms": 100.0}, {"round2_mean_ttft_ms": 90.0})
        self.assertEqual(rounds, [1, 2])
        self.assertEqual(series[0]["values"], [100.0, None])
        self.assertEqual(series[1]["values"], [None, 90.0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_rounds -v`
Expected: FAIL — `ImportError: cannot import name 'round_delta_rows'`

- [ ] **Step 3: Write implementation**（追加到 `sglbench/rounds.py`）

```python
def round_delta_rows(
    base: Mapping[str, Any] | None, cand: Mapping[str, Any] | None, field: str = "mean_ttft_ms"
) -> list[tuple[int, float, float, float]]:
    """逐轮 Δ 行：``[(round, base_v, cand_v, delta_pct)]``，仅保留两侧都有值的轮次。"""
    rb, rc = extract_rounds(base), extract_rounds(cand)
    rows: list[tuple[int, float, float, float]] = []
    for rnd in sorted(set(rb) & set(rc)):
        b, c = rb[rnd].get(field), rc[rnd].get(field)
        if b is None or c is None or b == 0:
            continue
        rows.append((rnd, b, c, (c - b) / b * 100.0))
    return rows


def _steady(rv: dict[int, dict[str, float]], field: str) -> float | None:
    xs = [v[field] for r, v in rv.items() if r >= STEADY_FROM and field in v]
    return sum(xs) / len(xs) if xs else None


def round_verdict(
    base: Mapping[str, Any] | None,
    cand: Mapping[str, Any] | None,
    *,
    field: str = "mean_ttft_ms",
    noise_pct: float = 10.0,
) -> dict[str, Any]:
    """cold(round0) / 首次热(round1) / 稳态(round>=3) 三点 Δ% 与总判定。"""
    rb, rc = extract_rounds(base), extract_rounds(cand)

    def pct(b: float | None, c: float | None) -> float | None:
        if b is None or c is None or not b:
            return None
        return (c - b) / b * 100.0

    r1 = pct(rb.get(1, {}).get(field), rc.get(1, {}).get(field))
    if r1 is None:
        verdict = "无逐轮数据"
    elif r1 <= -noise_pct:
        verdict = "✅ round1 改善"
    elif r1 >= noise_pct:
        verdict = "❌ round1 劣化"
    else:
        verdict = "➖ 持平"
    return {
        "round0_pct": pct(rb.get(0, {}).get(field), rc.get(0, {}).get(field)),
        "round1_pct": r1,
        "steady_pct": pct(_steady(rb, field), _steady(rc, field)),
        "verdict": verdict,
    }


def round_series(
    base: Mapping[str, Any] | None,
    cand: Mapping[str, Any] | None,
    field: str = "mean_ttft_ms",
    base_name: str = "基线",
    cand_name: str = "候选",
) -> tuple[list[int], list[dict[str, Any]]]:
    """``line_chart`` 输入：x=轮次列表，series=[基线, 候选]（缺失轮次为 None）。"""
    rb, rc = extract_rounds(base), extract_rounds(cand)
    rounds = sorted(set(rb) | set(rc))
    if not rounds:
        return [], []

    def vals(rv: dict[int, dict[str, float]]) -> list[float | None]:
        return [rv.get(r, {}).get(field) for r in rounds]

    return rounds, [
        {"name": base_name, "values": vals(rb), "color": "#5b8def"},
        {"name": cand_name, "values": vals(rc), "color": "#e8734a"},
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_rounds -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add sglbench/rounds.py tests/test_rounds.py
git commit -m "sglbench: rounds 逐轮 Δ 表/判定/曲线序列（round1 断崖 + 稳态三点判定）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: `compare` console 逐轮表（report.py 集成）

**Files:**
- Modify: `sglbench/report.py`（`render_console` 内、`── 结论` 段之前插入；新私有函数 `_round_console_block`）
- Test: `tests/test_report.py`（追加用例）

**Interfaces:**
- Consumes: Task 2 的 `round_delta_rows`、`round_verdict`；`report.ComparisonReport.runs`（`RunView`，含 `role`/`metrics`/`display`）。
- Produces: `render_console(report)` 对 multiturn run 自动追加"逐轮 TTFT"段；新增 `_round_console_block(report) -> str | None`。

- [ ] **Step 1: Write the failing test**（追加到 `tests/test_report.py`；参照该文件现有 ComparisonReport 构造方式）

```python
def _round_report():
    """基线+候选均为 multiturn 指标的最小 ComparisonReport。"""
    from sglbench.analyze import ComparisonReport, RunView, Scenario
    base = {f"round{r}_mean_ttft_ms": v for r, v in {0: 606.0, 1: 5178.0, 3: 1700.0}.items()}
    cand = {f"round{r}_mean_ttft_ms": v for r, v in {0: 656.0, 1: 1410.0, 3: 1720.0}.items()}
    base.update({"success_rate": 1.0})
    cand.update({"success_rate": 1.0})
    report = ComparisonReport(
        scenario_key="hicache-multiturn-20x10",
        scenario=None,
        baseline_run="b0",
        candidate_runs=["c0"],
        threshold_pct=5.0,
        runs=[
            RunView(run_id="b0", label="l2off", scenario_key="hicache-multiturn-20x10",
                    status="ok", role="baseline", metrics=base),
            RunView(run_id="c0", label="l2pp1", scenario_key="hicache-multiturn-20x10",
                    status="ok", role="candidate", metrics=cand),
        ],
    )
    return report


class RoundConsoleTest(unittest.TestCase):
    def test_round_block_appears(self):
        text = render_console(_round_report())
        self.assertIn("逐轮 TTFT", text)
        self.assertIn("round1", text.lower())  # 表内含轮次行
        self.assertIn("-72.8%", text)          # (1410-5178)/5178*100 ≈ -72.77 → 视格式断言更稳

    def test_round_block_absent_without_rounds(self):
        report = _round_report()
        report.runs[0].metrics = {"success_rate": 1.0}  # 去掉逐轮
        report.runs[1].metrics = {"success_rate": 1.0}
        self.assertNotIn("逐轮 TTFT", render_console(report))
```

注意：若 `ComparisonReport` 构造字段与该用例不符（dataclass 必填项不同），以 `analyze.py:141` 附近实际定义为准调整 fixture——断言不变。

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_report -v -k RoundConsole`
Expected: FAIL — "逐轮 TTFT" not in output

- [ ] **Step 3: Write implementation**

在 `sglbench/report.py` 顶部导入区追加：

```python
from .rounds import round_delta_rows, round_verdict
```

新增函数（放在 `render_console` 上方）：

```python
def _round_console_block(report: ComparisonReport) -> str | None:
    """multiturn run 的逐轮 TTFT 段（无逐轮数据返回 None）。"""
    base_view = next((r for r in report.runs if r.role == "baseline"), None)
    cand_views = [r for r in report.runs if r.role == "candidate"]
    if base_view is None or not cand_views:
        return None
    cand_view = cand_views[0]
    rows = round_delta_rows(base_view.metrics, cand_view.metrics)
    if not rows:
        return None
    table_rows = [
        [
            f"round{rnd:g}",
            f"{b:,.2f}",
            f"{c:,.2f}",
            f"{pct:+.1f}%",
            "✅" if pct <= -10 else ("❌" if pct >= 10 else "➖"),
        ]
        for rnd, b, c, pct in rows
    ]
    v = round_verdict(base_view.metrics, cand_view.metrics)
    lines = [
        render_table(
            ["轮次", f"{base_view.display} (ms)", f"{cand_view.display} (ms)", "Δ", "判定"],
            table_rows,
            ["left", "right", "right", "right", "left"],
        )
    ]
    parts = []
    if v["round1_pct"] is not None:
        parts.append(f"round1（首次热）Δ {v['round1_pct']:+.1f}% · 稳态 Δ {v['steady_pct']:+.1f}% · {v['verdict']}")
    parts.append("注：本引擎不填 usage.cached_tokens，cache_hit_rate 恒 0——命中判据看 round1 起的 TTFT 断崖。")
    extra = "\n".join(parts)
    if len(cand_views) > 1:
        extra += f"\n（候选多于一个，逐轮仅对比 {cand_view.display}）"
    return lines[0] + "\n" + extra
```

在 `render_console` 的 `if report.conclusions:` 段之前插入：

```python
    round_block = _round_console_block(report)
    if round_block:
        parts.append("")
        parts.append("── 逐轮 TTFT（多轮对话） " + "─" * 52)
        parts.append(round_block)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_report -v`
Expected: 新用例 PASS，既有用例不回归。若 `-72.8%` 断言与 `{pct:+.1f}%` 舍入不符，改为 `assertIn("-72.7", text) or assertIn("-72.8", text)` 前先跑一次看实际值再定死。

- [ ] **Step 5: Commit**

```bash
git add sglbench/report.py tests/test_report.py
git commit -m "sglbench: compare 对 multiturn 自动附加逐轮 TTFT 表（断崖/稳态判定）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: `report` HTML 逐轮曲线卡

**Files:**
- Modify: `sglbench/report.py`（`build_html` 图表装配处 + 新函数 `_round_chart_cards`）
- Test: `tests/test_report.py`（追加用例）

**Interfaces:**
- Consumes: Task 2 `round_series`、`round_delta_rows`；`build_html(report, runs=...)` 既有结构；`svgchart.line_chart`。
- Produces: `build_html` 在任一 run 含逐轮指标时增加"逐轮 TTFT 曲线"卡（曲线 + Δ 表）；无逐轮时输出与现状完全一致。

- [ ] **Step 1: Write the failing test**（追加到 `tests/test_report.py`）

```python
class RoundHtmlTest(unittest.TestCase):
    def test_round_chart_in_html(self):
        report = _round_report()
        html = build_html(report)
        self.assertIn("逐轮 TTFT", html)
        self.assertIn("<polyline", html)  # 曲线存在（若既有图表已含 polyline，改为断言 "逐轮 TTFT 曲线" 区块）

    def test_no_round_chart_without_rounds(self):
        report = _round_report()
        report.runs[0].metrics = {"success_rate": 1.0}
        report.runs[1].metrics = {"success_rate": 1.0}
        html = build_html(report, include_charts=True)
        self.assertNotIn("逐轮 TTFT", html)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_report -v -k RoundHtml`
Expected: FAIL — "逐轮 TTFT" not in html

- [ ] **Step 3: Write implementation**

在 `sglbench/report.py` 顶部导入追加 `round_series`（与 Task 3 导入合并成一行）：

```python
from .rounds import round_delta_rows, round_series, round_verdict
```

新增函数（放在 `build_html` 上方）：

```python
def _round_chart_cards(report: ComparisonReport, width: int = 1160) -> list[str]:
    """逐轮 TTFT 曲线 + Δ 表（multiturn run 才产出；否则空列表）。"""
    base_view = next((r for r in report.runs if r.role == "baseline"), None)
    cand_views = [r for r in report.runs if r.role == "candidate"]
    if base_view is None or not cand_views:
        return []
    cand_view = cand_views[0]
    x, series = round_series(base_view.metrics, cand_view.metrics,
                             base_name=base_view.display, cand_name=cand_view.display)
    if not x:
        return []
    chart = line_chart(
        [float(v) for v in x], series,
        title="逐轮 TTFT 均值",
        xlabel="轮次", ylabel="TTFT", unit="ms",
        width=width, height=360, annotate_points=False,
        subtitle="round0 冷 → round1 首次热 → 稳态；断崖越深命中越充分。本引擎 cache_hit_rate 恒 0（测量限制），看 TTFT 断崖。",
    )
    rows = round_delta_rows(base_view.metrics, cand_view.metrics)
    table_html = (
        "<table><tr><th>轮次</th><th>" + _esc(base_view.display) + "</th><th>" + _esc(cand_view.display)
        + "</th><th>Δ</th></tr>"
        + "".join(
            f"<tr><td>round{rnd:g}</td><td>{b:,.2f}</td><td>{c:,.2f}</td><td>{pct:+.1f}%</td></tr>"
            for rnd, b, c, pct in rows
        )
        + "</table>"
    )
    return [chart, table_html]
```

在 `build_html` 的图表装配区（`if include_charts:` 块内、循环之后）插入：

```python
        for card in _round_chart_cards(report):
            charts.append(_chart_card(card, full=True))
```

（`_chart_card` 已存在；`full=True` 宽度适配 1160。表格卡与曲线卡同放 charts 区。）

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_report -v`
Expected: 新用例 PASS；既有 HTML 用例不回归。

- [ ] **Step 5: Commit**

```bash
git add sglbench/report.py tests/test_report.py
git commit -m "sglbench: report HTML 逐轮 TTFT 曲线卡（multiturn 自动附加）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: `dashboard` 多轮断崖区

**Files:**
- Modify: `sglbench/dashboard.py`（`render_dashboard` 追加断崖区；新私有函数 `_round_section`）
- Test: `tests/test_dashboard.py`（新建；repo 无 dashboard 测试文件）

**Interfaces:**
- Consumes: Task 2 `round_series`；`render_dashboard(pairs, ...)` 既有入参（pairs 项含 `name`/`metrics_off`/`metrics_on`）。
- Produces: `render_dashboard` 在存在逐轮指标的成对场景时，追加"多轮断崖"区（每场景一张曲线卡）；否则输出不变。

- [ ] **Step 1: Write the failing test**（新建 `tests/test_dashboard.py`）

```python
"""dashboard 多轮断崖区单测。"""
from __future__ import annotations

import unittest

from sglbench.dashboard import render_dashboard


def _pair(name="hicache-multiturn-20x10"):
    return {
        "name": name,
        "off_run": "b0", "on_run": "c0",
        "metrics_off": {f"round{r}_mean_ttft_ms": v for r, v in {0: 606.0, 1: 5178.0, 3: 1700.0}.items()},
        "metrics_on": {f"round{r}_mean_ttft_ms": v for r, v in {0: 656.0, 1: 1410.0, 3: 1720.0}.items()},
    }


class RoundSectionTest(unittest.TestCase):
    def test_round_section_rendered(self):
        html = render_dashboard([_pair()])
        self.assertIn("多轮断崖", html)
        self.assertIn("逐轮 TTFT", html)

    def test_no_round_section_for_plain_pair(self):
        p = _pair("mini-l2-64c")
        p["metrics_off"] = {"mean_ttft_ms": 3380.0}
        p["metrics_on"] = {"mean_ttft_ms": 2720.0}
        html = render_dashboard([p])
        self.assertNotIn("多轮断崖", html)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_dashboard -v`
Expected: FAIL — "多轮断崖" not in html

- [ ] **Step 3: Write implementation**

`dashboard.py` 顶部导入追加：

```python
from .rounds import round_series
```

新增函数（放在 `render_dashboard` 上方）：

```python
def _round_section(pairs: Sequence[Mapping[str, Any]]) -> str:
    """多轮断崖区：每个含逐轮指标的成对场景一张 TTFT 曲线卡。"""
    cards: list[str] = []
    for p in pairs:
        x, series = round_series(p.get("metrics_off") or {}, p.get("metrics_on") or {})
        if not x:
            continue
        cards.append(
            '<div class="card">'
            + line_chart(
                [float(v) for v in x], series,
                title=f"{p['name']} · 逐轮 TTFT",
                xlabel="轮次", ylabel="TTFT", unit="ms",
                width=1160, height=330, annotate_points=False,
                subtitle="round1 断崖越深命中越充分（本引擎 cache_hit_rate 恒 0 属测量限制）",
            )
            + "</div>"
        )
    if not cards:
        return ""
    return (
        '<h2>多轮断崖（round0 冷 → round1 首次热 → 稳态）</h2>'
        '<div class="charts">' + "".join(cards) + "</div>"
    )
```

导入补充：`from .svgchart import grouped_bar_chart, line_chart`。

在 `render_dashboard` 的 `<h2>全指标表格` 之前插入：

```python
{_round_section(pairs)}
```

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_dashboard -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add sglbench/dashboard.py tests/test_dashboard.py
git commit -m "sglbench: dashboard 多轮断崖区（成对场景自动附加逐轮曲线）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: `svgchart.heatmap_chart`（Δ 热图）

**Files:**
- Modify: `sglbench/svgchart.py`（文件末追加）
- Test: `tests/test_svgchart.py`（新建）

**Interfaces:**
- Consumes: `svgchart._doc`、`_esc`、`_fmt_tick`、`_empty_chart` 既有私有工具。
- Produces:

```python
def heatmap_chart(
    row_labels: Sequence[str],
    col_labels: Sequence[str],
    values: Sequence[Sequence[float | None]],
    *,
    title: str,
    subtitle: str = "",
    better_dirs: Sequence[bool] | None = None,  # 每列: True=越大越好
    formats: Sequence[str] | None = None,       # 每列格式化, 默认 "{:+.1f}%"
    width: int = 1160,
    cell_h: int = 34,
    label_w: int = 240,
) -> str
```

颜色语义：**绿=改善、红=劣化**（按 `better_dirs` 定向后的符号）；`|Δ|≤3%` 中性浅灰；阶梯 10%/30% 加深；`None` → 灰底 "—"。

- [ ] **Step 1: Write the failing test**

```python
"""heatmap_chart 单测。"""
from __future__ import annotations

import unittest

from sglbench.svgchart import heatmap_chart


class HeatmapTest(unittest.TestCase):
    def test_renders_labels_and_values(self):
        svg = heatmap_chart(
            ["mini-32c", "mini-64c"], ["TTFT", "总吞吐"],
            [[43.9, -6.1], [-19.6, 2.4]],
            title="Δ 热图", better_dirs=[False, True], formats=["{:+.1f}%", "{:+.1f}%"],
        )
        self.assertIn("<svg", svg)
        self.assertIn("mini-64c", svg)
        self.assertIn("+43.9%", svg)
        # TTFT 列劣化(正Δ)用红系；改善(负Δ)用绿系
        self.assertIn("#dc2626", svg)
        self.assertIn("#16a34a", svg)

    def test_direction_oriented_color(self):
        # 吞吐列 higher-better：正Δ 应为绿
        svg = heatmap_chart(["s1"], ["吞吐"], [[25.0]], title="t", better_dirs=[True])
        self.assertIn("#16a34a", svg)
        self.assertNotIn("#dc2626", svg)

    def test_none_and_empty(self):
        svg = heatmap_chart(["s1"], ["TTFT"], [[None]], title="t")
        self.assertIn("—", svg)
        self.assertIn("没有可绘制", heatmap_chart([], [], [], title="t"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_svgchart -v`
Expected: FAIL — ImportError

- [ ] **Step 3: Write implementation**（追加到 `sglbench/svgchart.py`）

```python
def _heat_shade(delta: float, higher_better: bool) -> tuple[str, str]:
    """(底色, 字色)。按方向归一后：正=改善绿系，负=劣化红系。"""
    oriented = delta if higher_better else -delta
    a = abs(oriented)
    if a <= 3.0:
        return "#f1f5f9", "#475569"
    if oriented > 0:
        if a <= 10:
            return "#bbf7d0", "#166534"
        if a <= 30:
            return "#86efac", "#14532d"
        return "#16a34a", "#ffffff"
    if a <= 10:
        return "#fecaca", "#991b1b"
    if a <= 30:
        return "#fca5a5", "#7f1d1d"
    return "#dc2626", "#ffffff"


def heatmap_chart(
    row_labels: Sequence[str],
    col_labels: Sequence[str],
    values: Sequence[Sequence[float | None]],
    *,
    title: str,
    subtitle: str = "",
    better_dirs: Sequence[bool] | None = None,
    formats: Sequence[str] | None = None,
    width: int = 1160,
    cell_h: int = 34,
    label_w: int = 240,
) -> str:
    """Δ 热图：行=场景，列=指标。绿=改善、红=劣化（|Δ|≤3% 中性）。"""
    n_rows, n_cols = len(row_labels), len(col_labels)
    if not n_rows or not n_cols:
        return _empty_chart(title, "没有可绘制的数据", width=width, height=240)
    better = list(better_dirs or [True] * n_cols)
    fmts = list(formats or ["{:+.1f}%"] * n_cols)
    top = 64 if subtitle else 48
    cell_w = max(90.0, (width - label_w - 24.0) / n_cols)
    height = int(top + n_rows * cell_h + 34)
    body: list[str] = [
        f'<text x="{label_w:.0f}" y="24" font-size="14" font-weight="600" fill="#1e293b">{_esc(title)}</text>'
    ]
    if subtitle:
        body.append(f'<text x="{label_w:.0f}" y="42" font-size="11" fill="#64748b">{_esc(subtitle)}</text>')
    for j, cl in enumerate(col_labels):
        x = label_w + j * cell_w + cell_w / 2
        body.append(
            f'<text x="{x:.1f}" y="{top - 8}" text-anchor="middle" font-size="11.5" fill="#334155">{_esc(cl)}</text>'
        )
    for i, rl in enumerate(row_labels):
        y = top + i * cell_h
        body.append(
            f'<text x="{label_w - 10:.0f}" y="{y + cell_h * 0.68:.1f}" text-anchor="end" '
            f'font-size="11.5" fill="#334155">{_esc(rl)}</text>'
        )
        for j in range(n_cols):
            v = values[i][j] if i < len(values) and j < len(values[i]) else None
            x = label_w + j * cell_w
            if v is None:
                fill, ink, text = "#f8fafc", "#94a3b8", "—"
            else:
                fill, ink = _heat_shade(float(v), better[j])
                text = fmts[j].format(float(v))
            body.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell_w - 2:.1f}" height="{cell_h - 2:.1f}" '
                f'fill="{fill}" rx="4"><title>{_esc(rl)} · {_esc(col_labels[j])}: {_esc(text)}</title></rect>'
            )
            body.append(
                f'<text x="{x + cell_w / 2:.1f}" y="{y + cell_h * 0.68:.1f}" text-anchor="middle" '
                f'font-size="11" fill="{ink}">{_esc(text)}</text>'
            )
    body.append(
        f'<text x="{label_w:.0f}" y="{height - 8}" font-size="10.5" fill="#64748b">'
        f"绿=改善 红=劣化（|Δ|≤3% 中性；方向已按指标归一）</text>"
    )
    return _doc(width, height, title, subtitle, "".join(body))
```

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_svgchart -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add sglbench/svgchart.py tests/test_svgchart.py
git commit -m "sglbench: svgchart.heatmap_chart（Δ 热图, 方向归一着色）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: `svgchart.trend_chart`（跨 run 趋势, 非 ok 标记）

**Files:**
- Modify: `sglbench/svgchart.py`（追加）
- Test: `tests/test_svgchart.py`（追加用例）

**Interfaces:**
- Consumes: `Frame`、`nice_ticks`、`_doc`、`_empty_chart`、`legend`、`_esc`。
- Produces:

```python
def trend_chart(
    x_labels: Sequence[str],
    series: Sequence[Mapping[str, object]],  # {name, values, statuses?, color?}
    *,
    title: str,
    ylabel: str = "",
    unit: str = "",
    width: int = 1160,
    height: int = 320,
    subtitle: str = "",
) -> str
```

ok（或未给 statuses）点 = 实心；非 ok 点 = 白心红圈（`<title>` 带 status）。

- [ ] **Step 1: Write the failing test**（追加到 `tests/test_svgchart.py`）

```python
from sglbench.svgchart import trend_chart


class TrendChartTest(unittest.TestCase):
    def test_renders_lines_and_nonok_marker(self):
        svg = trend_chart(
            ["10-01", "10-02", "10-03"],
            [{"name": "l2pp1", "values": [3.3, None, 2.7], "statuses": ["ok", "failed", "ok"]}],
            title="TTFT 趋势", ylabel="TTFT", unit="ms",
        )
        self.assertIn("<polyline", svg)
        self.assertIn('stroke="#dc2626"', svg)  # 非 ok 点红圈样式存在（连续两点即可）

    def test_nonok_point_marked(self):
        svg = trend_chart(
            ["d1", "d2"],
            [{"name": "x", "values": [1.0, 2.0], "statuses": ["ok", "partial"]}],
            title="t",
        )
        self.assertIn("<title>d2 · x · partial", svg)

    def test_empty(self):
        self.assertIn("没有可绘制", trend_chart([], [], title="t"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_svgchart -v -k Trend`
Expected: FAIL — ImportError

- [ ] **Step 3: Write implementation**（追加到 `sglbench/svgchart.py`）

```python
def trend_chart(
    x_labels: Sequence[str],
    series: Sequence[Mapping[str, object]],
    *,
    title: str,
    ylabel: str = "",
    unit: str = "",
    width: int = 1160,
    height: int = 320,
    subtitle: str = "",
) -> str:
    """跨 run 趋势：ok 点实心、非 ok 点白心红圈。x 为等距时间标签。"""
    n = len(x_labels)
    if n == 0 or not series:
        return _empty_chart(title, "没有可绘制的数据", width=width, height=height)
    all_values = [float(v) for s in series for v in (s.get("values") or []) if v is not None]
    if not all_values:
        return _empty_chart(title, "没有可绘制的数值", width=width, height=height)
    x_lo, x_hi = 0.0, float(max(n - 1, 1))
    y_lo = min(0.0, min(all_values))
    y_hi = max(all_values) * 1.15 or 1.0
    frame = Frame(width, height, (x_lo, x_hi), (y_lo, y_hi))
    body = [frame.grid(nice_ticks(y_lo, y_hi), []), frame.y_axis(nice_ticks(y_lo, y_hi)), frame.y_label(ylabel, unit)]
    step = max(1, n // 12)
    for i, lbl in enumerate(x_labels):
        if i % step:
            continue
        body.append(
            f'<text x="{frame.sx(float(i)):.1f}" y="{frame.y0 + 18:.1f}" text-anchor="middle" '
            f'font-size="10" fill="#475569" transform="rotate(-30 {frame.sx(float(i)):.1f} {frame.y0 + 18:.1f})">'
            f"{_esc(lbl)}</text>"
        )
    for si, s in enumerate(series):
        color = str(s.get("color") or color_for(si))
        values = list(s.get("values") or [])
        statuses = list(s.get("statuses") or [])
        pts = [(float(i), float(v)) for i, v in enumerate(values) if v is not None]
        if not pts:
            continue
        body.append(
            f'<polyline points="{" ".join(f"{frame.sx(x):.1f},{frame.sy(v):.1f}" for x, v in pts)}" '
            f'fill="none" stroke="{color}" stroke-width="2.2" stroke-linejoin="round"/>'
        )
        for i, v in enumerate(values):
            if v is None:
                continue
            status = str(statuses[i]) if i < len(statuses) else "ok"
            x, y = frame.sx(float(i)), frame.sy(float(v))
            if status == "ok":
                body.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.6" fill="{color}" stroke="#ffffff" '
                    f'stroke-width="1.4"><title>{_esc(x_labels[i])} · {_esc(str(s.get("name")))}: '
                    f"{float(v):.4g}</title></circle>"
                )
            else:
                body.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.2" fill="#ffffff" stroke="#dc2626" '
                    f'stroke-width="1.8"><title>{_esc(x_labels[i])} · {_esc(str(s.get("name")))} · '
                    f"status={status}: {float(v):.4g}</title></circle>"
                )
    body.append(legend([(str(s.get("name")), str(s.get("color") or color_for(i))) for i, s in enumerate(series)], frame.x0, 52))
    if subtitle:
        body.append(f'<text x="{frame.x0}" y="40" font-size="11" fill="#64748b">{_esc(subtitle)}</text>')
    return _doc(width, height, title, subtitle, "".join(body))
```

注意：`legend` 与 `subtitle` 的 y 位可能重叠（legend 在 y=52、subtitle 在 y=40 靠左）——按现有 `line_chart` 的排版习惯微调（line_chart 同样先 legend(y=52)）。若重叠影响视觉，subtitle 移到 y=38 与 legend 左对齐后再截图自查。

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_svgchart -v`
Expected: PASS（含 Task 6 共 6 tests）

- [ ] **Step 5: Commit**

```bash
git add sglbench/svgchart.py tests/test_svgchart.py
git commit -m "sglbench: svgchart.trend_chart（跨 run 趋势, 非 ok 空心红圈标记）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 8: `analyze.analyze_trend`（趋势数据）

**Files:**
- Modify: `sglbench/analyze.py`（追加；顶部 `TrendPoint` dataclass 放 `SweepPoint` 附近）
- Test: `tests/test_analyze.py`（追加用例；该文件已有 temp store 基建）

**Interfaces:**
- Consumes: `Store.query`、`runs`/`metrics` 表结构。
- Produces:

```python
TREND_DEFAULT_METRICS = ("mean_ttft_ms", "total_token_throughput_tok_s", "success_rate")

@dataclass
class TrendPoint:
    run_id: str
    label: str
    started_at: str | None
    status: str
    value: float | None

def analyze_trend(store, scenario_key, *, labels=None, metrics=None, since=None) -> dict[str, list[TrendPoint]]
```

（每指标一点/run，按 started_at 升序；包含非 ok run。）

- [ ] **Step 1: Write the failing test**（追加到 `tests/test_analyze.py`；沿用该文件既有 store fixture 方式——若其用 `temp_home`/`Store` 临时库，照抄模式）

```python
    def test_analyze_trend_orders_and_filters(self):
        with temp_home() as home:
            store = Store(home / "bench.duckdb")
            self.addCleanup(store.close)
            for i, (label, status, ttft) in enumerate(
                [("l2off", "ok", 3.4), ("l2pp1", "ok", 2.8), ("l2pp1", "partial", 2.7)]
            ):
                rec = RunRecord(
                    run_id=f"r{i}", scenario_key="mini-l2-64c", label=label, status=status,
                    started_at=datetime(2026, 10, i + 1, tzinfo=timezone.utc),
                    metrics={"mean_ttft_ms": ttft},
                )
                store.save_run(rec)
            out = analyze_trend(store, "mini-l2-64c")
            pts = out["mean_ttft_ms"]
            self.assertEqual([p.run_id for p in pts], ["r0", "r1", "r2"])  # 时间升序
            self.assertEqual(out["success_rate"], [])                     # 无该指标 → 空
            only = analyze_trend(store, "mini-l2-64c", labels=["l2pp1"])
            self.assertEqual([p.label for p in only["mean_ttft_ms"]], ["l2pp1", "l2pp1"])
            since = analyze_trend(store, "mini-l2-64c", since="2026-10-02")
            self.assertEqual([p.run_id for p in since["mean_ttft_ms"]], ["r1", "r2"])
```

（导入补 `from datetime import datetime, timezone`、`RunRecord`、`analyze_trend`——按该文件现有 import 风格。）

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_analyze -v -k trend`
Expected: FAIL — ImportError

- [ ] **Step 3: Write implementation**（追加到 `sglbench/analyze.py` 的 sweep 区之后）

```python
TREND_DEFAULT_METRICS = ("mean_ttft_ms", "total_token_throughput_tok_s", "success_rate")


@dataclass
class TrendPoint:
    """趋势图上的一次 run 采样。"""

    run_id: str
    label: str
    started_at: str | None
    status: str
    value: float | None


def analyze_trend(
    store: Store,
    scenario_key: str,
    *,
    labels: Sequence[str] | None = None,
    metrics: Sequence[str] | None = None,
    since: str | None = None,
) -> dict[str, list[TrendPoint]]:
    """同一 scenario 跨 run 的指标时间序列（含非 ok run，供回归跟踪）。"""
    keys = list(metrics or TREND_DEFAULT_METRICS)
    sql = (
        "SELECT r.run_id, r.label, r.started_at, r.status, m.metric_key, m.value "
        "FROM runs r JOIN metrics m USING (run_id) WHERE r.scenario_key = ?"
    )
    params: list[Any] = [scenario_key]
    if labels:
        sql += " AND r.label IN (" + ",".join("?" * len(labels)) + ")"
        params.extend(labels)
    if since:
        sql += " AND r.started_at >= ?"
        params.append(since)
    sql += " ORDER BY r.started_at ASC NULLS LAST"
    out: dict[str, list[TrendPoint]] = {k: [] for k in keys}
    for row in store.query(sql, params):
        key = row["metric_key"]
        if key not in out:
            continue
        out[key].append(
            TrendPoint(
                run_id=row["run_id"],
                label=row["label"] or "",
                started_at=str(row["started_at"]) if row["started_at"] is not None else None,
                status=row["status"],
                value=float(row["value"]) if row["value"] is not None else None,
            )
        )
    return out
```

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_analyze -v`
Expected: PASS（既有 + 新增）

- [ ] **Step 5: Commit**

```bash
git add sglbench/analyze.py tests/test_analyze.py
git commit -m "sglbench: analyze_trend 跨 run 趋势序列（含非 ok 点）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 9: `trend` 命令 + HTML 渲染

**Files:**
- Modify: `sglbench/report.py`（+ `build_trend_html`）、`sglbench/cli.py`（parser + `cmd_trend`）
- Test: `tests/test_report.py`（HTML builder）、`tests/test_cli.py`（冒烟）

**Interfaces:**
- Consumes: Task 8 `analyze_trend`/`TrendPoint`、Task 7 `trend_chart`、`report.render_table`。
- Produces: `report.build_trend_html(series_by_metric, *, scenario_key, title=None) -> str`；CLI `sglbench trend --scenario K [--label L]... [--metric M]... [--since D] [--html OUT] [--json]`。

- [ ] **Step 1: Write the failing test**（追加到 `tests/test_report.py`）

```python
class TrendHtmlTest(unittest.TestCase):
    def test_build_trend_html(self):
        from sglbench.analyze import TrendPoint
        from sglbench.report import build_trend_html

        series = {
            "mean_ttft_ms": [
                TrendPoint("r0", "l2off", "2026-10-01T00:00:00Z", "ok", 3.4),
                TrendPoint("r1", "l2pp1", "2026-10-02T00:00:00Z", "ok", 2.8),
                TrendPoint("r2", "l2pp1", "2026-10-03T00:00:00Z", "partial", 2.7),
            ]
        }
        html = build_trend_html(series, scenario_key="mini-l2-64c")
        self.assertIn("mini-l2-64c", html)
        self.assertIn("<svg", html)
        self.assertIn("partial", html)  # 非 ok 状态在表格/提示中出现
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_report -v -k Trend`
Expected: FAIL — ImportError

- [ ] **Step 3: Implement `build_trend_html`（report.py 末尾追加）**

```python
def build_trend_html(
    series_by_metric: Mapping[str, Sequence[Any]],
    *,
    scenario_key: str,
    title: str | None = None,
) -> str:
    """趋势时间线 HTML：每指标一张 trend_chart + 明细表。"""
    from .svgchart import trend_chart

    page_title = title or f"指标趋势 · {scenario_key}"
    sections: list[str] = []
    for metric_key, points in series_by_metric.items():
        if not points:
            continue
        times = sorted({str(p.started_at or "") for p in points})
        if not times:
            continue
        labels = sorted({p.label or p.run_id for p in points})
        ser = []
        for i, lab in enumerate(labels):
            by_time = {(str(p.started_at or "")): p for p in points if (p.label or p.run_id) == lab}
            ser.append(
                {
                    "name": lab,
                    "color": color_for(i),
                    "values": [
                        (by_time[t].value if t in by_time else None) for t in times
                    ],
                    "statuses": [
                        (by_time[t].status if t in by_time and by_time[t].status else "ok") for t in times
                    ],
                }
            )
        chart = trend_chart(
            [t[:16].replace("T", " ") for t in times], ser,
            title=metric_key, ylabel=metric_key.split("_")[0], unit="",
            width=1160, height=320,
        )
        rows = [
            [t[:16].replace("T", " "), p.label or p.run_id, ("-" if p.value is None else f"{p.value:,.3f}"), p.status]
            for t in times
            for p in points
            if str(p.started_at or "") == t
        ]
        table = render_table(["时间(UTC)", "label", metric_key, "状态"], rows, ["left", "left", "right", "left"])
        sections.append(
            f'<div class="card">{chart}</div><div class="card"><pre>{_esc(table)}</pre></div>'
        )
    if not sections:
        sections.append('<div class="card">该场景在此过滤条件下没有数据。</div>')
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>{_esc(page_title)}</title>
<style>body{{font-family:system-ui,-apple-system,sans-serif;margin:24px auto;max-width:1220px;padding:0 16px;color:#222;background:#fafafa}}
h1{{font-size:22px}}.card{{background:#fff;border:1px solid #e3e3e3;border-radius:10px;padding:12px;margin:14px 0;overflow-x:auto}}
pre{{font-size:12px;margin:0}}</style></head><body>
<h1>{_esc(page_title)}</h1>
<p style="color:#666;font-size:13px">实心点=ok；空心红圈=非 ok run（failed/partial）。生成: sglbench trend · {generated}</p>
{''.join(sections)}
</body></html>"""
    )
```

（`color_for`、`_esc`、`render_table`、`datetime/timezone` 在 report.py 已可用；`Mapping/Sequence/Any` 已在 imports。）

- [ ] **Step 4: Implement CLI `cmd_trend`（cli.py）**

parser 段（放在 `sweep` parser 之后）：

```python
    # trend
    p = sub.add_parser("trend", help="指标趋势时间线（同一场景跨 run 回归跟踪）")
    _add_common(p)
    p.add_argument("--scenario", required=True)
    p.add_argument("--label", action="append", default=[], help="只看这些 label（可重复）")
    p.add_argument("--metric", action="append", default=[], help="指标（可重复; 默认 TTFT/总吞吐/成功率）")
    p.add_argument("--since", help="起始日期（YYYY-MM-DD 前缀匹配 started_at）")
    p.add_argument("--html", metavar="OUT", help="输出自包含 HTML")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_trend)
```

`cmd_trend`（放 `cmd_sweep` 之后）：

```python
def cmd_trend(args: argparse.Namespace) -> int:
    paths = _paths(args)
    with Store(paths.db) as store:
        series = analyze_trend(
            store,
            args.scenario,
            labels=args.label or None,
            metrics=args.metric or None,
            since=args.since,
        )
    if args.json:
        payload = {
            metric: [asdict(p) for p in points] for metric, points in series.items()
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        for metric in series:
            points = [p for p in series[metric] if p.value is not None]
            print(f"── {metric}（{len(points)} 个点）")
            for p in points:
                mark = " " if p.status == "ok" else f" ⚠{p.status}"
                print(f"  {str(p.started_at)[:16]}  {p.label:<16} {p.value:>12,.3f}{mark}")
    if args.html:
        from .report import build_trend_html

        html = build_trend_html(series, scenario_key=args.scenario)
        out = Path(args.html)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"HTML: {out}")
    return EXIT_OK
```

（imports 补：`from .analyze import analyze_trend`（合并进现有 analyze 导入行）、`from dataclasses import asdict` 若未导入。）

- [ ] **Step 5: Add CLI smoke test（tests/test_cli.py 追加; 参照该文件 main() 调用模式）**

```python
    def test_trend_smoke(self):
        # 复用现有 ingest examples 流程后：
        # rc = main(["trend", "--scenario", "single-standard", "--json"])
        # self.assertEqual(rc, 0)
```

以该文件现有实例方法风格补全（ingest examples/logs 后调 trend --json，断言返回 0 且输出含 "mean_ttft_ms"）。

- [ ] **Step 6: Run tests**

Run: `python3 -m unittest tests.test_report tests.test_cli -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add sglbench/report.py sglbench/cli.py tests/test_report.py tests/test_cli.py
git commit -m "sglbench: trend 命令 —— 跨 run 指标时间线（console/HTML/JSON, 非 ok 标记）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 10: sweep label 过滤 + 双 label overlay 数据

**Files:**
- Modify: `sglbench/analyze.py`（`analyze_sweep` 加 `labels` 参数；新增 `build_sweep_overlay`）
- Test: `tests/test_analyze.py`（追加用例）

**Interfaces:**
- Consumes: `store.list_runs(scenario_key=..., with_metrics=True)`（时间倒序）、`RunSummary`。
- Produces:
  - `analyze_sweep(..., labels: Sequence[str] | None = None)` — 新增关键字参数；无 labels 行为不变（**修正**：旧实现 latest-per-scenario 不区分 label，给定 labels 后按 label 过滤后再取最新）。
  - `build_sweep_overlay(store, *, family=None, scenario_keys=None, labels) -> list[tuple[str, SweepReport]]`。

- [ ] **Step 1: Write the failing test**（追加到 `tests/test_analyze.py`）

```python
    def test_sweep_labels_filter_and_overlay(self):
        with temp_home() as home:
            store = Store(home / "bench.duckdb")
            self.addCleanup(store.close)
            for i, (label, conc, ttft) in enumerate(
                [("a", 32, 3.0), ("a", 64, 5.0), ("b", 32, 2.0), ("b", 64, 1.9)]
            ):
                store.save_run(RunRecord(
                    run_id=f"r{i}", scenario_key="conc-32-deepseek" if conc == 32 else "conc-64-deepseek",
                    label=label, status="ok",
                    started_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
                    metrics={"mean_ttft_ms": ttft, "max_request_concurrency": float(conc),
                             "success_rate": 1.0, "total_token_throughput_tok_s": 1000.0 * conc / 32},
                ))
            only_b = analyze_sweep(store, family="concurrency", labels=["b"])
            self.assertEqual({p.label for p in only_b.points}, {"b"})
            self.assertEqual(len(only_b.points), 2)
            overlay = build_sweep_overlay(store, family="concurrency", labels=["a", "b"])
            self.assertEqual([lbl for lbl, _ in overlay], ["a", "b"])
            self.assertEqual(len(overlay[1][1].points), 2)
```

（scenario family key 与 `_scenario_keys_for_family` 的匹配方式以 `sglbench/scenarios.py` 现有 concurrency family 为准；如示例 key 不在 family，用真实存在的 key 名替换。）

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_analyze -v -k overlay`
Expected: FAIL — unexpected keyword 'labels'

- [ ] **Step 3: Implement**

`analyze_sweep` 签名加参数并在取 run 时过滤：

```python
def analyze_sweep(
    store: Store,
    *,
    scenario_keys: Sequence[str] | None = None,
    family: str | None = None,
    latest_per_scenario: bool = True,
    group_key: str = "concurrency",
    labels: Sequence[str] | None = None,
) -> SweepReport:
```

keys 分支改为（替换现有 `if latest_per_scenario:` 内两句）：

```python
        for key in keys:
            if latest_per_scenario:
                rows = store.list_runs(scenario_key=key, with_metrics=True)
                if labels:
                    rows = [r for r in rows if r.label in set(labels)]
                if rows:
                    s = rows[0]  # 已按 started_at 倒序
                    summaries.append(RunSummary(
                        run_id=s.run_id, scenario_key=s.scenario_key, label=s.label,
                        status=s.status, started_at=s.started_at, metrics=s.metrics,
                    ))
            else:
                rows = store.list_runs(scenario_key=key, with_metrics=True)
                if labels:
                    rows = [r for r in rows if r.label in set(labels)]
                summaries.extend(rows)
```

无 keys 分支同样在 `candidates = store.list_runs(with_metrics=True)` 后加 labels 过滤。

新增：

```python
def build_sweep_overlay(
    store: Store,
    *,
    family: str | None = None,
    scenario_keys: Sequence[str] | None = None,
    labels: Sequence[str],
) -> list[tuple[str, SweepReport]]:
    """每个 label 一条并发梯度曲线（同一 family/场景集）。"""
    return [
        (lbl, analyze_sweep(store, family=family, scenario_keys=scenario_keys, labels=[lbl]))
        for lbl in labels
    ]
```

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_analyze -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sglbench/analyze.py tests/test_analyze.py
git commit -m "sglbench: sweep label 过滤 + build_sweep_overlay（双部署叠加曲线数据）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 11: sweep CLI `--label` + 叠加渲染（console + HTML）

**Files:**
- Modify: `sglbench/report.py`（`render_sweep_compare_console` + `build_html` 增 `sweep_overlay` 参数）、`sglbench/cli.py`（`sweep`/`report` parser + `cmd_sweep`/`cmd_report`）
- Test: `tests/test_report.py`（console/html）

**Interfaces:**
- Consumes: Task 10 `build_sweep_overlay`、`SweepReport`（`points: list[SweepPoint]`，含 `max_concurrency/mean_ttft_ms/total_throughput/…`）。
- Produces:
  - `render_sweep_compare_console(overlay: Sequence[tuple[str, SweepReport]]) -> str`
  - `build_html(..., sweep_overlay: Sequence[tuple[str, SweepReport]] | None = None)`
  - CLI：`sweep --label A --label B`；`report --sweep --sweep-label A --sweep-label B`。

- [ ] **Step 1: Write the failing test**（追加到 `tests/test_report.py`）

```python
class SweepOverlayTest(unittest.TestCase):
    def _overlay(self):
        from sglbench.analyze import SweepPoint, SweepReport

        def rep(ttft):
            pts = [
                SweepPoint(run_id=f"{t}-{c}", label=t, scenario_key=f"conc-{c}-deepseek",
                           max_concurrency=float(c), request_rate=4.0, success_rate=1.0,
                           mean_ttft_ms=v, p99_ttft_ms=v * 1.2, mean_tpot_ms=25.0, p99_tpot_ms=30.0,
                           output_throughput=1000.0, total_throughput=3000.0 + c, status="ok")
                for c, v in ttft.items()
            ]
            return SweepReport(group_key="concurrency", points=pts)

        return [("l2off", rep({32: 3.9, 64: 4.5})), ("l2pp1", rep({32: 2.7, 64: 4.8}))]

    def test_console_table(self):
        text = render_sweep_compare_console(self._overlay())
        self.assertIn("l2off", text)
        self.assertIn("l2pp1", text)
        self.assertIn("32", text)

    def test_html_has_two_lines(self):
        from sglbench.analyze import compare_runs  # noqa: F401  仅为确认导入面
        report = _round_report()
        html = build_html(report, sweep_overlay=self._overlay())
        self.assertIn("双部署并发梯度对比", html)
        self.assertEqual(html.count("<polyline"), 2)  # 每图两条线，至少首图两张
```

（`html.count` 断言以实际渲染图数为准调整——若 overlay 渲染多张图，改为 `>= 2`。）

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_report -v -k Overlay`
Expected: FAIL — ImportError

- [ ] **Step 3: Implement 渲染（report.py）**

```python
def render_sweep_compare_console(overlay: Sequence[tuple[str, "SweepReport"]]) -> str:
    """双 label 并发梯度对照表：行=并发，列=label×指标。"""
    by_label: dict[str, dict[float, Any]] = {}
    concs: set[float] = set()
    for label, sweep in overlay:
        by_label[label] = {p.max_concurrency: p for p in sweep.points if p.max_concurrency is not None}
        concs |= set(by_label[label])
    labels = [lbl for lbl, _ in overlay]
    headers = ["并发"] + [f"{lbl} TTFT(ms)" for lbl in labels] + [f"{lbl} 总吞吐" for lbl in labels]
    rows = []
    for c in sorted(concs):
        row = [f"{c:g}"]
        for lbl in labels:
            p = by_label[lbl].get(c)
            row.append("-" if p is None or p.mean_ttft_ms is None else f"{p.mean_ttft_ms:,.1f}")
        for lbl in labels:
            p = by_label[lbl].get(c)
            row.append("-" if p is None or p.total_throughput is None else f"{p.total_throughput:,.0f}")
        rows.append(row)
    return render_table(headers, rows, ["left"] + ["right"] * (2 * len(labels)))


def _sweep_overlay_charts(
    overlay: Sequence[tuple[str, "SweepReport"]], width: int = 1160
) -> list[str]:
    """双部署并发梯度叠加图：TTFT / P99 TTFT / 总吞吐 每指标一张双线图。"""
    from .svgchart import line_chart

    xs = sorted({p.max_concurrency for _, s in overlay for p in s.points if p.max_concurrency})
    if not xs:
        return []
    charts = []
    for attr, title, unit, kb in (
        ("mean_ttft_ms", "TTFT 均值 vs 并发", "ms", False),
        ("p99_ttft_ms", "TTFT P99 vs 并发", "ms", False),
        ("total_throughput", "总吞吐 vs 并发", "tok/s", True),
    ):
        series = []
        for i, (label, sweep) in enumerate(overlay):
            by_c = {p.max_concurrency: getattr(p, attr) for p in sweep.points}
            series.append({
                "name": label,
                "color": color_for(i),
                "values": [by_c.get(c) for c in xs],
            })
        charts.append(line_chart([float(c) for c in xs], series, title=title,
                                 xlabel="并发", ylabel=title.split(" vs")[0], unit=unit,
                                 width=width, height=340, x_log=True, annotate_points=False))
    return charts
```

`build_html` 签名与图表区（在 sweep 渲染附近）：

```python
def build_html(
    report: ComparisonReport,
    *,
    title: str | None = None,
    runs: Sequence[Any] = (),
    sweep: SweepReport | None = None,
    sweep_overlay: Sequence[tuple[str, "SweepReport"]] | None = None,
    ...
```

在 `if include_charts:` 块（或 sweep 渲染处）追加：

```python
        if sweep_overlay:
            charts.append(f'<h2 id="sweep-overlay">双部署并发梯度对比</h2>')
            for c in _sweep_overlay_charts(sweep_overlay):
                charts.append(_chart_card(c, full=True))
```

- [ ] **Step 4: CLI 接线（cli.py）**

`sweep` parser 加：`p.add_argument("--label", action="append", default=[], help="只看这些 label（可重复; ≥2 时输出叠加对比）")`；`report` parser 加：`p.add_argument("--sweep-label", action="append", default=[])`。

`cmd_sweep`（在现有 reports 打印前）：

```python
    if args.label:
        from .analyze import build_sweep_overlay
        with Store(paths.db) as store:
            overlay = build_sweep_overlay(store, family=args.family, labels=args.label)
        if len(overlay) >= 2:
            from .report import render_sweep_compare_console
            print(render_sweep_compare_console(overlay))
            print()
        for label, sweep in overlay:
            print(f"── label: {label}")
            print(render_sweep_console(sweep))
        if args.json:
            print(json.dumps({lbl: s.to_dict() for lbl, s in overlay}, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK
```

（原有单 label 路径不动；`cmd_sweep` 现有实现先读一遍再插入，保证 `args.label` 为空时行为不变。）

`cmd_report` 的 sweep 段改为：

```python
        sweep = None
        sweep_overlay = None
        if args.sweep:
            if args.sweep_label:
                from .analyze import build_sweep_overlay
                sweep_overlay = build_sweep_overlay(store, family=args.sweep_family, labels=args.sweep_label)
            else:
                sweep = analyze_sweep(store, family=args.sweep_family, latest_per_scenario=not args.all_runs)
        ...
    html = build_html(report, title=args.title, runs=records, sweep=sweep,
                      sweep_overlay=sweep_overlay, report_path=out)
```

- [ ] **Step 5: Run tests**

Run: `python3 -m unittest tests.test_report tests.test_cli -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add sglbench/report.py sglbench/cli.py tests/test_report.py
git commit -m "sglbench: sweep 双 label 叠加（console 对照表 + 三张双线图）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 12: `campaign.py` —— suite 数据构建（受控条件/汇总/吞吐宽表/多轮/稳定性/结论）

**Files:**
- Create: `sglbench/campaign.py`
- Test: `tests/test_campaign.py`（新建）

**Interfaces:**
- Consumes: `dashboard.resolve_pairs`、`rounds.round_verdict`、`analyze.DEFAULT_THRESHOLD_PCT`、`store.get_environment(run_id)`、`store.query`。
- Produces:

```python
ENV_DISPLAY_KEYS: list[tuple[str, str]]        # (列名, 显示名) 受控条件表用
THROUGHPUT_KEYS: list[tuple[str, str, bool]]   # (metric, 显示名, higher_better) 吞吐宽表用

@dataclass
class ScenarioSummary:
    key: str; name: str; off_run: str; on_run: str
    off: dict[str, float]; on: dict[str, float]
    status_off: str; status_on: str
    verdict: str                    # TTFT 判定（✅/❌/➖, ±5%）
    round_verdict: dict | None      # 逐轮判定（无逐轮 → None）

@dataclass
class SuiteData:
    title: str; pair_titles: tuple[str, str]
    env_rows: list[tuple[str, str, str, bool]]     # (字段显示名, A值, B值, 是否差异)
    summaries: list[ScenarioSummary]
    conclusions: list[str]

def build_suite(store, *, label_pairs=None, run_pairs=None, title=None, pair_titles=None) -> SuiteData
def suite_conclusions(summaries, env_rows, pair_titles) -> list[str]
def delta_pct(off: float | None, on: float | None) -> float | None
def verdict_of(pct: float | None, *, higher_better: bool, threshold: float = 5.0) -> str
```

- [ ] **Step 1: Write the failing test**

```python
"""campaign suite 数据层单测。"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from sglbench.campaign import build_suite, delta_pct, verdict_of
from sglbench.models import RunRecord
from sglbench.store import Store

from .helpers import temp_home


def _mk(store, run_id, *, scenario, label, metrics, env=None, status="ok", day=1):
    rec = RunRecord(run_id=run_id, scenario_key=scenario, label=label, status=status,
                    started_at=datetime(2026, 10, day, tzinfo=timezone.utc), metrics=dict(metrics))
    if env:
        rec.meta["deployment_env"] = env
    store.save_run(rec)
    if env:
        store.save_environment(rec)
    return rec


class CampaignDataTest(unittest.TestCase):
    def test_delta_and_verdict(self):
        self.assertAlmostEqual(delta_pct(100.0, 90.0), -10.0)
        self.assertIsNone(delta_pct(None, 90.0))
        self.assertIsNone(delta_pct(0.0, 90.0))
        self.assertEqual(verdict_of(-6.0, higher_better=False), "✅")
        self.assertEqual(verdict_of(6.0, higher_better=False), "❌")
        self.assertEqual(verdict_of(2.0, higher_better=False), "➖")

    def test_build_suite(self):
        with temp_home() as home:
            store = Store(home / "bench.duckdb")
            self.addCleanup(store.close)
            base_env = {"deployment": "dsv4-rc3", "prefill_pp": 1, "hicache_enabled": False}
            on_env = {"deployment": "dsv4-l2", "prefill_pp": 1, "hicache_enabled": True}
            _mk(store, "off-32", scenario="mini-l2-32c", label="A", day=1,
                metrics={"mean_ttft_ms": 7660.0, "mean_tpot_ms": 26.1, "success_rate": 1.0,
                         "input_token_throughput_tok_s": 1961.7, "output_token_throughput_tok_s": 980.9,
                         "total_token_throughput_tok_s": 2942.6, "peak_output_token_throughput_tok_s": 1542.0},
                env=base_env)
            _mk(store, "on-32", scenario="mini-l2-32c", label="B", day=2,
                metrics={"mean_ttft_ms": 11020.0, "mean_tpot_ms": 26.4, "success_rate": 1.0,
                         "input_token_throughput_tok_s": 1841.2, "output_token_throughput_tok_s": 920.6,
                         "total_token_throughput_tok_s": 2761.8, "peak_output_token_throughput_tok_s": 1449.0},
                env=on_env)
            _mk(store, "off-mt", scenario="hicache-multiturn-20x10", label="A", day=1,
                metrics={"success_rate": 1.0, "round0_mean_ttft_ms": 606.0, "round1_mean_ttft_ms": 5178.0,
                         "round3_mean_ttft_ms": 1700.0},
                env=base_env)
            _mk(store, "on-mt", scenario="hicache-multiturn-20x10", label="B", day=2,
                metrics={"success_rate": 1.0, "round0_mean_ttft_ms": 656.0, "round1_mean_ttft_ms": 1410.0,
                         "round3_mean_ttft_ms": 1720.0},
                env=on_env)
            suite = build_suite(store, label_pairs=[("A", "B")])
            self.assertEqual([s.key for s in suite.summaries], ["hicache-multiturn-20x10", "mini-l2-32c"])
            s32 = next(s for s in suite.summaries if s.key == "mini-l2-32c")
            self.assertEqual(s32.verdict, "❌")            # TTFT +43.9% 劣化
            self.assertAlmostEqual(delta_pct(*[s32.off["mean_ttft_ms"], s32.on["mean_ttft_ms"]]), 43.9, places=1)
            smt = next(s for s in suite.summaries if s.key.startswith("hicache-multiturn"))
            self.assertIsNotNone(smt.round_verdict)
            self.assertEqual(smt.round_verdict["verdict"], "✅ round1 改善")
            self.assertTrue(any("⚠" in (r[3] and "⚠" or "") for r in suite.env_rows) or any(r[3] for r in suite.env_rows))
            self.assertTrue(any("改善" in c or "劣化" in c for c in suite.conclusions))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_campaign -v`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Implement `sglbench/campaign.py`**

```python
"""战役汇总报告（suite）—— 把"同一批场景, 两个部署对比"产出为完整测试报告。

对标人工维护的 docs/TEST-REPORT-*.md：受控条件表（run_environments 差异）+
场景汇总（TTFT/TPOT）+ 吞吐量完整宽表 + 多轮汇总 + 稳定性 + 结论草稿。
index.html / SUMMARY.md / 每场景明细 由 write_suite 落盘。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .analyze import DEFAULT_THRESHOLD_PCT
from .rounds import round_verdict
from .store import Store

# 受控条件表展示列（run_environments 列名 → 中文显示名）
ENV_DISPLAY_KEYS: list[tuple[str, str]] = [
    ("deployment", "部署"), ("prefill_tp", "prefill tp"), ("prefill_dp", "prefill dp"),
    ("prefill_pp", "prefill pp"), ("decode_tp", "decode tp"), ("decode_dp", "decode dp"),
    ("decode_pp", "decode pp"), ("launch_mode", "启动模式"),
    ("prefill_image", "prefill 镜像"), ("decode_image", "decode 镜像"),
    ("hicache_enabled", "HiCache"), ("hicache_io_backend", "io_backend"),
    ("hicache_ratio", "ratio"), ("hicache_mem_layout", "mem_layout"),
    ("hicache_storage_backend", "storage"), ("hicache_write_policy", "write_policy"),
    ("swa_full_tokens_ratio", "swa ratio"), ("maca_version", "MACA"),
    ("driver_version", "驱动"), ("engine_version", "引擎"),
]

# 吞吐宽表列（metric_key, 显示名, higher_better）
THROUGHPUT_KEYS: list[tuple[str, str, bool]] = [
    ("input_token_throughput_tok_s", "输入 tok/s", True),
    ("output_token_throughput_tok_s", "输出 tok/s", True),
    ("total_token_throughput_tok_s", "总 tok/s", True),
    ("peak_output_token_throughput_tok_s", "峰值输出 tok/s", True),
]


def delta_pct(off: float | None, on: float | None) -> float | None:
    if off is None or on is None or not off:
        return None
    return (on - off) / off * 100.0


def verdict_of(pct: float | None, *, higher_better: bool, threshold: float = DEFAULT_THRESHOLD_PCT) -> str:
    if pct is None:
        return "—"
    good = pct >= threshold if higher_better else pct <= -threshold
    bad = pct <= -threshold if higher_better else pct >= threshold
    return "✅" if good else ("❌" if bad else "➖")


@dataclass
class ScenarioSummary:
    key: str
    name: str
    off_run: str
    on_run: str
    off: dict[str, float]
    on: dict[str, float]
    status_off: str = "ok"
    status_on: str = "ok"
    verdict: str = "—"
    round_verdict: dict[str, Any] | None = None


@dataclass
class SuiteData:
    title: str
    pair_titles: tuple[str, str]
    env_rows: list[tuple[str, str, str, bool]] = field(default_factory=list)
    summaries: list[ScenarioSummary] = field(default_factory=list)
    conclusions: list[str] = field(default_factory=list)


def _scenario_names(store: Store, keys: Sequence[str]) -> dict[str, str]:
    if not keys:
        return {}
    rows = store.query(
        "SELECT scenario_key, name FROM scenarios WHERE scenario_key IN ("
        + ",".join("?" * len(keys)) + ")",
        list(keys),
    )
    return {r["scenario_key"]: r["name"] or "" for r in rows}


def build_suite(
    store: Store,
    *,
    label_pairs: Sequence[Sequence[str]] | None = None,
    run_pairs: Sequence[Sequence[str]] | None = None,
    title: str | None = None,
    pair_titles: tuple[str, str] | None = None,
) -> SuiteData:
    from .dashboard import resolve_pairs  # 延迟导入避免环

    pairs = resolve_pairs(store, label_pairs=label_pairs, run_pairs=run_pairs)
    pt = pair_titles
    if pt is None and label_pairs:
        pt = (label_pairs[0][0], label_pairs[0][1])
    pt = pt or ("A", "B")
    names = _scenario_names(store, [str(p["name"]) for p in pairs])

    summaries: list[ScenarioSummary] = []
    for p in sorted(pairs, key=lambda x: str(x["name"])):
        key = str(p["name"])
        off_m, on_m = p["metrics_off"], p["metrics_on"]
        t = delta_pct(off_m.get("mean_ttft_ms"), on_m.get("mean_ttft_ms"))
        s = ScenarioSummary(
            key=key, name=names.get(key, ""), off_run=p["off_run"], on_run=p["on_run"],
            off=off_m, on=on_m,
            status_off=str(p.get("status_off", "ok")), status_on=str(p.get("status_on", "ok")),
            verdict=verdict_of(t, higher_better=False),
            round_verdict=(rv if (rv := round_verdict(off_m, on_m))["verdict"] != "无逐轮数据" else None),
        )
        summaries.append(s)

    env_rows: list[tuple[str, str, str, bool]] = []
    if pairs:
        off_env = store.get_environment(pairs[0]["off_run"]) or {}
        on_env = store.get_environment(pairs[0]["on_run"]) or {}
        for col, disp in ENV_DISPLAY_KEYS:
            a, b = off_env.get(col), on_env.get(col)
            fa = "—" if a is None or a == "" else str(a)
            fb = "—" if b is None or b == "" else str(b)
            if not off_env and not on_env:
                continue
            env_rows.append((disp, fa, fb, fa != fb))

    t = title or f"跨场景对比: {pt[0]} vs {pt[1]}"
    suite = SuiteData(title=t, pair_titles=pt, env_rows=env_rows, summaries=summaries)
    suite.conclusions = suite_conclusions(suite)
    return suite


def suite_conclusions(suite: SuiteData) -> list[str]:
    """结论草稿：按指标计数 + 显著项 + 稳定性 + 多轮亮点。"""
    out: list[str] = []
    metric_specs = [
        ("mean_ttft_ms", "TTFT", False),
        ("mean_tpot_ms", "TPOT", False),
        ("total_token_throughput_tok_s", "总吞吐", True),
    ]
    for key, disp, higher in metric_specs:
        good = bad = flat = 0
        notable: list[str] = []
        for s in suite.summaries:
            d = delta_pct(s.off.get(key), s.on.get(key))
            if d is None:
                continue
            v = verdict_of(d, higher_better=higher)
            if v == "✅":
                good += 1
            elif v == "❌":
                bad += 1
            else:
                flat += 1
            if abs(d) >= 10.0:
                notable.append(f"{s.key} {d:+.1f}%")
        if good or bad or flat:
            line = f"{disp}: 改善 {good} / 劣化 {bad} / 持平 {flat}（±{DEFAULT_THRESHOLD_PCT:g}% 阈值）"
            if notable:
                line += "；显著项(|Δ|≥10%): " + "、".join(notable)
            out.append(line)
    ok_bad = [s.key for s in suite.summaries if s.status_off != "ok" or s.status_on != "ok"]
    if ok_bad:
        out.append("稳定性: 以下场景存在非 ok run —— " + "、".join(ok_bad))
    elif suite.summaries:
        out.append(f"稳定性: {len(suite.summaries)} 个场景两侧全部 ok。")
    rounds_hi = [
        f"{s.key} round1 {s.round_verdict['round1_pct']:+.1f}%"
        for s in suite.summaries
        if s.round_verdict and s.round_verdict.get("round1_pct") is not None
        and s.round_verdict["round1_pct"] <= -10
    ]
    if rounds_hi:
        out.append("多轮冷热转换（round1）显著改善: " + "、".join(rounds_hi) + "。")
    return out
```

注意：`resolve_pairs` 返回项当前无 `status_off/status_on` 键——Task 12 同时给 `dashboard.resolve_pairs` 补上（在构造 dict 时从 runs 查询带出 `status`；`_latest_ok_run` 与按场景查询都补 `status` 字段，`.get("status_off","ok")` 兜底不影响 Task 5 测试）。

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_campaign tests.test_dashboard -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sglbench/campaign.py sglbench/dashboard.py tests/test_campaign.py
git commit -m "sglbench: campaign suite 数据层（受控条件差异/汇总/宽表/结论草稿）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 13: `campaign` suite HTML（index 渲染）

**Files:**
- Modify: `sglbench/campaign.py`（+ `render_suite_html`、私有图表辅助）
- Test: `tests/test_campaign.py`（追加）

**Interfaces:**
- Consumes: Task 6 `heatmap_chart`、`svgchart.grouped_bar_chart`/`line_chart`、Task 2 `round_series`、`report.render_table`、`util.format_metric`。
- Produces: `render_suite_html(suite: SuiteData, *, report_links: Mapping[str, str] | None = None) -> str`（`report_links`: scenario_key → 相对文件名）。

- [ ] **Step 1: Write the failing test**（追加到 `tests/test_campaign.py`）

```python
    def test_render_suite_html(self):
        from sglbench.campaign import render_suite_html
        # 复用 test_build_suite 的 store 构造（抽成 helper _suite_store() 返回 suite）
        suite = self._built_suite()
        html = render_suite_html(suite, report_links={s.key: f"{i+1:02d}-{s.key}.html"
                                                      for i, s in enumerate(suite.summaries)})
        for needle in ("受控条件", "压测场景汇总", "吞吐量完整对比", "多轮对话", "稳定性",
                       "结论", "Δ 热图", "<svg", "01-", "round1"):
            self.assertIn(needle, html)
```

（把 Task 12 用例里的 store 构造抽成 `_built_suite(self)` helper 复用。）

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_campaign -v -k html`
Expected: FAIL — ImportError

- [ ] **Step 3: Implement `render_suite_html`（campaign.py 追加）**

```python
def _delta_text(d: float | None) -> str:
    return "—" if d is None else f"{d:+.1f}%"


def _delta_color(d: float | None, higher_better: bool) -> str:
    if d is None:
        return "#666"
    good = d >= DEFAULT_THRESHOLD_PCT if higher_better else d <= -DEFAULT_THRESHOLD_PCT
    bad = d <= -DEFAULT_THRESHOLD_PCT if higher_better else d >= DEFAULT_THRESHOLD_PCT
    return "#1a7f37" if good else ("#c62828" if bad else "#666")


def _summary_table(suite: SuiteData) -> str:
    a, b = suite.pair_titles
    rows = []
    for s in suite.summaries:
        t = delta_pct(s.off.get("mean_ttft_ms"), s.on.get("mean_ttft_ms"))
        p = delta_pct(s.off.get("mean_tpot_ms"), s.on.get("mean_tpot_ms"))
        rows.append(
            f"<tr><td>{esc(s.key)}</td>"
            f"<td>{fmt(s.off.get('mean_ttft_ms'), 'ms')} → {fmt(s.on.get('mean_ttft_ms'), 'ms')}</td>"
            f"<td style='color:{_delta_color(t, False)}'>{_delta_text(t)}</td>"
            f"<td>{fmt(s.off.get('mean_tpot_ms'), 'ms')} → {fmt(s.on.get('mean_tpot_ms'), 'ms')}</td>"
            f"<td style='color:{_delta_color(p, False)}'>{_delta_text(p)}</td>"
            f"<td>{s.verdict}</td></tr>"
        )
    return (
        f"<table><tr><th>场景</th><th>TTFT {a}→{b}</th><th>Δ</th><th>TPOT {a}→{b}</th><th>Δ</th><th>判定</th></tr>"
        + "".join(rows) + "</table>"
    )
```

随后按下列结构组装（全部函数式直接写，`esc=html.escape`、`fmt=util.format_metric`）：

```python
def render_suite_html(suite: SuiteData, *, report_links: Mapping[str, str] | None = None) -> str:
    from .svgchart import grouped_bar_chart, heatmap_chart, line_chart
    from .rounds import round_series

    links = dict(report_links or {})
    cats = [s.key for s in suite.summaries]
    charts: list[str] = []

    # 热图：场景 × [TTFT, TPOT, 总吞吐, 峰值输出]
    heat_rows = cats
    heat_cols = ["TTFT", "TPOT", "总吞吐", "峰值输出"]
    heat_vals = [
        [
            delta_pct(s.off.get("mean_ttft_ms"), s.on.get("mean_ttft_ms")),
            delta_pct(s.off.get("mean_tpot_ms"), s.on.get("mean_tpot_ms")),
            delta_pct(s.off.get("total_token_throughput_tok_s"), s.on.get("total_token_throughput_tok_s")),
            delta_pct(s.off.get("peak_output_token_throughput_tok_s"), s.on.get("peak_output_token_throughput_tok_s")),
        ]
        for s in suite.summaries
    ]
    charts.append(heatmap_chart(heat_rows, heat_cols, heat_vals, title="Δ 热图（方向已归一）",
                                better_dirs=[False, False, True, True]))

    # TTFT / 总吞吐 分组柱状（原始值对比）
    for key, disp, unit in (
        ("mean_ttft_ms", "TTFT 均值", "ms"),
        ("total_token_throughput_tok_s", "总吞吐", "tok/s"),
    ):
        series = [
            {"name": suite.pair_titles[0], "values": [s.off.get(key) for s in suite.summaries], "color": "#5b8def"},
            {"name": suite.pair_titles[1], "values": [s.on.get(key) for s in suite.summaries], "color": "#e8734a"},
        ]
        charts.append(grouped_bar_chart(cats, series, title=disp, ylabel=disp, unit=unit,
                                        higher_is_better=(key != "mean_ttft_ms"), width=1160, height=360))

    # 多轮断崖曲线
    for s in suite.summaries:
        x, series = round_series(s.off, s.on, base_name=suite.pair_titles[0], cand_name=suite.pair_titles[1])
        if not x:
            continue
        charts.append(line_chart([float(v) for v in x], series, title=f"{s.key} · 逐轮 TTFT",
                                 xlabel="轮次", ylabel="TTFT", unit="ms", width=1160, height=320,
                                 annotate_points=False,
                                 subtitle="本引擎 cache_hit_rate 恒 0（测量限制）——看 TTFT 断崖"))

    # 吞吐宽表 / 多轮表 / 稳定性表 / 结论 —— 均按 _summary_table 模式构造
    ...  # 见下 text 表构造
```

（此步剩余表格构造与页面 CSS：以 `dashboard.py` 的页面骨架为模板逐段写齐——①受控条件表（`suite.env_rows`，差异行加 class="diff" 高亮）②`_summary_table` ③吞吐宽表（`THROUGHPUT_KEYS` 每个指标三列 基线→候选→Δ 着色；列头分组 rowspan=2 参照 dashboard thead 写法）④多轮表（`round_verdict` 三点 Δ）⑤稳定性表 ⑥图表区 `''.join(charts)` ⑦结论 `<ul>` ⑧导航链接 `links.get(s.key)`。实现完成后 `python3 -m sglbench campaign report --help` 冒烟。）

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_campaign -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sglbench/campaign.py tests/test_campaign.py
git commit -m "sglbench: campaign suite HTML（热图/柱状/断崖曲线 + 全表 + 结论 + 导航）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 14: `campaign` suite Markdown + `write_suite`（index/SUMMARY/每场景报告落盘）

**Files:**
- Modify: `sglbench/campaign.py`（+ `render_suite_markdown`、`write_suite`）
- Test: `tests/test_campaign.py`（追加）

**Interfaces:**
- Consumes: Task 12/13；`analyze.compare_runs`、`analyze.scenario_for_run`、`report.build_html`/`write_html`。
- Produces:

```python
def render_suite_markdown(suite: SuiteData) -> str
def write_suite(store, out_dir, *, label_pairs=None, run_pairs=None, title=None,
                pair_titles=None, with_md: bool = True) -> dict[str, Any]
    # 返回 {"index": Path, "summary_md": Path|None, "reports": {scenario_key: Path}}
```

- [ ] **Step 1: Write the failing test**

```python
    def test_write_suite_outputs(self):
        import tempfile
        from pathlib import Path
        from sglbench.campaign import write_suite
        suite = self._built_suite()   # 内含 store（helper 返回 (store, suite)）
        ... # 以 helper 实际形态为准
        out = Path(tempfile.mkdtemp()) / "suite"
        result = write_suite(store, out, label_pairs=[("A", "B")])
        self.assertTrue(result["index"].exists())
        self.assertTrue(result["summary_md"].exists())
        self.assertEqual(len(result["reports"]), 2)
        idx = result["index"].read_text(encoding="utf-8")
        self.assertIn("01-hicache-multiturn-20x10.html", idx)   # 排序后编号导航
        self.assertIn("SUMMARY.md", idx)
        md = result["summary_md"].read_text(encoding="utf-8")
        self.assertIn("受控条件", md)
        self.assertIn("|", md)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_campaign -v -k write_suite`
Expected: FAIL — ImportError

- [ ] **Step 3: Implement**

```python
def render_suite_markdown(suite: SuiteData) -> str:
    """SUMMARY.md —— 表格与 index 同源（可直接 commit 进 docs/）。"""
    a, b = suite.pair_titles
    lines = [f"# {suite.title}", "", f"> 生成: sglbench campaign · {a} vs {b}", ""]
    lines += ["## 1. 受控条件", "", "| 项 | " + a + " | " + b + " |", "|---|---|---|"]
    lines += [
        f"| {k}{' ⚠差异' if diff else ''} | {va} | {vb} |" for k, va, vb, diff in suite.env_rows
    ]
    lines += ["", "## 2. 场景汇总（TTFT/TPOT）", "",
              f"| 场景 | TTFT {a}→{b} | Δ | TPOT {a}→{b} | Δ | 判定 |", "|---|---|---|---|---|---|"]
    for s in suite.summaries:
        t = delta_pct(s.off.get("mean_ttft_ms"), s.on.get("mean_ttft_ms"))
        p = delta_pct(s.off.get("mean_tpot_ms"), s.on.get("mean_tpot_ms"))
        lines.append(
            f"| {s.key} | {fmt(s.off.get('mean_ttft_ms'), 'ms')}→{fmt(s.on.get('mean_ttft_ms'), 'ms')} "
            f"| {_delta_text(t)} | {fmt(s.off.get('mean_tpot_ms'), 'ms')}→{fmt(s.on.get('mean_tpot_ms'), 'ms')} "
            f"| {_delta_text(p)} | {s.verdict} |"
        )
    lines += ["", "## 3. 吞吐量完整对比（token/s）", "",
              f"| 场景 | 输入 {a}→{b} | Δ | 输出 {a}→{b} | Δ | 总 {a}→{b} | Δ | 峰值输出 {a}→{b} | Δ |",
              "|---|---|---|---|---|---|---|---|---|"]
    for s in suite.summaries:
        cells = [s.key]
        for key, _disp, higher in THROUGHPUT_KEYS:
            d = delta_pct(s.off.get(key), s.on.get(key))
            cells += [f"{fmt(s.off.get(key), 'tok/s')}→{fmt(s.on.get(key), 'tok/s')}", _delta_text(d)]
        lines.append("| " + " | ".join(cells) + " |")
    mt = [s for s in suite.summaries if s.round_verdict]
    if mt:
        lines += ["", "## 4. 多轮对话（逐轮 TTFT）", "",
                  f"| 场景 | round0 Δ | round1 Δ | 稳态 Δ | 判定 |", "|---|---|---|---|---|"]
        for s in mt:
            rv = s.round_verdict
            lines.append(
                f"| {s.key} | {_delta_text(rv.get('round0_pct'))} | {_delta_text(rv.get('round1_pct'))} "
                f"| {_delta_text(rv.get('steady_pct'))} | {rv.get('verdict')} |"
            )
    lines += ["", "## 5. 稳定性", ""]
    for s in suite.summaries:
        lines.append(f"- {s.key}: {a}={s.status_off} / {b}={s.status_on}")
    lines += ["", "## 6. 结论（自动草稿）", ""]
    lines += [f"- {c}" for c in suite.conclusions]
    return "\n".join(lines) + "\n"


def write_suite(
    store: Store,
    out_dir: str | Path,
    *,
    label_pairs: Sequence[Sequence[str]] | None = None,
    run_pairs: Sequence[Sequence[str]] | None = None,
    title: str | None = None,
    pair_titles: tuple[str, str] | None = None,
    with_md: bool = True,
) -> dict[str, Any]:
    """落盘 index.html + SUMMARY.md + NN-<scenario>.html（每场景完整报告）。"""
    from .analyze import compare_runs, scenario_for_run
    from .report import build_html, write_html

    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    suite = build_suite(store, label_pairs=label_pairs, run_pairs=run_pairs,
                        title=title, pair_titles=pair_titles)

    links: dict[str, str] = {}
    reports: dict[str, Path] = {}
    for i, s in enumerate(suite.summaries):
        fname = f"{i + 1:02d}-{s.key}.html"
        links[s.key] = fname
        recs = [store.get_run(r, with_samples=True) for r in (s.off_run, s.on_run)]
        recs = [r for r in recs if r is not None]
        scenario = scenario_for_run(recs[0]) if recs else None
        comparison = compare_runs(store, [s.off_run, s.on_run], baseline=s.off_run, scenario=scenario)
        html = build_html(comparison, runs=recs, title=f"{s.key} · {suite.pair_titles[0]} vs {suite.pair_titles[1]}",
                          report_path=target / fname)
        write_html(target / fname, html)
        reports[s.key] = target / fname

    index_html = render_suite_html(suite, report_links=links)
    index_path = target / "index.html"
    write_html(index_path, index_html)

    md_path: Path | None = None
    if with_md:
        md_path = target / "SUMMARY.md"
        md_path.write_text(render_suite_markdown(suite), encoding="utf-8", newline="\n")
    return {"index": index_path, "summary_md": md_path, "reports": reports}
```

（`fmt` 采用 `from .util import format_metric as fmt`；`esc=html.escape`。）

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_campaign -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sglbench/campaign.py tests/test_campaign.py
git commit -m "sglbench: campaign write_suite（index + SUMMARY.md + 每场景报告落盘）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 15: CLI `campaign report` 子命令 + 端到端冒烟

**Files:**
- Modify: `sglbench/cli.py`（parser + `cmd_campaign_report`）
- Test: `tests/test_cli.py`（追加）

**Interfaces:**
- Consumes: Task 14 `write_suite`。
- Produces: `sglbench campaign report --pair A=B [--pair C=D] [--run-pair off=on] -o DIR [--title T] [--no-md]`（`campaign` 子命令预留 `run` 子命令位给 Plan 2）。

- [ ] **Step 1: Write the failing test**（追加到 `tests/test_cli.py`）

```python
    def test_campaign_report_smoke(self):
        import tempfile, os
        home = Path(tempfile.mkdtemp())
        os.environ["SGLBENCH_HOME"] = str(home)
        self.addCleanup(os.environ.pop, "SGLBENCH_HOME", None)
        # 1) ingest 示例日志（沿用本文件既有 examples 导入写法）
        rc = main(["ingest", str(EXAMPLES_DIR / "logs"), "--infer", "-q"])
        self.assertEqual(rc, 0)
        # 2) 用示例里的两个 label 出 suite（label 名以 examples 实际为准：
        #    例 logs 目录含 two runs 的同场景 bundle，label_from_log_path 推断）
        out = home / "suite"
        rc = main(["campaign", "report", "--pair", "<labelA>=<labelB>", "-o", str(out)])
        self.assertEqual(rc, 0)
        self.assertTrue((out / "index.html").exists())
        self.assertTrue((out / "SUMMARY.md").exists())
```

说明：`<labelA>=<labelB>` 以 `tests/test_cli.py` 中既有 compare 用例实际用的两个 label 为准（照抄该文件里 `--last 2` 用例所用示例数据与 label 推断结果）。实现前先跑 `python3 -m sglbench ingest examples/logs --infer` + `sglbench list` 看真实 label。

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cli -v -k campaign`
Expected: FAIL — argparse: invalid choice 'campaign'

- [ ] **Step 3: Implement（cli.py）**

parser 段（放在 `dashboard` parser 之后）：

```python
    # campaign —— 战役：汇总报告（run 子命令见 Plan 2）
    p = sub.add_parser("campaign", help="战役：完整测试报告 / 执行器")
    csub = p.add_subparsers(dest="campaign_cmd", required=True)
    cr = csub.add_parser("report", help="跨场景完整测试报告（index+SUMMARY.md+每场景）")
    _add_common(cr)
    cr.add_argument("--pair", action="append", default=[], metavar="A=B", help="label 配对（可重复）")
    cr.add_argument("--run-pair", action="append", default=[], metavar="OFF=ON", help="精确指定 run 配对")
    cr.add_argument("--title")
    cr.add_argument("-o", "--output", required=True, help="输出目录")
    cr.add_argument("--no-md", action="store_true", help="不生成 SUMMARY.md")
    cr.set_defaults(func=cmd_campaign_report)
```

`cmd_campaign_report`（放 `cmd_dashboard` 附近）：

```python
def cmd_campaign_report(args: argparse.Namespace) -> int:
    from .campaign import write_suite

    paths = _paths(args)
    label_pairs = [tuple(p.split("=", 1)) for p in args.pair if "=" in p]
    run_pairs = [tuple(p.split("=", 1)) for p in args.run_pair if "=" in p]
    if not label_pairs and not run_pairs:
        _eprint("❌ 至少需要一个 --pair A=B 或 --run-pair OFF=ON")
        return EXIT_FAIL
    with Store(paths.db) as store:
        result = write_suite(store, args.output, label_pairs=label_pairs, run_pairs=run_pairs,
                             title=args.title, with_md=not args.no_md)
    print(f"index : {result['index']}")
    if result["summary_md"]:
        print(f"综述  : {result['summary_md']}")
    print(f"明细  : {len(result['reports'])} 份（{Path(args.output)}/NN-*.html）")
    return EXIT_OK
```

（imports 补 `from .store import Store` 已有；`Path` 已有。）

- [ ] **Step 4: Run tests + 手工冒烟**

Run: `python3 -m unittest tests.test_cli -v -k campaign` → PASS
手工（真实数据，验证完整报告观感）：

```bash
cd ~/sglang-benchmark-tools
python3 -m sglbench campaign report --pair l2off-opt=l2pp1-opt -o reports/l2full-v2/
# 打开 reports/l2full-v2/index.html 检查：热图颜色方向、断崖曲线、表格数值与
# docs/TEST-REPORT-2026-10-05-L2-full-comparison.md 逐项对照一致
```

- [ ] **Step 5: 全量回归 + Commit**

Run: `python3 -m unittest discover -s tests -t . -q`
Expected: OK（191 + 新增全绿）

```bash
git add sglbench/cli.py tests/test_cli.py
git commit -m "sglbench: campaign report CLI —— 一键完整战役报告（含端到端冒烟）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## 自审记录（写完计划后自查）

1. **Spec 覆盖**：spec §2.1 逐轮 → T1–T5；§2.2 suite → T12–T15；§2.3 双线 → T10–T11；§2.4 趋势 → T7–T9；§7 测试计划中 svgchart/analyze/campaign/rounds 全部有对应用例；§8 S1–S3 一一对应。S4–S9（recover/forensics/executor/MCP/OpenClaw/文档）在 Plan 2/3，另行撰写（spec §8 为衔接契约）。
2. **占位符扫描**：Task 13 的"…见下 text 表构造"与 Task 9 Step 5、Task 15 Step 1 标注了"以现有文件实际形态为准"——均为**指向仓库现存代码的对齐说明**而非功能占位；核心逻辑与断言均已成文。
3. **类型一致性**：`round_delta_rows/round_series/round_verdict/extract_rounds`（T1–T2 定义）在 T3–T5、T13 使用签名一致；`build_suite → SuiteData.summaries[].round_verdict` 在 T12 定义、T13/T14 使用一致；`write_suite` 返回键 `index/summary_md/reports` 在 T14 定义、T15 使用一致。T12 明确要求给 `resolve_pairs` 补充 `status_off/status_on`（T13/T14 的表依赖它，兜底 `.get(...,"ok")` 不破坏 T5 用例）。
