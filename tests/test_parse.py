"""日志解析测试：宽容解析、派生指标、异常识别、与每请求明细的一致性。"""

from __future__ import annotations

import unittest
from pathlib import Path

from sglbench.models import Sample
from sglbench.parse import (
    add_derived_metrics,
    default_requests_path,
    detect_issues,
    distribution_stats,
    find_result_block,
    load_samples,
    metrics_from_samples,
    parse_bench_log,
    parse_bench_log_file,
    parse_namespace,
    parse_result_block,
    scan_metrics_anywhere,
    slugify_label,
)

from .helpers import EXAMPLES_DIR, render_log, synthesize_samples

MINIMAL_LOG = """Namespace(backend='sglang-oai-chat', base_url='http://10.66.1.232:8081', num_prompts=50)

100%|####| 50/50 [00:30<00:00,  1.67s/it]

============ Serving Benchmark Result ============
Backend:                                 sglang-oai-chat
Traffic request rate:                    1.00
Max request concurrency:                 1
Successful requests:                     50
Benchmark duration (s):                  83.42
Total input tokens:                      204800
Total generated tokens:                  102400
Request throughput (req/s):              0.60
Output token throughput (tok/s):         1227.53
----------------End-to-End Latency----------------
Mean E2E Latency (ms):                   1668.40
P99 E2E Latency (ms):                    1902.30
---------------Time to First Token----------------
Mean TTFT (ms):                          84.20
P99 TTFT (ms):                           118.70
-----Time per Output Token (excl. 1st token)------
Mean TPOT (ms):                          7.72
P99 TPOT (ms):                           9.02
==================================================
"""


class TestLabelSlug(unittest.TestCase):
    def test_known_labels(self) -> None:
        cases = {
            "Mean TTFT (ms)": "mean_ttft_ms",
            "Request throughput (req/s)": "request_throughput_req_s",
            "Total token throughput (tok/s)": "total_token_throughput_tok_s",
            "Benchmark duration (s)": "benchmark_duration_s",
            "Max request concurrency": "max_request_concurrency",
            "Total generated tokens (retokenized)": "total_generated_tokens_retokenized",
            "P99.9 TTFT (ms)": "p99_9_ttft_ms",
        }
        for label, expected in cases.items():
            with self.subTest(label=label):
                self.assertEqual(slugify_label(label), expected)


class TestResultBlock(unittest.TestCase):
    def test_block_located(self) -> None:
        block = find_result_block(MINIMAL_LOG)
        self.assertTrue(block.found)
        self.assertGreater(len(block.lines), 10)

    def test_values_parsed(self) -> None:
        numbers, strings = parse_result_block(find_result_block(MINIMAL_LOG))
        self.assertEqual(numbers["successful_requests"], 50.0)
        self.assertEqual(numbers["mean_ttft_ms"], 84.20)
        self.assertEqual(numbers["p99_tpot_ms"], 9.02)
        self.assertEqual(strings["backend"], "sglang-oai-chat")

    def test_separator_lines_ignored(self) -> None:
        numbers, _ = parse_result_block(find_result_block(MINIMAL_LOG))
        self.assertNotIn("end_to_end_latency", numbers)

    def test_comma_and_scientific_values(self) -> None:
        text = "============ Serving Benchmark Result ============\nTotal input tokens: 1,048,576\nP99 TTFT (ms): 1.2e2\n=====\n"
        numbers, _ = parse_result_block(find_result_block(text))
        self.assertEqual(numbers["total_input_tokens"], 1048576.0)
        self.assertAlmostEqual(numbers["p99_ttft_ms"], 120.0)

    def test_no_block_fallback_scan(self) -> None:
        text = "Mean TTFT (ms): 84.20\nsomething else: not-a-number\n"
        found = scan_metrics_anywhere(text, allowed={"mean_ttft_ms"})
        self.assertEqual(found, {"mean_ttft_ms": 84.20})

    def test_missing_block_flagged(self) -> None:
        parsed = parse_bench_log("no benchmark output here\n")
        self.assertFalse(parsed.block_found)
        self.assertTrue(any(i.kind == "parse" for i in parsed.issues))


