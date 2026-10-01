"""报告渲染：终端表格、Markdown、自包含 HTML（内联 SVG）。

HTML 报告刻意做到**零外部依赖**：没有 CDN、没有 JS 框架、没有外链字体，一个文件
可以直接 scp 出来用浏览器打开，也可以贴进内网 wiki。
"""

from __future__ import annotations

import html
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import __version__
from .analyze import (
    ComparisonReport,
    MetricComparison,
    RunView,
    SweepReport,
    spec_for,
)
from .models import DISTRIBUTION_METRICS, Sample
from .svgchart import (
    PALETTE,
    cdf_chart,
    color_for,
    grouped_bar_chart,
    hbar_chart,
    histogram_chart,
    line_chart,
    percentile_range_chart,
)
from .util import format_metric, iso, utcnow

# --------------------------------------------------------------------------- #
# 终端宽度对齐（CJK 按 2 列算）
# --------------------------------------------------------------------------- #

_WIDE_RANGES = (
    (0x1100, 0x115F),
    (0x2E80, 0x303E),
    (0x3041, 0x33FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xA000, 0xA4CF),
    (0xAC00, 0xD7A3),
    (0xF900, 0xFAFF),
    (0xFE30, 0xFE6F),
    (0xFF00, 0xFF60),
    (0xFFE0, 0xFFE6),
    (0x1F300, 0x1F64F),
    (0x20000, 0x3FFFD),
)


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    code = ord(ch)
    for lo, hi in _WIDE_RANGES:
        if lo <= code <= hi:
            return 2
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_width(c) for c in str(text))


def pad(text: str, width: int, align: str = "left") -> str:
    text = "" if text is None else str(text)
    filler = max(0, width - display_width(text))
    if align == "right":
        return " " * filler + text
    if align == "center":
        left = filler // 2
        return " " * left + text + " " * (filler - left)
    return text + " " * filler


