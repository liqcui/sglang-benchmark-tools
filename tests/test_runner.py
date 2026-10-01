"""执行/归档层测试：命令构造、覆盖项、状态判定、日志导入幂等。"""

from __future__ import annotations

import unittest

from sglbench.models import Scenario
from sglbench.parse import parse_bench_log
from sglbench.runner import (
    apply_overrides,
    build_command,
    command_string,
    discover_logs,
    infer_status,
    ingest_log,
    label_from_log_path,
    render_shell,
)
from sglbench.scenarios import BUILTIN_SCENARIOS, get_scenario
from sglbench.util import read_json

from .helpers import synthesize_samples, temp_home, write_log_bundle


def tokens(cmd: list[str]) -> dict[str, str]:
    """把 ``--k v`` 形式的命令解析成 dict（布尔开关值为 ``"1"``）。"""
    out: dict[str, str] = {}
    i = 0
    while i < len(cmd):
        if cmd[i].startswith("--"):
            key = cmd[i][2:]
            if i + 1 < len(cmd) and not cmd[i + 1].startswith("--"):
                out[key] = cmd[i + 1]
                i += 2
            else:
                out[key] = "1"
                i += 1
        else:
            i += 1
    return out


class TestBuildCommand(unittest.TestCase):
    def test_gateway_scenario(self) -> None:
        scenario = BUILTIN_SCENARIOS["conc-64-standard"]
        cmd = build_command(scenario, python="python3")
        opts = tokens(cmd)
        self.assertEqual(cmd[:3], ["python3", "-m", "sglang.bench_serving"])
        self.assertEqual(opts["backend"], "sglang-oai-chat")
        self.assertEqual(opts["base-url"], "http://10.66.1.232:8081")
        self.assertEqual(opts["max-concurrency"], "64")
        self.assertEqual(opts["num-prompts"], "200")
        self.assertEqual(opts["random-input"], "4096")
        self.assertEqual(opts["random-output"], "2048")
        self.assertEqual(opts["pd-separated"], "1")
        self.assertNotIn("host", opts)

    def test_single_stage_uses_host_port(self) -> None:
        scenario = BUILTIN_SCENARIOS["prefill-only"]
        opts = tokens(build_command(scenario, python="python3"))
        self.assertEqual(opts["host"], "10.66.1.232")
        self.assertEqual(opts["port"], "8000")
        self.assertNotIn("base-url", opts)
        self.assertNotIn("pd-separated", opts)

    def test_detail_flag_appended(self) -> None:
        cmd = build_command(BUILTIN_SCENARIOS["single-standard"], detail_path="/tmp/r.jsonl")
        self.assertIn("--output-file", cmd)
        self.assertEqual(cmd[-1], "/tmp/r.jsonl")

    def test_extra_args_preserved(self) -> None:
        scenario = Scenario(key="x", name="x", family="custom", extra_args=["--print-requests"])
        self.assertIn("--print-requests", build_command(scenario))

    def test_command_string_is_copy_pasteable(self) -> None:
        text = command_string(build_command(BUILTIN_SCENARIOS["single-standard"]))
        self.assertIn("sglang.bench_serving", text)
        self.assertIn("--pd-separated", text)

    def test_render_shell_has_exports_and_redirection(self) -> None:
        scenario = BUILTIN_SCENARIOS["single-standard"]
        shell = render_shell(scenario, build_command(scenario, python="python3"))
        self.assertIn("export HF_HUB_OFFLINE=1", shell)
        self.assertIn("export TRANSFORMERS_OFFLINE=1", shell)
        self.assertIn("nohup python3", shell)
        self.assertIn("2>&1 &", shell)

    def test_render_shell_foreground(self) -> None:
        scenario = BUILTIN_SCENARIOS["single-standard"]
        shell = render_shell(scenario, build_command(scenario), background=False)
        self.assertNotIn("nohup", shell)
        self.assertIn("2>&1", shell)


class TestOverrides(unittest.TestCase):
    def test_apply_overrides_does_not_mutate(self) -> None:
        scenario = get_scenario("conc-64-standard")
        updated = apply_overrides(scenario, {"max_concurrency": 128, "request_rate": 8.0})
        self.assertEqual(scenario.max_concurrency, 64)
        self.assertEqual(updated.max_concurrency, 128)
        self.assertEqual(updated.request_rate, 8.0)

    def test_unknown_keys_ignored(self) -> None:
        scenario = get_scenario("conc-64-standard")
        updated = apply_overrides(scenario, {"nope": 1})
        self.assertEqual(updated.max_concurrency, scenario.max_concurrency)

    def test_dash_keys_normalized(self) -> None:
        scenario = get_scenario("conc-64-standard")
        updated = apply_overrides(scenario, {"max-concurrency": 32})
        self.assertEqual(updated.max_concurrency, 32)

    def test_overrides_reach_command(self) -> None:
        scenario = apply_overrides(get_scenario("conc-64-standard"), {"num_prompts": 999})
        opts = tokens(build_command(scenario))
        self.assertEqual(opts["num-prompts"], "999")


