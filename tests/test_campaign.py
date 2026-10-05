"""campaign suite 数据层单测。"""
from __future__ import annotations

import json as _json
import tempfile as _tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path as _Path

from sglbench.campaign import (
    CampaignSpec,
    ScenarioEntry,
    SideSpec,
    build_suite,
    delta_pct,
    load_spec,
    parse_scenario_entry,
    should_skip,
    verdict_of,
)
from sglbench.models import RunRecord
from sglbench.store import Store

from .helpers import temp_home


def mk_run(
    store: Store,
    run_id: str,
    *,
    scenario: str,
    label: str,
    metrics: dict,
    env: dict | None = None,
    status: str = "ok",
    day: int = 1,
) -> RunRecord:
    rec = RunRecord(
        run_id=run_id, scenario_key=scenario, label=label, status=status,
        started_at=datetime(2026, 10, day, tzinfo=timezone.utc), metrics=dict(metrics),
    )
    if env:
        rec.meta["deployment_env"] = env
    store.save_run(rec)
    return rec


BASE_ENV = {"deployment": "dsv4-rc3", "prefill_pp": 1, "hicache_enabled": False}
ON_ENV = {"deployment": "dsv4-l2", "prefill_pp": 1, "hicache_enabled": True}


def seed_pair(store: Store) -> None:
    """A/B 两个 label 各两个场景（一个普通 + 一个 multiturn）。"""
    mk_run(store, "off-32", scenario="mini-l2-32c", label="A", day=1, env=BASE_ENV, metrics={
        "mean_ttft_ms": 7660.0, "mean_tpot_ms": 26.1, "success_rate": 1.0,
        "input_token_throughput_tok_s": 1961.7, "output_token_throughput_tok_s": 980.9,
        "total_token_throughput_tok_s": 2942.6, "peak_output_token_throughput_tok_s": 1542.0,
    })
    mk_run(store, "on-32", scenario="mini-l2-32c", label="B", day=2, env=ON_ENV, metrics={
        "mean_ttft_ms": 11020.0, "mean_tpot_ms": 26.4, "success_rate": 1.0,
        "input_token_throughput_tok_s": 1841.2, "output_token_throughput_tok_s": 920.6,
        "total_token_throughput_tok_s": 2761.8, "peak_output_token_throughput_tok_s": 1449.0,
    })
    mk_run(store, "off-mt", scenario="hicache-multiturn-20x10", label="A", day=1, env=BASE_ENV, metrics={
        "success_rate": 1.0, "round0_mean_ttft_ms": 606.0,
        "round1_mean_ttft_ms": 5178.0, "round3_mean_ttft_ms": 1700.0,
    })
    mk_run(store, "on-mt", scenario="hicache-multiturn-20x10", label="B", day=2, env=ON_ENV, metrics={
        "success_rate": 1.0, "round0_mean_ttft_ms": 656.0,
        "round1_mean_ttft_ms": 1410.0, "round3_mean_ttft_ms": 1720.0,
    })


