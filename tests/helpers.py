"""测试公共工具：临时家目录、合成日志、构造 RunRecord。"""

from __future__ import annotations

import contextlib
import random
import shutil
import tempfile
from pathlib import Path
from typing import Iterator

from sglbench.models import Sample
from sglbench.util import Paths, write_jsonl

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples" / "logs"


@contextlib.contextmanager
def temp_home() -> Iterator[Paths]:
    """一次性的 SGLBENCH_HOME，测试结束自动清理。"""
    root = Path(tempfile.mkdtemp(prefix="sglbench-test-"))
    try:
        paths = Paths(home=root).ensure()
        yield paths
    finally:
        shutil.rmtree(root, ignore_errors=True)


def synthesize_samples(
    n: int = 60,
    *,
    ttft_mean: float = 84.0,
    tpot_mean: float = 7.7,
    output_len: int = 2048,
    input_len: int = 4096,
    seed: int = 20260101,
    failures: int = 0,
) -> list[Sample]:
    """生成与真实 bench_serving 结构一致的每请求明细。"""
    rng = random.Random(seed)
    samples: list[Sample] = []
    for i in range(n):
        failed = i >= (n - failures)
        ttft = rng.lognormvariate(0.0, 0.14) * ttft_mean
        tpot = rng.lognormvariate(0.0, 0.07) * tpot_mean
        out_len = output_len if not failed else max(1, int(output_len * 0.2))
        samples.append(
            Sample(
                request_id=str(i),
                input_len=input_len,
                output_len=out_len,
                ttft_ms=round(ttft, 2),
                tpot_ms=round(tpot, 2),
                itl_ms=round(tpot * rng.uniform(0.98, 1.02), 2),
                e2e_ms=round(ttft + tpot * (out_len - 1), 2),
                status="error" if failed else "ok",
                error="torch.OutOfMemoryError: CUDA out of memory" if failed else None,
            )
        )
    return samples


