"""统计工具：分位数、直方图、ECDF、bootstrap、KS、Mann-Whitney。

全部纯标准库实现，行为与 numpy/scipy 对应函数保持一致（分位数用 linear 插值），
避免给离线集群引入 scipy 依赖。

**为什么不只比均值**：推理延迟是典型长尾分布，均值会被少数慢请求拖动；对比两个
run 时必须同时看分位数（尾部 SLA）、分布形状（KS）与随机性是否足以解释差异
（bootstrap 置信区间 / Mann-Whitney）。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

__all__ = [
    "percentile",
    "describe",
    "histogram",
    "ecdf_points",
    "bootstrap_ci_diff",
    "mann_whitney_u",
    "ks_2samp",
    "cohens_d",
    "cliffs_delta",
    "normal_cdf",
]


def _clean(values: Iterable[float]) -> list[float]:
    out: list[float] = []
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f and not math.isinf(f):  # 排除 NaN / inf
            out.append(f)
    return out


def percentile(values: Sequence[float], q: float) -> float:
    """线性插值分位数（等价 numpy.percentile(..., method='linear')）。``values`` 不必有序。"""
    vals = sorted(_clean(values))
    n = len(vals)
    if n == 0:
        raise ValueError("空序列没有分位数")
    if n == 1:
        return vals[0]
    q = min(100.0, max(0.0, float(q)))
    pos = (q / 100.0) * (n - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return vals[lo]
    frac = pos - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


QUANTILES: tuple[float, ...] = (0.0, 1.0, 5.0, 25.0, 50.0, 75.0, 90.0, 95.0, 99.0, 99.9, 100.0)


def describe(values: Sequence[float]) -> dict[str, float]:
    """常用汇总统计。空输入返回 ``{}``。"""
    vals = sorted(_clean(values))
    n = len(vals)
    if n == 0:
        return {}
    mean = sum(vals) / n
    if n > 1:
        var = sum((v - mean) ** 2 for v in vals) / (n - 1)
        std = math.sqrt(var)
    else:
        std = 0.0
    out: dict[str, float] = {
        "count": float(n),
        "mean": mean,
        "std": std,
        "min": vals[0],
        "max": vals[-1],
        "cv": (std / mean) if mean else 0.0,
    }
    for q in QUANTILES:
        out[f"p{q:g}".replace(".", "_")] = percentile(vals, q)
    # 常用别名
    out["median"] = out["p50"]
    out["iqr"] = out["p75"] - out["p25"]
    # 分布形状（Pearson 偏度）
    if n > 2 and std > 0:
        out["skew"] = sum(((v - mean) / std) ** 3 for v in vals) * n / ((n - 1) * (n - 2))
    else:
        out["skew"] = 0.0
    return out


def _nice_step(span: float, target_bins: int) -> float:
    """1/2/5×10^k 的"整齐"步长，让直方图刻度可读。"""
    if span <= 0:
        return 1.0
    raw = span / max(1, target_bins)
    mag = 10 ** math.floor(math.log10(raw))
    for mult in (1, 2, 2.5, 5, 10):
        if raw <= mult * mag:
            return mult * mag
    return 10 * mag


def histogram(
    values: Sequence[float],
    bins: int | Sequence[float] = 40,
    value_range: tuple[float, float] | None = None,
    nice: bool = True,
) -> tuple[list[float], list[int]]:
    """直方图，返回 ``(edges, counts)``；``len(counts) == len(edges) - 1``。"""
    vals = _clean(values)
    if not vals:
        return [], []
    lo, hi = value_range if value_range else (min(vals), max(vals))
    if hi <= lo:
        hi = lo + 1.0

    if isinstance(bins, int):
        if nice:
            step = _nice_step(hi - lo, bins)
            lo = math.floor(lo / step) * step
            hi = math.ceil(hi / step) * step
            if hi <= lo:
                hi = lo + step
            n = max(1, int(round((hi - lo) / step)))
            edges = [lo + i * step for i in range(n + 1)]
        else:
            width = (hi - lo) / bins
            edges = [lo + i * width for i in range(bins + 1)]
    else:
        edges = sorted(float(b) for b in bins)
        if len(edges) < 2:
            return [], []

    counts = [0] * (len(edges) - 1)
    for v in vals:
        if v < edges[0] or v > edges[-1]:
            continue
        if v == edges[-1]:
            counts[-1] += 1
            continue
        idx = 0
        # 二分查找
        lo_i, hi_i = 0, len(edges) - 1
        while lo_i < hi_i - 1:
            mid = (lo_i + hi_i) // 2
            if edges[mid] <= v:
                lo_i = mid
            else:
                hi_i = mid
        idx = lo_i
        counts[idx] += 1
    return edges, counts


def ecdf_points(values: Sequence[float], max_points: int = 240) -> list[tuple[float, float]]:
    """经验累积分布的点列，超过 ``max_points`` 时按等概率抽稀，控制 SVG 体积。"""
    vals = sorted(_clean(values))
    n = len(vals)
    if n == 0:
        return []
    if n <= max_points:
        return [(vals[i], (i + 1) / n) for i in range(n)]
    points: list[tuple[float, float]] = []
    for i in range(max_points):
        idx = min(n - 1, int(round(i * (n - 1) / (max_points - 1))))
        points.append((vals[idx], (idx + 1) / n))
    return points


# --------------------------------------------------------------------------- #
# 假设检验 / 重采样
# --------------------------------------------------------------------------- #

_STATISTICS: dict[str, Callable[[list[float]], float]] = {
    "median": lambda xs: percentile(xs, 50),
    "mean": lambda xs: sum(xs) / len(xs),
    "p95": lambda xs: percentile(xs, 95),
    "p99": lambda xs: percentile(xs, 99),
}


def bootstrap_ci_diff(
    baseline: Sequence[float],
    candidate: Sequence[float],
    statistic: str = "median",
    n_resamples: int = 2000,
    alpha: float = 0.05,
    seed: int = 20260101,
) -> dict[str, float]:
    """候选 - 基线 的差异点估计与 bootstrap 百分位置信区间。

    ``p_value`` 是"重采样分布落在 0 的另一侧"的比例（两尾），仅作方向性参考；
    严格检验请结合 :func:`mann_whitney_u`。
    """
    a = _clean(baseline)
    b = _clean(candidate)
    if not a or not b:
        return {}
    fn = _STATISTICS.get(statistic, _STATISTICS["median"])
    point = fn(b) - fn(a)

    rng = random.Random(seed)
    na, nb = len(a), len(b)
    diffs: list[float] = []
    for _ in range(max(200, int(n_resamples))):
        sa = [a[rng.randrange(na)] for _ in range(na)]
        sb = [b[rng.randrange(nb)] for _ in range(nb)]
        diffs.append(fn(sb) - fn(sa))
    diffs.sort()
    lo = percentile(diffs, 100 * (alpha / 2))
    hi = percentile(diffs, 100 * (1 - alpha / 2))

    if point > 0:
        p = 2 * sum(1 for d in diffs if d <= 0) / len(diffs)
    elif point < 0:
        p = 2 * sum(1 for d in diffs if d >= 0) / len(diffs)
    else:
        p = 1.0
    return {
        "point": point,
        "ci_low": lo,
        "ci_high": hi,
        "alpha": alpha,
        "n_resamples": float(n_resamples),
        "p_value": min(1.0, p),
        "statistic": statistic,  # type: ignore[dict-item]
    }


def _rank_with_ties(values: Sequence[float]) -> tuple[list[float], dict[float, int]]:
    """返回平均秩与并列组大小表。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    tie_sizes: dict[float, int] = {}
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j + 2) / 2.0  # 秩从 1 开始
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        tie_sizes[values[order[i]]] = j - i + 1
        i = j + 1
    return ranks, tie_sizes


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def mann_whitney_u(baseline: Sequence[float], candidate: Sequence[float]) -> dict[str, float]:
    """Mann-Whitney U 检验（正态近似 + 并列校正）。非参数，不假设分布形状。"""
    a = _clean(baseline)
    b = _clean(candidate)
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return {}
    combined = list(a) + list(b)
    ranks, tie_sizes = _rank_with_ties(combined)
    r1 = sum(ranks[:n1])
    u1 = r1 - n1 * (n1 + 1) / 2.0
    u2 = n1 * n2 - u1
    u = min(u1, u2)

    mu = n1 * n2 / 2.0
    n = n1 + n2
    tie_term = sum(t**3 - t for t in tie_sizes.values() if t > 1)
    sigma_sq = (n1 * n2 / 12.0) * ((n + 1) - tie_term / (n * (n - 1))) if n > 1 else 0.0
    if sigma_sq <= 0:
        return {"u": u, "p_value": 1.0, "z": 0.0, "n1": float(n1), "n2": float(n2)}
    z = (u1 - mu) / math.sqrt(sigma_sq)
    # 连续性校正
    z = (abs(u1 - mu) - 0.5) / math.sqrt(sigma_sq) if abs(u1 - mu) > 0.5 else 0.0
    p = 2 * (1.0 - normal_cdf(abs(z)))
    return {"u": u, "p_value": max(0.0, min(1.0, p)), "z": z, "n1": float(n1), "n2": float(n2)}


