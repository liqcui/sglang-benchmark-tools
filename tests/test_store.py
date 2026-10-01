"""DuckDB 持久化层测试。"""

from __future__ import annotations

import json
import unittest
from datetime import timedelta

from sglbench.models import Issue, RunRecord, Scenario
from sglbench.scenarios import BUILTIN_SCENARIOS
from sglbench.store import SCHEMA_VERSION, PreflightCheck, Store
from sglbench.util import utcnow

from .helpers import synthesize_samples, temp_home


def make_run(run_id: str, scenario_key: str = "single-standard", *, status: str = "ok", offset_s: int = 0) -> RunRecord:
    samples = synthesize_samples(30, seed=hash(run_id) % 10_000)
    started = utcnow() + timedelta(seconds=offset_s)
    return RunRecord(
        run_id=run_id,
        scenario_key=scenario_key,
        label=run_id,
        status=status,
        started_at=started,
        finished_at=started + timedelta(seconds=90),
        wall_s=90.0,
        command="python3 -m sglang.bench_serving ...",
        exit_code=0,
        host="test-host",
        operator="tester",
        tags=["unit"],
        params={"num_prompts": 30, "max_concurrency": 1, "model": "/workspace/data/GLM-5.2-W8A8"},
        env={"HF_HUB_OFFLINE": "1"},
        metrics={
            "successful_requests": 30.0,
            "success_rate": 1.0,
            "mean_ttft_ms": 84.0,
            "p99_ttft_ms": 113.0,
            "mean_tpot_ms": 7.7,
            "output_token_throughput_tok_s": 5153.5,
        },
        samples=samples,
        issues=[Issue(kind="oom", severity="error", message="CUDA out of memory", line_no=12)],
        artifacts={"stdout": f"/runs/{run_id}/stdout.log"},
        meta={"content_sha256": f"sha-{run_id}", "git_sha": "deadbeef"},
    )


