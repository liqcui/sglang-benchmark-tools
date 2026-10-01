"""统计工具测试：分位数、直方图、ECDF、bootstrap、KS、Mann-Whitney。"""

from __future__ import annotations

import unittest

from sglbench.stats import (
    bootstrap_ci_diff,
    cliffs_delta,
    cohens_d,
    compare_distributions,
    describe,
    ecdf_points,
    histogram,
    ks_2samp,
    mann_whitney_u,
    normal_cdf,
    percentile,
)


class TestPercentile(unittest.TestCase):
    def test_linear_interpolation(self) -> None:
        values = [1, 2, 3, 4, 5]
        self.assertAlmostEqual(percentile(values, 0), 1.0)
        self.assertAlmostEqual(percentile(values, 50), 3.0)
        self.assertAlmostEqual(percentile(values, 90), 4.6)
        self.assertAlmostEqual(percentile(values, 100), 5.0)

    def test_single_value(self) -> None:
        self.assertEqual(percentile([7.0], 99), 7.0)

    def test_unsorted_input_ok(self) -> None:
        self.assertAlmostEqual(percentile([5, 1, 3], 50), 3.0)

    def test_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            percentile([], 50)


class TestDescribe(unittest.TestCase):
    def test_basic(self) -> None:
        stats = describe([1, 2, 3, 4, 5])
        self.assertEqual(stats["count"], 5.0)
        self.assertAlmostEqual(stats["mean"], 3.0)
        self.assertAlmostEqual(stats["median"], 3.0)
        self.assertAlmostEqual(stats["iqr"], 2.0)
        self.assertGreater(stats["std"], 0)

    def test_nan_and_none_filtered(self) -> None:
        stats = describe([1.0, float("nan"), None, 3.0])  # type: ignore[list-item]
        self.assertEqual(stats["count"], 2.0)

    def test_empty(self) -> None:
        self.assertEqual(describe([]), {})


class TestHistogram(unittest.TestCase):
    def test_counts_sum_to_total(self) -> None:
        values = list(range(100))
        edges, counts = histogram(values, bins=10)
        self.assertEqual(sum(counts), 100)
        self.assertEqual(len(edges), len(counts) + 1)

    def test_last_edge_inclusive(self) -> None:
        edges, counts = histogram([0.0, 1.0, 2.0], bins=2)
        self.assertEqual(sum(counts), 3)

    def test_explicit_edges(self) -> None:
        _edges, counts = histogram([0.5, 1.5, 2.5], bins=[0, 1, 2, 3])
        self.assertEqual(counts, [1, 1, 1])

    def test_empty(self) -> None:
        self.assertEqual(histogram([]), ([], []))


class TestEcdf(unittest.TestCase):
    def test_monotone_and_bounded(self) -> None:
        points = ecdf_points(list(range(1000)), max_points=100)
        self.assertLessEqual(len(points), 100)
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        self.assertEqual(xs, sorted(xs))
        self.assertEqual(ys, sorted(ys))
        self.assertAlmostEqual(ys[-1], 1.0)

    def test_empty(self) -> None:
        self.assertEqual(ecdf_points([]), [])


class TestBootstrap(unittest.TestCase):
    def test_deterministic(self) -> None:
        a = list(range(100))
        b = [x + 5 for x in range(100)]
        first = bootstrap_ci_diff(a, b, n_resamples=300, seed=42)
        second = bootstrap_ci_diff(a, b, n_resamples=300, seed=42)
        self.assertEqual(first, second)

    def test_detects_shift(self) -> None:
        a = [float(x) for x in range(200)]
        b = [x + 50.0 for x in range(200)]
        result = bootstrap_ci_diff(a, b, "median", n_resamples=400)
        self.assertAlmostEqual(result["point"], 50.0, places=1)
        self.assertGreater(result["ci_low"], 0)  # 置信区间不含 0
        self.assertLess(result["p_value"], 0.05)

    def test_no_shift_interval_contains_zero(self) -> None:
        a = [float(x) for x in range(200)]
        b = [float(x) for x in range(200)]
        result = bootstrap_ci_diff(a, b, "median", n_resamples=300)
        self.assertLessEqual(result["ci_low"], 0)
        self.assertGreaterEqual(result["ci_high"], 0)

    def test_empty_input(self) -> None:
        self.assertEqual(bootstrap_ci_diff([], [1, 2]), {})


