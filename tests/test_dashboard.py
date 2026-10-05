"""dashboard 多轮断崖区单测。"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from sglbench.dashboard import render_dashboard, resolve_pairs
from sglbench.models import RunRecord
from sglbench.store import Store

from .helpers import temp_home


def _pair(name="hicache-multiturn-20x10"):
    return {
        "name": name,
        "off_run": "b0", "on_run": "c0",
        "metrics_off": {f"round{r}_mean_ttft_ms": v for r, v in {0: 606.0, 1: 5178.0, 3: 1700.0}.items()},
        "metrics_on": {f"round{r}_mean_ttft_ms": v for r, v in {0: 656.0, 1: 1410.0, 3: 1720.0}.items()},
    }


class RoundSectionTest(unittest.TestCase):
    def test_round_section_rendered(self):
        html = render_dashboard([_pair()])
        self.assertIn("多轮断崖", html)
        self.assertIn("逐轮 TTFT", html)

    def test_no_round_section_for_plain_pair(self):
        p = _pair("mini-l2-64c")
        p["metrics_off"] = {"mean_ttft_ms": 3380.0}
        p["metrics_on"] = {"mean_ttft_ms": 2720.0}
        html = render_dashboard([p])
        self.assertNotIn("多轮断崖", html)


class WildcardPairTest(unittest.TestCase):
    def _seed(self, store: Store) -> None:
        pairs = [
            ("off-a", "mini-l2-32c", 1.0),
            ("off-b-rt32", "conc-32-deepseek", 2.0),   # off 侧带批后缀
            ("on-a", "mini-l2-32c", 0.9),
            ("on-rt32", "conc-32-deepseek", 1.9),      # on 侧后缀不对称
            ("off-only", "conc-64-deepseek", 3.0),     # on 侧缺 → 不成对
        ]
        for i, (label, scenario, ttft) in enumerate(pairs):
            store.save_run(
                RunRecord(
                    run_id=f"r{i}", scenario_key=scenario, label=label, status="ok",
                    started_at=datetime(2026, 10, 5, 0, i, tzinfo=timezone.utc),
                    metrics={"mean_ttft_ms": ttft, "success_rate": 1.0},
                )
            )

    def test_prefix_pairs(self):
        with temp_home() as paths, Store(paths.db) as store:
            self._seed(store)
            # 精确 label 只配到 mini-32c
            exact = resolve_pairs(store, label_pairs=[("off-a", "on-a")])
            self.assertEqual([p["name"] for p in exact], ["mini-l2-32c"])
            # 通配：off* ↔ on* 每场景取最新 ok run 配对（不对后缀做镜像要求）
            pairs = resolve_pairs(store, label_pairs=[("off*", "on*")])
            self.assertEqual(
                sorted(p["name"] for p in pairs), ["conc-32-deepseek", "mini-l2-32c"]
            )
            by_name = {p["name"]: p for p in pairs}
            self.assertEqual(by_name["conc-32-deepseek"]["off_run"], "r1")
            self.assertEqual(by_name["conc-32-deepseek"]["on_run"], "r3")
            self.assertEqual(by_name["mini-l2-32c"]["off_run"], "r0")


if __name__ == "__main__":
    unittest.main()