class TestSchema(unittest.TestCase):
    def test_init_and_version(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            self.assertEqual(store.schema_version(), SCHEMA_VERSION)
            tables = {r["table_name"] for r in store.query("SELECT table_name FROM information_schema.tables")}
            for expected in ("runs", "metrics", "request_samples", "scenarios", "preflight_checks"):
                self.assertIn(expected, tables)

    def test_missing_file_read_only_raises(self) -> None:
        with temp_home() as paths:
            with self.assertRaises(FileNotFoundError):
                Store(paths.db, read_only=True)


class TestRunCrud(unittest.TestCase):
    def test_save_and_get_roundtrip(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            run = make_run("r1")
            store.save_run(run)
            loaded = store.get_run("r1")
            assert loaded is not None
            self.assertEqual(loaded.scenario_key, "single-standard")
            self.assertEqual(loaded.metrics["mean_ttft_ms"], 84.0)
            self.assertEqual(len(loaded.samples), 30)
            self.assertEqual(loaded.issues[0].kind, "oom")
            self.assertEqual(loaded.meta["git_sha"], "deadbeef")
            self.assertAlmostEqual(loaded.samples[0].ttft_ms, run.samples[0].ttft_ms)

    def test_save_is_idempotent(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            store.save_run(make_run("r1"))
            store.save_run(make_run("r1"))
            self.assertEqual(store.count_runs(), 1)
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM metrics WHERE run_id='r1'"), 6)
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM request_samples WHERE run_id='r1'"), 30)

    def test_delete(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            store.save_run(make_run("r1"))
            self.assertTrue(store.delete_run("r1"))
            self.assertIsNone(store.get_run("r1"))
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM request_samples"), 0)
            self.assertFalse(store.delete_run("r-nonexistent"))

    def test_list_runs_ordering_and_metrics(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            store.save_run(make_run("old", offset_s=-100))
            store.save_run(make_run("new", offset_s=0))
            runs = store.list_runs()
            self.assertEqual([r.run_id for r in runs], ["new", "old"])
            self.assertEqual(runs[0].metrics["mean_ttft_ms"], 84.0)
            self.assertEqual(runs[0].sample_count, 30)
            self.assertEqual(runs[0].issue_count, 1)
            self.assertEqual(store.latest_run("single-standard"), "new")

    def test_list_filters(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            store.upsert_scenarios(BUILTIN_SCENARIOS.values())
            store.save_run(make_run("a", "single-standard"))
            store.save_run(make_run("b", "conc-64-standard", status="failed"))
            self.assertEqual([r.run_id for r in store.list_runs(scenario_key="conc-64-standard")], ["b"])
            self.assertEqual([r.run_id for r in store.list_runs(status="failed")], ["b"])
            families = {r.run_id for r in store.list_runs(family="concurrency")}
            self.assertEqual(families, {"b"})

    def test_metrics_and_samples_helpers(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            store.save_run(make_run("r1"))
            self.assertEqual(store.metrics("r1")["success_rate"], 1.0)
            values = store.sample_values("r1", "ttft_ms")
            self.assertEqual(len(values), 30)
            self.assertTrue(all(v > 0 for v in values))

    def test_failed_samples_excluded_from_values(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            run = make_run("r1")
            run.samples[0].status = "error"
            store.save_run(run)
            self.assertEqual(len(store.sample_values("r1", "ttft_ms")), 29)
            self.assertEqual(len(store.sample_values("r1", "ttft_ms", ok_only=False)), 30)


class TestResolve(unittest.TestCase):
    def test_exact_and_prefix(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            store.save_run(make_run("alpha-20260101T000000Z-aaaa"))
            store.save_run(make_run("beta-20260101T000000Z-bbbb"))
            self.assertEqual(store.resolve_run("alpha-20260101T000000Z-aaaa"), "alpha-20260101T000000Z-aaaa")
            self.assertEqual(store.resolve_run("beta"), "beta-20260101T000000Z-bbbb")

    def test_ambiguous_prefix_raises(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            store.save_run(make_run("dup-20260101T000000Z-aaaa", offset_s=-10))
            store.save_run(make_run("dup-20260101T000000Z-bbbb", offset_s=0))
            with self.assertRaises(KeyError):
                store.resolve_run("dup")

    def test_missing_raises(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            with self.assertRaises(KeyError):
                store.resolve_run("nope")

    def test_latest_token(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            store.save_run(make_run("a", offset_s=-10))
            store.save_run(make_run("b", offset_s=0))
            self.assertEqual(store.resolve_run("latest"), "b")

    def test_scenario_ordinal(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            store.save_run(make_run("s-1", offset_s=-200))
            store.save_run(make_run("s-2", offset_s=-100))
            store.save_run(make_run("s-3", offset_s=0))
            self.assertEqual(store.resolve_run("single-standard:1"), "s-3")
            self.assertEqual(store.resolve_run("single-standard:3"), "s-1")

    def test_content_dedupe_lookup(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            store.save_run(make_run("r1"))
            self.assertEqual(store.find_run_by_content("single-standard", "sha-r1"), "r1")
            self.assertIsNone(store.find_run_by_content("single-standard", "sha-other"))


class TestScenariosAndPreflight(unittest.TestCase):
    def test_upsert_scenario(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            store.upsert_scenario(BUILTIN_SCENARIOS["single-standard"])
            store.save_run(make_run("r1"))
            rows = store.list_scenarios()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["run_count"], 1)
            spec = json.loads(rows[0]["spec_json"])
            self.assertEqual(spec["key"], "single-standard")

    def test_custom_scenario(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            store.upsert_scenario(Scenario(key="custom-1", name="自定义", family="custom"))
            self.assertEqual(store.list_scenarios()[0]["family"], "custom")

    def test_preflight_roundtrip(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            check = PreflightCheck(
                check_id="pf-1",
                checked_at=utcnow(),
                target="gateway",
                url="http://10.66.1.232:8081/v1/chat/completions",
                ok=True,
                model="/workspace/data/GLM-5.2-W8A8",
                http_status=200,
                ttft_ms=42.0,
                total_ms=900.0,
                output_chars=120,
                detail="OK",
            )
            store.save_preflight(check)
            rows = store.list_preflight()
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["ttft_ms"], 42.0)


class TestExport(unittest.TestCase):
    def test_export_parquet_and_csv(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            store.upsert_scenarios(BUILTIN_SCENARIOS.values())
            store.save_run(make_run("r1"))
            parquet = store.export(paths.exports_dir / "pq", fmt="parquet")
            self.assertEqual(len(parquet), 5)
            self.assertTrue(all(p.exists() and p.stat().st_size > 0 for p in parquet))
            csv = store.export(paths.exports_dir / "csv", fmt="csv")
            self.assertTrue(any(p.suffix == ".csv" for p in csv))
            data = store.export(paths.exports_dir / "json", fmt="json")
            self.assertTrue(any(p.suffix == ".json" for p in data))
            with self.assertRaises(ValueError):
                store.export(paths.exports_dir / "bad", fmt="xml")

    def test_view_query(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            store.save_run(make_run("r1"))
            rows = store.query("SELECT run_id, metric_key, value FROM v_run_metrics ORDER BY metric_key")
            self.assertEqual(len(rows), 6)
            self.assertEqual(rows[0]["run_id"], "r1")


if __name__ == "__main__":
    unittest.main()
