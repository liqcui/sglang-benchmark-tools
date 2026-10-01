"""领域模型：场景、run 记录、每请求样本、指标规格。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

# 指标方向：判断"变好了还是变坏了"
LOWER_BETTER = "lower"
HIGHER_BETTER = "higher"
NEUTRAL = "neutral"


@dataclass(frozen=True)
class MetricSpec:
    """一个可比较指标的元数据。"""

    key: str
    label: str
    unit: str = ""
    direction: str = NEUTRAL
    family: str = "other"  # latency / throughput / reliability / control / distribution
    control: bool = False  # True 表示这是配置项而非结果，默认不参与优劣判定

    @property
    def higher_is_better(self) -> bool | None:
        if self.direction == HIGHER_BETTER:
            return True
        if self.direction == LOWER_BETTER:
            return False
        return None


# --------------------------------------------------------------------------- #
# 指标规格表
# --------------------------------------------------------------------------- #

_LATENCY_FAMILIES = {
    "e2e_latency": "端到端延迟",
    "ttft": "首 token 延迟",
    "tpot": "单输出 token 耗时",
    "itl": "token 间隔",
}
_LATENCY_STATS = {
    "mean": "均值",
    "median": "中位数",
    "p90": "P90",
    "p95": "P95",
    "p99": "P99",
    "p99_9": "P99.9",
    "min": "最小",
    "max": "最大",
    "std": "标准差",
}


def _build_registry() -> dict[str, MetricSpec]:
    reg: dict[str, MetricSpec] = {}

    def add(spec: MetricSpec) -> None:
        reg[spec.key] = spec

    # —— 可靠性 / 计数 ——
    add(MetricSpec("successful_requests", "成功请求数", "count", HIGHER_BETTER, "reliability"))
    add(MetricSpec("success_rate", "请求成功率", "%", HIGHER_BETTER, "reliability"))
    add(MetricSpec("failed_requests", "失败请求数", "count", LOWER_BETTER, "reliability"))

    # —— 吞吐 ——
    add(MetricSpec("request_throughput_req_s", "请求吞吐", "req/s", HIGHER_BETTER, "throughput"))
    add(MetricSpec("input_token_throughput_tok_s", "输入 token 吞吐", "tok/s", HIGHER_BETTER, "throughput"))
    add(MetricSpec("output_token_throughput_tok_s", "输出 token 吞吐", "tok/s", HIGHER_BETTER, "throughput"))
    add(MetricSpec("total_token_throughput_tok_s", "总 token 吞吐", "tok/s", HIGHER_BETTER, "throughput"))
    add(MetricSpec("total_input_tokens", "总输入 token", "tokens", HIGHER_BETTER, "throughput", control=True))
    add(MetricSpec("total_generated_tokens", "总生成 token", "tokens", HIGHER_BETTER, "throughput", control=True))
    add(MetricSpec("total_generated_tokens_retokenized", "总生成 token(retokenized)", "tokens", NEUTRAL, "throughput", control=True))
    add(MetricSpec("benchmark_duration_s", "压测总时长", "s", LOWER_BETTER, "throughput"))

    # —— 延迟（四个族 × 常用分位）——
    for fam, fam_label in _LATENCY_FAMILIES.items():
        for stat, stat_label in _LATENCY_STATS.items():
            key = f"{stat}_{fam}_ms"
            label = f"{fam_label} {stat_label}"
            add(MetricSpec(key, label, "ms", LOWER_BETTER, "latency"))

    # —— 稳定性派生指标 ——
    add(MetricSpec("tail_ratio_ttft", "TTFT 尾延迟比 (P99/中位数)", "x", LOWER_BETTER, "stability"))
    add(MetricSpec("tail_ratio_tpot", "TPOT 尾延迟比 (P99/中位数)", "x", LOWER_BETTER, "stability"))
    add(MetricSpec("tail_ratio_e2e_latency", "E2E 尾延迟比 (P99/中位数)", "x", LOWER_BETTER, "stability"))
    add(MetricSpec("itl_cv", "ITL 抖动系数 (CV)", "x", LOWER_BETTER, "stability"))

    # —— 控制器参数（回显，用于校验两次 run 配置是否真的一致）——
    for key, label, unit in (
        ("max_request_concurrency", "配置最大并发", "count"),
        ("traffic_request_rate", "配置请求速率", "req/s"),
        ("concurrency", "实测平均并发", "count"),
        ("random_input", "配置输入长度", "tokens"),
        ("random_output", "配置输出长度", "tokens"),
        ("num_prompts", "配置请求总数", "count"),
    ):
        add(MetricSpec(key, label, unit, NEUTRAL, "control", control=True))

    return reg


METRIC_REGISTRY: dict[str, MetricSpec] = _build_registry()

# 用于对比报告的默认关键指标（有则显示）
KEY_METRICS: tuple[str, ...] = (
    "success_rate",
    "mean_ttft_ms",
    "p99_ttft_ms",
    "mean_tpot_ms",
    "p99_tpot_ms",
    "mean_e2e_latency_ms",
    "p99_e2e_latency_ms",
    "mean_itl_ms",
    "p99_itl_ms",
    "output_token_throughput_tok_s",
    "total_token_throughput_tok_s",
    "request_throughput_req_s",
    "tail_ratio_ttft",
    "tail_ratio_tpot",
    "itl_cv",
    "benchmark_duration_s",
)

# 图表用：分布类指标 -> 对应每请求样本字段
DISTRIBUTION_METRICS: dict[str, str] = {
    "ttft_ms": "TTFT (ms)",
    "tpot_ms": "TPOT (ms)",
    "itl_ms": "ITL (ms)",
    "e2e_ms": "端到端延迟 (ms)",
}


def spec_for(key: str) -> MetricSpec:
    """取指标规格；未登记的指标回退为"中性未知"，仍然会被存档。"""
    spec = METRIC_REGISTRY.get(key)
    if spec is not None:
        return spec
    label = key.replace("_", " ")
    unit = ""
    # 顺序敏感：先匹配更长的后缀，否则 ``_tok_s`` 会被 ``_s`` 抢走
    for suffix, u in (
        ("_tok_s", "tok/s"),
        ("_req_s", "req/s"),
        ("_ms", "ms"),
        ("_pct", "%"),
        ("_s", "s"),
    ):
        if key.endswith(suffix):
            unit = u
            break
    return MetricSpec(key, label, unit, NEUTRAL, "other")


# --------------------------------------------------------------------------- #
# 场景
# --------------------------------------------------------------------------- #


@dataclass
class Scenario:
    """一个可复现的压测场景定义（对应文档里的某一条命令）。"""

    key: str
    name: str
    family: str
    description: str = ""
    backend: str = "sglang-oai-chat"
    base_url: str | None = "http://10.66.1.232:8081"
    host: str | None = None
    port: int | None = None
    model: str = "/workspace/data/GLM-5.2-W8A8"
    tokenizer: str = "/workspace/data/GLM-5.2-W8A8"
    dataset_name: str = "random"
    dataset_path: str = "/workspace/data/ShareGPT_V3_unfiltered_cleaned_split.json"
    num_prompts: int = 50
    max_concurrency: int = 1
    random_input: int = 4096
    random_output: int = 2048
    random_range_ratio: float = 1.0
    request_rate: float = 1.0
    pd_separated: bool = True
    extra_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    sla: dict[str, float] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    # ---- 便捷属性 ----
    @property
    def endpoint(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return f"http://{self.host}:{self.port}"

    @property
    def is_pd_separated(self) -> bool:
        return bool(self.pd_separated)

    @property
    def stage(self) -> str:
        """full / prefill / decode —— 便于按链路环节筛选。"""
        if self.family == "single-stage" or not self.pd_separated:
            if "prefill" in self.key:
                return "prefill"
            if "decode" in self.key:
                return "decode"
            return "stage"
        return "pd-full"

    def benchmark_params(self) -> dict[str, Any]:
        """入库/展示用的参数快照（键名与命令行参数一一对应）。"""
        return {
            "backend": self.backend,
            "base_url": self.base_url,
            "host": self.host,
            "port": self.port,
            "model": self.model,
            "tokenizer": self.tokenizer,
            "dataset_name": self.dataset_name,
            "dataset_path": self.dataset_path,
            "num_prompts": self.num_prompts,
            "max_concurrency": self.max_concurrency,
            "random_input": self.random_input,
            "random_output": self.random_output,
            "random_range_ratio": self.random_range_ratio,
            "request_rate": self.request_rate,
            "pd_separated": self.pd_separated,
            "extra_args": list(self.extra_args),
        }

    def identity(self) -> str:
        """场景指纹：忽略名称/描述，只看真正影响结果的参数。"""
        import hashlib
        import json as _json

        payload = _json.dumps(self.benchmark_params(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Scenario":
        fields = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in dict(data).items() if k in fields}
        clean.setdefault("key", "custom")
        clean.setdefault("name", clean.get("key", "custom"))
        clean.setdefault("family", "custom")
        return cls(**clean)


# --------------------------------------------------------------------------- #
# 每请求样本
# --------------------------------------------------------------------------- #


@dataclass
class Sample:
    """一条请求的实测明细。分布分析的原子数据。"""

    request_id: str = ""
    input_len: int | None = None
    output_len: int | None = None
    ttft_ms: float | None = None
    tpot_ms: float | None = None
    itl_ms: float | None = None
    e2e_ms: float | None = None
    status: str = "ok"
    error: str | None = None

    @property
    def ok(self) -> bool:
        return str(self.status).lower() in ("ok", "success", "succeeded", "200", "true")

    def to_row(self, run_id: str, seq: int) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "seq": seq,
            "request_id": self.request_id or str(seq),
            "input_len": self.input_len,
            "output_len": self.output_len,
            "ttft_ms": self.ttft_ms,
            "tpot_ms": self.tpot_ms,
            "itl_ms": self.itl_ms,
            "e2e_ms": self.e2e_ms,
            "status": self.status,
            "error": self.error,
        }

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "Sample":
        def num(*names: str) -> float | None:
            for n in names:
                v = row.get(n)
                if v is None or v == "":
                    continue
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
            return None

        def integer(*names: str) -> int | None:
            v = num(*names)
            return None if v is None else int(v)

        status = row.get("status")
        if status is None:
            status = "ok" if not row.get("error") else "error"
        return cls(
            request_id=str(row.get("request_id", row.get("id", "")) or ""),
            input_len=integer("input_len", "input_tokens", "prompt_tokens", "input_length"),
            output_len=integer("output_len", "output_tokens", "completion_tokens", "output_length"),
            ttft_ms=num("ttft_ms", "ttft", "first_token_latency_ms"),
            tpot_ms=num("tpot_ms", "tpot", "mean_tpot_ms", "time_per_output_token_ms"),
            itl_ms=num("itl_ms", "itl", "mean_itl_ms", "inter_token_latency_ms"),
            e2e_ms=num("e2e_ms", "e2e", "latency_ms", "latency", "total_latency_ms"),
            status=str(status),
            error=(str(row["error"]) if row.get("error") else None),
        )


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #


@dataclass
class Issue:
    """从日志里识别出来的异常信号。"""

    kind: str  # oom / crash / timeout / http_error / connection / truncation / parse
    severity: str  # error / warning
    message: str
    line_no: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunRecord:
    """一次 test run 的完整记录。"""

    run_id: str
    scenario_key: str
    label: str = ""
    status: str = "ok"  # ok / failed / partial / aborted
    source: str = "executed"  # executed / ingested
    started_at: datetime | None = None
    finished_at: datetime | None = None
    wall_s: float | None = None
    command: str = ""
    exit_code: int | None = None
    host: str = ""
    operator: str = ""
    notes: str = ""
    tags: list[str] = field(default_factory=list)
    raw_dir: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    samples: list[Sample] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    @property
    def duration_s(self) -> float | None:
        if self.wall_s is not None:
            return self.wall_s
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    @property
    def ok_samples(self) -> list[Sample]:
        return [s for s in self.samples if s.ok]

    def metric(self, key: str, default: float | None = None) -> float | None:
        v = self.metrics.get(key)
        return default if v is None else v

    def to_dict(self, include_samples: bool = False) -> dict[str, Any]:
        from .util import iso

        data = {
            "run_id": self.run_id,
            "scenario_key": self.scenario_key,
            "label": self.label,
            "status": self.status,
            "source": self.source,
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
            "wall_s": self.wall_s,
            "command": self.command,
            "exit_code": self.exit_code,
            "host": self.host,
            "operator": self.operator,
            "notes": self.notes,
            "tags": list(self.tags),
            "raw_dir": self.raw_dir,
            "params": dict(self.params),
            "env": dict(self.env),
            "metrics": dict(self.metrics),
            "issues": [i.to_dict() for i in self.issues],
            "artifacts": dict(self.artifacts),
            "meta": dict(self.meta),
            "sample_count": len(self.samples),
            "schema_version": self.schema_version,
        }
        if include_samples:
            data["samples"] = [asdict(s) for s in self.samples]
        return data


@dataclass
class RunSummary:
    """列表页用的轻量 run 视图（不加载样本）。"""

    run_id: str
    scenario_key: str
    label: str
    status: str
    started_at: datetime | None
    metrics: dict[str, float] = field(default_factory=dict)
    sample_count: int = 0
    issue_count: int = 0
    source: str = "executed"

    def metric(self, key: str, default: float | None = None) -> float | None:
        v = self.metrics.get(key)
        return default if v is None else v


def collect_samples(samples: Iterable[Sample], field_name: str) -> list[float]:
    """取某个字段的有效数值序列（跳过 None/NaN），用于分布统计。"""
    out: list[float] = []
    for s in samples:
        if not s.ok:
            continue
        v = getattr(s, field_name, None)
        if v is None:
            continue
        v = float(v)
        if v == v:  # 非 NaN
            out.append(v)
    return out


def required_sample_count(run: RunRecord) -> int | None:
    """期望的请求总数（来自参数），用于计算成功率。"""
    for key in ("num_prompts", "num-prompts"):
        v = run.params.get(key)
        if v:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return None


def sequence_or_empty(seq: Sequence[Any] | None) -> list[Any]:
    return list(seq) if seq else []
