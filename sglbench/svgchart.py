"""零依赖 SVG 图表：直方图、CDF、分组柱状、折线、分位区间。

为什么不用 matplotlib / plotly：

* **离线**：集群强制 ``HF_HUB_OFFLINE=1``，报告不能引用任何 CDN；内联 SVG 的
  单文件 HTML 可以直接 scp 回本地打开。
* **矢量**：论文/汇报里放大不糊，且能被浏览器原生 tooltip（``<title>``）交互。
* **可控体积**：ECDF 做等概率抽稀，避免每个 run 几万个点把 HTML 撑爆。

所有函数都是纯函数：``输入数据 -> SVG 字符串``，便于单测。
"""

from __future__ import annotations

import html
import math
from itertools import count
from typing import Iterable, Mapping, Sequence

from .stats import ecdf_points, percentile

# 与报告配色一致（对色盲相对友好）
PALETTE: tuple[str, ...] = (
    "#2563eb",  # blue
    "#dc2626",  # red
    "#059669",  # emerald
    "#d97706",  # amber
    "#7c3aed",  # violet
    "#0891b2",  # cyan
    "#be185d",  # pink
    "#4d7c0f",  # lime
)

FONT = "ui-sans-serif, -apple-system, 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', sans-serif"
_ID_SEQ = count(1)


def color_for(index: int) -> str:
    return PALETTE[index % len(PALETTE)]


def _uid(prefix: str) -> str:
    return f"{prefix}{next(_ID_SEQ)}"


def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def _fmt_tick(value: float) -> str:
    av = abs(value)
    if av >= 1e6:
        return f"{value / 1e6:.1f}M"
    if av >= 1e4:
        return f"{value / 1e3:.0f}k"
    if av >= 1000:
        return f"{value:,.0f}"
    if av >= 10:
        return f"{value:.0f}"
    if av == 0:
        return "0"
    if av >= 1:
        return f"{value:.0f}" if abs(value - round(value)) < 1e-9 else f"{value:.1f}"
    return f"{value:.3g}"


def _round_tick(value: float, step: float) -> float:
    """按步长决定小数位，消掉 ``5.6000000000000005`` 这类浮点累积误差。"""
    if step <= 0:
        return value
    decimals = max(0, -int(math.floor(math.log10(step))) + 1)
    return round(value, min(12, decimals))


def nice_ticks(lo: float, hi: float, target: int = 5) -> list[float]:
    """1/2/2.5/5×10^k 的整齐刻度。"""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return []
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / max(1, target)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    step = 10 * mag
    for mult in (1, 2, 2.5, 5, 10):
        if raw <= mult * mag:
            step = mult * mag
            break
    start = math.ceil(lo / step - 1e-9) * step
    ticks: list[float] = []
    index = 0
    while index < 200:
        value = start + index * step
        if value > hi + step * 1e-9:
            break
        ticks.append(0.0 if abs(value) < step * 1e-9 else _round_tick(value, step))
        index += 1
    return ticks