def _kolmogorov_sf(lam: float) -> float:
    """Kolmogorov 分布的生存函数 Q(λ) = 2 Σ (-1)^{k-1} e^{-2k²λ²}。"""
    if lam <= 0:
        return 1.0
    if lam < 0.4:
        # 级数收敛慢，改用等价形式
        total = 0.0
        for k in range(1, 100):
            total += math.exp(-((2 * k - 1) ** 2) * math.pi**2 / (8 * lam**2))
        return max(0.0, min(1.0, 1.0 - total))
    total = 0.0
    for k in range(1, 100):
        term = 2 * ((-1) ** (k - 1)) * math.exp(-2 * k * k * lam * lam)
        total += term
        if abs(term) < 1e-12:
            break
    return max(0.0, min(1.0, total))


def ks_2samp(baseline: Sequence[float], candidate: Sequence[float]) -> dict[str, float]:
    """两样本 Kolmogorov-Smirnov 检验：最大累积分布距离 D 与渐近 p 值。

    D 是个有意义的"分布差异幅度"（0=完全同分布），比 p 值更适合做回归告警。
    """
    a = sorted(_clean(baseline))
    b = sorted(_clean(candidate))
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return {}
    i = j = 0
    d = 0.0
    # 关键点：两个样本都要跳过「当前值」的全部并列元素，否则同分布样本也会
    # 因为处理先后顺序产生虚假的 D（例如 a=b=[0..49] 会得到 D=0.02）。
    while i < n1 and j < n2:
        if a[i] == b[j]:
            value = a[i]
            while i < n1 and a[i] == value:
                i += 1
            while j < n2 and b[j] == value:
                j += 1
        elif a[i] < b[j]:
            value = a[i]
            while i < n1 and a[i] == value:
                i += 1
        else:
            value = b[j]
            while j < n2 and b[j] == value:
                j += 1
        d = max(d, abs(i / n1 - j / n2))
    en = math.sqrt(n1 * n2 / (n1 + n2))
    lam = (en + 0.12 + 0.11 / en) * d
    return {"d": d, "p_value": _kolmogorov_sf(lam), "n1": float(n1), "n2": float(n2)}


