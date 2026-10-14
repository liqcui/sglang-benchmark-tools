"""生成一张包含全部图表类型的自包含 HTML，用于人工目视校验图表渲染。

用法::

    python tools/chart_gallery.py -o reports/chart-gallery.html

不依赖数据库，使用固定随机种子的合成数据 —— 这样任何人跑出来的图都一致，
适合作为"图表回归"的目视基线。
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from sglbench.svgchart import (
    cdf_chart,
    grouped_bar_chart,
    hbar_chart,
    histogram_chart,
    line_chart,
    percentile_range_chart,
)

SEED = 20260101
CARD = '<div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:8px">{}</div>'


def synthesize() -> tuple[list[float], list[float], list[float]]:
    rng = random.Random(SEED)

    def lognormal(mu: float, sigma: float, n: int) -> list[float]:
        return [min(4.0 * mu, rng.lognormvariate(0.0, sigma) * mu) for _ in range(n)]

    # 三个同量级的序列，便于直观看出分位区间图的差别
    return lognormal(84.0, 0.16, 500), lognormal(93.0, 0.18, 500), lognormal(106.0, 0.23, 500)


def build() -> str:
    base, cand, third = synthesize()
    charts = [
        grouped_bar_chart(
            ["均值", "P99"],
            [
                {"name": "baseline", "values": [84.0, 112.9]},
                {"name": "candidate", "values": [93.0, 131.5]},
            ],
            title="分组柱状图 · TTFT",
            ylabel="延迟",
            unit="ms",
            higher_is_better=False,
        ),
        grouped_bar_chart(
            ["输出 token 吞吐", "总 token 吞吐"],
            [
                {"name": "baseline", "values": [5153.5, 15460.5]},
                {"name": "candidate", "values": [4834.8, 14504.3]},
            ],
            title="分组柱状图 · 吞吐",
            ylabel="吞吐",
            unit="tok/s",
            higher_is_better=True,
        ),
        hbar_chart(
            [
                {"label": "baseline", "value": 100.0},
                {"label": "candidate", "value": 99.2},
                {"label": "conc-128 (crash)", "value": 70.67},
            ],
            title="横向条形图 · 请求成功率",
            unit="%",
            max_value=100.0,
        ),
        histogram_chart(
            [{"name": "baseline", "values": base}, {"name": "candidate", "values": cand}],
            title="直方图 · TTFT 分布",
            xlabel="TTFT (ms)",
        ),
        cdf_chart(
            [{"name": "baseline", "values": base}, {"name": "candidate", "values": cand}],
            title="累积分布 · TTFT CDF",
            xlabel="TTFT (ms)",
        ),
        percentile_range_chart(
            [
                {"name": "baseline", "values": base},
                {"name": "candidate", "values": cand},
                {"name": "candidate-b", "values": third},
            ],
            title="分位区间 · P1~P99 / P25~P75 / 中位数 / 均值",
        ),
        line_chart(
            [1, 32, 64, 128, 256],
            [
                {"name": "总 token 吞吐 (tok/s)", "values": [15460, 30454, 15500, 10100, 6000]},
            ],
            title="折线图 · 吞吐随并发变化（对数 X 轴）",
            xlabel="max-concurrency",
            ylabel="总 token 吞吐",
            unit="tok/s",
            x_log=True,
        ),
        line_chart(
            [1, 32, 64, 128, 256],
            [
                {"name": "Mean TTFT (ms)", "values": [84, 620, 1180, 2450, 5200]},
                {"name": "P99 TTFT (ms)", "values": [113, 980, 1940, 3980, 8600]},
            ],
            title="折线图 · 延迟随并发变化（对数 X 轴）",
            xlabel="max-concurrency",
            ylabel="TTFT",
            unit="ms",
            x_log=True,
        ),
    ]
    body = "".join(CARD.format(c) for c in charts)
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>sglbench 图表总览</title></head>"
        "<body style='margin:0;padding:20px;background:#f1f5f9;"
        "font-family:ui-sans-serif,-apple-system,Segoe UI,Microsoft YaHei,sans-serif'>"
        "<h1 style='font-size:20px;margin:0 0 4px'>sglbench 图表总览</h1>"
        f"<p style='color:#64748b;font-size:13px;margin:0 0 16px'>全部图表由内联 SVG 生成，"
        f"无外部依赖；合成数据 seed={SEED}</p>"
        f"<div style='display:grid;gap:14px;max-width:1180px'>{body}</div>"
        "</body></html>"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 sglbench 图表总览 HTML")
    parser.add_argument("-o", "--output", default="reports/chart-gallery.html")
    args = parser.parse_args()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(), encoding="utf-8", newline="\n")
    print(f"已生成: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