class TestNamespace(unittest.TestCase):
    def test_quoted_and_numeric(self) -> None:
        ns = parse_namespace(
            "Namespace(backend='sglang', host='10.0.0.1', port=8000, "
            "num_prompts=300, request_rate=5.0, pd_separated=True)"
        )
        self.assertEqual(ns["backend"], "sglang")
        self.assertEqual(ns["host"], "10.0.0.1")
        self.assertEqual(ns["port"], 8000)
        self.assertEqual(ns["num_prompts"], 300)
        self.assertIs(ns["pd_separated"], True)

    def test_missing_namespace(self) -> None:
        self.assertEqual(parse_namespace("nothing here"), {})


class TestIssueDetection(unittest.TestCase):
    def test_oom_and_restart(self) -> None:
        text = (
            "[2026-01-14 08:31:22] ERROR: Prefill worker 3 crashed: torch.OutOfMemoryError: CUDA out of memory\n"
            "[2026-01-14 08:31:22] WARNING: pod mxgpu-prefill-2 restarted (restart count: 1)\n"
            "HTTP 500 Internal Server Error\n"
        )
        kinds = {i.kind for i in detect_issues(text)}
        self.assertIn("oom", kinds)
        self.assertIn("crash", kinds)
        self.assertIn("restart", kinds)

    def test_clean_log_has_no_issues(self) -> None:
        self.assertEqual(detect_issues(MINIMAL_LOG), [])

    def test_dedupe_keeps_line_numbers(self) -> None:
        text = "\n".join(["CUDA out of memory"] * 3)
        issues = detect_issues(text)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].line_no, 1)


