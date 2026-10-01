"""对比分析测试：方向判定、门禁、显著性、可比性检查、并发扫描。"""

from __future__ import annotations

import unittest

from sglbench.analyze import (
    DEFAULT_THRESHOLD_PCT,
    analyze_sweep,
    check_comparability,
    compare_metrics,
    compare_runs,
    evaluate_gates,
    key_metric_rows,
    relative_improvement,
    sparkline,
    sweep_point,
)
from sglbench.models import Issue, RunRecord
from sglbench.scenarios import BUILTIN_SCENARIOS, get_scenario
from sglbench.store import Store
from sglbench.util import utcnow

from .helpers import synthesize_samples, temp_home


def record(
    run_id: str,
    scenario_key: str = "single-standard",
    *,
    status: str = "ok",
    metrics: dict[str, float] | None = None,
    samples=None,
    params: dict | None = None,
    issues=None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        scenario_key=scenario_key,
        label=run_id,
        status=status,
        started_at=utcnow(),
        metrics=dict(metrics or {}),
        samples=list(samples or []),
        params=dict(params or {"num_prompts": 50, "max_concurrency": 1}),
        issues=list(issues or []),
    )


BASE_METRICS = {
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


class TestCompareMetrics(unittest.TestCase):
    def test_same_within_threshold(self) -> None:
        rows = compare_metrics(BASE_METRICS, {**BASE_METRICS, "mean_ttft_ms": 86.0})
        row = next(r for r in rows if r.key == "mean_ttft_ms")
        self.assertEqual(row.verdict, "same")
        self.assertAlmostEqual(row.delta_pct, 2.38, places=1)

    def test_lower_is_better_regression(self) -> None:
        rows = compare_metrics(BASE_METRICS, {**BASE_METRICS, "mean_ttft_ms": 92.8})
        row = next(r for r in rows if r.key == "mean_ttft_ms")
        self.assertEqual(row.verdict, "worse")
        self.assertAlmostEqual(row.delta, 8.8, places=3)

    def test_lower_is_better_improvement(self) -> None:
        rows = compare_metrics(BASE_METRICS, {**BASE_METRICS, "p99_ttft_ms": 100.0})
        row = next(r for r in rows if r.key == "p99_ttft_ms")
        self.assertEqual(row.verdict, "better")

    def test_higher_is_better(self) -> None:
        better = compare_metrics(BASE_METRICS, {**BASE_METRICS, "output_token_throughput_tok_s": 6000.0})
        row = next(r for r in better if r.key == "output_token_throughput_tok_s")
        self.assertEqual(row.verdict, "better")

        worse = compare_metrics(BASE_METRICS, {**BASE_METRICS, "output_token_throughput_tok_s": 4000.0})
        row = next(r for r in worse if r.key == "output_token_throughput_tok_s")
        self.assertEqual(row.verdict, "worse")

    def test_success_rate_drop_is_regression(self) -> None:
        rows = compare_metrics(BASE_METRICS, {**BASE_METRICS, "success_rate": 0.7067})
        row = next(r for r in rows if r.key == "success_rate")
        self.assertEqual(row.verdict, "worse")

    def test_missing_metric(self) -> None:
        rows = compare_metrics(BASE_METRICS, {"mean_ttft_ms": 84.0})
        row = next(r for r in rows if r.key == "p99_tpot_ms")
        self.assertEqual(row.verdict, "missing")
        self.assertIsNone(row.delta)

    def test_threshold_zero_marks_any_change(self) -> None:
        rows = compare_metrics(BASE_METRICS, {**BASE_METRICS, "mean_ttft_ms": 84.5}, threshold_pct=0.0)
        row = next(r for r in rows if r.key == "mean_ttft_ms")
        self.assertEqual(row.verdict, "worse")

    def test_default_threshold_constant(self) -> None:
        self.assertEqual(DEFAULT_THRESHOLD_PCT, 5.0)

    def test_display_text(self) -> None:
        rows = compare_metrics(BASE_METRICS, {**BASE_METRICS, "mean_ttft_ms": 92.8})
        row = next(r for r in rows if r.key == "mean_ttft_ms")
        self.assertEqual(row.baseline_text, "84.00 ms")
        self.assertIn("+8.80 ms", row.delta_text)
        self.assertIn("+10.48%", row.delta_text)


class TestGates(unittest.TestCase):
    def test_all_pass(self) -> None:
        gates = evaluate_gates(get_scenario("single-standard"), BASE_METRICS)
        self.assertTrue(all(g.status == "pass" for g in gates), [g.to_dict() for g in gates])

    def test_success_rate_fail(self) -> None:
        metrics = {**BASE_METRICS, "success_rate": 0.7067, "failed_requests": 88.0}
        gates = evaluate_gates(get_scenario("conc-128-standard"), metrics)
        statuses = {g.metric: g.status for g in gates}
        self.assertEqual(statuses["success_rate"], "fail")
        self.assertEqual(statuses["failed_requests"], "fail")
        self.assertFalse(all(g.status == "pass" for g in gates))

    def test_missing_metric_unknown(self) -> None:
        gates = evaluate_gates(get_scenario("single-standard"), {"success_rate": 1.0})
        statuses = {g.metric: g.status for g in gates}
        self.assertEqual(statuses["p99_ttft_ms"], "unknown")

    def test_error_issues_force_failure(self) -> None:
        gates = evaluate_gates(
            get_scenario("single-standard"),
            BASE_METRICS,
            [Issue(kind="oom", severity="error", message="CUDA out of memory")],
        )
        log_gate = next(g for g in gates if g.metric == "log_errors")
        self.assertEqual(log_gate.status, "fail")
        self.assertIn("oom", log_gate.detail)

    def test_extra_sla_overrides(self) -> None:
        gates = evaluate_gates(get_scenario("single-standard"), BASE_METRICS, extra_sla={"max_p99_ttft_ms": 50.0})
        self.assertTrue(any(g.metric == "p99_ttft_ms" and g.status == "fail" for g in gates))

    def test_no_scenario_falls_back_to_success_rate(self) -> None:
        gates = evaluate_gates(None, BASE_METRICS)
        self.assertEqual([g.metric for g in gates], ["success_rate"])


class TestComparability(unittest.TestCase):
    def test_different_scenarios_warned(self) -> None:
        runs = [record("a", "single-standard"), record("b", "conc-64-standard")]
        warnings, _notes = check_comparability(runs, "a")
        self.assertTrue(any("不属于同一场景" in w for w in warnings))

    def test_param_diff_warned(self) -> None:
        runs = [
            record("a", params={"num_prompts": 50, "max_concurrency": 1}),
            record("b", params={"num_prompts": 200, "max_concurrency": 32}),
        ]
        warnings, _ = check_comparability(runs, "a")
        self.assertTrue(any("max_concurrency" in w for w in warnings))

    def test_equal_params_no_warning(self) -> None:
        runs = [record("a"), record("b")]
        warnings, _ = check_comparability(runs, "a")
        self.assertEqual(warnings, [])

    def test_sample_count_mismatch_noted(self) -> None:
        runs = [record("a", samples=synthesize_samples(10)), record("b")]
        _warnings, notes = check_comparability(runs, "a")
        self.assertTrue(any("每请求明细" in n for n in notes))


class TestCompareRuns(unittest.TestCase):
    def _seed(self, store: Store) -> None:
        base_samples = synthesize_samples(80, ttft_mean=84.0, tpot_mean=7.7)
        cand_samples = synthesize_samples(80, ttft_mean=93.0, tpot_mean=7.8, seed=99)
        store.save_run(
            record("base", metrics=BASE_METRICS, samples=base_samples, params={"num_prompts": 80, "max_concurrency": 1})
        )
        store.save_run(
            record(
                "cand",
                metrics={
                    **BASE_METRICS,
                    "mean_ttft_ms": 92.8,
                    "p99_ttft_ms": 124.0,
                    "output_token_throughput_tok_s": 4834.0,
                },
                samples=cand_samples,
                params={"num_prompts": 80, "max_concurrency": 1},
            )
        )

    def test_requires_two_runs(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            with self.assertRaises(ValueError):
                compare_runs(store, ["one"])

    def test_baseline_and_candidates(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            self._seed(store)
            report = compare_runs(store, ["base", "cand"], scenario=get_scenario("single-standard"))
            self.assertEqual(report.baseline_run, "base")
            self.assertEqual(report.candidate_runs, ["cand"])
            self.assertEqual(report.runs[0].role, "baseline")
            self.assertEqual(report.runs[1].role, "candidate")
            self.assertGreater(len(report.conclusions), 0)

    def test_metrics_flagged_as_regression(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            self._seed(store)
            report = compare_runs(store, ["base", "cand"], scenario=get_scenario("single-standard"))
            ttft = report.metric("mean_ttft_ms")
            assert ttft is not None
            self.assertEqual(ttft.verdict, "worse")

    def test_distributions_computed_and_significant(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            self._seed(store)
            report = compare_runs(
                store, ["base", "cand"], scenario=get_scenario("single-standard"), resamples=300
            )
            ttft = next(d for d in report.distributions if d.metric == "ttft_ms")
            self.assertTrue(ttft.significant)
            self.assertGreater(ttft.delta_median, 5.0)
            self.assertGreater(ttft.cliffs_delta, 0.25)  # 候选整体更慢
            self.assertGreater(ttft.cohens_d, 0.5)
            self.assertLess(ttft.mann_whitney["p_value"], 0.05)

    def test_gates_evaluated_per_run(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            self._seed(store)
            report = compare_runs(store, ["base", "cand"], scenario=get_scenario("single-standard"))
            self.assertEqual(set(report.gates), {"base", "cand"})
            self.assertTrue(report.all_gates_pass)

    def test_crash_run_fails_gates(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            store.save_run(record("base", metrics=BASE_METRICS))
            store.save_run(
                record(
                    "crash",
                    scenario_key="conc-128-standard",
                    status="partial",
                    metrics={**BASE_METRICS, "success_rate": 0.7067, "failed_requests": 88.0},
                    params={"num_prompts": 300, "max_concurrency": 128},
                )
            )
            report = compare_runs(store, ["base", "crash"], with_distributions=False)
            self.assertFalse(report.all_gates_pass)
            self.assertGreater(report.gate_summary()["fail"], 0)
            self.assertTrue(any("门禁未通过" in c for c in report.conclusions))
            self.assertTrue(report.warnings)

    def test_to_dict_is_json_serialisable(self) -> None:
        import json

        with temp_home() as paths, Store(paths.db) as store:
            self._seed(store)
            report = compare_runs(store, ["base", "cand"], resamples=200)
            payload = json.loads(json.dumps(report.to_dict(), default=str))
            self.assertEqual(payload["baseline_run"], "base")
            self.assertIn("gate_summary", payload)


class TestSweep(unittest.TestCase):
    def _seed_ladder(self, store: Store) -> None:
        ladder = [
            ("conc-32-standard", 32.0, 4.0, 30454.0, 620.0, 980.0, 11.2, 1.0),
            ("conc-64-standard", 64.0, 6.0, 15500.0, 1180.0, 1940.0, 14.8, 1.0),
            ("conc-128-standard", 128.0, 8.0, 10100.0, 2450.0, 3980.0, 21.5, 0.7067),
            ("conc-256-standard", 256.0, 16.0, 6000.0, 5200.0, 8600.0, 30.0, 1.0),
        ]
        for key, conc, rate, total_tp, mttft, p99ttft, mtpot, sr in ladder:
            store.save_run(
                record(
                    f"{key}-run",
                    key,
                    status="ok" if sr >= 0.999 else "partial",
                    metrics={
                        "max_request_concurrency": conc,
                        "traffic_request_rate": rate,
                        "total_token_throughput_tok_s": total_tp,
                        "output_token_throughput_tok_s": total_tp / 3,
                        "mean_ttft_ms": mttft,
                        "p99_ttft_ms": p99ttft,
                        "mean_tpot_ms": mtpot,
                        "p99_tpot_ms": mtpot * 1.6,
                        "success_rate": sr,
                    },
                )
            )

    def test_sweep_orders_by_concurrency(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            self._seed_ladder(store)
            sweep = analyze_sweep(store, scenario_keys=[k for k, *_ in [
                ("conc-32-standard",), ("conc-64-standard",), ("conc-128-standard",), ("conc-256-standard",)
            ]])
            self.assertEqual([p.max_concurrency for p in sweep.points], [32.0, 64.0, 128.0, 256.0])

    def test_best_throughput_and_healthy_ceiling(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            self._seed_ladder(store)
            sweep = analyze_sweep(store, family="concurrency")
            assert sweep.best_throughput is not None
            self.assertEqual(sweep.best_throughput.max_concurrency, 32.0)
            assert sweep.highest_healthy_concurrency is not None
            self.assertEqual(sweep.highest_healthy_concurrency.max_concurrency, 256.0)
            self.assertTrue(any("吞吐峰值" in n for n in sweep.notes))
            self.assertTrue(any("未通过成功率检查" in n for n in sweep.notes))

    def test_sweep_to_dict(self) -> None:
        import json

        with temp_home() as paths, Store(paths.db) as store:
            self._seed_ladder(store)
            sweep = analyze_sweep(store, family="concurrency")
            payload = json.loads(json.dumps(sweep.to_dict(), default=str))
            self.assertEqual(len(payload["points"]), 4)

    def test_sweep_point_from_record(self) -> None:
        run = record("x", metrics={"max_request_concurrency": 64.0, "success_rate": 1.0})
        point = sweep_point(run)
        self.assertEqual(point.max_concurrency, 64.0)
        self.assertTrue(point.healthy)


class TestSmallHelpers(unittest.TestCase):
    def test_relative_improvement(self) -> None:
        self.assertAlmostEqual(relative_improvement(100.0, 90.0, higher_is_better=False), 10.0)
        self.assertAlmostEqual(relative_improvement(100.0, 90.0, higher_is_better=True), -10.0)
        self.assertIsNone(relative_improvement(0.0, 90.0, False))

    def test_key_metric_rows(self) -> None:
        runs = [record("a", metrics=BASE_METRICS)]
        header, rows = key_metric_rows(runs, keys=["mean_ttft_ms", "success_rate"])
        self.assertEqual(header, ["run", "首 token 延迟 均值", "请求成功率"])
        self.assertEqual(rows[0][1], "84.00 ms")
        self.assertEqual(rows[0][2], "100.00%")

    def test_sparkline(self) -> None:
        self.assertEqual(sparkline([]), "")
        art = sparkline([1, 2, 3, 4])
        self.assertEqual(len(art), 4)
        self.assertEqual(art[0], "▁")
        self.assertEqual(art[-1], "█")

    def test_scenario_catalog_usable(self) -> None:
        self.assertIn("conc-128-standard", BUILTIN_SCENARIOS)


if __name__ == "__main__":
    unittest.main()
