"""逐轮指标提取与判定的单测。"""
from __future__ import annotations

import unittest

from sglbench.rounds import extract_rounds


class ExtractRoundsTest(unittest.TestCase):
    def test_parses_round_metrics(self):
        metrics = {
            "round0_mean_ttft_ms": 606.0,
            "round1_mean_ttft_ms": 5178.0,
            "round1_median_ttft_ms": 5100.0,
            "round3_cache_hit_rate": 0.0,
            "success_rate": 1.0,          # 非逐轮键忽略
            "mean_ttft_ms": 123.0,        # 非逐轮键忽略
            "roundX_mean_ttft_ms": 1.0,   # 非法轮次忽略
            "round2_mean_ttft_ms": "bad",  # 非数值忽略
        }
        out = extract_rounds(metrics)
        self.assertEqual(sorted(out), [0, 1, 3])
        self.assertEqual(out[0], {"mean_ttft_ms": 606.0})
        self.assertEqual(out[1]["mean_ttft_ms"], 5178.0)
        self.assertEqual(out[1]["median_ttft_ms"], 5100.0)
        self.assertEqual(out[3], {"cache_hit_rate": 0.0})

    def test_empty_and_none(self):
        self.assertEqual(extract_rounds({}), {})
        self.assertEqual(extract_rounds(None), {})


if __name__ == "__main__":
    unittest.main()