class Frame:
    """绘图区：像素映射 + 坐标系 + 网格。"""

    def __init__(
        self,
        width: int,
        height: int,
        xlim: tuple[float, float],
        ylim: tuple[float, float],
        *,
        pad_left: int = 70,
        pad_right: int = 20,
        pad_top: int = 60,
        pad_bottom: int = 56,
    ):
        self.width = width
        self.height = height
        self.pad_left = pad_left
        self.pad_right = pad_right
        self.pad_top = pad_top
        self.pad_bottom = pad_bottom
        self.x0 = pad_left
        self.x1 = width - pad_right
        self.y0 = height - pad_bottom
        self.y1 = pad_top
        x_lo, x_hi = xlim
        y_lo, y_hi = ylim
        if x_hi <= x_lo:
            x_hi = x_lo + 1.0
        if y_hi <= y_lo:
            y_hi = y_lo + 1.0
        self.xlim = (x_lo, x_hi)
        self.ylim = (y_lo, y_hi)
        self._sx = (self.x1 - self.x0) / (x_hi - x_lo)
        self._sy = (self.y0 - self.y1) / (y_hi - y_lo)

    def sx(self, value: float) -> float:
        return self.x0 + (value - self.xlim[0]) * self._sx

    def sy(self, value: float) -> float:
        return self.y0 - (value - self.ylim[0]) * self._sy

    @property
    def plot_width(self) -> float:
        return self.x1 - self.x0

    @property
    def plot_height(self) -> float:
        return self.y0 - self.y1

    def grid(self, y_ticks: Sequence[float], x_ticks: Sequence[float] = (), x_is_category: bool = False) -> str:
        parts = ['<g class="grid">']
        for value in y_ticks:
            y = self.sy(value)
            parts.append(
                f'<line x1="{self.x0:.1f}" y1="{y:.1f}" x2="{self.x1:.1f}" y2="{y:.1f}" '
                'stroke="#e5e7eb" stroke-width="1"/>'
            )
        if not x_is_category:
            for value in x_ticks:
                x = self.sx(value)
                parts.append(
                    f'<line x1="{x:.1f}" y1="{self.y1:.1f}" x2="{x:.1f}" y2="{self.y0:.1f}" '
                    'stroke="#f1f5f9" stroke-width="1"/>'
                )
        parts.append("</g>")
        return "".join(parts)

    def y_axis(self, ticks: Sequence[float]) -> str:
        """只画 Y 轴本体与刻度值；轴标题请用 :meth:`y_label`。"""
        parts = ['<g class="axis">']
        parts.append(
            f'<line x1="{self.x0:.1f}" y1="{self.y1:.1f}" x2="{self.x0:.1f}" y2="{self.y0:.1f}" '
            'stroke="#94a3b8" stroke-width="1.2"/>'
        )
        for value in ticks:
            y = self.sy(value)
            parts.append(
                f'<line x1="{self.x0 - 4:.1f}" y1="{y:.1f}" x2="{self.x0:.1f}" y2="{y:.1f}" '
                'stroke="#94a3b8" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{self.x0 - 8:.1f}" y="{y + 4:.1f}" text-anchor="end" '
                f'font-size="11" fill="#475569">{_esc(_fmt_tick(value))}</text>'
            )
        parts.append("</g>")
        return "".join(parts)

    def x_axis_numeric(self, ticks: Sequence[float], label: str = "") -> str:
        parts = ['<g class="axis">']
        parts.append(
            f'<line x1="{self.x0:.1f}" y1="{self.y0:.1f}" x2="{self.x1:.1f}" y2="{self.y0:.1f}" '
            'stroke="#94a3b8" stroke-width="1.2"/>'
        )
        for value in ticks:
            x = self.sx(value)
            parts.append(
                f'<line x1="{x:.1f}" y1="{self.y0:.1f}" x2="{x:.1f}" y2="{self.y0 + 4:.1f}" '
                'stroke="#94a3b8" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{x:.1f}" y="{self.y0 + 18:.1f}" text-anchor="middle" '
                f'font-size="11" fill="#475569">{_esc(_fmt_tick(value))}</text>'
            )
        if label:
            parts.append(
                f'<text x="{(self.x0 + self.x1) / 2:.1f}" y="{self.height - 8:.1f}" '
                f'text-anchor="middle" font-size="12" fill="#334155">{_esc(label)}</text>'
            )
        parts.append("</g>")
        return "".join(parts)

    def y_label(self, text: str, unit: str = "") -> str:
        label = text + (f"（{unit}）" if unit else "")
        cx = 14
        cy = (self.y0 + self.y1) / 2
        return (
            f'<text x="{cx}" y="{cy:.1f}" text-anchor="middle" font-size="12" fill="#334155" '
            f'transform="rotate(-90 {cx} {cy:.1f})">{_esc(label)}</text>'
        )


# --------------------------------------------------------------------------- #
# 图例 / 文档外壳
# --------------------------------------------------------------------------- #


def legend(entries: Sequence[tuple[str, str]], x: float, y: float, *, item_gap: float = 16) -> str:
    parts = ['<g class="legend">']
    cursor = x
    for name, color in entries:
        parts.append(f'<rect x="{cursor:.1f}" y="{y - 8:.1f}" width="10" height="10" rx="2" fill="{color}"/>')
        text = _esc(name)
        parts.append(
            f'<text x="{cursor + 15:.1f}" y="{y:.1f}" font-size="11.5" fill="#334155">{text}</text>'
        )
        cursor += 15 + max(38.0, len(str(name)) * 6.6) + item_gap
    parts.append("</g>")
    return "".join(parts)