class TestDistributionStats(unittest.TestCase):
    def test_matches_numpy_linear_interpolation(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        stats = distribution_stats(values)
        # 手工按 numpy.percentile(method='linear') 计算
        self.assertAlmostEqual(stats["median"], 3.0)
        self.assertAlmostEqual(stats["p90"], 4.6)
        self.assertAlmostEqual(stats["p99"], 4.96)
        self.assertAlmostEqual(stats["mean"], 3.0)

    def test_empty(self) -> None:
        self.assertEqual(distribution_stats([]), {})


class TestDerivedMetrics(unittest.TestCase):
    def test_success_rate_from_params(self) -> None:
        out = add_derived_metrics({"successful_requests": 212.0}, {"num_prompts": 300})
        self.assertAlmostEqual(out["success_rate"], 212 / 300, places=5)
        self.assertEqual(out["failed_requests"], 88.0)

    def test_tail_ratio(self) -> None:
        out = add_derived_metrics({"p99_ttft_ms": 120.0, "median_ttft_ms": 80.0})
        self.assertAlmostEqual(out["tail_ratio_ttft"], 1.5)

    def test_itl_cv_from_samples(self) -> None:
        samples = synthesize_samples(40, tpot_mean=10.0)
        metrics = metrics_from_samples(samples)
        self.assertIn("mean_itl_ms", metrics)
        self.assertGreater(metrics["itl_cv"], 0.0)

    def test_control_params_echoed(self) -> None:
        out = add_derived_metrics({}, {"max_concurrency": 64, "request_rate": 6.0})
        self.assertEqual(out["max_request_concurrency"], 64.0)
        self.assertEqual(out["traffic_request_rate"], 6.0)


class TestSamples(unittest.TestCase):
    def test_sample_from_alternate_field_names(self) -> None:
        sample = Sample.from_mapping(
            {"id": "r1", "input_tokens": 10, "output_tokens": 20, "ttft": 5.0, "latency": 100.0}
        )
        self.assertEqual(sample.request_id, "r1")
        self.assertEqual(sample.input_len, 10)
        self.assertEqual(sample.ttft_ms, 5.0)
        self.assertEqual(sample.e2e_ms, 100.0)
        self.assertTrue(sample.ok)

    def test_error_status_inferred(self) -> None:
        sample = Sample.from_mapping({"request_id": "x", "error": "boom"})
        self.assertFalse(sample.ok)

    def test_default_requests_path(self) -> None:
        self.assertEqual(
            default_requests_path(Path("/tmp/foo.log")).name, "foo.requests.jsonl"
        )

    def test_load_jsonl_roundtrip(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.jsonl"
            from sglbench.util import write_jsonl

            samples = synthesize_samples(5)
            write_jsonl(path, [s.to_row("run", i) for i, s in enumerate(samples)])
            loaded = load_samples(path)
        self.assertEqual(len(loaded), 5)
        self.assertAlmostEqual(loaded[0].ttft_ms, samples[0].ttft_ms)


class TestRendererConsistency(unittest.TestCase):
    """自造日志的汇总统计必须与样本重算一致（防止解析器与统计口径漂移）。"""

    def test_summary_matches_samples(self) -> None:
        samples = synthesize_samples(80, ttft_mean=90.0, tpot_mean=8.0)
        text = render_log(samples, num_prompts=80, max_concurrency=8)
        parsed = parse_bench_log(text, params={"num_prompts": 80}, samples=samples)
        recomputed = metrics_from_samples(samples)
        for key in ("mean_ttft_ms", "p99_ttft_ms", "mean_tpot_ms", "p99_tpot_ms", "median_itl_ms"):
            with self.subTest(key=key):
                self.assertAlmostEqual(parsed.metrics[key], recomputed[key], delta=0.05)


@unittest.skipUnless(EXAMPLES_DIR.exists(), "示例数据不存在（先运行 tools/make_examples.py）")
class TestExampleLogs(unittest.TestCase):
    """对仓库内示例数据的端到端解析校验。"""

    def _logs(self) -> list[Path]:
        return sorted(p for p in EXAMPLES_DIR.rglob("*.log") if p.is_file())

    def test_all_logs_parse_with_block(self) -> None:
        logs = self._logs()
        self.assertGreaterEqual(len(logs), 8)
        for log in logs:
            with self.subTest(log=log.name):
                parsed = parse_bench_log_file(log)
                self.assertTrue(parsed.block_found)
                self.assertIn("mean_ttft_ms", parsed.metrics)
                self.assertIn("mean_tpot_ms", parsed.metrics)
                self.assertGreater(len(parsed.samples), 0)

    def test_log_summary_matches_request_details(self) -> None:
        for log in self._logs():
            with self.subTest(log=log.name):
                parsed = parse_bench_log_file(log)
                recomputed = metrics_from_samples(parsed.samples)
                for key in ("mean_ttft_ms", "median_ttft_ms", "p99_ttft_ms", "mean_tpot_ms", "p99_tpot_ms"):
                    logged = parsed.metrics[key]
                    self.assertAlmostEqual(
                        recomputed[key],
                        logged,
                        delta=max(0.05, abs(logged) * 0.005),
                        msg=f"{log.name}: {key} 日志值={logged} 重算值={recomputed[key]}",
                    )

    def test_crash_run_detected(self) -> None:
        log = EXAMPLES_DIR / "conc-128-standard__baseline.log"
        parsed = parse_bench_log_file(log)
        self.assertLess(parsed.metrics["success_rate"], 0.999)
        kinds = {i.kind for i in parsed.issues}
        self.assertTrue({"oom", "crash", "restart"} & kinds, f"未识别到异常: {kinds}")

    def test_healthy_runs_have_full_success(self) -> None:
        log = EXAMPLES_DIR / "single-standard__baseline.log"
        parsed = parse_bench_log_file(log)
        self.assertAlmostEqual(parsed.metrics["success_rate"], 1.0)
        self.assertEqual(parsed.issues, [])


if __name__ == "__main__":
    unittest.main()