class TestMannWhitney(unittest.TestCase):
    def test_clearly_separated(self) -> None:
        a = [1.0, 2.0, 3.0, 4.0, 5.0]
        b = [11.0, 12.0, 13.0, 14.0, 15.0]
        result = mann_whitney_u(a, b)
        self.assertLess(result["p_value"], 0.05)
        self.assertEqual(result["u"], 0.0)

    def test_identical_samples(self) -> None:
        a = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = mann_whitney_u(a, list(a))
        self.assertGreater(result["p_value"], 0.9)

    def test_all_ties_no_crash(self) -> None:
        result = mann_whitney_u([1.0] * 10, [1.0] * 10)
        self.assertEqual(result["p_value"], 1.0)

    def test_empty(self) -> None:
        self.assertEqual(mann_whitney_u([], [1.0]), {})


class TestKS(unittest.TestCase):
    def test_identical(self) -> None:
        a = [float(x) for x in range(50)]
        result = ks_2samp(a, list(a))
        self.assertAlmostEqual(result["d"], 0.0, places=6)
        self.assertGreater(result["p_value"], 0.9)

    def test_disjoint(self) -> None:
        a = [float(x) for x in range(50)]
        b = [x + 100.0 for x in range(50)]
        result = ks_2samp(a, b)
        self.assertAlmostEqual(result["d"], 1.0, places=6)
        self.assertLess(result["p_value"], 0.01)

    def test_d_bounded(self) -> None:
        result = ks_2samp([1.0, 2.0, 3.0], [2.0, 3.0, 4.0])
        self.assertGreaterEqual(result["d"], 0.0)
        self.assertLessEqual(result["d"], 1.0)

    def test_empty(self) -> None:
        self.assertEqual(ks_2samp([], [1.0]), {})


class TestEffectSize(unittest.TestCase):
    def test_cohens_d_sign(self) -> None:
        a = [1.0, 2.0, 3.0, 4.0, 5.0]
        b = [3.0, 4.0, 5.0, 6.0, 7.0]
        self.assertGreater(cohens_d(a, b), 0)
        self.assertLess(cohens_d(b, a), 0)

    def test_cliffs_delta_range(self) -> None:
        a = [1.0, 2.0, 3.0, 4.0]
        b = [5.0, 6.0, 7.0, 8.0]
        # δ = P(candidate > baseline) - P(candidate < baseline)：候选更大 → +1
        self.assertAlmostEqual(cliffs_delta(a, b), 1.0, places=6)
        self.assertAlmostEqual(cliffs_delta(b, a), -1.0, places=6)
        self.assertAlmostEqual(cliffs_delta(a, list(a)), 0.0, places=6)

    def test_normal_cdf(self) -> None:
        self.assertAlmostEqual(normal_cdf(0.0), 0.5)
        self.assertGreater(normal_cdf(1.96), 0.97)


class TestCompareDistributions(unittest.TestCase):
    def test_regression_is_significant(self) -> None:
        baseline = [80.0 + i * 0.1 for i in range(200)]
        candidate = [x + 12.0 for x in baseline]
        comp = compare_distributions(baseline, candidate, metric="ttft_ms")
        assert comp is not None
        self.assertAlmostEqual(comp.delta_median, 12.0, places=1)
        self.assertTrue(comp.significant)
        self.assertGreater(comp.cliffs_delta, 0)  # 候选更慢 → δ>0
        self.assertGreater(comp.cohens_d, 0)

    def test_identical_is_not_significant(self) -> None:
        values = [80.0 + (i % 7) for i in range(200)]
        comp = compare_distributions(values, list(values), metric="ttft_ms")
        assert comp is not None
        self.assertFalse(comp.significant)

    def test_too_few_samples(self) -> None:
        self.assertIsNone(compare_distributions([], [1.0], metric="ttft_ms"))


if __name__ == "__main__":
    unittest.main()