def _doc(width: int, height: int, title: str, subtitle: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="100%" height="auto" role="img" font-family="{FONT}" '
        f'style="max-width:{width}px;display:block;margin:0 auto">'
        f"<title>{_esc(title)}</title>"
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>'
        f'<text x="14" y="20" font-size="14" font-weight="600" fill="#0f172a">{_esc(title)}</text>'
        + (f'<text x="14" y="36" font-size="11.5" fill="#64748b">{_esc(subtitle)}</text>' if subtitle else "")
        + body
        + "</svg>"
    )


def _empty_chart(title: str, message: str, width: int = 720, height: int = 240) -> str:
    return _doc(
        width,
        height,
        title,
        "",
        f'<text x="{width / 2:.0f}" y="{height / 2:.0f}" text-anchor="middle" font-size="13" '
        f'fill="#94a3b8">{_esc(message)}</text>',
    )


def _dataset(item: Mapping[str, object], index: int) -> dict[str, object]:
    return {
        "name": str(item.get("name", f"series{index + 1}")),
        "color": str(item.get("color") or color_for(index)),
        "values": [float(v) for v in (item.get("values") or []) if v is not None and float(v) == float(v)],  # type: ignore[arg-type]
    }


# --------------------------------------------------------------------------- #
# 直方图
# --------------------------------------------------------------------------- #


def histogram_chart(
    datasets: Sequence[Mapping[str, object]],
    *,
    title: str,
    xlabel: str = "",
    unit: str = "ms",
    bins: int = 40,
    width: int = 720,
    height: int = 340,
    normalize: bool = True,
    subtitle: str = "",
) -> str:
    """多 run 叠加直方图。

    ``normalize=True``（默认）按"占比 %"画：不同 num_prompts 的 run 也能公平对比
    分布形状，这是压测对比的常见坑。
    """
    series = [_dataset(d, i) for i, d in enumerate(datasets) if d.get("values")]
    all_values = [v for s in series for v in s["values"]]  # type: ignore[union-attr]
    if not all_values:
        return _empty_chart(title, "没有可用的每请求样本（该 run 未保存明细）", width, height)

    lo, hi = min(all_values), max(all_values)
    if hi <= lo:
        hi = lo + 1.0
    step = (hi - lo) / bins
    edges = [lo + i * step for i in range(bins + 1)]

    series_counts: list[list[float]] = []
    peak = 0.0
    for s in series:
        counts = [0.0] * bins
        total = len(s["values"])  # type: ignore[arg-type]
        for v in s["values"]:  # type: ignore[union-attr]
            if v < lo or v > hi:
                continue
            idx = min(bins - 1, max(0, int((v - lo) / step)))
            counts[idx] += 1.0
        if normalize and total:
            counts = [c / total * 100.0 for c in counts]
        series_counts.append(counts)
        peak = max(peak, max(counts) if counts else 0.0)

    frame = Frame(width, height, (lo, hi), (0.0, peak * 1.08 or 1.0))
    body = [frame.grid(nice_ticks(0, peak * 1.08 or 1.0), nice_ticks(lo, hi))]
    body.append(frame.x_axis_numeric(nice_ticks(lo, hi), xlabel or "延迟"))
    body.append(frame.y_label("样本占比" if normalize else "样本数", "%" if normalize else ""))
    body.append(frame.y_axis(nice_ticks(0, peak * 1.08 or 1.0)))

    for idx, s in enumerate(series):
        counts = series_counts[idx]
        color = str(s["color"])
        points = [f"{frame.sx(edges[0]):.1f},{frame.sy(0):.1f}"]
        for i, c in enumerate(counts):
            points.append(f"{frame.sx(edges[i]):.1f},{frame.sy(c):.1f}")
            points.append(f"{frame.sx(edges[i + 1]):.1f},{frame.sy(c):.1f}")
        points.append(f"{frame.sx(edges[-1]):.1f},{frame.sy(0):.1f}")
        tooltip = f"{s['name']}：n={len(s['values'])}，P50={percentile(s['values'], 50):.2f}，P99={percentile(s['values'], 99):.2f}"  # type: ignore[arg-type]
        body.append(
            f'<polygon points="{" ".join(points)}" fill="{color}" fill-opacity="0.20" '
            f'stroke="{color}" stroke-width="1.6" stroke-linejoin="round"><title>{_esc(tooltip)}</title></polygon>'
        )

    body.append(legend([(str(s["name"]), str(s["color"])) for s in series], frame.x0, 52))
    return _doc(width, height, title, subtitle, "".join(body))


