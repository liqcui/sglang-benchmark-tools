"""dashboard 多轮断崖区单测。"""
from __future__ import annotations

import unittest

from sglbench.dashboard import render_dashboard


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


if __name__ == "__main__":
    unittest.main()
