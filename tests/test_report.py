"""报告与图表渲染测试。"""

from __future__ import annotations

import re
import unittest
import xml.etree.ElementTree as ET

from sglbench.analyze import analyze_sweep, compare_runs
from sglbench.models import RunRecord
from sglbench.report import (
    build_html,
    display_width,
    markdown_table,
    pad,
    render_console,
    render_markdown,
    render_sweep_console,
    render_table,
)
from sglbench.scenarios import get_scenario
from sglbench.store import Store
from sglbench.svgchart import (
    cdf_chart,
    color_for,
    grouped_bar_chart,
    hbar_chart,
    histogram_chart,
    line_chart,
    nice_ticks,
    percentile_range_chart,
)
from sglbench.util import utcnow

from .helpers import synthesize_samples, temp_home

METRICS_A = {
    "successful_requests": 50.0,
    "success_rate": 1.0,
    "failed_requests": 0.0,
    "mean_ttft_ms": 84.0,
    "p99_ttft_ms": 113.0,
    "mean_tpot_ms": 7.7,
    "p99_tpot_ms": 8.7,
    "mean_e2e_latency_ms": 15856.0,
    "p99_e2e_latency_ms": 17860.0,
    "output_token_throughput_tok_s": 5153.5,
    "total_token_throughput_tok_s": 15460.5,
    "request_throughput_req_s": 2.52,
    "tail_ratio_ttft": 1.405,
    "itl_cv": 0.088,
}
METRICS_B = {**METRICS_A, "mean_ttft_ms": 92.8, "p99_ttft_ms": 124.0, "output_token_throughput_tok_s": 4834.0}


def seed(store: Store) -> None:
    store.upsert_scenarios([get_scenario("single-standard"), get_scenario("conc-128-standard")])
    store.save_run(
        RunRecord(
            run_id="base",
            scenario_key="single-standard",
            label="baseline",
            status="ok",
            started_at=utcnow(),
            metrics=dict(METRICS_A),
            samples=synthesize_samples(70, ttft_mean=84.0),
            params={"num_prompts": 70, "max_concurrency": 1},
            command="python3 -m sglang.bench_serving --max-concurrency 1",
            artifacts={"stdout": "/runs/base/stdout.log"},
        )
    )
    store.save_run(
        RunRecord(
            run_id="cand",
            scenario_key="single-standard",
            label="candidate",
            status="ok",
            started_at=utcnow(),
            metrics=dict(METRICS_B),
            samples=synthesize_samples(70, ttft_mean=93.0, seed=5),
            params={"num_prompts": 70, "max_concurrency": 1},
            command="python3 -m sglang.bench_serving --max-concurrency 1",
            artifacts={"stdout": "/runs/cand/stdout.log"},
        )
    )
    store.save_run(
        RunRecord(
            run_id="crash",
            scenario_key="conc-128-standard",
            label="crash-run",
            status="partial",
            started_at=utcnow(),
            metrics={**METRICS_A, "success_rate": 0.7067, "failed_requests": 88.0, "max_request_concurrency": 128.0},
            params={"num_prompts": 300, "max_concurrency": 128},
        )
    )


class TestTextTables(unittest.TestCase):
    def test_display_width_cjk(self) -> None:
        self.assertEqual(display_width("abc"), 3)
        self.assertEqual(display_width("中文"), 4)
        self.assertEqual(display_width("a中"), 3)

    def test_pad_alignment(self) -> None:
        self.assertEqual(pad("中", 4), "中  ")
        self.assertEqual(pad("1", 3, "right"), "  1")

    def test_render_table_columns_aligned(self) -> None:
        table = render_table(["指标", "值"], [["首 token 延迟", "84.00 ms"], ["TPOT", "7.70 ms"]])
        lines = table.splitlines()
        self.assertEqual(len(lines), 6)
        self.assertTrue(all(line.startswith("+") or line.startswith("|") for line in lines))
        # 每行显示宽度一致
        widths = {display_width(line) for line in lines}
        self.assertEqual(len(widths), 1, table)

    def test_markdown_table(self) -> None:
        md = markdown_table(["a", "b"], [["1", "2"]], ["left", "right"])
        self.assertIn("| a | b |", md)
        self.assertIn("| :--- | ---: |", md)
        self.assertIn("| 1 | 2 |", md)