def render_log(
    samples: list[Sample],
    *,
    num_prompts: int,
    max_concurrency: int,
    random_input: int = 4096,
    random_output: int = 2048,
    request_rate: float = 1.0,
    model: str = "/workspace/data/GLM-5.2-W8A8",
    base_url: str = "http://10.66.1.232:8081",
    backend: str = "sglang-oai-chat",
    error_lines: list[str] | None = None,
) -> str:
    """按 bench_serving 的真实输出格式渲染一份日志（统计量由样本真实计算）。"""
    ok = [s for s in samples if s.ok]
    ttfts = sorted(s.ttft_ms for s in ok if s.ttft_ms is not None)
    tpots = sorted(s.tpot_ms for s in ok if s.tpot_ms is not None)
    e2es = sorted(s.e2e_ms for s in ok if s.e2e_ms is not None)
    itls = sorted(s.itl_ms for s in ok if s.itl_ms is not None)

    def q(values: list[float], pct: float) -> float:
        if not values:
            return 0.0
        if len(values) == 1:
            return values[0]
        pos = pct / 100 * (len(values) - 1)
        lo, hi = int(pos), min(len(values) - 1, int(pos) + 1)
        return values[lo] + (values[hi] - values[lo]) * (pos - lo)

    duration = (max(e2es) / 1000.0) * 1.05 + 1.0 if e2es else 1.0
    total_in = sum(s.input_len or 0 for s in ok)
    total_out = sum(s.output_len or 0 for s in ok)

    def line(label: str, value: str) -> str:
        return f"{label:<40}{value}"

    parts = [
        f"Namespace(backend='{backend}', base_url='{base_url}', model='{model}', tokenizer='{model}', "
        f"num_prompts={num_prompts}, max_concurrency={max_concurrency}, random_input={random_input}, "
        f"random_output={random_output}, random_range_ratio=1.0, request_rate={request_rate}, pd_separated=True)",
        "",
        f"100%|████████████████| {num_prompts}/{num_prompts} [01:23<00:00,  1.67s/it]",
        "",
    ]
    if error_lines:
        parts.extend(error_lines)
        parts.append("")
    parts += [
        "============ Serving Benchmark Result ============",
        line("Backend:", backend),
        line("Traffic request rate:", f"{request_rate:.2f}"),
        line("Max request concurrency:", f"{max_concurrency}"),
        line("Successful requests:", f"{len(ok)}"),
        line("Benchmark duration (s):", f"{duration:.2f}"),
        line("Total input tokens:", f"{total_in}"),
        line("Total generated tokens:", f"{total_out}"),
        line("Request throughput (req/s):", f"{len(ok) / duration:.2f}"),
        line("Input token throughput (tok/s):", f"{total_in / duration:.2f}"),
        line("Output token throughput (tok/s):", f"{total_out / duration:.2f}"),
        line("Total token throughput (tok/s):", f"{(total_in + total_out) / duration:.2f}"),
        line("Concurrency:", f"{len(ok) / duration:.2f}"),
        "----------------End-to-End Latency----------------",
        line("Mean E2E Latency (ms):", f"{sum(e2es) / len(e2es):.2f}" if e2es else "0.00"),
        line("Median E2E Latency (ms):", f"{q(e2es, 50):.2f}"),
        line("P90 E2E Latency (ms):", f"{q(e2es, 90):.2f}"),
        line("P95 E2E Latency (ms):", f"{q(e2es, 95):.2f}"),
        line("P99 E2E Latency (ms):", f"{q(e2es, 99):.2f}"),
        "---------------Time to First Token----------------",
        line("Mean TTFT (ms):", f"{sum(ttfts) / len(ttfts):.2f}" if ttfts else "0.00"),
        line("Median TTFT (ms):", f"{q(ttfts, 50):.2f}"),
        line("P90 TTFT (ms):", f"{q(ttfts, 90):.2f}"),
        line("P95 TTFT (ms):", f"{q(ttfts, 95):.2f}"),
        line("P99 TTFT (ms):", f"{q(ttfts, 99):.2f}"),
        "-----Time per Output Token (excl. 1st token)------",
        line("Mean TPOT (ms):", f"{sum(tpots) / len(tpots):.2f}" if tpots else "0.00"),
        line("Median TPOT (ms):", f"{q(tpots, 50):.2f}"),
        line("P90 TPOT (ms):", f"{q(tpots, 90):.2f}"),
        line("P95 TPOT (ms):", f"{q(tpots, 95):.2f}"),
        line("P99 TPOT (ms):", f"{q(tpots, 99):.2f}"),
        "---------------Inter-token Latency----------------",
        line("Mean ITL (ms):", f"{sum(itls) / len(itls):.2f}" if itls else "0.00"),
        line("Median ITL (ms):", f"{q(itls, 50):.2f}"),
        line("P90 ITL (ms):", f"{q(itls, 90):.2f}"),
        line("P95 ITL (ms):", f"{q(itls, 95):.2f}"),
        line("P99 ITL (ms):", f"{q(itls, 99):.2f}"),
        "==================================================",
        "",
    ]
    return "\n".join(parts)


def write_log_bundle(
    directory: Path,
    stem: str,
    samples: list[Sample],
    *,
    num_prompts: int,
    max_concurrency: int,
    **kwargs: object,
) -> tuple[Path, Path]:
    """写出一份 ``<stem>.log`` + ``<stem>.requests.jsonl``。"""
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / f"{stem}.log"
    req_path = directory / f"{stem}.requests.jsonl"
    log_path.write_text(
        render_log(samples, num_prompts=num_prompts, max_concurrency=max_concurrency, **kwargs),  # type: ignore[arg-type]
        encoding="utf-8",
        newline="\n",
    )
    write_jsonl(req_path, [s.to_row(stem, i) for i, s in enumerate(samples)])
    return log_path, req_path
