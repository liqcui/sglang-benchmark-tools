"""CLI 端到端测试：真实走一遍 ingest -> list -> compare -> report -> gate -> export。"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from sglbench.cli import main

from .helpers import synthesize_samples, temp_home, write_log_bundle


def run_cli(*argv: str) -> tuple[int, str, str]:
    """执行 CLI 并捕获输出（避免测试刷屏）。"""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:  # argparse 的用法错误
            code = exc.code if isinstance(exc.code, int) else 1
    return int(code), out.getvalue(), err.getvalue()


def prepare_logs(paths) -> Path:
    """造一批示例日志：同场景两次 run + 一个崩溃 run。"""
    logs = paths.home / "logs"
    write_log_bundle(logs, "single-standard__baseline", synthesize_samples(40, ttft_mean=84.0), num_prompts=40, max_concurrency=1)
    write_log_bundle(logs, "single-standard__candidate", synthesize_samples(40, ttft_mean=95.0, seed=3), num_prompts=40, max_concurrency=1)
    write_log_bundle(
        logs,
        "conc-128-standard__crash",
        synthesize_samples(40, failures=12, seed=4),
        num_prompts=40,
        max_concurrency=128,
        error_lines=["ERROR: Prefill worker 3 crashed: torch.OutOfMemoryError: CUDA out of memory"],
    )
    write_log_bundle(logs, "conc-64-standard__run", synthesize_samples(40, ttft_mean=1180.0, tpot_mean=14.8, seed=5), num_prompts=40, max_concurrency=64)
    return logs


class TestCli(unittest.TestCase):
    def test_scenarios_list_and_show(self) -> None:
        code, out, _ = run_cli("scenarios")
        self.assertEqual(code, 0)
        self.assertIn("single-standard", out)
        self.assertIn("conc-256-standard", out)

        code, out, _ = run_cli("scenarios", "show", "conc-64-standard")
        self.assertEqual(code, 0)
        payload = json.loads(out[: out.index("\n执行命令:")])
        self.assertEqual(payload["max_concurrency"], 64)
        self.assertIn("--pd-separated", out)

    def test_run_dry_run(self) -> None:
        with temp_home() as paths:
            code, out, _ = run_cli("run", "--home", str(paths.home), "--scenario", "conc-128-safe", "--dry-run")
            self.assertEqual(code, 0)
            self.assertIn("nohup python3", out)
            self.assertIn("--max-concurrency 128", out)
            self.assertIn("--request-rate 5.0", out)
            self.assertIn("export HF_HUB_OFFLINE=1", out)

    def test_run_dry_run_json_and_set(self) -> None:
        with temp_home() as paths:
            code, out, _ = run_cli(
                "run", "--home", str(paths.home), "--scenario", "conc-64-standard",
                "--set", "max_concurrency=96", "--set", "request_rate=7.5", "--dry-run", "--json",
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertIn("--max-concurrency", payload["command"])
            self.assertEqual(payload["command"][payload["command"].index("--max-concurrency") + 1], "96")
            self.assertEqual(payload["scenario"]["request_rate"], 7.5)

    def test_run_rejects_unknown_field(self) -> None:
        with temp_home() as paths:
            code, _out, err = run_cli(
                "run", "--home", str(paths.home), "--scenario", "single-standard", "--set", "nope=1", "--dry-run"
            )
            self.assertEqual(code, 2)
            self.assertIn("未知的场景字段", err)

    def test_ingest_list_show(self) -> None:
        with temp_home() as paths:
            logs = prepare_logs(paths)
            code, out, err = run_cli("ingest", "--home", str(paths.home), str(logs), "--infer")
            # 崩溃 run 的数据是 partial，但导入本身成功 → 退出码仍为 0
            self.assertEqual(code, 0, err)
            self.assertIn("导入完成", err)
            self.assertIn("未达 ok 状态", err)

            code, out, _ = run_cli("list", "--home", str(paths.home))
            self.assertEqual(code, 0)
            for key in ("single-standard", "conc-128-standard", "conc-64-standard"):
                self.assertIn(key, out)

            code, out, _ = run_cli("list", "--home", str(paths.home), "--json")
            runs = json.loads(out)
            self.assertEqual(len(runs), 4)
            crash = next(r for r in runs if r["scenario_key"] == "conc-128-standard")
            self.assertEqual(crash["status"], "partial")

            code, out, _ = run_cli("show", "--home", str(paths.home), crash["run_id"])
            self.assertEqual(code, 0)
            self.assertIn("SLA 门禁", out)
            self.assertIn("❌", out)

    def test_ingest_is_idempotent(self) -> None:
        with temp_home() as paths:
            logs = prepare_logs(paths)
            run_cli("ingest", "--home", str(paths.home), str(logs), "--infer")
            code, _out, err = run_cli("ingest", "--home", str(paths.home), str(logs), "--infer")
            self.assertEqual(code, 0)
            self.assertIn("跳过", err)
            code, out, _ = run_cli("list", "--home", str(paths.home), "--json")
            self.assertEqual(len(json.loads(out)), 4)

    def test_ingest_force_creates_duplicate(self) -> None:
        with temp_home() as paths:
            logs = prepare_logs(paths)
            run_cli("ingest", "--home", str(paths.home), str(logs), "--infer")
            run_cli("ingest", "--home", str(paths.home), str(logs), "--infer", "--force")
            code, out, _ = run_cli("list", "--home", str(paths.home), "--json")
            self.assertEqual(len(json.loads(out)), 8)

    def test_compare_table_and_gate_failure(self) -> None:
        with temp_home() as paths:
            logs = prepare_logs(paths)
            run_cli("ingest", "--home", str(paths.home), str(logs), "--infer")

            code, out, _ = run_cli("compare", "--home", str(paths.home), "--scenario", "single-standard", "--last", "2")
            self.assertEqual(code, 0)
            self.assertIn("关键指标对比", out)
            self.assertIn("分布级检验", out)
            self.assertIn("回归", out)  # candidate 的 TTFT 从 84 涨到 95

            # 崩溃场景的门禁应该失败
            code, out, _ = run_cli(
                "compare", "--home", str(paths.home),
                "conc-128-standard:1", "single-standard:1", "--fail-on-gate",
            )
            self.assertEqual(code, 1)

    def test_compare_markdown_and_json(self) -> None:
        with temp_home() as paths:
            logs = prepare_logs(paths)
            run_cli("ingest", "--home", str(paths.home), str(logs), "--infer")
            code, out, _ = run_cli(
                "compare", "--home", str(paths.home), "--scenario", "single-standard", "--last", "2",
                "--format", "markdown",
            )
            self.assertEqual(code, 0)
            self.assertIn("## 压测对比", out)

            code, out, _ = run_cli(
                "compare", "--home", str(paths.home), "--scenario", "single-standard", "--last", "2",
                "--format", "json",
            )
            payload = json.loads(out)
            self.assertEqual(len(payload["runs"]), 2)
            self.assertIn("gate_summary", payload)

    def test_gate_command(self) -> None:
        with temp_home() as paths:
            logs = prepare_logs(paths)
            run_cli("ingest", "--home", str(paths.home), str(logs), "--infer")

            code, out, _ = run_cli("gate", "--home", str(paths.home), "--scenario", "single-standard")
            self.assertEqual(code, 0)
            self.assertIn("PASS", out)

            code, out, _ = run_cli("gate", "--home", str(paths.home), "--scenario", "conc-128-standard")
            self.assertEqual(code, 1)
            self.assertIn("FAIL", out)

            code, out, _ = run_cli(
                "gate", "--home", str(paths.home), "--scenario", "single-standard",
                "--set-sla", "max_mean_ttft_ms=10",
            )
            self.assertEqual(code, 1)

    def test_report_command(self) -> None:
        with temp_home() as paths:
            logs = prepare_logs(paths)
            run_cli("ingest", "--home", str(paths.home), str(logs), "--infer")
            target = paths.home / "out" / "report.html"
            code, out, err = run_cli(
                "report", "--home", str(paths.home), "--scenario", "single-standard", "--last", "2",
                "-o", str(target), "--sweep", "--markdown", str(target.with_suffix(".md")),
                "--json", str(target.with_suffix(".json")),
            )
            self.assertEqual(code, 0, err)
            self.assertTrue(target.exists())
            html = target.read_text(encoding="utf-8")
            self.assertIn("<!doctype html>", html)
            self.assertIn("<svg", html)
            self.assertTrue(target.with_suffix(".md").exists())
            self.assertTrue(target.with_suffix(".json").exists())

    def test_sweep_command(self) -> None:
        with temp_home() as paths:
            logs = prepare_logs(paths)
            run_cli("ingest", "--home", str(paths.home), str(logs), "--infer")
            code, out, _ = run_cli("sweep", "--home", str(paths.home), "--family", "concurrency")
            self.assertEqual(code, 0)
            self.assertIn("并发梯度扫描", out)

    def test_export_and_sql_and_db(self) -> None:
        with temp_home() as paths:
            logs = prepare_logs(paths)
            run_cli("ingest", "--home", str(paths.home), str(logs), "--infer")

            code, out, _ = run_cli("sql", "--home", str(paths.home), "SELECT COUNT(*) AS n FROM runs")
            self.assertEqual(code, 0)
            self.assertIn("4", out)

            target = paths.home / "exp"
            code, out, _ = run_cli("export", "--home", str(paths.home), "-o", str(target), "--format", "csv")
            self.assertEqual(code, 0)
            self.assertTrue((target / "metrics.csv").exists())

            code, out, _ = run_cli("db", "--home", str(paths.home), "info")
            self.assertEqual(code, 0)
            self.assertIn("request_samples", out)

            code, out, _ = run_cli("db", "--home", str(paths.home), "path")
            self.assertIn("bench.duckdb", out)

    def test_missing_run_reports_error(self) -> None:
        with temp_home() as paths:
            code, _out, err = run_cli("show", "--home", str(paths.home), "nope")
            self.assertEqual(code, 1)
            self.assertIn("❌", err)

    def test_preflight_without_target_is_usage_error(self) -> None:
        with temp_home() as paths:
            code, _out, err = run_cli("preflight", "--home", str(paths.home))
            self.assertEqual(code, 2)
            self.assertIn("--scenario 或 --url", err)

    def test_preflight_unreachable_endpoint_fails_cleanly(self) -> None:
        with temp_home() as paths:
            code, out, err = run_cli(
                "preflight", "--home", str(paths.home),
                "--url", "http://127.0.0.1:9/v1/chat/completions",
                "--timeout-s", "1",
            )
            self.assertEqual(code, 1)
            combined = out + err
            self.assertTrue("FAIL" in combined or "连接失败" in combined)


if __name__ == "__main__":
    unittest.main()