# --------------------------------------------------------------------------- #
# CDF
# --------------------------------------------------------------------------- #


def cdf_chart(
    datasets: Sequence[Mapping[str, object]],
    *,
    title: str,
    xlabel: str = "延迟",
    unit: str = "ms",
    width: int = 720,
    height: int = 340,
    max_points: int = 200,
    subtitle: str = "",
    markers: Sequence[float] = (50, 90, 99),
) -> str:
    """累积分布曲线：曲线越靠左越好，两线交叉说明各有优势区间。"""
    series = [_dataset(d, i) for i, d in enumerate(datasets) if d.get("values")]
    all_values = [v for s in series for v in s["values"]]  # type: ignore[union-attr]
    if not all_values:
        return _empty_chart(title, "没有可用的每请求样本", width, height)

    lo, hi = min(all_values), max(all_values)
    if hi <= lo:
        hi = lo + 1.0
    frame = Frame(width, height, (lo, hi), (0.0, 100.0))
    body = [frame.grid(nice_ticks(0, 100), nice_ticks(lo, hi))]
    body.append(frame.x_axis_numeric(nice_ticks(lo, hi), xlabel))
    body.append(frame.y_axis([0, 20, 40, 60, 80, 100]))
    body.append(frame.y_label("累积占比", "%"))

    for idx, s in enumerate(series):
        color = str(s["color"])
        values = s["values"]  # type: ignore[assignment]
        points = ecdf_points(values, max_points=max_points)  # type: ignore[arg-type]
        if not points:
            continue
        path = " ".join(f"{frame.sx(x):.1f},{frame.sy(y * 100):.1f}" for x, y in points)
        p50, p99 = percentile(values, 50), percentile(values, 99)  # type: ignore[arg-type]
        body.append(
            f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2">'
            f"<title>{_esc(str(s['name']))}：P50={p50:.2f}，P99={p99:.2f}</title></polyline>"
        )
        for mark in markers:
            value = percentile(values, mark)  # type: ignore[arg-type]
            body.append(
                f'<circle cx="{frame.sx(value):.1f}" cy="{frame.sy(mark):.1f}" r="3.2" '
                f'fill="#ffffff" stroke="{color}" stroke-width="1.8"><title>P{mark:g} = {value:.2f}</title></circle>'
            )

    body.append(legend([(str(s["name"]), str(s["color"])) for s in series], frame.x0, 52))
    return _doc(width, height, title, subtitle, "".join(body))


# --------------------------------------------------------------------------- #
# 分组柱状
# --------------------------------------------------------------------------- #


