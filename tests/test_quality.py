"""精度评测（lm-eval 子集）测试。

全程**不依赖真实 lm-eval、不依赖 GPU**：通过注入 fake runner 模拟子进程，
验证「命令构造 → 结果解析 → 入库 → 基线对比 → best-effort 钩子」整条链路。
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from sglbench.cli import main
from sglbench.models import Scenario
from sglbench.quality import (
    DEFAULT_SUBSETS,
    archive_eval,
    build_eval_command,
    command_string,
    compare_quality,
    latest_baseline,
    lm_eval_target,
    load_subsets,
    metrics_from_results,
    parse_results_payload,
    resolve_subset,
    run_eval,
    run_eval_after,
)
from sglbench.scenarios import get_scenario
from sglbench.store import Store

from .helpers import temp_home


class EvalOutMixin:
    """让 run_eval 的输出落在临时目录，避免污染仓库根。"""

    def setUp(self) -> None:  # noqa: D401
        self._tmp = tempfile.mkdtemp(prefix="sglbench-eval-test-")

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _out(self, name: str = "out") -> Path:
        return Path(self._tmp) / name


def fake_exec(exit_code: int = 0, *, timed_out: bool = False) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        exit_code=exit_code,
        timed_out=timed_out,
        interrupted=False,
        log_text="",
        error=None,
        started_at=None,
        finished_at=None,
        wall_s=2.5,
    )


SAMPLE_RESULTS = {
    "results": {
        "gsm8k": {
            "alias": "gsm8k",
            "acc,none": 0.6123,
            "acc_stderr,none": 0.0134,
            "exact_match,strict-match": 0.6123,
            "higher_is_better": {"acc,none": True, "exact_match,strict-match": True},
        },
        "ifeval": {
            "alias": "ifeval",
            "inst_level_strict_acc,none": 0.554,
            "prompt_level_strict_acc,none": 0.421,
        },
    },
    "configs": {"model": "mock"},
}


def write_results(out_dir: Path, payload: dict, name: str = "mock") -> Path:
    """模拟 lm-eval 在 <output_path>/<model>/results_*.json 落盘。"""
    sub = out_dir / name
    sub.mkdir(parents=True, exist_ok=True)
    p = sub / "results_2026-10-13T00-00-00.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def make_runner(payload: dict | None = None, *, exit_code: int = 0, timed_out: bool = False, raise_exc: Exception | None = None):
    """造一个 fake runner：从命令里取 --output_path，写入 results。"""

    def runner(cmd, *, log_path, env=None, timeout=None):
        if raise_exc is not None:
            raise raise_exc
        if payload is not None:
            out_idx = list(cmd).index("--output_path") + 1
            write_results(Path(cmd[out_idx]), payload)
        return fake_exec(exit_code, timed_out=timed_out)

    return runner


def make_scenario() -> Scenario:
    return get_scenario("prod-standard-c64")


# --------------------------------------------------------------------------- #
# 子集配置
# --------------------------------------------------------------------------- #


class TestSubsets(unittest.TestCase):
    def test_builtin_defaults(self) -> None:
        subsets = load_subsets(path="/nonexistent/x.json")
        self.assertIn("smoke", subsets)
        self.assertEqual(subsets["smoke"].tasks, ("gsm8k", "ifeval"))
        self.assertIn("full", subsets)

    def test_file_override_and_extend(self) -> None:
        with temp_home() as paths:
            f = paths.home / "eval-subsets.json"
            f.write_text(
                json.dumps({
                    "subsets": {
                        "smoke": {"tasks": "gsm8k", "limit": 50, "num_fewshot": 3},
                        "custom-case": {"tasks": ["mmlu"], "limit": 10},
                    }
                }),
                encoding="utf-8",
            )
            subsets = load_subsets(f)
            self.assertEqual(subsets["smoke"].tasks, ("gsm8k",))  # 覆盖内置
            self.assertEqual(subsets["smoke"].limit, 50.0)
            self.assertEqual(subsets["smoke"].num_fewshot, 3)
            self.assertEqual(subsets["custom-case"].tasks, ("mmlu",))
            self.assertIn("regression", subsets)  # 未覆盖的内置保留

    def test_resolve(self) -> None:
        subsets = load_subsets(path="/nonexistent")
        s = resolve_subset("smoke", subsets)
        self.assertEqual(s.name, "smoke")
        s = resolve_subset("smoke", subsets, tasks=["arc_challenge"], limit=7)
        self.assertEqual(s.tasks, ("arc_challenge",))
        self.assertEqual(s.limit, 7.0)
        with self.assertRaises(KeyError):
            resolve_subset("nope", subsets)
        # 无命名 + 无 tasks → 落到默认 smoke
        self.assertEqual(resolve_subset(None, subsets).name, DEFAULT_SUBSETS["smoke"].name)


# --------------------------------------------------------------------------- #
# 命令构造
# --------------------------------------------------------------------------- #


class TestCommand(unittest.TestCase):
    def test_completions_endpoint(self) -> None:
        sc = make_scenario()
        sc.backend = "sglang"
        subset = load_subsets(path="/nonexistent")["smoke"]
        cmd = build_eval_command(sc, subset, output_path="/tmp/out", python="python3")
        joined = command_string(cmd)
        self.assertIn("lm_eval", joined)
        self.assertIn("--model local-completions", joined)
        self.assertIn("/v1/completions", joined)
        self.assertIn("--tasks gsm8k,ifeval", joined)
        self.assertIn("--limit 200", joined)
        self.assertIn("--num_fewshot 0", joined)
        self.assertIn("--apply_chat_template", joined)

    def test_chat_endpoint_via_override(self) -> None:
        sc = make_scenario()
        subset = load_subsets(path="/nonexistent")["smoke"]
        kind, url = lm_eval_target(sc, "chat")
        self.assertEqual(kind, "local-chat-completions")
        self.assertTrue(url.endswith("/v1/chat/completions"))
        cmd = build_eval_command(sc, subset, output_path="/tmp/o", endpoint_kind="chat")
        self.assertIn("local-chat-completions", cmd)

    def test_no_chat_template_and_extra(self) -> None:
        sc = make_scenario()
        s = resolve_subset("smoke", load_subsets(path="/nonexistent"))
        from dataclasses import replace

        s = replace(s, apply_chat_template=False)
        cmd = build_eval_command(sc, s, output_path="/tmp/o", extra_args=["--seed", "0"])
        self.assertNotIn("--apply_chat_template", cmd)
        self.assertEqual(cmd[-2:], ["--seed", "0"])


# --------------------------------------------------------------------------- #
# 结果解析
# --------------------------------------------------------------------------- #


class TestParsing(unittest.TestCase):
    def test_parse_and_metrics(self) -> None:
        results = parse_results_payload(SAMPLE_RESULTS)
        self.assertIn("gsm8k", results)
        metrics = metrics_from_results(results)
        keys = {m.metric_key for m in metrics}
        self.assertIn("gsm8k:acc,none", keys)
        self.assertIn("ifeval:inst_level_strict_acc,none", keys)
        # stderr / alias 被跳过
        self.assertNotIn("gsm8k:acc_stderr,none", keys)
        self.assertNotIn("gsm8k:alias", keys)
        gsm = next(m for m in metrics if m.metric_key == "gsm8k:acc,none")
        self.assertAlmostEqual(gsm.value, 0.6123, places=6)
        self.assertTrue(gsm.higher_is_better)
        self.assertEqual(gsm.task, "gsm8k")

    def test_parse_tolerates_flat(self) -> None:
        flat = {"gsm8k": {"acc,none": 0.5}}
        self.assertIn("gsm8k", parse_results_payload(flat))

    def test_compare(self) -> None:
        d = compare_quality({"a": 0.5, "b": 0.7}, {"a": 0.3, "b": 0.7, "c": 9.0})
        self.assertAlmostEqual(d["a"], -0.2, places=6)
        self.assertEqual(d["b"], 0.0)
        self.assertNotIn("c", d)  # 基线没有的指标不算差


# --------------------------------------------------------------------------- #
# run_eval / archive_eval
# --------------------------------------------------------------------------- #


class TestRunEval(EvalOutMixin, unittest.TestCase):
    def test_ok_and_status(self) -> None:
        sc = make_scenario()
        subset = load_subsets(path="/nonexistent")["smoke"]
        ev = run_eval(sc, subset, eval_id="eval-t1", out_dir=self._out("eval-t1"), runner=make_runner(SAMPLE_RESULTS))
        self.assertEqual(ev.status, "ok")
        self.assertEqual(len(ev.metrics), 4)
        self.assertIn("--tasks", ev.lm_eval_cmd)
        self.assertEqual(ev.scenario_key, sc.key)

    def test_failed_on_nonzero_exit(self) -> None:
        sc = make_scenario()
        subset = load_subsets(path="/nonexistent")["smoke"]
        ev = run_eval(sc, subset, eval_id="eval-t2", out_dir=self._out("eval-t2"),
                      runner=make_runner(None, exit_code=1))
        self.assertEqual(ev.status, "failed")

    def test_partial_when_no_results(self) -> None:
        sc = make_scenario()
        subset = load_subsets(path="/nonexistent")["smoke"]
        ev = run_eval(sc, subset, eval_id="eval-t3", out_dir=self._out("eval-t3"), runner=make_runner(None))
        self.assertEqual(ev.status, "partial")
        self.assertEqual(ev.metrics, [])

    def test_timeout_is_failed(self) -> None:
        sc = make_scenario()
        subset = load_subsets(path="/nonexistent")["smoke"]
        ev = run_eval(sc, subset, eval_id="eval-t4", out_dir=self._out("eval-t4"),
                      runner=make_runner(SAMPLE_RESULTS, timed_out=True))
        self.assertEqual(ev.status, "failed")

    def test_baseline_deltas(self) -> None:
        sc = make_scenario()
        subset = load_subsets(path="/nonexistent")["smoke"]
        base = run_eval(sc, subset, eval_id="eval-base", out_dir=self._out("eval-base"), runner=make_runner(SAMPLE_RESULTS))
        lower = json.loads(json.dumps(SAMPLE_RESULTS))
        lower["results"]["gsm8k"]["acc,none"] = 0.5
        cur = run_eval(sc, subset, eval_id="eval-cur", out_dir=self._out("eval-cur"),
                       runner=make_runner(lower), baseline=base)
        self.assertAlmostEqual(cur.deltas["gsm8k:acc,none"], 0.5 - 0.6123, places=6)
        self.assertEqual(cur.baseline_id, "eval-base")

    def test_archive_writes_files_and_stores(self) -> None:
        sc = make_scenario()
        subset = load_subsets(path="/nonexistent")["smoke"]
        ev = run_eval(sc, subset, eval_id="eval-arch", out_dir=self._out("eval-arch"), runner=make_runner(SAMPLE_RESULTS))
        with temp_home() as paths, Store(paths.db) as store:
            archive_eval(paths, ev, store=store, log_text="mock log\n")
            edir = paths.eval_dir("eval-arch")
            self.assertTrue((edir / "command.txt").exists())
            self.assertTrue((edir / "results.json").exists())
            self.assertTrue((edir / "eval.json").exists())
            loaded = store.get_quality_eval("eval-arch")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.status, "ok")
            self.assertEqual(len(loaded.metrics), 4)
            rows = store.quality_metrics("eval-arch")
            self.assertEqual(len(rows), 4)
            listed = store.list_quality_evals(scenario_key=sc.key, subset="smoke")
            self.assertEqual(listed[0]["eval_id"], "eval-arch")
            # 视图可查
            v = store.query("SELECT COUNT(*) AS n FROM v_quality_metrics")
            self.assertEqual(v[0]["n"], 4)

    def test_store_roundtrip_is_idempotent(self) -> None:
        sc = make_scenario()
        subset = load_subsets(path="/nonexistent")["smoke"]
        ev = run_eval(sc, subset, eval_id="eval-idem", out_dir=self._out("eval-idem"), runner=make_runner(SAMPLE_RESULTS))
        with temp_home() as paths, Store(paths.db) as store:
            archive_eval(paths, ev, store=store)
            archive_eval(paths, ev, store=store)  # 重复归档不产生重复
            n = store.query("SELECT COUNT(*) AS n FROM quality_metrics WHERE eval_id='eval-idem'")
            self.assertEqual(n[0]["n"], 4)


class TestBaseline(EvalOutMixin, unittest.TestCase):
    def test_latest_baseline_skips_non_ok(self) -> None:
        sc = make_scenario()
        subset = load_subsets(path="/nonexistent")["smoke"]
        with temp_home() as paths, Store(paths.db) as store:
            ok = run_eval(sc, subset, eval_id="eval-ok", out_dir=self._out("eval-ok"), runner=make_runner(SAMPLE_RESULTS))
            archive_eval(paths, ok, store=store)
            bad = run_eval(sc, subset, eval_id="eval-bad", out_dir=self._out("eval-bad"), runner=make_runner(None, exit_code=1))
            archive_eval(paths, bad, store=store)
            base = latest_baseline(store, sc.key, "smoke")
            self.assertIsNotNone(base)
            self.assertEqual(base.eval_id, "eval-ok")
            # 排除自身
            self.assertIsNone(latest_baseline(store, sc.key, "smoke", exclude_id="eval-ok"))


class TestHook(EvalOutMixin, unittest.TestCase):
    def test_run_eval_after_best_effort(self) -> None:
        """钩子在 runner 抛异常时返回 None，绝不抛出。"""
        sc = make_scenario()
        with temp_home() as paths:
            ev = run_eval_after(
                paths, sc, subset_name="smoke", linked_run_id="r1",
                runner=make_runner(None, raise_exc=RuntimeError("lm-eval not installed")),
            )
            self.assertIsNone(ev)

    def test_run_eval_after_success(self) -> None:
        sc = make_scenario()
        with temp_home() as paths, Store(paths.db) as store:
            ev = run_eval_after(
                paths, sc, subset_name="smoke", linked_run_id="run-xyz", store=store,
                runner=make_runner(SAMPLE_RESULTS),
            )
            self.assertIsNotNone(ev)
            self.assertEqual(ev.run_id, "run-xyz")
            self.assertEqual(store.get_quality_eval(ev.eval_id).run_id, "run-xyz")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return int(code), out.getvalue(), err.getvalue()


class TestCliEval(unittest.TestCase):
    def test_dry_run(self) -> None:
        code, out, _ = run_cli("eval", "--scenario", "prod-standard-c64",
                               "--subset", "smoke", "--dry-run", "--no-store")
        self.assertEqual(code, 0)
        self.assertIn("lm_eval", out)
        self.assertIn("gsm8k,ifeval", out)

    def test_dry_run_json(self) -> None:
        code, out, _ = run_cli("eval", "--scenario", "prod-standard-c64",
                               "--tasks", "mmlu", "--limit-n", "25",
                               "--dry-run", "--json", "--no-store")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIn("--tasks", payload["command"])
        self.assertIn("mmlu", payload["command"])
        self.assertEqual(payload["subset"]["limit"], 25.0)

    def test_unknown_scenario(self) -> None:
        code, _, err = run_cli("eval", "--scenario", "no-such-scenario", "--dry-run", "--no-store")
        self.assertNotEqual(code, 0)

    def test_list_empty(self) -> None:
        with temp_home() as paths:
            code, out, _ = run_cli("eval", "list", "--home", str(paths.home))
            self.assertEqual(code, 0)
            self.assertIn("暂无", out)


if __name__ == "__main__":
    unittest.main()