def cohens_d(baseline: Sequence[float], candidate: Sequence[float]) -> float:
    """标准化均值差（合并标准差）。>0 表示候选更大（延迟场景即更慢）。"""
    a = _clean(baseline)
    b = _clean(candidate)
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return 0.0
    m1, m2 = sum(a) / n1, sum(b) / n2
    v1 = sum((x - m1) ** 2 for x in a) / (n1 - 1)
    v2 = sum((x - m2) ** 2 for x in b) / (n2 - 1)
    pooled = math.sqrt(((n1 - 1) * v1 + (n2 - 1) * v2) / (n1 + n2 - 2))
    if pooled == 0:
        return 0.0
    return (m2 - m1) / pooled


def cliffs_delta(baseline: Sequence[float], candidate: Sequence[float]) -> float:
    """Cliff's δ = P(candidate > baseline) - P(candidate < baseline)，范围 [-1, 1]。

    正值表示候选整体更大（延迟场景即更慢），与 cohens_d 的符号方向一致。
    """
    a = _clean(baseline)
    b = _clean(candidate)
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return 0.0
    combined = list(a) + list(b)
    ranks, _ = _rank_with_ties(combined)
    r1 = sum(ranks[:n1])
    # u1 = 基线大于候选的配对数（并列各计 0.5）
    u1 = r1 - n1 * (n1 + 1) / 2.0
    return 1.0 - 2.0 * u1 / (n1 * n2)


@dataclass
class DistributionComparison:
    """两个 run 在某个延迟指标上的分布级对比。"""

    metric: str
    baseline_run: str
    candidate_run: str
    baseline_stats: dict[str, float]
    candidate_stats: dict[str, float]
    delta_median: float
    bootstrap: dict[str, float]
    ks: dict[str, float]
    mann_whitney: dict[str, float]
    cohens_d: float
    cliffs_delta: float

    @property
    def significant(self) -> bool:
        """95% 置信区间不含 0，或 Mann-Whitney p<0.05。"""
        ci = self.bootstrap
        if ci and (ci.get("ci_low", 0.0) > 0 or ci.get("ci_high", 0.0) < 0):
            return True
        return bool(self.mann_whitney.get("p_value", 1.0) < 0.05)

    def to_dict(self) -> dict[str, object]:
        from dataclasses import asdict

        return asdict(self)


def compare_distributions(
    baseline: Sequence[float],
    candidate: Sequence[float],
    *,
    metric: str,
    baseline_run: str = "baseline",
    candidate_run: str = "candidate",
    resamples: int = 2000,
) -> DistributionComparison | None:
    """一步做完分布对比（分位 + bootstrap + KS + MWU + 效应量）。"""
    a = _clean(baseline)
    b = _clean(candidate)
    if not a or not b:
        return None
    bs = describe(a)
    cs = describe(b)
    return DistributionComparison(
        metric=metric,
        baseline_run=baseline_run,
        candidate_run=candidate_run,
        baseline_stats=bs,
        candidate_stats=cs,
        delta_median=cs["median"] - bs["median"],
        bootstrap=bootstrap_ci_diff(a, b, "median", n_resamples=resamples),
        ks=ks_2samp(a, b),
        mann_whitney=mann_whitney_u(a, b),
        cohens_d=cohens_d(a, b),
        cliffs_delta=cliffs_delta(a, b),
    )
