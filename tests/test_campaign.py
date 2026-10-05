"""campaign suite 数据层单测。"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from sglbench.campaign import build_suite, delta_pct, verdict_of
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


if __name__ == "__main__":
    unittest.main()
