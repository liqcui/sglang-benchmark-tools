"""解析 ``python3 -m sglang.bench_serving`` 的输出。

设计原则：**宽容解析**。不同 SGLang 版本的结果块标签、单位、字段顺序都会变，因此
这里不写死字段列表，而是：

1. 定位结果块（``==== Serving Benchmark Result ====`` 到下一行全 ``=`` 之间）；
2. 块内按「标签 : 数值」逐行归一化成 ``slug`` 指标键；
3. 能拿到每请求明细（``requests.jsonl``）时，用它重算/补全分位与派生指标。

于是新增指标不需要改代码 —— 未在注册表里的指标依然会被存档，只是不参与
"变好/变坏"的方向判定。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .models import Issue, MetricSpec, Sample, collect_samples, spec_for
from .util import iter_jsonl, read_json

# --------------------------------------------------------------------------- #
# 标签归一化
# --------------------------------------------------------------------------- #

_BLOCK_START = re.compile(r"^[=\-]{4,}\s*Serving Benchmark Result\s*[=\-]{4,}\s*$", re.IGNORECASE)
_SEPARATOR = re.compile(r"^\s*[=\-]{10,}\s*$")
_VALUE = re.compile(
    r"^\s*(?P<value>[-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*(?P<unit>[A-Za-z%][A-Za-z0-9/%.^_-]*)?\s*$"
)


def slugify_label(label: str) -> str:
    """``Mean TTFT (ms)`` -> ``mean_ttft_ms``；``Request throughput (req/s)`` -> ``request_throughput_req_s``。"""
    text = label.strip().lower()
    text = text.replace("%", " pct ").replace("&", " and ")
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "_", text)
    return text.strip("_")


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 结果块 / Namespace
# --------------------------------------------------------------------------- #


@dataclass
class ResultBlock:
    lines: list[str] = field(default_factory=list)
    start_line: int | None = None  # 1-based
    end_line: int | None = None
    found: bool = False


def find_result_block(text: str) -> ResultBlock:
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if _BLOCK_START.match(line):
            start = idx
            break
    if start is None:
        return ResultBlock()

    end = len(lines) - 1
    for idx in range(start + 1, len(lines)):
        if _SEPARATOR.match(lines[idx]):
            end = idx
            break
    return ResultBlock(lines=lines[start + 1 : end], start_line=start + 1, end_line=end + 1, found=True)


def parse_result_block(block: ResultBlock) -> tuple[dict[str, float], dict[str, str]]:
    """块内逐行解析，返回 (数值指标, 文本元数据)。"""
    numbers: dict[str, float] = {}
    strings: dict[str, str] = {}
    for line in block.lines:
        if ":" not in line:
            continue
        label, _, rest = line.partition(":")
        label = label.strip(" -\t")
        if not label:
            continue
        key = slugify_label(label)
        if not key:
            continue
        m = _VALUE.match(rest)
        if m:
            value = _to_float(m.group("value"))
            if value is not None:
                # 同名指标重复出现时以首次为准（避免被尾部噪声覆盖）
                numbers.setdefault(key, value)
                continue
        stripped = rest.strip()
        if stripped:
            strings.setdefault(key, stripped)
    return numbers, strings


def scan_metrics_anywhere(text: str, allowed: Iterable[str] | None = None) -> dict[str, float]:
    """兜底解析：全文扫描「标签: 数值」。

    仅在找不到结果块时使用；``allowed`` 用于限制为已知指标键，避免把日志里的
    随机数字（时间戳、PID）当成指标。
    """
    allow = set(allowed) if allowed else None
    out: dict[str, float] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        label, _, rest = line.partition(":")
        label = label.strip(" -\t")
        if not label or len(label) > 80:
            continue
        key = slugify_label(label)
        if allow is not None and key not in allow:
            continue
        m = _VALUE.match(rest)
        if not m:
            continue
        value = _to_float(m.group("value"))
        if value is not None:
            out.setdefault(key, value)
    return out


def _split_top_level(text: str) -> list[str]:
    """按顶层逗号切分（跳过引号与括号内部）。"""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    buf: list[str] = []
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def parse_namespace(text: str) -> dict[str, Any]:
    """解析 ``Namespace(a=1, b='x', ...)`` 回显行，得到本次 run 的真实参数。"""
    match = re.search(r"Namespace\s*\(", text)
    if not match:
        return {}
    start = match.end()
    depth = 1
    idx = start
    while idx < len(text) and depth > 0:
        ch = text[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        idx += 1
    body = text[start:idx]
    out: dict[str, Any] = {}
    for chunk in _split_top_level(body):
        if "=" not in chunk:
            continue
        k, _, v = chunk.partition("=")
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            out[k] = v[1:-1]
            continue
        low = v.lower()
        if low in ("true", "false"):
            out[k] = low == "true"
            continue
        num = _to_float(v)
        out[k] = num if num is not None else v
    return out


# --------------------------------------------------------------------------- #
# 异常信号
# --------------------------------------------------------------------------- #

_ISSUE_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("oom", "error", r"(out of memory|outofmemoryerror|\boom\b|memoryerror|HSA_STATUS_ERROR_OUT_OF_RESOURCES)"),
    ("crash", "error", r"(worker\s+\d+\s+crashed|crashed|segmentation fault|sigsegv|sigkill|sigabrt|core dumped|panic:|fatal error)"),
    ("restart", "error", r"(pod\s+[\w.\-]+\s+restarted|restart count|CrashLoopBackOff|container .* restarted)"),
    ("timeout", "error", r"(timed out|timeout|DeadlineExceeded|ReadTimeout|ConnectTimeout)"),
    ("http_error", "error", r"(HTTP[/ ]?5\d\d|\b5(?:00|02|03|04)\b\s*(?:Internal|Bad Gateway|Service Unavailable|Gateway Timeout))"),
    ("connection", "error", r"(connection refused|connection reset|broken pipe|econnreset|econnrefused|RemoteProtocolError|Max retries exceeded)"),
    ("kv_transfer", "error", r"(nixl[^\n]{0,40}(fail|error)|kv[ _-]?transfer[^\n]{0,40}(fail|error|timeout))"),
    ("abort", "warning", r"(aborted|cancell?ed|client disconnected)"),
    ("retry", "warning", r"(retry(?:ing)?\b|retries)"),
)
_ISSUE_RES = tuple((kind, sev, re.compile(pat, re.IGNORECASE)) for kind, sev, pat in _ISSUE_PATTERNS)

_IGNORE_LINE = re.compile(r"^\s*(100%\|[^|]*\|\s*\d+/\d+|Namespace\s*\()", re.IGNORECASE)


def detect_issues(text: str, limit_per_kind: int = 5) -> list[Issue]:
    """扫描日志里的失败信号，按 (kind, 归一化消息) 去重并保留原始行号。"""
    seen: dict[tuple[str, str], int] = {}
    issues: list[Issue] = []
    counts: dict[str, int] = {}

    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or _IGNORE_LINE.match(line):
            continue
        for kind, severity, regex in _ISSUE_RES:
            if not regex.search(line):
                continue
            message = line.strip()
            fingerprint = re.sub(r"\d+", "#", message)[:120]
            key = (kind, fingerprint)
            if key in seen:
                continue
            if counts.get(kind, 0) >= limit_per_kind:
                continue
            counts[kind] = counts.get(kind, 0) + 1
            seen[key] = line_no
            issues.append(Issue(kind=kind, severity=severity, message=message[:500], line_no=line_no))
    return issues


# --------------------------------------------------------------------------- #
# 每请求明细
# --------------------------------------------------------------------------- #


def load_samples(path: str | Path) -> list[Sample]:
    """读取每请求明细：支持 ``.jsonl``（逐行）与 ``.json``（数组或对象）。"""
    p = Path(path)
    if not p.exists():
        return []
    if p.suffix.lower() == ".jsonl":
        return [Sample.from_mapping(row) for row in iter_jsonl(p)]
    data = read_json(p, default=None)
    if data is None:
        return []
    if isinstance(data, dict):
        for key in ("requests", "samples", "results", "details", "per_request"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        return []
    return [Sample.from_mapping(row) for row in data if isinstance(row, Mapping)]


def default_requests_path(log_path: str | Path) -> Path:
    """约定：``foo.log`` 的明细放在 ``foo.requests.jsonl``。"""
    p = Path(log_path)
    return p.with_suffix("").with_name(p.stem + ".requests.jsonl")


# --------------------------------------------------------------------------- #
# 派生指标
# --------------------------------------------------------------------------- #


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    n = len(sorted_values)
    if n == 0:
        raise ValueError("空序列无分位数")
    if n == 1:
        return float(sorted_values[0])
    pos = (q / 100.0) * (n - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(sorted_values[lo])
    frac = pos - lo
    return float(sorted_values[lo]) * (1.0 - frac) + float(sorted_values[hi]) * frac


def distribution_stats(values: Sequence[float]) -> dict[str, float]:
    """均值/中位/分位/标准差 —— 与 numpy ``method='linear'`` 一致。"""
    vals = sorted(float(v) for v in values if v is not None and float(v) == float(v))
    n = len(vals)
    if n == 0:
        return {}
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1) if n > 1 else 0.0
    return {
        "mean": mean,
        "median": _percentile(vals, 50),
        "p90": _percentile(vals, 90),
        "p95": _percentile(vals, 95),
        "p99": _percentile(vals, 99),
        "min": vals[0],
        "max": vals[-1],
        "std": math.sqrt(var),
    }


_SAMPLE_METRIC_PREFIX: dict[str, str] = {
    "ttft_ms": "ttft",
    "tpot_ms": "tpot",
    "itl_ms": "itl",
    "e2e_ms": "e2e_latency",
}


def metrics_from_samples(samples: Sequence[Sample]) -> dict[str, float]:
    """从每请求明细重算延迟分位与吞吐，用作缺项补全与一致性校验。"""
    ok = [s for s in samples if s.ok]
    out: dict[str, float] = {}
    if not ok:
        return out

    for field_name, prefix in _SAMPLE_METRIC_PREFIX.items():
        stats = distribution_stats(collect_samples(ok, field_name))
        for stat, value in stats.items():
            out[f"{stat}_{prefix}_ms"] = round(value, 4)

    if "mean_itl_ms" in out and out["mean_itl_ms"] > 0 and "std_itl_ms" in out:
        out["itl_cv"] = round(out["std_itl_ms"] / out["mean_itl_ms"], 4)

    durations = [s.e2e_ms for s in ok if s.e2e_ms]
    if durations:
        wall = max(durations) / 1000.0
        if wall > 0:
            out.setdefault("benchmark_duration_s", round(wall, 4))
    return out


def add_derived_metrics(
    metrics: Mapping[str, float],
    params: Mapping[str, Any] | None = None,
    samples: Sequence[Sample] | None = None,
) -> dict[str, float]:
    """补齐可靠性/稳定性派生指标。不覆盖日志里已有的原始值。"""
    out: dict[str, float] = {k: float(v) for k, v in metrics.items() if isinstance(v, (int, float))}
    params = params or {}
    samples = list(samples or [])

    # 样本能给出的分位/抖动：只补缺
    if samples:
        for key, value in metrics_from_samples(samples).items():
            out.setdefault(key, value)

    # 成功率
    expected = None
    for key in ("num_prompts", "num-prompts"):
        if params.get(key):
            try:
                expected = int(params[key])
                break
            except (TypeError, ValueError):
                pass
    successful = out.get("successful_requests")
    if successful is None and samples:
        successful = float(sum(1 for s in samples if s.ok))
        out["successful_requests"] = successful
    if expected:
        out.setdefault("num_prompts", float(expected))
        if successful is not None:
            out["success_rate"] = round(min(1.0, successful / expected), 6) if expected else 1.0
            out["failed_requests"] = float(max(0, expected - successful))
    elif successful is not None and samples:
        failed = float(sum(1 for s in samples if not s.ok))
        total = successful + failed
        out["failed_requests"] = failed
        if total:
            out["success_rate"] = round(successful / total, 6)

    # 尾延迟比（抖动代理指标）
    for fam in ("ttft", "tpot", "e2e_latency"):
        p99 = out.get(f"p99_{fam}_ms")
        median = out.get(f"median_{fam}_ms") or out.get(f"mean_{fam}_ms")
        if p99 and median and median > 0:
            out.setdefault(f"tail_ratio_{fam}", round(p99 / median, 4))

    # 回显控制器参数
    for src, dst in (
        ("max_concurrency", "max_request_concurrency"),
        ("max-concurrency", "max_request_concurrency"),
        ("request_rate", "traffic_request_rate"),
        ("request-rate", "traffic_request_rate"),
        ("random_input", "random_input"),
        ("random_output", "random_output"),
    ):
        if params.get(src) is not None and dst not in out:
            try:
                out[dst] = float(params[src])
            except (TypeError, ValueError):
                pass

    return out


# --------------------------------------------------------------------------- #
# 顶层入口
# --------------------------------------------------------------------------- #


@dataclass
class ParsedLog:
    """一份日志的解析结果。"""

    text: str
    metrics: dict[str, float] = field(default_factory=dict)
    text_meta: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    samples: list[Sample] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    block_found: bool = False
    parse_notes: list[str] = field(default_factory=list)

    @property
    def metrics_are_summary_only(self) -> bool:
        return not self.samples


def parse_bench_log(
    text: str,
    *,
    params: Mapping[str, Any] | None = None,
    samples: Sequence[Sample] | None = None,
    scan_fallback: bool = True,
) -> ParsedLog:
    """把一份 bench_serving 输出解析成结构化结果。

    ``params`` 用于补全（例如从场景定义带入 num_prompts，日志里可能没有回显）。
    ``samples`` 可以外部传入（已从 ``requests.jsonl`` 读好）。
    """
    block = find_result_block(text)
    notes: list[str] = []
    numbers: dict[str, float] = {}
    strings: dict[str, str] = {}

    if block.found:
        numbers, strings = parse_result_block(block)
        notes.append(f"结果块位于第 {block.start_line}-{block.end_line} 行")
    else:
        notes.append("未找到 'Serving Benchmark Result' 结果块")

    ns = parse_namespace(text)
    # 优先级：日志里的 Namespace 回显 > 调用方传入的场景参数。
    # 回显是进程**真正收到**的参数；若用场景定义覆盖它，一旦两者不一致
    # （例如日志是旧参数跑的），成功率等派生指标就会被算错。
    effective_params: dict[str, Any] = {}
    effective_params.update({k: v for k, v in (params or {}).items() if v is not None})
    effective_params.update(ns)

    if scan_fallback and (not block.found or not numbers):
        allowed = set(_known_metric_keys())
        extra = scan_metrics_anywhere(text, allowed=allowed)
        if extra:
            notes.append(f"兜底扫描补入 {len(extra)} 项指标")
            for key, value in extra.items():
                numbers.setdefault(key, value)

    samples = list(samples or [])
    metrics = add_derived_metrics(numbers, effective_params, samples)
    issues = detect_issues(text)

    if not block.found:
        issues.append(
            Issue(
                kind="parse",
                severity="error",
                message="日志中没有找到 Serving Benchmark Result 结果块（压测可能未跑完或进程被中断）",
            )
        )

    return ParsedLog(
        text=text,
        metrics=metrics,
        text_meta=strings,
        params=effective_params,
        samples=samples,
        issues=issues,
        block_found=block.found,
        parse_notes=notes,
    )


def _known_metric_keys() -> list[str]:
    from .models import METRIC_REGISTRY

    keys = list(METRIC_REGISTRY)
    for fam in ("ttft", "tpot", "itl", "e2e_latency"):
        for stat in ("mean", "median", "p90", "p95", "p99", "min", "max", "std"):
            keys.append(f"{stat}_{fam}_ms")
    return keys


def parse_bench_log_file(
    log_path: str | Path,
    *,
    params: Mapping[str, Any] | None = None,
    requests_path: str | Path | None = None,
    encoding: str = "utf-8",
) -> ParsedLog:
    """读取日志文件并按约定自动发现同名 ``.requests.jsonl``。"""
    p = Path(log_path)
    text = p.read_text(encoding=encoding, errors="replace")
    samples: list[Sample] = []
    if requests_path is not None:
        samples = load_samples(requests_path)
    else:
        guess = default_requests_path(p)
        if guess.exists():
            samples = load_samples(guess)
    return parse_bench_log(text, params=params, samples=samples)


def metric_spec(key: str) -> MetricSpec:
    return spec_for(key)
