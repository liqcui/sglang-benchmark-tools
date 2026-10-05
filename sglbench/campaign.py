"""战役汇总报告（suite）—— 把"同一批场景, 两个部署对比"产出为完整测试报告。

对标人工维护的 docs/TEST-REPORT-*.md：受控条件表（run_environments 差异）+
场景汇总（TTFT/TPOT）+ 吞吐量完整宽表 + 多轮汇总 + 稳定性 + 结论草稿。
index.html / SUMMARY.md / 每场景明细 由 write_suite 落盘。
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .analyze import DEFAULT_THRESHOLD_PCT
from .rounds import round_verdict
from .store import Store
from .util import format_metric as _fmt

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
    """(on-off)/off*100%；任一缺失或 off=0 → None。"""
    if off is None or on is None or not off:
        return None
    return (on - off) / off * 100.0


def verdict_of(
    pct: float | None, *, higher_better: bool, threshold: float = DEFAULT_THRESHOLD_PCT
) -> str:
    """按指标方向给出 ✅/❌/➖/— 判定。"""
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
    """从 DuckDB 组装 suite 数据（配对逻辑与 dashboard 一致：label→每场景最新 ok run）。"""
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
        rv = round_verdict(off_m, on_m)
        summaries.append(
            ScenarioSummary(
                key=key,
                name=names.get(key, ""),
                off_run=p["off_run"],
                on_run=p["on_run"],
                off=off_m,
                on=on_m,
                status_off=str(p.get("status_off", "ok")),
                status_on=str(p.get("status_on", "ok")),
                verdict=verdict_of(t, higher_better=False),
                round_verdict=(rv if rv["verdict"] != "无逐轮数据" else None),
            )
        )

    env_rows: list[tuple[str, str, str, bool]] = []
    if pairs:
        off_env = store.get_environment(pairs[0]["off_run"]) or {}
        on_env = store.get_environment(pairs[0]["on_run"]) or {}
        if off_env or on_env:
            for col, disp in ENV_DISPLAY_KEYS:
                a, b = off_env.get(col), on_env.get(col)
                fa = "—" if a is None or a == "" else str(a)
                fb = "—" if b is None or b == "" else str(b)
                env_rows.append((disp, fa, fb, fa != fb))

    suite = SuiteData(
        title=title or f"跨场景对比: {pt[0]} vs {pt[1]}",
        pair_titles=pt,
        env_rows=env_rows,
        summaries=summaries,
    )
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


# --------------------------------------------------------------------------- #
# suite HTML / Markdown（index + SUMMARY.md）
# --------------------------------------------------------------------------- #

_CSS = """
body{font-family:system-ui,-apple-system,sans-serif;margin:24px auto;max-width:1220px;padding:0 16px;color:#222;background:#fafafa}
h1{font-size:22px} h2{font-size:17px;margin-top:28px}
.card{background:#fff;border:1px solid #e3e3e3;border-radius:10px;padding:12px;margin:14px 0;overflow-x:auto}
.charts{display:grid;grid-template-columns:1fr;gap:14px}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{border:1px solid #e0e0e0;padding:5px 8px;text-align:right}
th{background:#f4f6f8;text-align:center} td:first-child,th:first-child{text-align:left}
tr.diff td{background:#fff7ed} tr.diff td:first-child{font-weight:600}
a{color:#2563eb;text-decoration:none}
.note{color:#666;font-size:13px;line-height:1.7}
ul{margin:6px 0 0 18px;padding:0;font-size:13px;line-height:1.8}
"""


def _delta_text(d: float | None) -> str:
    return "—" if d is None else f"{d:+.1f}%"


def _delta_color(d: float | None, higher_better: bool) -> str:
    if d is None:
        return "#666"
    good = d >= DEFAULT_THRESHOLD_PCT if higher_better else d <= -DEFAULT_THRESHOLD_PCT
    bad = d <= -DEFAULT_THRESHOLD_PCT if higher_better else d >= DEFAULT_THRESHOLD_PCT
    return "#1a7f37" if good else ("#c62828" if bad else "#666")


def _env_table(suite: SuiteData) -> str:
    a, b = suite.pair_titles
    rows = "".join(
        f'<tr class="{"diff" if diff else ""}"><td>{html.escape(name)}'
        f'{" ⚠差异" if diff else ""}</td><td>{html.escape(va)}</td><td>{html.escape(vb)}</td></tr>'
        for name, va, vb, diff in suite.env_rows
    )
    return (
        f"<table><tr><th>项</th><th>{html.escape(a)}</th><th>{html.escape(b)}</th></tr>{rows}</table>"
    )


def _summary_table(suite: SuiteData) -> str:
    a, b = suite.pair_titles
    rows = []
    for s in suite.summaries:
        t = delta_pct(s.off.get("mean_ttft_ms"), s.on.get("mean_ttft_ms"))
        p = delta_pct(s.off.get("mean_tpot_ms"), s.on.get("mean_tpot_ms"))
        rows.append(
            f"<tr><td>{html.escape(s.key)}</td>"
            f"<td>{html.escape(_fmt(s.off.get('mean_ttft_ms'), 'ms'))} → "
            f"{html.escape(_fmt(s.on.get('mean_ttft_ms'), 'ms'))}</td>"
            f'<td style="color:{_delta_color(t, False)}">{_delta_text(t)}</td>'
            f"<td>{html.escape(_fmt(s.off.get('mean_tpot_ms'), 'ms'))} → "
            f"{html.escape(_fmt(s.on.get('mean_tpot_ms'), 'ms'))}</td>"
            f'<td style="color:{_delta_color(p, False)}">{_delta_text(p)}</td>'
            f"<td>{s.verdict}</td></tr>"
        )
    return (
        f"<table><tr><th rowspan=2>场景</th><th colspan=2>TTFT</th><th colspan=2>TPOT</th>"
        f"<th rowspan=2>判定</th></tr><tr><th>{html.escape(a)} → {html.escape(b)}</th><th>Δ</th>"
        f"<th>{html.escape(a)} → {html.escape(b)}</th><th>Δ</th></tr>" + "".join(rows) + "</table>"
    )


def _throughput_table(suite: SuiteData) -> str:
    a, b = suite.pair_titles
    head = (
        "<tr><th rowspan=2>场景</th>"
        + "".join(f"<th colspan=2>{html.escape(disp)}</th>" for _k, disp, _h in THROUGHPUT_KEYS)
        + "</tr><tr>"
        + "".join(
            f"<th>{html.escape(a)} → {html.escape(b)}</th><th>Δ</th>" for _k, _d, _h in THROUGHPUT_KEYS
        )
        + "</tr>"
    )
    rows = []
    for s in suite.summaries:
        cells = [f"<td>{html.escape(s.key)}</td>"]
        for key, _disp, higher in THROUGHPUT_KEYS:
            d = delta_pct(s.off.get(key), s.on.get(key))
            cells.append(
                f"<td>{html.escape(_fmt(s.off.get(key), 'tok/s'))} → "
                f"{html.escape(_fmt(s.on.get(key), 'tok/s'))}</td>"
                f'<td style="color:{_delta_color(d, higher)}">{_delta_text(d)}</td>'
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table>{head}{''.join(rows)}</table>"


def _round_table(suite: SuiteData) -> str:
    rows = []
    for s in suite.summaries:
        rv = s.round_verdict
        if not rv:
            continue
        rows.append(
            f"<tr><td>{html.escape(s.key)}</td>"
            f'<td style="color:{_delta_color(rv.get("round0_pct"), False)}">{_delta_text(rv.get("round0_pct"))}</td>'
            f'<td style="color:{_delta_color(rv.get("round1_pct"), False)}">{_delta_text(rv.get("round1_pct"))}</td>'
            f'<td style="color:{_delta_color(rv.get("steady_pct"), False)}">{_delta_text(rv.get("steady_pct"))}</td>'
            f"<td>{html.escape(str(rv.get('verdict', '')))}</td></tr>"
        )
    if not rows:
        return "<p class='note'>本次对比没有多轮（multiturn）场景。</p>"
    return (
        "<table><tr><th>场景</th><th>round0(冷) Δ</th><th>round1(首次热) Δ</th>"
        "<th>稳态 Δ</th><th>判定</th></tr>" + "".join(rows) + "</table>"
    )


def _stability_table(suite: SuiteData) -> str:
    a, b = suite.pair_titles
    rows = "".join(
        f"<tr><td>{html.escape(s.key)}</td><td>{html.escape(s.status_off)}</td>"
        f"<td>{html.escape(s.status_on)}</td></tr>"
        for s in suite.summaries
    )
    return (
        f"<table><tr><th>场景</th><th>{html.escape(a)}</th><th>{html.escape(b)}</th></tr>"
        + rows + "</table>"
    )


def _suite_charts(suite: SuiteData) -> list[str]:
    from .rounds import round_series
    from .svgchart import grouped_bar_chart, heatmap_chart, line_chart

    cats = [s.key for s in suite.summaries]
    charts: list[str] = []
    heat_vals = [
        [
            delta_pct(s.off.get("mean_ttft_ms"), s.on.get("mean_ttft_ms")),
            delta_pct(s.off.get("mean_tpot_ms"), s.on.get("mean_tpot_ms")),
            delta_pct(s.off.get("total_token_throughput_tok_s"), s.on.get("total_token_throughput_tok_s")),
            delta_pct(s.off.get("peak_output_token_throughput_tok_s"), s.on.get("peak_output_token_throughput_tok_s")),
        ]
        for s in suite.summaries
    ]
    charts.append(
        heatmap_chart(
            cats, ["TTFT", "TPOT", "总吞吐", "峰值输出"], heat_vals,
            title="Δ 热图（方向已归一）",
            better_dirs=[False, False, True, True],
        )
    )
    for key, disp, unit in (
        ("mean_ttft_ms", "TTFT 均值对比", "ms"),
        ("total_token_throughput_tok_s", "总吞吐对比", "tok/s"),
    ):
        series = [
            {"name": suite.pair_titles[0], "values": [s.off.get(key) for s in suite.summaries], "color": "#5b8def"},
            {"name": suite.pair_titles[1], "values": [s.on.get(key) for s in suite.summaries], "color": "#e8734a"},
        ]
        charts.append(
            grouped_bar_chart(
                cats, series, title=disp, ylabel=disp, unit=unit,
                higher_is_better=(key != "mean_ttft_ms"), width=1160, height=360,
            )
        )
    for s in suite.summaries:
        x, series = round_series(
            s.off, s.on, base_name=suite.pair_titles[0], cand_name=suite.pair_titles[1]
        )
        if not x:
            continue
        charts.append(
            line_chart(
                [float(v) for v in x], series, title=f"{s.key} · 逐轮 TTFT",
                xlabel="轮次", ylabel="TTFT", unit="ms", width=1160, height=320,
                annotate_points=False,
                subtitle="本引擎 cache_hit_rate 恒 0（测量限制）——看 TTFT 断崖",
            )
        )
    return charts


def render_suite_html(
    suite: SuiteData, *, report_links: Mapping[str, str] | None = None
) -> str:
    """suite index.html：受控条件 + 全表 + 热图/柱状/断崖曲线 + 结论 + 导航。"""
    from .util import iso, utcnow

    links = dict(report_links or {})
    a, b = suite.pair_titles
    nav = " · ".join(
        f'<a href="{html.escape(links[s.key])}">{html.escape(s.key)} 明细</a>'
        for s in suite.summaries
        if s.key in links
    )
    conclusions = "".join(f"<li>{html.escape(c)}</li>" for c in suite.conclusions)
    charts_html = "".join(f'<div class="card">{c}</div>' for c in _suite_charts(suite))
    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>{html.escape(suite.title)}</title>
<style>{_CSS}</style></head><body>
<h1>{html.escape(suite.title)}</h1>
<div class="card note">
战役汇总（sglbench campaign）· <b>{html.escape(a)}</b> vs <b>{html.escape(b)}</b>
· 每场景取最新 ok run 按 scenario_key 对齐 · 绿=改善 红=劣化（±{DEFAULT_THRESHOLD_PCT:g}% 噪声阈值）
· 生成 {html.escape(iso(utcnow()))}
{'<br>每场景完整报告（含直方图/CDF/分位区间）: ' + nav if nav else ''}
</div>

<h2 id="env">① 受控条件（两侧 run 的环境快照差异）</h2>
<div class="card">{_env_table(suite)}</div>

<h2 id="summary">② 压测场景汇总（TTFT / TPOT）</h2>
<div class="card">{_summary_table(suite)}</div>

<h2 id="throughput">③ 吞吐量完整对比（token/s）</h2>
<div class="card">{_throughput_table(suite)}</div>

<h2 id="rounds">④ 多轮对话（逐轮 TTFT）</h2>
<div class="card">{_round_table(suite)}</div>

<h2 id="stability">⑤ 稳定性</h2>
<div class="card">{_stability_table(suite)}</div>

<h2 id="charts">⑥ 图表</h2>
<div class="charts">{charts_html}</div>

<h2 id="conclusions">⑦ 结论（自动草稿）</h2>
<div class="card"><ul>{conclusions}</ul></div>

<p class="note">生成: sglbench campaign report · 数据源 DuckDB（run_environments / metrics / request_samples）</p>
</body></html>"""


def render_suite_markdown(suite: SuiteData) -> str:
    """SUMMARY.md —— 表格与 index 同源（可直接 commit 进 docs/）。"""
    a, b = suite.pair_titles
    lines = [f"# {suite.title}", "", f"> 生成: sglbench campaign · {a} vs {b}", ""]
    lines += ["## 1. 受控条件", "", f"| 项 | {a} | {b} |", "|---|---|---|"]
    lines += [
        f"| {k}{' ⚠差异' if diff else ''} | {va} | {vb} |" for k, va, vb, diff in suite.env_rows
    ]
    lines += [
        "", "## 2. 场景汇总（TTFT/TPOT）", "",
        f"| 场景 | TTFT {a}→{b} | Δ | TPOT {a}→{b} | Δ | 判定 |",
        "|---|---|---|---|---|---|",
    ]
    for s in suite.summaries:
        t = delta_pct(s.off.get("mean_ttft_ms"), s.on.get("mean_ttft_ms"))
        p = delta_pct(s.off.get("mean_tpot_ms"), s.on.get("mean_tpot_ms"))
        lines.append(
            f"| {s.key} | {_fmt(s.off.get('mean_ttft_ms'), 'ms')}→{_fmt(s.on.get('mean_ttft_ms'), 'ms')} "
            f"| {_delta_text(t)} | {_fmt(s.off.get('mean_tpot_ms'), 'ms')}→{_fmt(s.on.get('mean_tpot_ms'), 'ms')} "
            f"| {_delta_text(p)} | {s.verdict} |"
        )
    lines += [
        "", "## 3. 吞吐量完整对比（token/s）", "",
        f"| 场景 | 输入 {a}→{b} | Δ | 输出 {a}→{b} | Δ | 总 {a}→{b} | Δ | 峰值输出 {a}→{b} | Δ |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in suite.summaries:
        cells = [s.key]
        for key, _disp, _higher in THROUGHPUT_KEYS:
            d = delta_pct(s.off.get(key), s.on.get(key))
            cells += [f"{_fmt(s.off.get(key), 'tok/s')}→{_fmt(s.on.get(key), 'tok/s')}", _delta_text(d)]
        lines.append("| " + " | ".join(cells) + " |")
    mt = [s for s in suite.summaries if s.round_verdict]
    if mt:
        lines += [
            "", "## 4. 多轮对话（逐轮 TTFT）", "",
            "| 场景 | round0(冷) Δ | round1(首次热) Δ | 稳态 Δ | 判定 |",
            "|---|---|---|---|---|",
        ]
        for s in mt:
            rv = s.round_verdict or {}
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
    suite = build_suite(
        store, label_pairs=label_pairs, run_pairs=run_pairs, title=title, pair_titles=pair_titles
    )

    links: dict[str, str] = {}
    reports: dict[str, Path] = {}
    for i, s in enumerate(suite.summaries):
        fname = f"{i + 1:02d}-{s.key}.html"
        links[s.key] = fname
        recs = [store.get_run(r, with_samples=True) for r in (s.off_run, s.on_run)]
        recs = [r for r in recs if r is not None]
        scenario = scenario_for_run(recs[0]) if recs else None
        comparison = compare_runs(store, [s.off_run, s.on_run], baseline=s.off_run, scenario=scenario)
        html = build_html(
            comparison, runs=recs,
            title=f"{s.key} · {suite.pair_titles[0]} vs {suite.pair_titles[1]}",
            report_path=target / fname,
        )
        write_html(target / fname, html)
        reports[s.key] = target / fname

    index_path = target / "index.html"
    write_html(index_path, render_suite_html(suite, report_links=links))

    md_path: Path | None = None
    if with_md:
        md_path = target / "SUMMARY.md"
        md_path.write_text(render_suite_markdown(suite), encoding="utf-8", newline="\n")
    return {"index": index_path, "summary_md": md_path, "reports": reports}
