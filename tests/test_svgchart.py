"""heatmap / trend 图表单测。"""
from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from sglbench.svgchart import heatmap_chart, trend_chart


class HeatmapTest(unittest.TestCase):
    def test_renders_labels_and_values(self):
        svg = heatmap_chart(
            ["mini-32c", "mini-64c"], ["TTFT", "总吞吐"],
            [[43.9, -6.1], [-19.6, 2.4]],
            title="Δ 热图", better_dirs=[False, True], formats=["{:+.1f}%", "{:+.1f}%"],
        )
        ET.fromstring(svg)
        self.assertIn("mini-64c", svg)
        self.assertIn("+43.9%", svg)
        # TTFT 列 43.9% 劣化 → 最深红 #dc2626；-19.6% 改善 → 中绿 #86efac
        self.assertIn("#dc2626", svg)
        self.assertIn("#86efac", svg)

    def test_direction_oriented_color(self):
        # 吞吐列 higher-better：+25% 改善 → 绿系；-25% 劣化 → 红系
        up = heatmap_chart(["s1"], ["吞吐"], [[25.0]], title="t", better_dirs=[True])
        self.assertIn("#86efac", up)
        self.assertNotIn("#dc2626", up)
        down = heatmap_chart(["s1"], ["吞吐"], [[-25.0]], title="t", better_dirs=[True])
        self.assertIn("#fca5a5", down)

    def test_none_and_empty(self):
        svg = heatmap_chart(["s1"], ["TTFT"], [[None]], title="t")
        ET.fromstring(svg)
        self.assertIn("—", svg)
        self.assertIn("没有可绘制", heatmap_chart([], [], [], title="t"))


class TrendChartTest(unittest.TestCase):
    def test_renders_lines_and_values(self):
        svg = trend_chart(
            ["10-01", "10-02", "10-03"],
            [{"name": "l2pp1", "values": [3.3, None, 2.7], "statuses": ["ok", "failed", "ok"]}],
            title="TTFT 趋势", ylabel="TTFT", unit="ms",
        )
        ET.fromstring(svg)
        self.assertIn("<polyline", svg)
        self.assertIn("l2pp1", svg)

    def test_nonok_point_marked(self):
        svg = trend_chart(
            ["d1", "d2"],
            [{"name": "x", "values": [1.0, 2.0], "statuses": ["ok", "partial"]}],
            title="t",
        )
        self.assertIn('stroke="#dc2626"', svg)  # 非 ok 空心红圈
        self.assertIn("status=partial", svg)

    def test_empty(self):
        self.assertIn("没有可绘制", trend_chart([], [], title="t"))


if __name__ == "__main__":
    unittest.main()