def grouped_bar_chart(
    categories: Sequence[str],
    series: Sequence[Mapping[str, object]],
    *,
    title: str,
    ylabel: str = "",
    unit: str = "",
    width: int = 720,
    height: int = 340,
    value_labels: bool = True,
    higher_is_better: bool | None = None,
    subtitle: str = "",
) -> str:
    """对比关键指标：每个类别一组柱，一根柱 = 一个 run。"""
    cats = list(categories)
    if not cats or not series:
        return _empty_chart(title, "没有可对比的数据", width, height)

    values_by_series = [
        [float(v) if v is not None else 0.0 for v in (s.get("values") or [])] for s in series
    ]
    flat = [v for row in values_by_series for v in row]
    peak = max(flat) if flat else 1.0
    peak = peak * 1.15 if peak > 0 else 1.0

    frame = Frame(width, height, (0.0, float(len(cats))), (0.0, peak), pad_bottom=64)
    ticks = nice_ticks(0, peak)
    body = [frame.grid(ticks)]
    body.append(frame.y_axis(ticks))
    body.append(frame.y_label(ylabel, unit))

    group_width = frame.plot_width / len(cats)
    n_series = len(series)
    bar_gap = group_width * 0.18
    bar_width = (group_width - bar_gap * 2) / max(1, n_series)

    for ci, cat in enumerate(cats):
        group_x = frame.x0 + ci * group_width + bar_gap
        for si, s in enumerate(series):
            values = values_by_series[si]
            value = values[ci] if ci < len(values) else 0.0
            color = str(s.get("color") or color_for(si))
            x = group_x + si * bar_width
            h = max(0.0, frame.sy(0) - frame.sy(value))
            body.append(
                f'<rect x="{x:.1f}" y="{frame.sy(value):.1f}" width="{max(1.0, bar_width - 1.5):.1f}" '
                f'height="{h:.1f}" rx="2" fill="{color}" fill-opacity="0.88">'
                f"<title>{_esc(cat)} · {_esc(str(s.get('name')))} = {value:.4g}</title></rect>"
            )
            if value_labels and value > 0:
                body.append(
                    f'<text x="{x + (bar_width - 1.5) / 2:.1f}" y="{frame.sy(value) - 4:.1f}" '
                    f'text-anchor="middle" font-size="9.5" fill="#475569">{_esc(_fmt_tick(value))}</text>'
                )
        body.append(
            f'<text x="{frame.x0 + ci * group_width + group_width / 2:.1f}" y="{frame.y0 + 18:.1f}" '
            f'text-anchor="middle" font-size="10.5" fill="#334155">{_esc(cat)}</text>'
        )

    hint = ""
    if higher_is_better is True:
        hint = "（越高越好）"
    elif higher_is_better is False:
        hint = "（越低越好）"
    body.append(legend([(str(s.get("name")), str(s.get("color") or color_for(i))) for i, s in enumerate(series)], frame.x0, 52))
    if hint:
        body.append(
            f'<text x="{frame.x1:.0f}" y="20" text-anchor="end" font-size="11" fill="#64748b">{_esc(hint)}</text>'
        )
    return _doc(width, height, title, subtitle, "".join(body))


# --------------------------------------------------------------------------- #
# 折线（并发梯度）
# --------------------------------------------------------------------------- #


def line_chart(
    x_values: Sequence[float],
    series: Sequence[Mapping[str, object]],
    *,
    title: str,
    xlabel: str = "并发",
    ylabel: str = "",
    unit: str = "",
    width: int = 720,
    height: int = 340,
    x_log: bool = False,
    annotate_points: bool = True,
    subtitle: str = "",
) -> str:
    """并发梯度曲线（吞吐/延迟随并发变化）。"""
    xs = [float(x) for x in x_values]
    if not xs or not series:
        return _empty_chart(title, "没有可绘制的扫描数据", width, height)

    def tx(value: float) -> float:
        return math.log10(max(value, 1e-9)) if x_log else value

    txs = [tx(x) for x in xs]
    x_lo, x_hi = min(txs), max(txs)
    if x_hi <= x_lo:
        x_hi = x_lo + 1.0

    all_values = [float(v) for s in series for v in (s.get("values") or []) if v is not None]
    if not all_values:
        return _empty_chart(title, "没有可绘制的数值", width, height)
    y_lo = min(0.0, min(all_values))
    y_hi = max(all_values) * 1.15 or 1.0

    frame = Frame(width, height, (x_lo, x_hi), (y_lo, y_hi))
    y_ticks = nice_ticks(y_lo, y_hi)
    x_ticks = sorted({tx(x) for x in xs})
    body = [frame.grid(y_ticks, x_ticks)]
    body.append(frame.y_axis(y_ticks))
    body.append(frame.y_label(ylabel, unit))
    body.append(
        "".join(
            f'<text x="{frame.sx(tx(x)):.1f}" y="{frame.y0 + 18:.1f}" text-anchor="middle" '
            f'font-size="10.5" fill="#475569">{_esc(_fmt_tick(x))}</text>'
            for x in sorted(set(xs))
        )
    )
    body.append(
        f'<text x="{(frame.x0 + frame.x1) / 2:.1f}" y="{frame.height - 8:.1f}" text-anchor="middle" '
        f'font-size="12" fill="#334155">{_esc(xlabel)}</text>'
    )

    for si, s in enumerate(series):
        color = str(s.get("color") or color_for(si))
        values = [v for v in (s.get("values") or [])]
        pts: list[tuple[float, float]] = []
        for i, x in enumerate(xs):
            if i >= len(values) or values[i] is None:
                continue
            pts.append((tx(x), float(values[i])))
        if not pts:
            continue
        body.append(
            f'<polyline points="{" ".join(f"{frame.sx(px):.1f},{frame.sy(py):.1f}" for px, py in pts)}" '
            f'fill="none" stroke="{color}" stroke-width="2.2" stroke-linejoin="round"/>'
        )
        for i, (px, py) in enumerate(pts):
            raw_value = float(values[i]) if i < len(values) and values[i] is not None else 0.0
            body.append(
                f'<circle cx="{frame.sx(px):.1f}" cy="{frame.sy(py):.1f}" r="3.6" fill="{color}" '
                f'stroke="#ffffff" stroke-width="1.4"><title>x={_fmt_tick(xs[i])} · '
                f"{_esc(str(s.get('name')))}={raw_value:.2f}</title></circle>"
            )
            if annotate_points:
                body.append(
                    f'<text x="{frame.sx(px):.1f}" y="{frame.sy(py) - 8:.1f}" text-anchor="middle" '
                    f'font-size="9.5" fill="{color}">{_esc(_fmt_tick(raw_value))}</text>'
                )

    body.append(legend([(str(s.get("name")), str(s.get("color") or color_for(i))) for i, s in enumerate(series)], frame.x0, 52))
    return _doc(width, height, title, subtitle, "".join(body))