class TestConsoleReport(unittest.TestCase):
    def _report(self):
        with temp_home() as paths, Store(paths.db) as store:
            seed(store)
            return compare_runs(
                store, ["base", "cand"], scenario=get_scenario("single-standard"), resamples=300
            )

    def test_sections_present(self) -> None:
        text = render_console(self._report())
        for needle in ("关键指标对比", "分布级检验", "SLA 门禁", "结论", "回归"):
            self.assertIn(needle, text)

    def test_markdown_report(self) -> None:
        md = render_markdown(self._report())
        self.assertIn("## 压测对比", md)
        self.assertIn("### 结论", md)
        self.assertIn("### SLA 门禁", md)
        self.assertIn("| 指标 | 基线 | 候选 | 变化 | 判定 |", md)

    def test_sweep_console(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            seed(store)
            sweep = analyze_sweep(store, family=None)
            text = render_sweep_console(sweep)
        self.assertIn("并发梯度扫描", text)
        self.assertIn("健康", text)


class TestHtmlReport(unittest.TestCase):
    def _build(self) -> str:
        with temp_home() as paths, Store(paths.db) as store:
            seed(store)
            report = compare_runs(store, ["base", "cand"], scenario=get_scenario("single-standard"), resamples=300)
            records = [store.get_run(r, with_samples=True) for r in ["base", "cand"]]
            sweep = analyze_sweep(store, family=None)
            return build_html(report, runs=[r for r in records if r], sweep=sweep)

    def test_self_contained(self) -> None:
        html = self._build()
        self.assertEqual(re.findall(r'(?:src|href)\s*=\s*"https?://', html), [])
        self.assertEqual(re.findall(r"<script", html), [])
        self.assertIn("<!doctype html>", html)

    def test_required_sections(self) -> None:
        html = self._build()
        for needle in ("结论与门禁", "关键指标对比", "分布级检验", "可视化对比", "SLA 门禁明细", "Run 详情与工件"):
            self.assertIn(needle, html)

    def test_svgs_are_well_formed_xml(self) -> None:
        html = self._build()
        svgs = re.findall(r"<svg\b.*?</svg>", html, re.S)
        self.assertGreaterEqual(len(svgs), 8)
        for index, svg in enumerate(svgs):
            with self.subTest(svg=index):
                ET.fromstring(svg)

    def test_escapes_untrusted_text(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            seed(store)
            run = store.get_run("base", with_samples=False)
            assert run is not None
            run.label = '<script>alert("x")</script>'
            store.save_run(run)
            report = compare_runs(store, ["base", "cand"], resamples=200)
            html = build_html(report, runs=[])
        self.assertNotIn('<script>alert("x")</script>', html)
        self.assertIn("&lt;script&gt;", html)

    def test_note_when_no_samples(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            seed(store)
            report = compare_runs(store, ["base", "cand"], with_distributions=False, resamples=200)
            html = build_html(report, runs=[])
        self.assertIn("已跳过分布级检验", html)


class TestCharts(unittest.TestCase):
    def test_nice_ticks(self) -> None:
        ticks = nice_ticks(0, 100)
        self.assertEqual(ticks[0], 0)
        self.assertLessEqual(ticks[-1], 100)
        self.assertGreater(len(ticks), 2)
        self.assertEqual(nice_ticks(5, 5), [5])

    def test_color_palette_wraps(self) -> None:
        from sglbench.svgchart import PALETTE

        self.assertEqual(color_for(0), PALETTE[0])
        self.assertEqual(color_for(len(PALETTE)), PALETTE[0])

    def test_each_chart_returns_svg(self) -> None:
        values = synthesize_samples(60, ttft_mean=84.0)
        series = [{"name": "a", "values": [s.ttft_ms for s in values if s.ttft_ms]}]
        cases = [
            ("h", histogram_chart(series, title="h", xlabel="TTFT")),
            ("c", cdf_chart(series, title="c", xlabel="TTFT")),
            ("g", grouped_bar_chart(["mean", "p99"], [{"name": "a", "values": [84.0, 113.0]}], title="g")),
            ("l", line_chart([1, 32, 64], [{"name": "tp", "values": [10, 30, 15]}], title="l", x_log=True)),
            ("r", percentile_range_chart(series, title="r")),
            ("b", hbar_chart([{"label": "a", "value": 100.0}], title="b", unit="%", max_value=100.0)),
        ]
        for title, svg in cases:
            with self.subTest(chart=title):
                self.assertTrue(svg.startswith("<svg"))
                self.assertTrue(svg.endswith("</svg>"))
                ET.fromstring(svg)
                self.assertIn(f"<title>{title}</title>", svg)

    def test_empty_data_placeholder(self) -> None:
        svg = histogram_chart([], title="empty")
        self.assertIn("没有可用的每请求样本", svg)
        ET.fromstring(svg)

    def test_labels_escaped(self) -> None:
        svg = grouped_bar_chart(["<b>"], [{"name": "<i>", "values": [1.0]}], title="t<x>")
        self.assertNotIn("<b>", svg)
        ET.fromstring(svg)

    def test_histogram_normalised_percent(self) -> None:
        a = [{"name": "a", "values": [1.0] * 10}, {"name": "b", "values": [1.0] * 100}]
        svg = histogram_chart(a, title="t")
        ET.fromstring(svg)
        self.assertIn("样本占比", svg)

    def test_cdf_marks_percentiles(self) -> None:
        values = [{"name": "a", "values": [float(i) for i in range(100)]}]
        svg = cdf_chart(values, title="t")
        self.assertIn("P50", svg)
        self.assertIn("P99", svg)

    def test_line_chart_handles_none(self) -> None:
        svg = line_chart([1, 2], [{"name": "a", "values": [1.0, None]}], title="t")
        ET.fromstring(svg)


if __name__ == "__main__":
    unittest.main()