def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]], aligns: Sequence[str] | None = None) -> str:
    """等宽终端表格（自动处理中英文混排宽度）。"""
    cols = len(headers)
    aligns = list(aligns or (["left"] + ["right"] * (cols - 1)))
    widths = [display_width(h) for h in headers]
    for row in rows:
        for i in range(cols):
            cell = row[i] if i < len(row) else ""
            widths[i] = max(widths[i], display_width(cell))
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def line(cells: Sequence[str], align_row: Sequence[str]) -> str:
        return "| " + " | ".join(
            pad(cells[i] if i < len(cells) else "", widths[i], align_row[i]) for i in range(cols)
        ) + " |"

    out = [sep, line(headers, ["center"] * cols), sep]
    out += [line(row, aligns) for row in rows]
    out.append(sep)
    return "\n".join(out)


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[str]], aligns: Sequence[str] | None = None) -> str:
    cols = len(headers)
    aligns = list(aligns or (["---"] * cols))
    marker = []
    for i in range(cols):
        a = aligns[i] if i < len(aligns) else "---"
        marker.append({"left": ":---", "right": "---:", "center": ":---:"}.get(a, "---"))
    lines = ["| " + " | ".join(str(h) for h in headers) + " |", "| " + " | ".join(marker) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 文本 / Markdown 报告
# --------------------------------------------------------------------------- #

_VERDICT_MARK = {"better": "✅", "worse": "❌", "same": "➖", "changed": "🔸", "missing": "❔", "control": "⚙️"}


def _metric_rows(rows: Sequence[MetricComparison]) -> list[list[str]]:
    out: list[list[str]] = []
    for m in rows:
        out.append(
            [
                m.label,
                m.baseline_text,
                m.candidate_text,
                m.delta_text,
                f"{_VERDICT_MARK.get(m.verdict, '')}{m.verdict_label}",
            ]
        )
    return out


def render_console(report: ComparisonReport, *, max_rows: int | None = None) -> str:
    """给 CLI 用的纯文本对比视图。"""
    parts: list[str] = []
    scenario = report.scenario
    parts.append("=" * 78)
    parts.append(f"场景: {report.scenario_key}" + (f"  ({scenario.name})" if scenario else ""))
    parts.append(f"基线: {report.baseline_run}")
    parts.append(f"候选: {', '.join(report.candidate_runs)}")
    parts.append(f"噪声阈值: ±{report.threshold_pct:g}%   生成时间: {iso(report.generated_at or utcnow())}")
    parts.append("=" * 78)

    if report.warnings:
        parts.append("")
        parts.append("⚠️  可比性提示")
        for w in report.warnings:
            parts.append(f"  - {w}")

    rows = report.metrics
    if max_rows:
        rows = rows[:max_rows]
    if rows:
        parts.append("")
        parts.append("── 关键指标对比 " + "─" * 60)
        parts.append(
            render_table(
                ["指标", "基线", "候选", "变化", "判定"],
                _metric_rows(rows),
                ["left", "right", "right", "right", "left"],
            )
        )

    if report.distributions:
        parts.append("")
        parts.append("── 分布级检验（每请求样本） " + "─" * 45)
        drows = []
        for d in report.distributions:
            label = DISTRIBUTION_METRICS.get(d.metric, d.metric)
            boot = d.bootstrap
            drows.append(
                [
                    label,
                    f"{d.baseline_stats.get('median', float('nan')):.2f}",
                    f"{d.candidate_stats.get('median', float('nan')):.2f}",
                    f"{d.delta_median:+.2f}",
                    f"[{boot.get('ci_low', 0):+.2f}, {boot.get('ci_high', 0):+.2f}]" if boot else "-",
                    f"{d.ks.get('d', 0):.3f}",
                    f"{d.mann_whitney.get('p_value', 1):.4f}",
                    "显著" if d.significant else "不显著",
                ]
            )
        parts.append(
            render_table(
                ["指标", "基线中位", "候选中位", "Δ中位(ms)", "95% CI", "KS D", "MWU p", "结论"],
                drows,
                ["left", "right", "right", "right", "right", "right", "right", "left"],
            )
        )

    gates = report.gates
    if gates:
        parts.append("")
        parts.append("── SLA 门禁 " + "─" * 65)
        grows = []
        for run_id, results in gates.items():
            view = next((r for r in report.runs if r.run_id == run_id), None)
            for g in results:
                grows.append(
                    [
                        (view.display if view else run_id)[:26],
                        g.label,
                        g.actual_text,
                        g.threshold_text,
                        {"pass": "✅ PASS", "fail": "❌ FAIL", "unknown": "❔ N/A"}[g.status],
                    ]
                )
        parts.append(render_table(["run", "门禁项", "实测", "阈值", "结果"], grows, ["left", "left", "right", "right", "left"]))
        summary = report.gate_summary()
        parts.append(f"门禁汇总: PASS={summary.get('pass', 0)}  FAIL={summary.get('fail', 0)}  N/A={summary.get('unknown', 0)}")

    if report.conclusions:
        parts.append("")
        parts.append("── 结论 " + "─" * 68)
        for line in report.conclusions:
            parts.append(f"  • {line}")

    return "\n".join(parts)


def render_markdown(report: ComparisonReport) -> str:
    """给 PR / 测试报告粘贴用的 Markdown。"""
    out: list[str] = []
    scenario = report.scenario
    out.append(f"## 压测对比：{scenario.name if scenario else report.scenario_key}")
    out.append("")
    out.append(f"- 场景 key：`{report.scenario_key}`")
    out.append(f"- 基线 run：`{report.baseline_run}`")
    out.append(f"- 候选 run：{', '.join(f'`{r}`' for r in report.candidate_runs)}")
    out.append(f"- 噪声阈值：±{report.threshold_pct:g}%")
    out.append(f"- 生成时间：{iso(report.generated_at or utcnow())}")
    out.append(f"- 门禁结果：{'✅ 全部通过' if report.all_gates_pass else '❌ 存在失败项'}（{report.gate_summary()}）")
    out.append("")

    if report.conclusions:
        out.append("### 结论")
        out.append("")
        for line in report.conclusions:
            out.append(f"- {line}")
        out.append("")

    if report.warnings:
        out.append("### 可比性提示")
        out.append("")
        for w in report.warnings:
            out.append(f"- ⚠️ {w}")
        out.append("")

    if report.metrics:
        out.append("### 关键指标")
        out.append("")
        out.append(markdown_table(["指标", "基线", "候选", "变化", "判定"], _metric_rows(report.metrics)))
        out.append("")

    if report.distributions:
        out.append("### 分布级检验")
        out.append("")
        rows = []
        for d in report.distributions:
            boot = d.bootstrap
            rows.append(
                [
                    DISTRIBUTION_METRICS.get(d.metric, d.metric),
                    f"{d.baseline_stats.get('median', 0):.2f}",
                    f"{d.candidate_stats.get('median', 0):.2f}",
                    f"{d.delta_median:+.2f}",
                    f"[{boot.get('ci_low', 0):+.2f}, {boot.get('ci_high', 0):+.2f}]" if boot else "-",
                    f"{d.ks.get('d', 0):.3f}",
                    f"{d.mann_whitney.get('p_value', 1):.4f}",
                    "显著" if d.significant else "不显著",
                ]
            )
        out.append(
            markdown_table(["指标", "基线中位", "候选中位", "Δ中位(ms)", "95% CI", "KS D", "MWU p", "结论"], rows)
        )
        out.append("")

    if report.gates:
        out.append("### SLA 门禁")
        out.append("")
        rows = []
        for run_id, results in report.gates.items():
            view = next((r for r in report.runs if r.run_id == run_id), None)
            for g in results:
                rows.append(
                    [
                        view.display if view else run_id,
                        g.label,
                        g.actual_text,
                        g.threshold_text,
                        {"pass": "PASS", "fail": "FAIL", "unknown": "N/A"}[g.status],
                    ]
                )
        out.append(markdown_table(["run", "门禁项", "实测", "阈值", "结果"], rows))
        out.append("")
    return "\n".join(out)


def render_sweep_console(sweep: SweepReport) -> str:
    parts: list[str] = []
    parts.append("=" * 78)
    parts.append(f"并发梯度扫描：{sweep.group_key}")
    parts.append("=" * 78)
    rows = []
    for p in sweep.points:
        rows.append(
            [
                p.scenario_key,
                f"{p.max_concurrency:g}" if p.max_concurrency is not None else "-",
                f"{p.request_rate:g}" if p.request_rate is not None else "-",
                format_metric(p.success_rate, "%"),
                format_metric(p.mean_ttft_ms, "ms"),
                format_metric(p.p99_ttft_ms, "ms"),
                format_metric(p.mean_tpot_ms, "ms"),
                format_metric(p.total_throughput, "tok/s"),
                "✅" if p.healthy else "❌",
            ]
        )
    parts.append(
        render_table(
            ["场景", "并发", "速率", "成功率", "Mean TTFT", "P99 TTFT", "Mean TPOT", "总吞吐", "健康"],
            rows,
            ["left", "right", "right", "right", "right", "right", "right", "right", "center"],
        )
    )
    if sweep.notes:
        parts.append("")
        for note in sweep.notes:
            parts.append(f"  • {note}")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# HTML 报告
# --------------------------------------------------------------------------- #

_CSS = """
:root{--bg:#f8fafc;--card:#ffffff;--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;
      --ok:#059669;--bad:#dc2626;--warn:#d97706;--accent:#2563eb}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font-family:ui-sans-serif,-apple-system,'Segoe UI','Microsoft YaHei','PingFang SC',sans-serif;
     font-size:14px;line-height:1.6}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 64px}
h1{font-size:24px;margin:0 0 6px}
h2{font-size:17px;margin:34px 0 14px;padding-bottom:6px;border-bottom:2px solid var(--line)}
h3{font-size:14px;margin:20px 0 8px;color:#334155}
.meta{color:var(--muted);font-size:12.5px}
.meta code{background:#eef2f7;padding:1px 5px;border-radius:4px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(238px,1fr));gap:14px;margin:18px 0 6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;
      box-shadow:0 1px 2px rgba(15,23,42,.04)}
.card.baseline{border-left:4px solid var(--accent)}
.card.candidate{border-left:4px solid #7c3aed}
.card .role{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.card .name{font-weight:600;margin:2px 0 10px;word-break:break-all}
.kv{display:flex;justify-content:space-between;gap:12px;font-size:12.5px;padding:2px 0}
.kv span:first-child{color:var(--muted)}
.kv .v{font-variant-numeric:tabular-nums;font-weight:600}
.badge{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;font-weight:600}
.badge.better{background:#dcfce7;color:#166534}
.badge.worse{background:#fee2e2;color:#991b1b}
.badge.same{background:#e2e8f0;color:#334155}
.badge.changed{background:#fef3c7;color:#92400e}
.badge.missing,.badge.unknown{background:#f1f5f9;color:#64748b}
.badge.pass{background:#dcfce7;color:#166534}
.badge.fail{background:#fee2e2;color:#991b1b}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:10px;overflow:hidden;
      border:1px solid var(--line);font-size:13px}
th,td{padding:7px 11px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{background:#f1f5f9;font-weight:600;font-size:12.5px;color:#334155}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
tbody tr:hover{background:#f8fafc}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(520px,1fr));gap:16px}
.chart{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:8px;
       box-shadow:0 1px 2px rgba(15,23,42,.04);overflow:hidden}
.chart.full{grid-column:1/-1}
ul.concl{margin:0;padding-left:20px}
ul.concl li{margin:3px 0}
.note{background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:10px 14px;color:#78350f;font-size:13px}
.note.info{background:#eff6ff;border-color:#bfdbfe;color:#1e3a8a}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:8px 0}
summary{cursor:pointer;font-weight:600}
pre{background:#0f172a;color:#e2e8f0;padding:12px 14px;border-radius:8px;overflow-x:auto;font-size:12px}
code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
footer{margin-top:40px;color:var(--muted);font-size:12px;text-align:center}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;background:#eef2f7;color:#475569;font-size:11.5px;margin-right:6px}
@media print{body{background:#fff}.card,.chart,table{box-shadow:none}}
"""


def _esc(text: Any) -> str:
    return html.escape("" if text is None else str(text))


def _badge(verdict: str, label: str | None = None) -> str:
    text = label or {
        "better": "改善",
        "worse": "回归",
        "same": "持平",
        "changed": "变化",
        "missing": "缺失",
        "pass": "PASS",
        "fail": "FAIL",
        "unknown": "N/A",
    }.get(verdict, verdict)
    return f'<span class="badge {verdict}">{_esc(text)}</span>'


def _kpi_card(view: RunView, key_metrics: Sequence[tuple[str, str]]) -> str:
    role = "基线" if view.role == "baseline" else "候选"
    rows = []
    for key, label in key_metrics:
        value = view.metrics.get(key)
        unit = spec_for(key).unit
        text = format_metric(value, unit)
        if value is not None and unit in ("tok/s", "req/s"):
            text = f"{text} {unit}"
        rows.append(
            f'<div class="kv"><span>{_esc(label)}</span>'
            f'<span class="v">{_esc(text)}</span></div>'
        )
    status_badge = _badge("pass" if view.status == "ok" else "fail", view.status)
    return (
        f'<div class="card {view.role}">'
        f'<div class="role">{role} · {_esc(view.scenario_key)}</div>'
        f'<div class="name">{_esc(view.label or view.run_id)}</div>'
        + "".join(rows)
        + f'<div class="kv"><span>样本数</span><span class="v">{view.sample_count}</span></div>'
        f'<div class="kv"><span>状态</span><span class="v">{status_badge}</span></div>'
        f"</div>"
    )


def _metric_table(rows: Sequence[MetricComparison], title: str | None = None) -> str:
    body = []
    for m in rows:
        badge = _badge(m.verdict)
        body.append(
            "<tr>"
            f"<td>{_esc(m.label)}</td>"
            f'<td class="num">{_esc(m.baseline_text)}</td>'
            f'<td class="num">{_esc(m.candidate_text)}</td>'
            f'<td class="num">{_esc(m.delta_text)}</td>'
            f"<td>{badge}</td>"
            "</tr>"
        )
    head = (
        "<tr><th>指标</th><th class='num'>基线</th><th class='num'>候选</th>"
        "<th class='num'>变化</th><th>判定</th></tr>"
    )
    caption = f"<h3>{_esc(title)}</h3>" if title else ""
    return caption + f"<table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table>"


def _distribution_table(report: ComparisonReport) -> str:
    if not report.distributions:
        return '<div class="note info">本次对比没有可用的每请求明细，已跳过分布级检验。压测时加上 <code>--dump-requests</code>（或确认 <code>.requests.jsonl</code> 已生成）即可获得直方图与显著性检验。</div>'
    body = []
    for d in report.distributions:
        label = DISTRIBUTION_METRICS.get(d.metric, d.metric)
        boot = d.bootstrap
        ci = f"[{boot.get('ci_low', 0):+.2f}, {boot.get('ci_high', 0):+.2f}]" if boot else "-"
        body.append(
            "<tr>"
            f"<td>{_esc(label)}</td>"
            f'<td class="num">{d.baseline_stats.get("median", 0):.2f}</td>'
            f'<td class="num">{d.candidate_stats.get("median", 0):.2f}</td>'
            f'<td class="num">{d.delta_median:+.2f}</td>'
            f'<td class="num">{_esc(ci)}</td>'
            f'<td class="num">{d.ks.get("d", 0):.3f}</td>'
            f'<td class="num">{d.mann_whitney.get("p_value", 1):.4f}</td>'
            f'<td class="num">{d.cliffs_delta:+.3f}</td>'
            f'<td>{_badge("worse" if d.significant else "same", "显著" if d.significant else "不显著")}</td>'
            "</tr>"
        )
    head = (
        "<tr><th>指标</th><th class='num'>基线中位</th><th class='num'>候选中位</th>"
        "<th class='num'>Δ中位(ms)</th><th class='num'>bootstrap 95% CI</th>"
        "<th class='num'>KS D</th><th class='num'>MWU p</th><th class='num'>Cliff's δ</th><th>结论</th></tr>"
    )
    return f"<table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table>"


def _gate_table(report: ComparisonReport) -> str:
    body = []
    for run_id, results in report.gates.items():
        view = next((r for r in report.runs if r.run_id == run_id), None)
        for g in results:
            body.append(
                "<tr>"
                f"<td>{_esc(view.display if view else run_id)}</td>"
                f"<td>{_esc(g.label)}</td>"
                f'<td class="num">{_esc(g.actual_text)}</td>'
                f'<td class="num">{_esc(g.threshold_text)}</td>'
                f"<td>{_badge(g.status)}</td>"
                f"<td>{_esc(g.detail)}</td>"
                "</tr>"
            )
    head = "<tr><th>run</th><th>门禁项</th><th class='num'>实测</th><th class='num'>阈值</th><th>结果</th><th>说明</th></tr>"
    return f"<table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table>"


def _chart_card(svg: str, full: bool = False) -> str:
    return f'<div class="chart{" full" if full else ""}">{svg}</div>'


def _run_details(report: ComparisonReport, runs_meta: Mapping[str, Any] | None = None) -> str:
    parts: list[str] = []
    for view in report.runs:
        meta = (runs_meta or {}).get(view.run_id, {})
        issues = "".join(
            f"<li><code>{_esc(i.kind)}</code> (line {i.line_no}): {_esc(i.message[:260])}</li>"
            for i in view.issues[:12]
        )
        artifacts = meta.get("artifacts") or {}
        artifact_rows = "".join(
            f'<div class="kv"><span>{_esc(k)}</span><span class="v"><code>{_esc(v)}</code></span></div>'
            for k, v in artifacts.items()
        )
        params = meta.get("params") or {}
        param_rows = "".join(
            f'<div class="kv"><span>{_esc(k)}</span><span class="v">{_esc(v)}</span></div>'
            for k, v in list(params.items())[:22]
        )
        parts.append(
            f"<details><summary>{_esc(view.label or view.run_id)} "
            f'<span class="pill">{_esc(view.run_id)}</span></summary>'
            f'<div class="kv"><span>启动时间</span><span class="v">{_esc(iso(view.started_at) if view.started_at else "-")}</span></div>'
            + artifact_rows
            + (f'<h3>参数</h3>{param_rows}' if param_rows else "")
            + (f'<h3>日志异常</h3><ul>{"".join(issues)}</ul>' if issues else '<div class="note info">未检测到异常日志信号。</div>')
            + (
                f"<h3>执行命令</h3><pre>{_esc(meta.get('command') or '-')}</pre>"
                if meta.get("command")
                else ""
            )
            + "</details>"
        )
    return "".join(parts) if parts else '<div class="note">没有可展示的 run 详情。</div>'


def _dist_datasets(report: ComparisonReport, field: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, view in enumerate(report.runs):
        values = (view.metrics or {}).get(f"__samples__{field}")
        if values:
            out.append({"name": view.display, "values": values, "color": color_for(idx)})
    return out


def _samples_map(runs: Sequence[Any]) -> dict[str, dict[str, list[float]]]:
    """``{run_id: {field: [values]}}``，供图表复用。"""
    out: dict[str, dict[str, list[float]]] = {}
    for run in runs:
        samples: Sequence[Sample] = getattr(run, "samples", []) or []
        per_field: dict[str, list[float]] = {}
        for field in DISTRIBUTION_METRICS:
            vals = [
                float(getattr(s, field))
                for s in samples
                if s.ok and getattr(s, field, None) is not None
            ]
            if vals:
                per_field[field] = vals
        out[run.run_id] = per_field
    return out


def build_html(
    report: ComparisonReport,
    *,
    title: str | None = None,
    runs: Sequence[Any] = (),
    sweep: SweepReport | None = None,
    include_charts: bool = True,
    extra_notes: Sequence[str] = (),
    report_path: str | Path | None = None,
) -> str:
    """生成自包含 HTML 报告。

    ``runs`` 传入原始 :class:`RunRecord` 序列（带 samples），用于绘制分布图；
    只传 ``report`` 时退化为汇总指标对比。
    """
    scenario = report.scenario
    page_title = title or f"SGLang PD 压测对比 · {scenario.name if scenario else report.scenario_key}"
    samples_by_run = _samples_map(runs)
    run_meta = {
        getattr(r, "run_id", ""): {
            "params": getattr(r, "params", {}) or {},
            "artifacts": getattr(r, "artifacts", {}) or {},
            "command": getattr(r, "command", "") or "",
        }
        for r in runs
    }

    # ---- KPI 卡片 ----
    kpi_keys = [
        ("success_rate", "成功率"),
        ("mean_ttft_ms", "Mean TTFT"),
        ("p99_ttft_ms", "P99 TTFT"),
        ("mean_tpot_ms", "Mean TPOT"),
        ("p99_tpot_ms", "P99 TPOT"),
        ("output_token_throughput_tok_s", "输出吞吐"),
    ]
    cards = "".join(_kpi_card(v, kpi_keys) for v in report.runs)

    # ---- 图表 ----
    charts: list[str] = []
    if include_charts:
        # TTFT / TPOT 分两张图：两者量级差 10 倍以上，画在一张图里小指标会被压没
        for keys, chart_title, subtitle in (
            (
                ("mean_ttft_ms", "p99_ttft_ms"),
                "首 token 延迟 TTFT",
                "Prefill 算力 + KV 传输链路的直接体现",
            ),
            (
                ("mean_tpot_ms", "p99_tpot_ms"),
                "单输出 token 耗时 TPOT",
                "Decode 流式生成稳定性的直接体现",
            ),
        ):
            datasets = [
                {"name": v.display, "color": color_for(i), "values": [v.metrics.get(k) for k in keys]}
                for i, v in enumerate(report.runs)
            ]
            charts.append(
                _chart_card(
                    grouped_bar_chart(
                        ["均值", "P99"],
                        datasets,
                        title=chart_title,
                        ylabel="延迟",
                        unit="ms",
                        higher_is_better=False,
                        subtitle=subtitle,
                    )
                )
            )
        tp_datasets = [
            {
                "name": v.display,
                "color": color_for(i),
                "values": [
                    v.metrics.get("output_token_throughput_tok_s"),
                    v.metrics.get("total_token_throughput_tok_s"),
                ],
            }
            for i, v in enumerate(report.runs)
        ]
        charts.append(
            _chart_card(
                grouped_bar_chart(
                    ["输出 token 吞吐", "总 token 吞吐"],
                    tp_datasets,
                    title="吞吐",
                    ylabel="吞吐",
                    unit="tok/s",
                    higher_is_better=True,
                )
            )
        )
        charts.append(
            _chart_card(
                hbar_chart(
                    [
                        {
                            "label": f"{v.display}",
                            "value": (v.metrics.get("success_rate") or 0.0) * 100.0,
                            "color": color_for(i),
                        }
                        for i, v in enumerate(report.runs)
                    ],
                    title="请求成功率",
                    unit="%",
                    max_value=100.0,
                    subtitle="低于 100% 即存在失败请求，需结合日志排查",
                )
            )
        )

        for field, chart_title in (("ttft_ms", "TTFT 分布"), ("tpot_ms", "TPOT 分布")):
            ds = []
            for idx, view in enumerate(report.runs):
                vals = samples_by_run.get(view.run_id, {}).get(field)
                if vals:
                    ds.append({"name": view.display, "values": vals, "color": color_for(idx)})
            if len(ds) >= 1:
                charts.append(
                    _chart_card(
                        histogram_chart(
                            ds,
                            title=f"{chart_title}直方图",
                            xlabel=DISTRIBUTION_METRICS.get(field, field),
                            unit="ms",
                            subtitle="按样本占比归一化，可比不同请求数的 run",
                        )
                    )
                )
                charts.append(
                    _chart_card(
                        cdf_chart(
                            ds,
                            title=f"{chart_title} CDF",
                            xlabel=DISTRIBUTION_METRICS.get(field, field),
                            subtitle="曲线越靠左越好；交叉表示各有优势区间",
                        )
                    )
                )
                charts.append(
                    _chart_card(percentile_range_chart(ds, title=f"{chart_title}分位区间", unit="ms"), full=True)
                )

    # ---- 并发梯度 ----
    sweep_html = ""
    if sweep and sweep.points:
        xs = [p.max_concurrency or 0.0 for p in sweep.points]
        sweep_html += "<h2>并发梯度扫描</h2>"
        sweep_html += _chart_card(
            line_chart(
                xs,
                [
                    {"name": "总 token 吞吐 (tok/s)", "values": [p.total_throughput for p in sweep.points], "color": PALETTE[0]},
                ],
                title="吞吐随并发变化",
                xlabel="max-concurrency",
                ylabel="总 token 吞吐",
                unit="tok/s",
                x_log=True,
            ),
            full=True,
        )
        sweep_html += _chart_card(
            line_chart(
                xs,
                [
                    {"name": "Mean TTFT (ms)", "values": [p.mean_ttft_ms for p in sweep.points], "color": PALETTE[1]},
                    {"name": "P99 TTFT (ms)", "values": [p.p99_ttft_ms for p in sweep.points], "color": PALETTE[3]},
                ],
                title="TTFT 随并发变化",
                xlabel="max-concurrency",
                ylabel="TTFT",
                unit="ms",
                x_log=True,
            ),
            full=True,
        )
        sweep_html += _chart_card(
            line_chart(
                xs,
                [
                    {"name": "Mean TPOT (ms)", "values": [p.mean_tpot_ms for p in sweep.points], "color": PALETTE[2]},
                    {"name": "P99 TPOT (ms)", "values": [p.p99_tpot_ms for p in sweep.points], "color": PALETTE[4]},
                ],
                title="TPOT 随并发变化",
                xlabel="max-concurrency",
                ylabel="TPOT",
                unit="ms",
                x_log=True,
            ),
            full=True,
        )
        if sweep.notes:
            sweep_html += '<div class="note">' + "<br>".join(_esc(n) for n in sweep.notes) + "</div>"

    # ---- 组装 ----
    conclusions = "".join(f"<li>{_esc(c)}</li>" for c in report.conclusions)
    warnings = "".join(f'<div class="note">⚠️ {_esc(w)}</div>' for w in report.warnings)
    extra = "".join(f'<div class="note info">{_esc(n)}</div>' for n in extra_notes)
    gate_summary = report.gate_summary()

    doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(page_title)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>{_esc(page_title)}</h1>
  <div class="meta">
    场景 <code>{_esc(report.scenario_key)}</code>
    · 基线 <code>{_esc(report.baseline_run)}</code>
    · 候选 {", ".join(f"<code>{_esc(r)}</code>" for r in report.candidate_runs)}
    · 噪声阈值 ±{report.threshold_pct:g}%
    · 生成于 {_esc(iso(report.generated_at or utcnow()))}
    · sglbench v{__version__}
  </div>

  <div class="cards">{cards}</div>

  <h2>结论与门禁</h2>
  <div class="note {'info' if report.all_gates_pass else ''}">
    门禁：PASS {gate_summary.get('pass', 0)} · FAIL {gate_summary.get('fail', 0)} · N/A {gate_summary.get('unknown', 0)}
    —— {"全部通过" if report.all_gates_pass else "存在失败项"}
  </div>
  <ul class="concl">{conclusions or '<li>没有可用的结论（数据不足）。</li>'}</ul>
  {warnings}
  {extra}

  <h2>关键指标对比</h2>
  {_metric_table(report.metrics)}

  <h2>分布级检验</h2>
  {_distribution_table(report)}

  {"" if not include_charts else '<h2>可视化对比</h2><div class="charts">' + "".join(charts) + "</div>"}

  {sweep_html}

  <h2>SLA 门禁明细</h2>
  {_gate_table(report)}

  <h2>Run 详情与工件</h2>
  {_run_details(report, run_meta)}

  <footer>
    由 sglbench v{__version__} 生成 · 报告为自包含单文件（无外部依赖）· 数据源：DuckDB + 不可变 run 工件
    {f" · 路径 {_esc(report_path)}" if report_path else ""}
  </footer>
</div>
</body>
</html>
"""
    return doc


def write_html(path: str | Path, content: str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="\n")
    return p


__all__ = [
    "build_html",
    "display_width",
    "markdown_table",
    "pad",
    "render_console",
    "render_markdown",
    "render_sweep_console",
    "render_table",
    "write_html",
]