# --------------------------------------------------------------------------- #
# 分位区间（比箱线图信息更明确：p1-p99 全尾 + 中位/均值）
# --------------------------------------------------------------------------- #


def percentile_range_chart(
    datasets: Sequence[Mapping[str, object]],
    *,
    title: str,
    unit: str = "ms",
    width: int = 720,
    height: int | None = None,
    subtitle: str = "",
) -> str:
    """横向分位区间图：外条 = P1~P99，内条 = P25~P75，竖线 = 中位数，× = 均值。"""
    series = [_dataset(d, i) for i, d in enumerate(datasets) if d.get("values")]
    if not series:
        return _empty_chart(title, "没有可用的每请求样本", width, height or 240)

    all_values = [v for s in series for v in s["values"]]  # type: ignore[union-attr]
    lo, hi = min(all_values), max(all_values)
    if hi <= lo:
        hi = lo + 1.0
    span = hi - lo
    lo_axis = lo - span * 0.03
    hi_axis = hi + span * 0.03

    row_h = 46
    height = height or (70 + row_h * len(series) + 40)
    frame = Frame(width, height, (lo_axis, hi_axis), (0.0, float(len(series))), pad_left=96, pad_bottom=56)
    ticks = nice_ticks(lo_axis, hi_axis)
    body = [frame.grid(ticks, ticks)]
    body.append(frame.x_axis_numeric(ticks, "延迟"))
    body.append(frame.y_label("延迟", unit))

    for ri, s in enumerate(series):
        values = s["values"]  # type: ignore[assignment]
        color = str(s["color"])
        cy = frame.y0 - (ri + 0.5) * (frame.plot_height / len(series))
        p1, p25, p50, p75, p99 = (percentile(values, q) for q in (1, 25, 50, 75, 99))  # type: ignore[arg-type]
        mean = sum(values) / len(values)  # type: ignore[arg-type]
        label = str(s["name"])
        body.append(
            f'<text x="{frame.x0 - 10:.1f}" y="{cy + 4:.1f}" text-anchor="end" font-size="11" '
            f'fill="#334155">{_esc(label[:22])}</text>'
        )
        body.append(
            f'<line x1="{frame.sx(p1):.1f}" y1="{cy:.1f}" x2="{frame.sx(p99):.1f}" y2="{cy:.1f}" '
            f'stroke="{color}" stroke-width="3" stroke-opacity="0.35" stroke-linecap="round">'
            f"<title>{_esc(label)}：P1={p1:.2f} P99={p99:.2f}</title></line>"
        )
        body.append(
            f'<line x1="{frame.sx(p25):.1f}" y1="{cy:.1f}" x2="{frame.sx(p75):.1f}" y2="{cy:.1f}" '
            f'stroke="{color}" stroke-width="9" stroke-opacity="0.75" stroke-linecap="round">'
            f"<title>{_esc(label)}：P25={p25:.2f} P75={p75:.2f}</title></line>"
        )
        body.append(
            f'<line x1="{frame.sx(p50):.1f}" y1="{cy - 9:.1f}" x2="{frame.sx(p50):.1f}" y2="{cy + 9:.1f}" '
            f'stroke="#0f172a" stroke-width="2"><title>中位数 = {p50:.2f}</title></line>'
        )
        body.append(
            f'<path d="M {frame.sx(mean) - 4:.1f} {cy - 4:.1f} L {frame.sx(mean) + 4:.1f} {cy + 4:.1f} '
            f'M {frame.sx(mean) - 4:.1f} {cy + 4:.1f} L {frame.sx(mean) + 4:.1f} {cy - 4:.1f}" '
            f'stroke="{color}" stroke-width="1.6"><title>均值 = {mean:.2f}</title></path>'
        )
        body.append(
            f'<text x="{frame.x1:.1f}" y="{cy - 6:.1f}" text-anchor="end" font-size="9.5" fill="#64748b">'
            f"n={len(values)}</text>"  # type: ignore[arg-type]
        )

    return _doc(width, height, title, subtitle, "".join(body))


