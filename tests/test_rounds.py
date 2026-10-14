"""逐轮指标提取与判定的单测。"""
from __future__ import annotations

import unittest

from sglbench.rounds import extract_rounds, round_delta_rows, round_series, round_verdict

BASE = {f"round{r}_mean_ttft_ms": v for r, v in {0: 606.0, 1: 5178.0, 2: 1600.0, 3: 1700.0, 4: 1700.0}.items()}
CAND = {f"round{r}_mean_ttft_ms": v for r, v in {0: 656.0, 1: 1410.0, 2: 1650.0, 3: 1720.0, 4: 1690.0}.items()}


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


class RoundDeltaTest(unittest.TestCase):
    def test_delta_rows_values_and_pct(self):
        rows = round_delta_rows(BASE, CAND)
        self.assertEqual([r for r, *_ in rows], [0, 1, 2, 3, 4])
        r1 = next(row for row in rows if row[0] == 1)
        self.assertAlmostEqual(r1[1], 5178.0)
        self.assertAlmostEqual(r1[2], 1410.0)
        self.assertAlmostEqual(r1[3], (1410.0 - 5178.0) / 5178.0 * 100.0)

    def test_delta_rows_skips_missing_side(self):
        rows = round_delta_rows({"round1_mean_ttft_ms": 100.0}, {"round2_mean_ttft_ms": 90.0})
        self.assertEqual(rows, [])

    def test_verdict_improvement(self):
        v = round_verdict(BASE, CAND)
        self.assertAlmostEqual(v["round0_pct"], (656.0 - 606.0) / 606.0 * 100.0)
        self.assertAlmostEqual(v["round1_pct"], (1410.0 - 5178.0) / 5178.0 * 100.0)
        self.assertAlmostEqual(
            v["steady_pct"], ((1720.0 + 1690.0) / 2 - (1700.0 + 1700.0) / 2) / 1700.0 * 100.0
        )
        self.assertEqual(v["verdict"], "✅ round1 改善")

    def test_verdict_regression_and_no_data(self):
        bad = {f"round{r}_mean_ttft_ms": BASE[f"round{r}_mean_ttft_ms"] * 1.2 for r in range(5)}
        self.assertEqual(round_verdict(BASE, bad)["verdict"], "❌ round1 劣化")
        self.assertEqual(round_verdict({}, {})["verdict"], "无逐轮数据")

    def test_series_aligned(self):
        rounds, series = round_series(BASE, CAND, base_name="A", cand_name="B")
        self.assertEqual(rounds, [0, 1, 2, 3, 4])
        self.assertEqual([s["name"] for s in series], ["A", "B"])
        self.assertEqual(series[0]["values"], [606.0, 5178.0, 1600.0, 1700.0, 1700.0])
        self.assertEqual(series[1]["values"], [656.0, 1410.0, 1650.0, 1720.0, 1690.0])

    def test_series_fills_missing_side(self):
        rounds, series = round_series({"round1_mean_ttft_ms": 100.0}, {"round2_mean_ttft_ms": 90.0})
        self.assertEqual(rounds, [1, 2])
        self.assertEqual(series[0]["values"], [100.0, None])
        self.assertEqual(series[1]["values"], [None, 90.0])


if __name__ == "__main__":
    unittest.main()
