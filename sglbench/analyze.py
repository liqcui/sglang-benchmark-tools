"""对比分析：同一场景不同 test run 的差异、显著性与 SLA 门禁。

三层结论递进：

1. **指标级**：关键指标的绝对差 / 相对差 + 方向判定（更快/更慢，无视噪声阈值）；
2. **分布级**：有每请求明细时，做 bootstrap 置信区间、KS 距离、Mann-Whitney、
   Cliff's δ —— 回答"这点差异是真实回归还是抖动"；
3. **门禁级**：按场景 SLA 判定 pass/fail，可直接用于 CI 或发布卡点。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .models import (
    KEY_METRICS,
    Issue,
    MetricSpec,
    RunRecord,
    RunSummary,
    Scenario,
    spec_for,
)
from .stats import DistributionComparison, compare_distributions, describe
from .store import Store
from .util import format_delta, format_metric, iso, utcnow

# 方向判定的措辞
_VERDICT_LABEL = {
    "better": "改善",
    "worse": "回归",
    "same": "持平",
    "changed": "变化",
    "missing": "缺失",
    "control": "配置",
}

# 默认噪声阈值：小于该相对变化视为持平（长尾指标本身有抖动）
DEFAULT_THRESHOLD_PCT = 5.0

_DIST_FIELDS: tuple[tuple[str, str], ...] = (
    ("ttft_ms", "首 token 延迟 TTFT"),
    ("tpot_ms", "单输出 token 耗时 TPOT"),
    ("itl_ms", "token 间隔 ITL"),
    ("e2e_ms", "端到端延迟"),
)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class RunView:
    """报告里对一次 run 的展示视图。"""

    run_id: str
    label: str
    scenario_key: str
    status: str
    role: str = "candidate"  # baseline / candidate
    started_at: Any = None
    metrics: dict[str, float] = field(default_factory=dict)
    sample_count: int = 0
    issues: list[Issue] = field(default_factory=list)

    @property
    def display(self) -> str:
        return self.label or self.run_id

    def metric(self, key: str) -> float | None:
        v = self.metrics.get(key)
        return None if v is None else float(v)


@dataclass
class MetricComparison:
    key: str
    label: str
    unit: str
    direction: str
    family: str
    control: bool
    baseline: float | None
    candidate: float | None
    delta: float | None
    delta_pct: float | None
    verdict: str
    significant: bool | None = None
    note: str = ""

    @property
    def verdict_label(self) -> str:
        return _VERDICT_LABEL.get(self.verdict, self.verdict)

    @property
    def baseline_text(self) -> str:
        return format_metric(self.baseline, self.unit)

    @property
    def candidate_text(self) -> str:
        return format_metric(self.candidate, self.unit)

    @property
    def delta_text(self) -> str:
        if self.delta is None:
            return "-"
        return format_delta(self.delta, self.unit, self.delta_pct)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GateResult:
    metric: str
    label: str
    operator: str  # >= / <=
    threshold: float
    actual: float | None
    status: str  # pass / fail / unknown
    unit: str = ""
    detail: str = ""

    @property
    def threshold_text(self) -> str:
        return f"{self.operator} {format_metric(self.threshold, self.unit)}"

    @property
    def actual_text(self) -> str:
        return format_metric(self.actual, self.unit)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ComparisonReport:
    """一次对比分析的完整结果（可 JSON 化，供 CI 消费）。"""

    scenario_key: str
    baseline_run: str
    candidate_runs: list[str]
    generated_at: Any = None
    threshold_pct: float = DEFAULT_THRESHOLD_PCT
    runs: list[RunView] = field(default_factory=list)
    metrics: list[MetricComparison] = field(default_factory=list)
    distributions: list[DistributionComparison] = field(default_factory=list)
    gates: dict[str, list[GateResult]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    conclusions: list[str] = field(default_factory=list)
    scenario: Scenario | None = None

    # ---- 便捷查询 ----
    @property
    def baseline_view(self) -> RunView | None:
        for r in self.runs:
            if r.run_id == self.baseline_run:
                return r
        return self.runs[0] if self.runs else None

    @property
    def candidate_views(self) -> list[RunView]:
        return [r for r in self.runs if r.run_id != self.baseline_run]

    def gate_summary(self) -> dict[str, int]:
        counts = {"pass": 0, "fail": 0, "unknown": 0}
        for results in self.gates.values():
            for g in results:
                counts[g.status] = counts.get(g.status, 0) + 1
        return counts

    def metric(self, key: str) -> MetricComparison | None:
        for m in self.metrics:
            if m.key == key:
                return m
        return None

    @property
    def all_gates_pass(self) -> bool:
        return all(g.status != "fail" for results in self.gates.values() for g in results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_key": self.scenario_key,
            "scenario_name": self.scenario.name if self.scenario else None,
            "baseline_run": self.baseline_run,
            "candidate_runs": list(self.candidate_runs),
            "generated_at": iso(self.generated_at or utcnow()),
            "threshold_pct": self.threshold_pct,
            "runs": [
                {
                    "run_id": r.run_id,
                    "label": r.label,
                    "role": r.role,
                    "status": r.status,
                    "started_at": iso(r.started_at) if r.started_at else None,
                    "sample_count": r.sample_count,
                    "metrics": r.metrics,
                    "issues": [i.to_dict() for i in r.issues],
                }
                for r in self.runs
            ],
            "metrics": [m.to_dict() for m in self.metrics],
            "distributions": [d.to_dict() for d in self.distributions],
            "gates": {k: [g.to_dict() for g in v] for k, v in self.gates.items()},
            "gate_summary": self.gate_summary(),
            "all_gates_pass": self.all_gates_pass,
            "warnings": list(self.warnings),
            "conclusions": list(self.conclusions),
        }


@dataclass
class SweepPoint:
    run_id: str
    label: str
    scenario_key: str
    max_concurrency: float | None
    request_rate: float | None
    success_rate: float | None
    mean_ttft_ms: float | None
    p99_ttft_ms: float | None
    mean_tpot_ms: float | None
    p99_tpot_ms: float | None
    output_throughput: float | None
    total_throughput: float | None
    status: str = "ok"

    @property
    def healthy(self) -> bool:
        if self.status not in ("ok", "partial"):
            return False
        return self.success_rate is None or self.success_rate >= 0.999


@dataclass
class SweepReport:
    """并发梯度扫描：定位吞吐上限与最优并发水位。"""

    group_key: str
    points: list[SweepPoint] = field(default_factory=list)
    best_throughput: SweepPoint | None = None
    highest_healthy_concurrency: SweepPoint | None = None
    knee: SweepPoint | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_key": self.group_key,
            "points": [asdict(p) for p in self.points],
            "best_throughput": asdict(self.best_throughput) if self.best_throughput else None,
            "highest_healthy_concurrency": (
                asdict(self.highest_healthy_concurrency) if self.highest_healthy_concurrency else None
            ),
            "knee": asdict(self.knee) if self.knee else None,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# 门禁
# --------------------------------------------------------------------------- #


def evaluate_gates(
    scenario: Scenario | None,
    metrics: Mapping[str, float],
    issues: Sequence[Issue] = (),
    *,
    extra_sla: Mapping[str, float] | None = None,
) -> list[GateResult]:
    """把场景 SLA 变成一条条 pass/fail。

    SLA 键约定：``min_<metric>`` / ``max_<metric>``，例如 ``max_p99_ttft_ms: 200``。
    未配置 SLA 时回退到通用门禁（成功率 100%）。
    """
    sla: dict[str, float] = dict(scenario.sla) if scenario else {}
    sla.update(extra_sla or {})
    if not sla:
        sla = {"min_success_rate": 1.0}

    results: list[GateResult] = []
    for key, threshold in sla.items():
        if key.startswith("min_"):
            metric, operator = key[4:], ">="
        elif key.startswith("max_"):
            metric, operator = key[4:], "<="
        else:
            continue
        spec = spec_for(metric)
        actual_raw = metrics.get(metric)
        actual = float(actual_raw) if isinstance(actual_raw, (int, float)) else None
        if actual is None:
            results.append(
                GateResult(
                    metric=metric,
                    label=spec.label,
                    operator=operator,
                    threshold=float(threshold),
                    actual=None,
                    status="unknown",
                    unit=spec.unit,
                    detail="指标缺失（可能未采到样本）",
                )
            )
            continue
        ok = actual >= float(threshold) if operator == ">=" else actual <= float(threshold)
        results.append(
            GateResult(
                metric=metric,
                label=spec.label,
                operator=operator,
                threshold=float(threshold),
                actual=actual,
                status="pass" if ok else "fail",
                unit=spec.unit,
                detail="" if ok else "未达 SLA 阈值",
            )
        )

    # 日志里出现严重异常信号时，即使指标看起来正常也判失败
    severe = [i for i in issues if i.severity == "error"]
    if severe:
        kinds = sorted({i.kind for i in severe})
        results.append(
            GateResult(
                metric="log_errors",
                label="日志严重错误",
                operator="<=",
                threshold=0.0,
                actual=float(len(severe)),
                status="fail",
                unit="count",
                detail="检测到: " + ", ".join(kinds),
            )
        )
    return results


# --------------------------------------------------------------------------- #
# 指标级对比
# --------------------------------------------------------------------------- #


def _verdict(
    spec: MetricSpec,
    baseline: float | None,
    candidate: float | None,
    threshold_pct: float,
) -> tuple[str, float | None, float | None]:
    if baseline is None or candidate is None:
        return "missing", None, None
    delta = candidate - baseline
    if baseline == 0:
        return ("same" if delta == 0 else "changed"), delta, None
    pct = delta / abs(baseline) * 100.0
    if spec.control:
        return ("same" if abs(pct) < 1e-9 else "control"), delta, pct
    if abs(pct) <= threshold_pct:
        return "same", delta, pct
    if spec.higher_is_better is True:
        return ("better" if delta > 0 else "worse"), delta, pct
    if spec.higher_is_better is False:
        return ("better" if delta < 0 else "worse"), delta, pct
    return "changed", delta, pct


def compare_metrics(
    baseline: Mapping[str, float],
    candidate: Mapping[str, float],
    *,
    keys: Iterable[str] | None = None,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    include_control: bool = False,
) -> list[MetricComparison]:
    """逐指标生成对比行。默认只输出关键指标；``keys=None`` 时自动挑选。"""
    if keys is None:
        ordered = [k for k in KEY_METRICS if k in baseline or k in candidate]
        extras = sorted(
            k
            for k in set(baseline) | set(candidate)
            if k not in ordered and (include_control or not spec_for(k).control)
        )
        ordered += extras
    else:
        ordered = list(keys)

    out: list[MetricComparison] = []
    for key in ordered:
        spec = spec_for(key)
        if spec.control and not include_control:
            continue
        b = baseline.get(key)
        c = candidate.get(key)
        b = float(b) if isinstance(b, (int, float)) else None
        c = float(c) if isinstance(c, (int, float)) else None
        verdict, delta, pct = _verdict(spec, b, c, threshold_pct)
        out.append(
            MetricComparison(
                key=key,
                label=spec.label,
                unit=spec.unit,
                direction=spec.direction,
                family=spec.family,
                control=spec.control,
                baseline=b,
                candidate=c,
                delta=delta,
                delta_pct=pct,
                verdict=verdict,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# 可比性检查
# --------------------------------------------------------------------------- #

_COMPARE_KEYS = ("max_concurrency", "num_prompts", "random_input", "random_output", "request_rate", "dataset_name", "model")


def check_comparability(runs: Sequence[RunRecord], baseline_id: str) -> tuple[list[str], list[str]]:
    """返回 (警告, 备注)：不同场景、不同参数、不同样本量的 run 不应被直接比较。"""
    warnings: list[str] = []
    notes: list[str] = []
    if not runs:
        return warnings, notes

    scenario_keys = {r.scenario_key for r in runs}
    if len(scenario_keys) > 1:
        warnings.append(
            "所选 run 不属于同一场景（" + "、".join(sorted(scenario_keys)) + "），参数差异可能主导结果，请谨慎解读。"
        )

    base = next((r for r in runs if r.run_id == baseline_id), runs[0])
    for run in runs:
        if run.run_id == base.run_id:
            continue
        diffs = []
        for key in _COMPARE_KEYS:
            bv = base.params.get(key)
            cv = run.params.get(key)
            if bv is not None and cv is not None and str(bv) != str(cv):
                diffs.append(f"{key}: {bv} → {cv}")
        if diffs:
            warnings.append(f"{run.run_id} 与基线的配置不同 —— " + "；".join(diffs))

    counts = {r.run_id: len(r.samples) for r in runs}
    with_samples = [c for c in counts.values() if c > 0]
    if with_samples and len(with_samples) != len(runs):
        missing = [rid for rid, c in counts.items() if c == 0]
        notes.append(
            "以下 run 没有每请求明细（只能做汇总指标对比，无法做分布检验）: " + "、".join(missing)
        )
    if with_samples:
        smallest = min(with_samples)
        largest = max(with_samples)
        if smallest > 0 and largest / smallest >= 5:
            warnings.append(
                f"各 run 样本量差异较大（{smallest} ~ {largest} 条），分位数与显著性检验的解释力不一致。"
            )
    return warnings, notes


# --------------------------------------------------------------------------- #
# 顶层对比
# --------------------------------------------------------------------------- #


def _load_run(store: Store, run_id: str, with_samples: bool) -> RunRecord:
    run = store.get_run(run_id, with_samples=with_samples)
    if run is None:
        raise KeyError(f"数据库里没有 run: {run_id}")
    return run


def compare_runs(
    store: Store,
    run_ids: Sequence[str],
    *,
    baseline: str | None = None,
    scenario: Scenario | None = None,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    with_distributions: bool = True,
    resamples: int = 2000,
    extra_sla: Mapping[str, float] | None = None,
    all_metrics: bool = False,
) -> ComparisonReport:
    """对比同一场景的多次 test run。

    ``run_ids`` 的第一个默认作为基线；``baseline`` 可显式指定。只支持"一个基线 vs
    多个候选"的星形对比 —— 这样结论方向明确，不会出现两两比较的结论冲突。
    """
    if len(run_ids) < 2:
        raise ValueError("对比至少需要两个 run（例如 --last 2 或显式给出两个 run_id）")
    resolved = list(dict.fromkeys(run_ids))
    baseline_id = baseline or resolved[0]
    if baseline_id not in resolved:
        resolved.insert(0, baseline_id)

    need_samples = with_distributions
    runs = [_load_run(store, rid, need_samples) for rid in resolved]
    run_map = {r.run_id: r for r in runs}

    base = run_map[baseline_id]
    sc = scenario
    if sc is None:
        scenario_key = base.scenario_key
    else:
        scenario_key = sc.key

    warnings, notes = check_comparability(runs, baseline_id)

    views: list[RunView] = []
    for r in runs:
        views.append(
            RunView(
                run_id=r.run_id,
                label=r.label,
                scenario_key=r.scenario_key,
                status=r.status,
                role="baseline" if r.run_id == baseline_id else "candidate",
                started_at=r.started_at,
                metrics=dict(r.metrics),
                sample_count=len(r.samples),
                issues=list(r.issues),
            )
        )

    report = ComparisonReport(
        scenario_key=scenario_key,
        baseline_run=baseline_id,
        candidate_runs=[rid for rid in resolved if rid != baseline_id],
        generated_at=utcnow(),
        threshold_pct=threshold_pct,
        runs=views,
        warnings=warnings,
        scenario=sc,
    )

    # ---- 指标级：所有候选 vs 基线 ----
    keys = _union_metric_keys(runs, all_metrics=all_metrics)
    multiple = len(report.candidate_runs) > 1
    for candidate in report.candidate_views:
        for row in compare_metrics(
            base.metrics, candidate.metrics, keys=keys, threshold_pct=threshold_pct
        ):
            if multiple:
                row.note = f"对比 {candidate.display}"
            report.metrics.append(row)

    # ---- 分布级 ----
    if with_distributions:
        for candidate in report.candidate_views:
            cand_run = run_map[candidate.run_id]
            for field_name, label in _DIST_FIELDS:
                a = [getattr(s, field_name) for s in base.ok_samples if getattr(s, field_name) is not None]
                b = [getattr(s, field_name) for s in cand_run.ok_samples if getattr(s, field_name) is not None]
                if len(a) < 5 or len(b) < 5:
                    continue
                comp = compare_distributions(
                    [float(x) for x in a],
                    [float(x) for x in b],
                    metric=field_name,
                    baseline_run=baseline_id,
                    candidate_run=candidate.run_id,
                    resamples=resamples,
                )
                if comp is not None:
                    report.distributions.append(comp)

    # ---- 门禁：每个 run 各自判定 ----
    for r in runs:
        report.gates[r.run_id] = evaluate_gates(
            scenario_for_run(r, sc),
            r.metrics,
            r.issues,
            extra_sla=extra_sla,
        )

    report.conclusions = build_conclusions(report, run_map, notes)
    return report


def scenario_for_run(run: RunRecord, explicit: Scenario | None = None) -> Scenario | None:
    """给一个 run 找到最合适的场景定义（用于取 SLA 门禁阈值）。

    优先级：显式传入且 key 匹配 > 内置/用户场景目录 > 从 run 参数还原。
    """
    if explicit is not None and explicit.key == run.scenario_key:
        return explicit
    try:
        from .scenarios import get_scenario

        return get_scenario(run.scenario_key)
    except Exception:  # noqa: BLE001 - 场景未注册是正常情况
        pass
    if explicit is not None:
        return explicit
    return _scenario_from_params(run)


def _scenario_from_params(run: RunRecord) -> Scenario | None:
    """尽量从参数还原场景（用于 ingest 进来的、场景未注册的 run）。"""
    params = run.params or {}
    if not params:
        return None
    try:
        return Scenario(
            key=run.scenario_key,
            name=run.scenario_key,
            family="custom",
            backend=str(params.get("backend", "sglang-oai-chat")),
            base_url=params.get("base_url"),
            host=params.get("host"),
            port=int(params["port"]) if params.get("port") else None,
            model=str(params.get("model", "")),
            tokenizer=str(params.get("tokenizer", params.get("model", ""))),
            dataset_name=str(params.get("dataset_name", "random")),
            dataset_path=str(params.get("dataset_path", "")),
            num_prompts=int(params.get("num_prompts") or 0),
            max_concurrency=int(params.get("max_concurrency") or 1),
            random_input=int(params.get("random_input") or 0),
            random_output=int(params.get("random_output") or 0),
            request_rate=float(params.get("request_rate") or 0.0),
            pd_separated=bool(params.get("pd_separated", False)),
        )
    except (TypeError, ValueError):
        return None


def _union_metric_keys(runs: Sequence[RunRecord], all_metrics: bool = False) -> list[str]:
    """默认只对比关键指标：把 min/max/std 这类噪声敏感的统计量淹进来只会制造假告警。"""
    present = set()
    for r in runs:
        present |= {k for k, v in r.metrics.items() if isinstance(v, (int, float))}
    if all_metrics:
        ordered = [k for k in KEY_METRICS if k in present]
        ordered += sorted(k for k in present if k not in ordered and not spec_for(k).control)
        return ordered
    defaults = [*KEY_METRICS, "failed_requests"]
    return [k for k in defaults if k in present]


# --------------------------------------------------------------------------- #
# 结论文本
# --------------------------------------------------------------------------- #


def _direction_word(verdict: str, spec: MetricSpec) -> str:
    if verdict == "better":
        return "改善"
    if verdict == "worse":
        return "回归"
    if verdict == "same":
        return "持平"
    return "变化"


def build_conclusions(
    report: ComparisonReport,
    run_map: Mapping[str, RunRecord],
    notes: Sequence[str] = (),
) -> list[str]:
    """把数字翻译成可以读给人听、也可以贴进测试报告的结论。"""
    out: list[str] = []

    base = report.baseline_view
    if base is None:
        return out

    gates = report.gate_summary()
    if gates["fail"]:
        out.append(
            f"❌ 门禁未通过：{gates['fail']} 项失败、{gates['pass']} 项通过、{gates['unknown']} 项无法判定。"
        )
    elif gates["pass"]:
        out.append(f"✅ 全部门禁通过（{gates['pass']} 项）。")
    if gates["unknown"]:
        out.append(f"⚠️ 有 {gates['unknown']} 项门禁因指标缺失无法判定。")

    for candidate in report.candidate_views:
        prefix = f"[{candidate.display}] " if len(report.candidate_runs) > 1 else ""
        rows = [
            m
            for m in report.metrics
            if (m.note == f"对比 {candidate.display}" or not m.note)
        ]
        worse = [m for m in rows if m.verdict == "worse"]
        better = [m for m in rows if m.verdict == "better"]

        headline = _headline_metric(rows)
        if headline is not None and headline.delta_pct is not None:
            out.append(
                f"{prefix}{headline.label}：{headline.baseline_text} → {headline.candidate_text}"
                f"（{headline.delta_pct:+.2f}%，判定为{_direction_word(headline.verdict, spec_for(headline.key))}，"
                f"阈值 ±{report.threshold_pct:g}%）。"
            )
        if worse:
            names = "、".join(f"{m.label}({m.delta_pct:+.1f}%)" for m in worse[:5] if m.delta_pct is not None)
            out.append(f"{prefix}出现 {len(worse)} 项回归：{names}。")
        if better:
            names = "、".join(f"{m.label}({m.delta_pct:+.1f}%)" for m in better[:5] if m.delta_pct is not None)
            out.append(f"{prefix}{len(better)} 项改善：{names}。")

        # 成功率
        sr = next((m for m in rows if m.key.endswith("success_rate")), None)
        if sr is not None and sr.candidate is not None and sr.candidate < 0.999:
            out.append(
                f"{prefix}请求成功率仅 {format_metric(sr.candidate, '%')}"
                f"（基线 {format_metric(sr.baseline, '%')}），存在失败请求，需排查链路。"
            )

        # 失败日志
        severe = [i for i in candidate.issues if i.severity == "error"]
        if severe:
            kinds = sorted({i.kind for i in severe})
            out.append(f"{prefix}日志中发现严重异常信号：{', '.join(kinds)}（共 {len(severe)} 条）。")

    # 分布级结论
    for comp in report.distributions:
        label = dict(_DIST_FIELDS).get(comp.metric, comp.metric)
        boot = comp.bootstrap
        ks = comp.ks
        delta_med = comp.delta_median
        if not boot or not ks:
            continue
        sig = "差异显著" if comp.significant else "差异不显著"
        ci = f"[{boot['ci_low']:+.2f}, {boot['ci_high']:+.2f}] ms"
        effect = _effect_size_word(abs(comp.cliffs_delta))
        out.append(
            f"{label} 中位数变化 {delta_med:+.2f} ms，bootstrap 95% CI {ci}，"
            f"KS D={ks['d']:.3f}（p={ks['p_value']:.3f}）→ {sig}；效应量 {effect}（|δ|={abs(comp.cliffs_delta):.3f}）。"
        )

    # 可比性备注
    for note in notes:
        out.append(f"ℹ️ {note}")

    return out


def _headline_metric(rows: Sequence[MetricComparison]) -> MetricComparison | None:
    """挑一个最能代表本次对比的指标：优先 TTFT，其次吞吐。"""
    for key in ("mean_ttft_ms", "median_ttft_ms", "output_token_throughput_tok_s", "request_throughput_req_s"):
        for m in rows:
            if m.key == key and m.delta_pct is not None:
                return m
    for m in rows:
        if m.delta_pct is not None and m.verdict in ("better", "worse"):
            return m
    return None


def _effect_size_word(delta: float) -> str:
    if delta < 0.147:
        return "可忽略"
    if delta < 0.33:
        return "小"
    if delta < 0.474:
        return "中"
    return "大"


# --------------------------------------------------------------------------- #
# 并发梯度扫描
# --------------------------------------------------------------------------- #


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def sweep_point(run: RunRecord) -> SweepPoint:
    m = run.metrics
    return SweepPoint(
        run_id=run.run_id,
        label=run.label,
        scenario_key=run.scenario_key,
        max_concurrency=_num(m.get("max_request_concurrency")) or _num(run.params.get("max_concurrency")),
        request_rate=_num(m.get("traffic_request_rate")) or _num(run.params.get("request_rate")),
        success_rate=_num(m.get("success_rate")),
        mean_ttft_ms=_num(m.get("mean_ttft_ms")),
        p99_ttft_ms=_num(m.get("p99_ttft_ms")),
        mean_tpot_ms=_num(m.get("mean_tpot_ms")),
        p99_tpot_ms=_num(m.get("p99_tpot_ms")),
        output_throughput=_num(m.get("output_token_throughput_tok_s")),
        total_throughput=_num(m.get("total_token_throughput_tok_s")),
        status=run.status,
    )


def _scenario_keys_for_family(store: Store, family: str | None) -> list[str]:
    """解析一个"族"包含哪些场景 key。

    三条来源取并集，避免"场景目录没入库 / 库里有目录里没有的 run"导致扫描漏点：
    内置场景目录、``scenarios`` 表、``runs`` 表里实际出现过的 key。
    """
    present = {row["scenario_key"] for row in store.query("SELECT DISTINCT scenario_key FROM runs")}
    if family is None:
        return sorted(present)

    keys: set[str] = set()
    try:
        from .scenarios import BUILTIN_SCENARIOS

        keys |= {k for k, s in BUILTIN_SCENARIOS.items() if s.family == family}
    except Exception:  # noqa: BLE001 - 目录不可用不应导致扫描失败
        pass
    for row in store.query("SELECT scenario_key FROM scenarios WHERE family = ?", [family]):
        keys.add(row["scenario_key"])
    try:
        from .scenarios import get_scenario

        for key in present - keys:
            try:
                if get_scenario(key).family == family:
                    keys.add(key)
            except Exception:  # noqa: BLE001 - 未注册场景忽略
                continue
    except Exception:  # noqa: BLE001
        pass
    return sorted(keys & present)


def analyze_sweep(
    store: Store,
    *,
    scenario_keys: Sequence[str] | None = None,
    family: str | None = None,
    latest_per_scenario: bool = True,
    group_key: str = "concurrency",
) -> SweepReport:
    """把同一"规模维度"的多次 run 串成一条曲线，找吞吐上限与最优并发出水线。

    ``latest_per_scenario=True`` 时每个场景只取最新一次 run，避免重复点。
    """
    keys = [k for k in (scenario_keys or []) if k]
    if not keys and family is not None:
        keys = _scenario_keys_for_family(store, family)

    if keys:
        summaries: list[RunSummary] = []
        for key in keys:
            if latest_per_scenario:
                rid = store.latest_run(key)
                run = store.get_run(rid, with_samples=False) if rid else None
                if run:
                    summaries.append(
                        RunSummary(
                            run_id=run.run_id,
                            scenario_key=run.scenario_key,
                            label=run.label,
                            status=run.status,
                            started_at=run.started_at,
                            metrics=run.metrics,
                        )
                    )
            else:
                summaries.extend(store.list_runs(scenario_key=key, with_metrics=True))
    else:
        candidates = store.list_runs(with_metrics=True)
        if latest_per_scenario:
            seen: dict[str, RunSummary] = {}
            for s in candidates:  # 已按时间倒序
                seen.setdefault(s.scenario_key, s)
            summaries = list(seen.values())
        else:
            summaries = candidates

    points: list[SweepPoint] = []
    for s in summaries:
        points.append(
            SweepPoint(
                run_id=s.run_id,
                label=s.label or s.run_id,
                scenario_key=s.scenario_key,
                max_concurrency=_num(s.metrics.get("max_request_concurrency")),
                request_rate=_num(s.metrics.get("traffic_request_rate")),
                success_rate=_num(s.metrics.get("success_rate")),
                mean_ttft_ms=_num(s.metrics.get("mean_ttft_ms")),
                p99_ttft_ms=_num(s.metrics.get("p99_ttft_ms")),
                mean_tpot_ms=_num(s.metrics.get("mean_tpot_ms")),
                p99_tpot_ms=_num(s.metrics.get("p99_tpot_ms")),
                output_throughput=_num(s.metrics.get("output_token_throughput_tok_s")),
                total_throughput=_num(s.metrics.get("total_token_throughput_tok_s")),
                status=s.status,
            )
        )

    points.sort(key=lambda p: (p.max_concurrency is None, p.max_concurrency or 0, p.request_rate or 0))
    report = SweepReport(group_key=group_key, points=points)

    with_tp = [p for p in points if p.total_throughput is not None]
    if with_tp:
        report.best_throughput = max(with_tp, key=lambda p: p.total_throughput or 0.0)
    healthy = [p for p in points if p.healthy and p.max_concurrency is not None]
    if healthy:
        report.highest_healthy_concurrency = max(healthy, key=lambda p: p.max_concurrency or 0.0)

    # 拐点：吞吐增益 < 15% 但 P99 TTFT 涨幅 > 50% 的第一个点
    for prev, cur in zip(with_tp, with_tp[1:]):
        if not prev.total_throughput or not cur.total_throughput:
            continue
        gain = (cur.total_throughput - prev.total_throughput) / prev.total_throughput
        lat_prev, lat_cur = prev.p99_ttft_ms, cur.p99_ttft_ms
        lat_penalty = (
            (lat_cur - lat_prev) / lat_prev if lat_prev and lat_cur and lat_prev > 0 else 0.0
        )
        if gain < 0.15 and lat_penalty > 0.5:
            report.knee = cur
            break

    if report.best_throughput:
        best = report.best_throughput
        if best.total_throughput is not None and best.max_concurrency is not None:
            report.notes.append(
                f"吞吐峰值出现在 {best.scenario_key}"
                f"（并发 {best.max_concurrency:g}，总吞吐 {best.total_throughput:,.0f} tok/s）。"
            )
        else:
            report.notes.append(f"吞吐峰值出现在 {best.scenario_key}。")
    if report.highest_healthy_concurrency:
        report.notes.append(
            f"零失败的最高并发为 {report.highest_healthy_concurrency.max_concurrency:g}"
            f"（{report.highest_healthy_concurrency.scenario_key}），建议作为线上安全水位。"
        )
    failed = [p for p in points if not p.healthy]
    if failed:
        report.notes.append(
            "以下配置未通过成功率检查，不建议上线："
            + "、".join(f"{p.scenario_key}(并发 {p.max_concurrency:g})" for p in failed if p.max_concurrency)
        )
    if report.knee:
        report.notes.append(
            f"检测到吞吐拐点：{report.knee.scenario_key}"
            f"（并发 {report.knee.max_concurrency:g}）之后吞吐增益明显放缓而延迟继续抬升。"
        )
    return report


# --------------------------------------------------------------------------- #
# 汇总表（供 CLI / 报告复用）
# --------------------------------------------------------------------------- #


def key_metric_rows(runs: Sequence[RunView | RunSummary], keys: Sequence[str] | None = None) -> tuple[list[str], list[list[str]]]:
    """生成 (表头, 行) 的关键指标矩阵，列表页与报告共用。"""
    use = list(keys or KEY_METRICS)
    header = ["run"] + [spec_for(k).label for k in use]
    rows: list[list[str]] = []
    for r in runs:
        metrics = r.metrics
        cells = []
        for k in use:
            v = metrics.get(k)
            cells.append(format_metric(v, spec_for(k).unit) if isinstance(v, (int, float)) else "-")
        label = getattr(r, "label", "") or getattr(r, "run_id", "")
        rows.append([str(label)] + cells)
    return header, rows


def relative_improvement(baseline: float | None, candidate: float | None, higher_is_better: bool) -> float | None:
    """归一化改善度：正数=变好，负数=变差（百分比）。用于多指标排序。"""
    if baseline in (None, 0) or candidate is None:
        return None
    raw = (candidate - baseline) / abs(baseline) * 100.0
    return raw if higher_is_better else -raw


def geometric_mean(values: Sequence[float]) -> float | None:
    vals = [v for v in values if v is not None and v > 0]
    if not vals:
        return None
    return math.exp(sum(math.log(v) for v in vals) / len(vals))


def stability_from_samples(samples: Sequence[Any], field: str = "itl_ms") -> dict[str, float]:
    """单 run 的抖动画像（CV / 尾比 / 最大值），用于"稳定性"单列指标。"""
    vals = [float(getattr(s, field)) for s in samples if getattr(s, field, None) is not None]
    stats = describe(vals)
    if not stats:
        return {}
    return {
        f"{field}_mean": stats["mean"],
        f"{field}_cv": stats["cv"],
        f"{field}_p50": stats["median"],
        f"{field}_p99": stats["p99"],
        f"{field}_p99_over_p50": (stats["p99"] / stats["median"]) if stats["median"] else 0.0,
        f"{field}_max": stats["max"],
    }


def sparkline(values: Sequence[float], width: int = 24) -> str:
    """终端里的迷你曲线，用于 list 输出。"""
    blocks = "▁▂▃▄▅▆▇█"
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1.0
    out = []
    for v in vals[-width:]:
        idx = int((v - lo) / span * (len(blocks) - 1))
        out.append(blocks[max(0, min(len(blocks) - 1, idx))])
    return "".join(out)


__all__ = [
    "DEFAULT_THRESHOLD_PCT",
    "ComparisonReport",
    "GateResult",
    "MetricComparison",
    "RunView",
    "SweepPoint",
    "SweepReport",
    "analyze_sweep",
    "build_conclusions",
    "check_comparability",
    "compare_metrics",
    "compare_runs",
    "evaluate_gates",
    "key_metric_rows",
    "relative_improvement",
    "sparkline",
    "stability_from_samples",
    "sweep_point",
]