# --------------------------------------------------------------------------- #
# 单值水平条（门禁/成功率用）
# --------------------------------------------------------------------------- #


def hbar_chart(
    items: Sequence[Mapping[str, object]],
    *,
    title: str,
    unit: str = "%",
    width: int = 720,
    height: int | None = None,
    max_value: float | None = None,
    subtitle: str = "",
) -> str:
    """横向条形图，适合成功率、通过项数这类单标量对比。"""
    rows = [
        {
            "label": str(it.get("label", "")),
            "value": float(it.get("value") or 0.0),
            "color": str(it.get("color") or color_for(i)),
        }
        for i, it in enumerate(items)
    ]
    if not rows:
        return _empty_chart(title, "没有数据", width, height or 200)

    height = height or (70 + 28 * len(rows) + 30)
    peak = max_value or (max(r["value"] for r in rows) * 1.15 or 1.0)
    frame = Frame(width, height, (0.0, peak), (0.0, float(len(rows))), pad_left=110, pad_bottom=50)
    ticks = nice_ticks(0, peak)
    body = [frame.grid(ticks, ticks)]
    body.append(frame.x_axis_numeric(ticks, ""))

    for ri, row in enumerate(rows):
        cy = frame.y0 - (ri + 0.5) * (frame.plot_height / len(rows))
        bar_h = min(18.0, frame.plot_height / len(rows) * 0.55)
        body.append(
            f'<text x="{frame.x0 - 10:.1f}" y="{cy + 4:.1f}" text-anchor="end" font-size="11" '
            f'fill="#334155">{_esc(row["label"][:26])}</text>'
        )
        body.append(
            f'<rect x="{frame.x0:.1f}" y="{cy - bar_h / 2:.1f}" width="{max(0.6, frame.sx(row["value"]) - frame.x0):.1f}" '
            f'height="{bar_h:.1f}" rx="3" fill="{row["color"]}" fill-opacity="0.85">'
            f'<title>{_esc(row["label"])} = {row["value"]:.4g}{_esc(unit)}</title></rect>'
        )
        text = f"{_fmt_tick(row['value'])}{unit}"
        bar_end = frame.sx(row["value"])
        # 条形顶到右边界时把数值标在条内侧，避免被裁掉
        if bar_end + 8 + len(text) * 6.2 > frame.x1:
            body.append(
                f'<text x="{bar_end - 6:.1f}" y="{cy + 4:.1f}" text-anchor="end" font-size="10.5" '
                f'fill="#ffffff" font-weight="600">{_esc(text)}</text>'
            )
        else:
            body.append(
                f'<text x="{bar_end + 6:.1f}" y="{cy + 4:.1f}" font-size="10.5" fill="#475569">'
                f"{_esc(text)}</text>"
            )

    return _doc(width, height, title, subtitle, "".join(body))


def join_charts(*charts: str, gap: int = 14) -> str:
    """把多张 SVG 纵向拼成一张（仅用于需要单文件的场合）。"""
    return f'<div style="display:grid;gap:{gap}px">' + "".join(charts) + "</div>"


def series_from_values(name: str, values: Iterable[float], index: int = 0) -> dict[str, object]:
    return {"name": name, "values": list(values), "color": color_for(index)}
