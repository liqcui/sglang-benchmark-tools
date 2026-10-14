"""逐轮（多轮对话）指标提取与呈现数据。

HiCache 多轮基准把每轮的 TTFT/命中率存成 ``round<N>_<field>`` 长表指标，
本模块把这些散键还原成"逐轮序列"，供 compare / report / dashboard / suite 四层复用。

判读要点（本集群引擎限制）：fork 版 ``/v1/completions`` 不填 usage.cached_tokens，
``cache_hit_rate`` 恒为 0 —— 命中看的是 round1 起的 TTFT 断崖，不是命中率。
"""
from __future__ import annotations

import re
from typing import Any, Mapping

ROUND_RE = re.compile(r"^round(\d+)_(mean_ttft_ms|median_ttft_ms|cache_hit_rate)$")
ROUND_FIELDS = ("mean_ttft_ms", "median_ttft_ms", "cache_hit_rate")
STEADY_FROM = 3  # round>=3 视为稳态（round1 首次热、round2 个别残留冷）


def extract_rounds(metrics: Mapping[str, Any] | None) -> dict[int, dict[str, float]]:
    """解析 ``round<N>_<field>`` 指标为 ``{轮次: {字段: 值}}``，轮次升序。"""
    out: dict[int, dict[str, float]] = {}
    for key, value in (metrics or {}).items():
        m = ROUND_RE.match(str(key))
        if not m:
            continue
        try:
            fv = float(value)
        except (TypeError, ValueError):
            continue
        out.setdefault(int(m.group(1)), {})[m.group(2)] = fv
    return dict(sorted(out.items()))


def round_delta_rows(
    base: Mapping[str, Any] | None, cand: Mapping[str, Any] | None, field: str = "mean_ttft_ms"
) -> list[tuple[int, float, float, float]]:
    """逐轮 Δ 行：``[(round, base_v, cand_v, delta_pct)]``，仅保留两侧都有值的轮次。"""
    rb, rc = extract_rounds(base), extract_rounds(cand)
    rows: list[tuple[int, float, float, float]] = []
    for rnd in sorted(set(rb) & set(rc)):
        b, c = rb[rnd].get(field), rc[rnd].get(field)
        if b is None or c is None or b == 0:
            continue
        rows.append((rnd, b, c, (c - b) / b * 100.0))
    return rows


def _steady(rv: dict[int, dict[str, float]], field: str) -> float | None:
    xs = [v[field] for r, v in rv.items() if r >= STEADY_FROM and field in v]
    return sum(xs) / len(xs) if xs else None


def round_verdict(
    base: Mapping[str, Any] | None,
    cand: Mapping[str, Any] | None,
    *,
    field: str = "mean_ttft_ms",
    noise_pct: float = 10.0,
) -> dict[str, Any]:
    """cold(round0) / 首次热(round1) / 稳态(round>=3) 三点 Δ% 与总判定。"""
    rb, rc = extract_rounds(base), extract_rounds(cand)

    def pct(b: float | None, c: float | None) -> float | None:
        if b is None or c is None or not b:
            return None
        return (c - b) / b * 100.0

    r1 = pct(rb.get(1, {}).get(field), rc.get(1, {}).get(field))
    if r1 is None:
        verdict = "无逐轮数据"
    elif r1 <= -noise_pct:
        verdict = "✅ round1 改善"
    elif r1 >= noise_pct:
        verdict = "❌ round1 劣化"
    else:
        verdict = "➖ 持平"
    return {
        "round0_pct": pct(rb.get(0, {}).get(field), rc.get(0, {}).get(field)),
        "round1_pct": r1,
        "steady_pct": pct(_steady(rb, field), _steady(rc, field)),
        "verdict": verdict,
    }


def round_series(
    base: Mapping[str, Any] | None,
    cand: Mapping[str, Any] | None,
    field: str = "mean_ttft_ms",
    base_name: str = "基线",
    cand_name: str = "候选",
) -> tuple[list[int], list[dict[str, Any]]]:
    """``line_chart`` 输入：x=轮次列表，series=[基线, 候选]（缺失轮次为 None）。"""
    rb, rc = extract_rounds(base), extract_rounds(cand)
    rounds = sorted(set(rb) | set(rc))
    if not rounds:
        return [], []

    def vals(rv: dict[int, dict[str, float]]) -> list[float | None]:
        return [rv.get(r, {}).get(field) for r in rounds]

    return rounds, [
        {"name": base_name, "values": vals(rb), "color": "#5b8def"},
        {"name": cand_name, "values": vals(rc), "color": "#e8734a"},
    ]
