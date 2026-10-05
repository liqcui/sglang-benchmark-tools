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
        "conc-128-crash-probe__crash",
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
            for key in ("single-standard", "conc-128-crash-probe", "conc-64-standard"):
                self.assertIn(key, out)

            code, out, _ = run_cli("list", "--home", str(paths.home), "--json")
            runs = json.loads(out)
            self.assertEqual(len(runs), 4)
            crash = next(r for r in runs if r["scenario_key"] == "conc-128-crash-probe")
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
                "conc-128-crash-probe:1", "single-standard:1", "--fail-on-gate",
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

            code, out, _ = run_cli("gate", "--home", str(paths.home), "--scenario", "conc-128-crash-probe")
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

    def test_sweep_with_labels(self) -> None:
        with temp_home() as paths:
            logs = prepare_logs(paths)
            run_cli("ingest", "--home", str(paths.home), str(logs), "--infer")
            # 单 label：走叠加分支的 per-label 报告（不做跨 label 对照表）
            code, out, _ = run_cli(
                "sweep", "--home", str(paths.home), "--family", "concurrency",
                "--label", "baseline",
            )
            self.assertEqual(code, 0)
            self.assertIn("label: baseline", out)
            # json 形态
            code, out, _ = run_cli(
                "sweep", "--home", str(paths.home), "--family", "concurrency",
                "--label", "baseline", "--json",
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertIn("baseline", payload)

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

    def test_campaign_run_dry_run(self) -> None:
        import json as _json

        with temp_home() as paths:
            spec = paths.home / "spec.json"
            spec.write_text(_json.dumps({
                "name": "t", "sides": {"baseline": {"label": "l2off-opt"}},
                "scenarios": ["mini-l2-64c", {"key": "hicache-multiturn-20x10", "mode": "multiturn"}],
            }), encoding="utf-8")
            code, out, err = run_cli(
                "campaign", "run", str(spec), "--side", "baseline",
                "--home", str(paths.home), "--dry-run",
            )
            self.assertEqual(code, 0, err)
            self.assertIn("mini-l2-64c", out)
            self.assertIn("hicache-multiturn-20x10", out)
            # side 不存在 → 报错
            code, _out, err = run_cli(
                "campaign", "run", str(spec), "--side", "nope",
                "--home", str(paths.home), "--dry-run",
            )
            self.assertEqual(code, 1)
            self.assertIn("未知 side", err)
            # spec 非法 → 报错
            bad = paths.home / "bad.json"
            bad.write_text("{ not json", encoding="utf-8")
            code, _out, err = run_cli(
                "campaign", "run", str(bad), "--side", "baseline",
                "--home", str(paths.home), "--dry-run",
            )
            self.assertEqual(code, 1)
            self.assertIn("读取失败", err)

    def test_recover_usage_needs_target(self) -> None:
        with temp_home() as paths:
            code, _out, err = run_cli("recover", "--home", str(paths.home))
            self.assertEqual(code, 2)
            self.assertIn("--from-pods", err)

    def test_recover_dry_run_with_node(self) -> None:
        with temp_home() as paths:
            code, out, err = run_cli(
                "recover", "--home", str(paths.home), "--dry-run", "--node", "mxgpu-3-29"
            )
            self.assertEqual(code, 0, err)
            self.assertIn("10.66.3.29", out)
            self.assertIn("计划", out)

    def test_gpu_reset_gate(self) -> None:
        from unittest import mock

        from sglbench import cli as cli_mod

        args = type("A", (), {"gpu_reset": False})()
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(cli_mod._maybe_gpu_recover(args))  # 未开启 → 不动作

        called = {}

        def fake_run_recovery(**kw):
            called.update(kw)

            class R:
                ok = True
                nodes = []
                notes = []

            return R()

        with mock.patch("sglbench.recover.run_recovery", side_effect=fake_run_recovery):
            args = type("A", (), {"gpu_reset": True})()
            with redirect_stderr(io.StringIO()):
                text = cli_mod._maybe_gpu_recover(args)
        self.assertTrue(called.get("from_pods"))
        self.assertTrue(called.get("rebuild"))
        self.assertIsNotNone(text)

    def test_run_gpu_reset_flag_parses(self) -> None:
        code, out, _ = run_cli("run", "--scenario", "conc-128-safe", "--dry-run", "--gpu-reset")
        self.assertEqual(code, 0)  # dry-run 只预览, 不触发恢复
        from sglbench.cli import build_parser

        args = build_parser().parse_args(
            ["multiturn", "--scenario", "hicache-multiturn-20x10", "--gpu-reset", "--auto-retry", "2"]
        )
        self.assertTrue(args.gpu_reset)
        args = build_parser().parse_args(["run", "--scenario", "conc-128-safe"])
        self.assertFalse(args.gpu_reset)  # 默认关闭

    def test_maybe_forensics_gate(self) -> None:
        from unittest import mock

        from sglbench import cli as cli_mod

        class R:
            run_id = "r1"
            status = "ok"

        args = type("A", (), {"no_forensics": False})()
        self.assertIsNone(cli_mod._maybe_forensics(args, object(), None, R()))  # ok → 不取证

        class Bad(R):
            status = "partial"

        class Rep:
            ok = False
            notes = ["x"]
            signatures = [{"name": "xnack"}]
            nodes = [{"node": "n"}]
            evidence_dir = "/tmp/ev"

            def summary_line(self):
                return "取证 partial: 节点 1 个, 签名[xnack]"

        saved = {}

        class FakeStore:
            def save_forensics(self, run_id, **kw):
                saved["run_id"] = run_id
                saved.update(kw)

        with mock.patch("sglbench.forensics.collect_evidence", return_value=Rep()):
            with redirect_stderr(io.StringIO()):
                text = cli_mod._maybe_forensics(args, object(), FakeStore(), Bad())
        self.assertIsNotNone(text)
        self.assertIn("xnack", text)
        self.assertEqual(saved["run_id"], "r1")
        self.assertEqual(saved["trigger"], "auto")

        args2 = type("A", (), {"no_forensics": True})()
        self.assertIsNone(cli_mod._maybe_forensics(args2, object(), FakeStore(), Bad()))

    def test_forensics_no_run_found(self) -> None:
        with temp_home() as paths:
            code, out, err = run_cli("forensics", "nope-run", "--home", str(paths.home))
            self.assertEqual(code, 1)
            self.assertIn("未找到 run", out + err)

    def test_no_forensics_flag_parses(self) -> None:
        from sglbench.cli import build_parser

        args = build_parser().parse_args(["run", "--scenario", "conc-128-safe", "--no-forensics"])
        self.assertTrue(args.no_forensics)
        args = build_parser().parse_args(["run", "--scenario", "conc-128-safe"])
        self.assertFalse(args.no_forensics)

    def test_trend_command(self) -> None:
        with temp_home() as paths:
            logs = prepare_logs(paths)
            run_cli("ingest", "--home", str(paths.home), str(logs), "--infer")
            code, out, err = run_cli(
                "trend", "--home", str(paths.home),
                "--scenario", "single-standard", "--json",
            )
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertIn("mean_ttft_ms", payload)
            self.assertEqual(len(payload["mean_ttft_ms"]), 2)  # baseline + candidate
            labels = {p["label"] for p in payload["mean_ttft_ms"]}
            self.assertEqual(labels, {"baseline", "candidate"})

            # console 形态
            code, out, _ = run_cli(
                "trend", "--home", str(paths.home), "--scenario", "single-standard"
            )
            self.assertEqual(code, 0)
            self.assertIn("mean_ttft_ms", out)

    def test_campaign_report_command(self) -> None:
        with temp_home() as paths:
            logs = prepare_logs(paths)
            run_cli("ingest", "--home", str(paths.home), str(logs), "--infer")
            out_dir = paths.home / "suite"
            code, out, err = run_cli(
                "campaign", "report", "--home", str(paths.home),
                "--pair", "baseline=candidate", "-o", str(out_dir),
            )
            self.assertEqual(code, 0, err)
            self.assertIn("index", out)
            self.assertTrue((out_dir / "index.html").exists())
            self.assertTrue((out_dir / "SUMMARY.md").exists())
            idx = (out_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn("受控条件", idx)
            # 缺 --pair/--run-pair → 用法错误（UsageError → 退出码 2）
            code, _out, err = run_cli(
                "campaign", "report", "--home", str(paths.home), "-o", str(out_dir)
            )
            self.assertEqual(code, 2)
            self.assertIn("--pair", err)


if __name__ == "__main__":
    unittest.main()