class CampaignDataTest(unittest.TestCase):
    def test_delta_and_verdict(self) -> None:
        self.assertAlmostEqual(delta_pct(100.0, 90.0), -10.0)
        self.assertIsNone(delta_pct(None, 90.0))
        self.assertIsNone(delta_pct(0.0, 90.0))
        self.assertEqual(verdict_of(-6.0, higher_better=False), "✅")
        self.assertEqual(verdict_of(6.0, higher_better=False), "❌")
        self.assertEqual(verdict_of(2.0, higher_better=False), "➖")
        self.assertEqual(verdict_of(None, higher_better=False), "—")

    def test_build_suite(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            seed_pair(store)
            suite = build_suite(store, label_pairs=[("A", "B")])

            self.assertEqual(
                [s.key for s in suite.summaries],
                ["hicache-multiturn-20x10", "mini-l2-32c"],
            )
            s32 = next(s for s in suite.summaries if s.key == "mini-l2-32c")
            self.assertEqual(s32.verdict, "❌")  # TTFT +43.9% 劣化
            self.assertAlmostEqual(
                delta_pct(s32.off["mean_ttft_ms"], s32.on["mean_ttft_ms"]), 43.9, places=1
            )
            self.assertEqual(s32.status_off, "ok")
            self.assertEqual(s32.status_on, "ok")

            smt = next(s for s in suite.summaries if s.key.startswith("hicache-multiturn"))
            self.assertIsNotNone(smt.round_verdict)
            self.assertEqual(smt.round_verdict["verdict"], "✅ round1 改善")

            # 受控条件：deployment / HiCache 两行应标记差异
            diffs = {name for name, *_rest, diff in suite.env_rows if diff}
            self.assertIn("部署", diffs)
            self.assertIn("HiCache", diffs)
            self.assertTrue(any("劣化" in c for c in suite.conclusions))
            self.assertTrue(any("round1" in c for c in suite.conclusions))

    def test_duplicate_keys_and_multiturn_marker(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            seed_pair(store)
            # 同 scenario 的第二 rate 变体（经 run-pair 明确指定）
            mk_run(store, "off-32b", scenario="mini-l2-32c", label="A-bd", day=3, env=BASE_ENV,
                   metrics={"mean_ttft_ms": 8000.0, "total_token_throughput_tok_s": 3000.0,
                            "success_rate": 1.0})
            mk_run(store, "on-32b", scenario="mini-l2-32c", label="B-bd", day=4, env=ON_ENV,
                   metrics={"mean_ttft_ms": 9000.0, "total_token_throughput_tok_s": 2900.0,
                            "success_rate": 1.0})
            suite = build_suite(store, label_pairs=[("A", "B")], run_pairs=[["off-32b", "on-32b"]])

            mini_titles = [s.title for s in suite.summaries if s.key == "mini-l2-32c"]
            self.assertEqual(len(mini_titles), 2)
            self.assertTrue(all(t != "mini-l2-32c" and "[" in t for t in mini_titles))

            smt = next(s for s in suite.summaries if s.key.startswith("hicache-multiturn"))
            self.assertTrue(smt.is_multiturn)

            from sglbench.campaign import render_suite_html, render_suite_markdown

            html = render_suite_html(suite, report_links={s.title: "x.html" for s in suite.summaries})
            self.assertIn("多轮·见④", html)
            md = render_suite_markdown(suite)
            self.assertIn("多轮·见 §4", md)

    def test_render_suite_html(self) -> None:
        from sglbench.campaign import render_suite_html

        with temp_home() as paths, Store(paths.db) as store:
            seed_pair(store)
            suite = build_suite(store, label_pairs=[("A", "B")])
        links = {s.key: f"{i + 1:02d}-{s.key}.html" for i, s in enumerate(suite.summaries)}
        html = render_suite_html(suite, report_links=links)
        for needle in (
            "受控条件", "压测场景汇总", "吞吐量完整对比", "多轮对话", "稳定性", "结论",
            "Δ 热图", "<svg", "01-", "round1", "⚠差异",
        ):
            self.assertIn(needle, html)

    def test_write_suite_outputs(self) -> None:
        import tempfile
        from pathlib import Path

        from sglbench.campaign import write_suite

        with temp_home() as paths, Store(paths.db) as store:
            seed_pair(store)
            out = Path(tempfile.mkdtemp()) / "suite"
            result = write_suite(store, out, label_pairs=[("A", "B")])

        self.assertTrue(result["index"].exists())
        self.assertIsNotNone(result["summary_md"])
        self.assertTrue(result["summary_md"].exists())
        self.assertEqual(len(result["reports"]), 2)

        idx = result["index"].read_text(encoding="utf-8")
        self.assertIn("01-hicache-multiturn-20x10.html", idx)
        self.assertIn("02-mini-l2-32c.html", idx)
        self.assertTrue((out / "02-mini-l2-32c.html").exists())

        md = result["summary_md"].read_text(encoding="utf-8")
        self.assertIn("受控条件", md)
        self.assertIn("多轮对话", md)
        self.assertIn("|", md)


class CampaignSpecTest(unittest.TestCase):
    def test_parse_entry(self):
        self.assertEqual(parse_scenario_entry("mini-l2-64c").key, "mini-l2-64c")
        e = parse_scenario_entry({"key": "hicache-multiturn-80x10-r16", "mode": "multiturn",
                                  "max_parallel": 4, "set": {"request_rate": 16.0}})
        self.assertEqual(e.mode, "multiturn")
        self.assertEqual(e.max_parallel, 4)
        self.assertEqual(e.set["request_rate"], 16.0)
        with self.assertRaises(ValueError):
            parse_scenario_entry({"mode": "bench"})  # 缺 key

    def test_load_spec_validation(self):
        with _tempfile.TemporaryDirectory() as td:
            p = _Path(td) / "c.json"
            p.write_text(_json.dumps({
                "name": "x",
                "sides": {"baseline": {"label": "A", "env_file": "envs/a.json"}},
                "scenarios": ["mini-l2-64c"],
                "gpu_reset": True,
            }), encoding="utf-8")
            spec = load_spec(p)
            self.assertEqual(spec.sides["baseline"].label, "A")
            self.assertEqual(spec.sides["baseline"].env_file, "envs/a.json")
            self.assertEqual(spec.scenarios[0].key, "mini-l2-64c")
            self.assertTrue(spec.gpu_reset)
            p.write_text(_json.dumps({"name": "x", "sides": {}, "scenarios": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_spec(p)
            p.write_text(_json.dumps({"name": "x", "sides": {"a": {}}, "scenarios": ["k"]}),
                         encoding="utf-8")
            with self.assertRaises(ValueError):
                load_spec(p)  # side 缺 label
            p.write_text("{ not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_spec(p)

    def test_should_skip(self):
        with temp_home() as paths, Store(paths.db) as store:
            mk_run(store, "r1", scenario="mini-l2-64c", label="A", day=1,
                   env={"deployment": "dep-1"}, metrics={"mean_ttft_ms": 1.0})
            ent = parse_scenario_entry("mini-l2-64c")
            # 无 env_file → 有 ok run 就跳过
            self.assertTrue(should_skip(store, ent, SideSpec("A", None)))
            # env 指纹一致 → 跳过；不一致 → 重跑
            with _tempfile.TemporaryDirectory() as td:
                same = _Path(td) / "same.json"
                same.write_text(_json.dumps({"deployment": "dep-1"}), encoding="utf-8")
                self.assertTrue(should_skip(store, ent, SideSpec("A", str(same))))
                diff = _Path(td) / "diff.json"
                diff.write_text(_json.dumps({"deployment": "dep-2"}), encoding="utf-8")
                self.assertFalse(should_skip(store, ent, SideSpec("A", str(diff))))
            # 不同 label / 未知场景 → 不跳过
            self.assertFalse(should_skip(store, ent, SideSpec("B", None)))
            self.assertFalse(should_skip(store, parse_scenario_entry("mini-l2-999x"), SideSpec("A", None)))


if __name__ == "__main__":
    unittest.main()