class TestStatusInference(unittest.TestCase):
    def _parsed(self, success_rate: float | None, block_found: bool = True):
        text = "============ Serving Benchmark Result ============\nSuccessful requests: 10\n=====\n" if block_found else "nothing\n"
        return parse_bench_log(text, params={"num_prompts": 10} if success_rate == 1.0 else {"num_prompts": 20})

    def test_ok(self) -> None:
        self.assertEqual(infer_status(self._parsed(1.0), exit_code=0), "ok")

    def test_partial_on_missing_requests(self) -> None:
        self.assertEqual(infer_status(self._parsed(0.5), exit_code=0), "partial")

    def test_failed_on_nonzero_exit(self) -> None:
        self.assertEqual(infer_status(self._parsed(1.0), exit_code=1), "failed")

    def test_failed_on_timeout(self) -> None:
        self.assertEqual(infer_status(self._parsed(1.0), exit_code=0, timed_out=True), "failed")

    def test_failed_without_result_block(self) -> None:
        self.assertEqual(infer_status(self._parsed(None, block_found=False), exit_code=0), "failed")

    def test_partial_on_oom_signal(self) -> None:
        text = "============ Serving Benchmark Result ============\nSuccessful requests: 10\n=====\nCUDA out of memory\n"
        parsed = parse_bench_log(text, params={"num_prompts": 10})
        self.assertEqual(infer_status(parsed, exit_code=0), "partial")


class TestIngest(unittest.TestCase):
    def test_ingest_writes_artifacts_and_is_reusable(self) -> None:
        samples = synthesize_samples(40)
        with temp_home() as paths:
            src_dir = paths.home / "src"
            log_path, _ = write_log_bundle(
                src_dir, "single-standard__baseline", samples, num_prompts=40, max_concurrency=1
            )
            scenario = get_scenario("single-standard")
            outcome = ingest_log(paths, log_path, scenario, run_id="fixed-run")
            record = outcome.record

            self.assertEqual(record.run_id, "fixed-run")
            self.assertEqual(record.status, "ok")
            self.assertEqual(record.source, "ingested")
            self.assertEqual(len(record.samples), 40)
            self.assertAlmostEqual(record.metrics["success_rate"], 1.0)

            run_dir = paths.run_dir("fixed-run")
            for name in ("run.json", "stdout.log", "requests.jsonl", "meta.json"):
                self.assertTrue((run_dir / name).exists(), name)
            payload = read_json(run_dir / "run.json")
            self.assertEqual(payload["run_id"], "fixed-run")
            self.assertIn("content_sha256", payload["meta"])
            # 明细被重新落盘成标准字段
            self.assertTrue((run_dir / "requests.jsonl").read_text(encoding="utf-8").startswith("{"))

    def test_ingest_records_command_sidecar(self) -> None:
        samples = synthesize_samples(20)
        with temp_home() as paths:
            log_path, _ = write_log_bundle(paths.home / "src", "single-standard__x", samples, num_prompts=20, max_concurrency=1)
            log_path.with_suffix(".cmd").write_text("python3 -m sglang.bench_serving --demo", encoding="utf-8")
            outcome = ingest_log(paths, log_path, get_scenario("single-standard"))
            self.assertIn("--demo", outcome.record.command)
            self.assertIn("--demo", (paths.run_dir(outcome.record.run_id) / "command.txt").read_text(encoding="utf-8"))

    def test_ingest_missing_file_raises(self) -> None:
        with temp_home() as paths:
            with self.assertRaises(FileNotFoundError):
                ingest_log(paths, paths.home / "nope.log", get_scenario("single-standard"))

    def test_crash_log_marked_partial(self) -> None:
        samples = synthesize_samples(30, failures=9)
        with temp_home() as paths:
            log_path, _ = write_log_bundle(
                paths.home / "src",
                "conc-128-standard__baseline",
                samples,
                num_prompts=30,
                max_concurrency=128,
                error_lines=["ERROR: Prefill worker 3 crashed: torch.OutOfMemoryError: CUDA out of memory"],
            )
            outcome = ingest_log(paths, log_path, get_scenario("conc-128-standard"))
            self.assertEqual(outcome.record.status, "partial")
            self.assertAlmostEqual(outcome.record.metrics["success_rate"], 21 / 30, places=5)
            self.assertTrue(any(i.kind == "oom" for i in outcome.record.issues))


class TestHelpers(unittest.TestCase):
    def test_label_from_log_path(self) -> None:
        self.assertEqual(label_from_log_path("single-standard__candidate.log"), "candidate")
        self.assertEqual(label_from_log_path("plain.log"), "plain")

    def test_discover_logs_recursive(self) -> None:
        with temp_home() as paths:
            base = paths.home / "logs"
            (base / "sub").mkdir(parents=True)
            (base / "a.log").write_text("x", encoding="utf-8")
            (base / "sub" / "b.log").write_text("x", encoding="utf-8")
            (base / "c.txt").write_text("x", encoding="utf-8")
            found = discover_logs(base)
            self.assertEqual([p.name for p in found], ["a.log", "b.log"])


if __name__ == "__main__":
    unittest.main()
