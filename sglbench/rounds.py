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
