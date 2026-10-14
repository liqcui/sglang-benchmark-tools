"""跨场景对比 Dashboard —— 直读 DuckDB, 生成多图 + 表格的自包含 HTML。

用途: 把"同一批场景, 两个部署(label)对比"的例行分析固化 (此前靠临时脚本)。
典型: L2-off vs L2-on、修复前 vs 修复后、镜像 A vs 镜像 B。

用法::

    sglbench dashboard --pair l2off-opt=l2pp1-opt -o reports/l2.html
    sglbench dashboard --pair A=B --pair C=D --title "..." -o out.html
    sglbench dashboard --run-pair <off_run>=<on_run> -o out.html   # 精确指定 run

配对逻辑: 每个 label 取**最新一次成功 run**, 按 scenario_key 对齐 (只用两侧都有的场景)。
"""
from __future__ import annotations

import html as _html
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .store import Store
from .rounds import round_series
from .svgchart import grouped_bar_chart, line_chart
from .util import format_metric

# 默认指标集: (metric_key, 显示名, 单位, 越高越好)
DEFAULT_METRICS: list[tuple[str, str, str, bool]] = [
    ("mean_ttft_ms", "首 token 延迟 均值", "ms", False),
    ("mean_tpot_ms", "单输出 token 耗时 均值", "ms", False),
    ("output_token_throughput_tok_s", "输出 token 吞吐", "tok/s", True),
    ("total_token_throughput_tok_s", "总 token 吞吐", "tok/s", True),
    ("input_token_throughput_tok_s", "输入 token 吞吐", "tok/s", True),
    ("success_rate", "请求成功率", "%", True),
    ("peak_output_token_throughput_tok_s", "峰值输出吞吐", "tok/s", True),
]


def _label_cond(label: str) -> tuple[str, str]:
    """label → SQL 条件片段与参数。尾部 ``*`` 表示前缀通配（如 ``l2off-opt*``）。"""
    if label.endswith("*"):
        return "label LIKE ?", label[:-1] + "%"
    return "label = ?", label


def _latest_ok_run(store: Store, label: str) -> dict[str, Any] | None:
    cond, param = _label_cond(label)
    rows = store.query(
        f"SELECT run_id, scenario_key, label, started_at FROM runs "
        f"WHERE {cond} AND status = 'ok' ORDER BY started_at DESC NULLS LAST LIMIT 1",
        [param],
    )
    return rows[0] if rows else None


def _runs_metrics(store: Store, run_id: str) -> dict[str, float]:
    return {
        r["metric_key"]: r["value"]
        for r in store.query("SELECT metric_key, value FROM metrics WHERE run_id = ?", [run_id])
    }


def resolve_pairs(
    store: Store,
    label_pairs: Sequence[Sequence[str]] | None = None,
    run_pairs: Sequence[Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    """解析成 [{name, off_run, on_run, metrics_off, metrics_on}] 列表。

    ``label_pairs``: [(label_off, label_on), ...] → 各取最新 ok run, 按 scenario_key 对齐;
    ``run_pairs``:   [(run_off, run_on), ...]     → 直接使用指定 run。
    """
    out: list[dict[str, Any]] = []
    for off_label, on_label in label_pairs or []:
        off = _latest_ok_run(store, off_label)
        on = _latest_ok_run(store, on_label)
        if not off or not on:
            continue
        # 两 label 下所有 ok run, 按 scenario_key 配对 (各取最新)
        off_cond, off_param = _label_cond(off_label)
        on_cond, on_param = _label_cond(on_label)
        off_runs = {
            r["scenario_key"]: r
            for r in store.query(
                f"SELECT run_id, scenario_key, status FROM runs WHERE {off_cond} AND status='ok' "
                "ORDER BY started_at ASC NULLS LAST",
                [off_param],
            )
        }
        on_runs = {
            r["scenario_key"]: r
            for r in store.query(
                f"SELECT run_id, scenario_key, status FROM runs WHERE {on_cond} AND status='ok' "
                "ORDER BY started_at ASC NULLS LAST",
                [on_param],
            )
        }
        for key in off_runs:
            if key not in on_runs:
                continue
            o, n = off_runs[key]["run_id"], on_runs[key]["run_id"]
            out.append(
                {
                    "name": key,
                    "off_run": o,
                    "on_run": n,
                    "status_off": off_runs[key]["status"],
                    "status_on": on_runs[key]["status"],
                    "metrics_off": _runs_metrics(store, o),
                    "metrics_on": _runs_metrics(store, n),
                }
            )
    for off_run, on_run in run_pairs or []:
        mo, mn = _runs_metrics(store, off_run), _runs_metrics(store, on_run)
        rows = store.query(
            "SELECT run_id, scenario_key, status FROM runs WHERE run_id IN (?, ?)",
            [off_run, on_run],
        )
        by_id = {r["run_id"]: r for r in rows}
        off_row = by_id.get(off_run, {})
        out.append(
            {
                "name": off_row.get("scenario_key") or off_run,
                "off_run": off_run,
                "on_run": on_run,
                "status_off": (off_row.get("status") or "ok"),
                "status_on": (by_id.get(on_run, {}).get("status") or "ok"),
                "metrics_off": mo,
                "metrics_on": mn,
            }
        )
    return out


def _delta_cell(off: float | None, on: float | None, unit: str, higher_better: bool) -> str:
    if off is None or on is None:
        return "<td>—</td><td>—</td><td>—</td>"
    d = (on - off) / off * 100 if off else 0.0
    good = d > 3 if higher_better else d < -3
    bad = d < -3 if higher_better else d > 3
    color = "#1a7f37" if good else ("#c62828" if bad else "#666")
    return (
        f"<td>{_html.escape(format_metric(off, unit))}</td>"
        f"<td>{_html.escape(format_metric(on, unit))}</td>"
        f'<td style="color:{color}">{d:+.1f}%</td>'
    )


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


def render_dashboard(
    pairs: Sequence[Mapping[str, Any]],
    *,
    title: str = "跨场景对比 Dashboard",
    pair_titles: tuple[str, str] = ("A", "B"),
    metrics: Sequence[tuple[str, str, str, bool]] = DEFAULT_METRICS,
    note: str = "",
) -> str:
    if not pairs:
        return "<html><body><p>没有可对比的成对数据 (检查 label/run 是否有同 scenario_key 的 ok run)。</p></body></html>"

    cats = [str(p["name"]) for p in pairs]
    charts: list[str] = []
    for key, label, unit, higher in metrics:
        off_vals = [p["metrics_off"].get(key) for p in pairs]
        on_vals = [p["metrics_on"].get(key) for p in pairs]
        if all(v is None for v in off_vals + on_vals):
            continue
        series = [
            {"name": pair_titles[0], "values": off_vals, "color": "#5b8def"},
            {"name": pair_titles[1], "values": on_vals, "color": "#e8734a"},
        ]
        charts.append(
            grouped_bar_chart(
                cats,
                series,
                title=label,
                ylabel=label,
                unit=unit,
                higher_is_better=higher,
                width=1160,
                height=360,
                subtitle=f"{pair_titles[0]} vs {pair_titles[1]} · 每场景一组柱",
            )
        )

    thead = (
        "<tr><th rowspan=2>场景</th>"
        + "".join(f"<th colspan=3>{_html.escape(label)}</th>" for _, label, _, _ in metrics)
        + "</tr><tr>"
        + "".join(f"<th>{pair_titles[0]}</th><th>{pair_titles[1]}</th><th>Δ</th>" for _ in metrics)
        + "</tr>"
    )
    trs = []
    for p in pairs:
        tds = "".join(
            _delta_cell(p["metrics_off"].get(k), p["metrics_on"].get(k), unit, higher)
            for k, _, unit, higher in metrics
        )
        trs.append(
            f'<tr><td>{_html.escape(str(p["name"]))}<div class="runs">{_html.escape(p["off_run"][:36])}'
            f' → {_html.escape(p["on_run"][:36])}</div></td>{tds}</tr>'
        )

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>{_html.escape(title)}</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;margin:24px auto;max-width:1220px;padding:0 16px;color:#222;background:#fafafa}}
h1{{font-size:22px}} h2{{font-size:17px;margin-top:26px}}
.card{{background:#fff;border:1px solid #e3e3e3;border-radius:10px;padding:12px;margin:14px 0}}
.charts{{display:grid;grid-template-columns:1fr;gap:14px}}
table{{border-collapse:collapse;width:100%;font-size:12.5px}}
th,td{{border:1px solid #e0e0e0;padding:5px 8px;text-align:right}}
th{{background:#f4f6f8;text-align:center}} td:first-child,th:first-child{{text-align:left}}
.runs{{color:#999;font-size:10px;font-family:ui-monospace,monospace}}
.note{{color:#666;font-size:13px;line-height:1.6}}
</style></head><body>
<h1>{_html.escape(title)}</h1>
<div class="card note">{note or "同一批场景, 两个部署（或两个 run）的逐场景对比。绿=改善 (&gt;3%), 红=劣化 (&gt;3%)。"}</div>
<div class="charts">{''.join(f'<div class="card">{c}</div>' for c in charts)}</div>
{_round_section(pairs)}
<h2>全指标表格（{pair_titles[0]} → {pair_titles[1]}）</h2>
<div class="card"><table>{thead}{''.join(trs)}</table></div>
<p class="note">生成: sglbench dashboard · {generated} · 数据源 DuckDB</p>
</body></html>"""


def write_dashboard(
    store: Store,
    out: str | Path,
    *,
    label_pairs: Sequence[Sequence[str]] | None = None,
    run_pairs: Sequence[Sequence[str]] | None = None,
    title: str | None = None,
    pair_titles: tuple[str, str] | None = None,
    note: str = "",
) -> Path:
    pairs = resolve_pairs(store, label_pairs=label_pairs, run_pairs=run_pairs)
    pt = pair_titles
    if pt is None and label_pairs:
        pt = (label_pairs[0][0], label_pairs[0][1])
    t = title or (f"跨场景对比: {pt[0]} vs {pt[1]}" if pt else "跨场景对比 Dashboard")
    text = render_dashboard(pairs, title=t, pair_titles=pt or ("A", "B"), note=note)
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target
