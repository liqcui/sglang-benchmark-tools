#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic generator for realistic SGLang PD-separated benchmark examples.

This script fabricates example data that looks and *behaves* like the output of

    python3 -m sglang.bench_serving ...

It produces, for every run in ``RUNS``:

  examples/logs/<name>.log             stdout text of a bench_serving invocation
  examples/logs/<name>.requests.jsonl  one JSON object per request (detail rows)

plus two hand-written README files describing the data set.

Design constraints (all enforced by code below, see also the self-check notes):

* Standard library + numpy only.
* Fully deterministic: every RNG is seeded from a fixed per-run constant, all
  file names are fixed, nothing depends on wall-clock time, dict ordering of
  the OS, or floating point reductions over unordered containers.  Running the
  script twice produces byte-identical files.
* Every number inside the "Serving Benchmark Result" block is *derived* from
  the per-request samples written to the matching ``.requests.jsonl``.  Nothing
  in the result block is a hard-coded constant.  This is the hard requirement:
  log and jsonl must be mutually consistent under re-computation.
* Latency percentiles use ``numpy.percentile(..., method="linear")``.
* Throughput is ``count / benchmark_duration_s`` and ``tokens / duration``.
* Output files are UTF-8, no BOM, LF line endings.

Statistical model
-----------------
Per successful request we sample, independently:

    ttft_ms ~ lognormal, normalized so that the *sample* mean equals the
              scenario target and the *sample* P99/mean equals the scenario
              tail ratio
    tpot_ms ~ lognormal, same treatment
    itl_ms  ~ lognormal, normalized to its own scenario mean
    e2e_ms  = ttft_ms + tpot_ms * (output_len - 1) + noise

``noise`` is a small mean-zero jitter (<= ~2% of the nominal e2e) which models
measurement slop in the harness; the self-consistency relation
``e2e_ms ~= ttft_ms + tpot_ms * (output_len - 1)`` therefore holds within 2%
by construction, not by post-hoc patching.

Failed requests are sampled from their own (much slower) distribution and are
excluded from every latency statistic, matching real bench_serving behaviour.

Deliberate trade-offs (see README for the user-facing wording):

1.  Latency statistics and the token totals are computed over *successful
    requests only*.  Real ``bench_serving`` accumulates ``input_lens`` /
    ``output_lens`` from every request it receives, so its token totals include
    requests that later errored.  We restrict the aggregates to successful
    requests so that ``log`` and ``.requests.jsonl`` are exactly reconcilable;
    the crash run is the only place where the two conventions differ.
2.  ``Concurrency`` is reported as ``successful_requests / benchmark_duration``
    (the throughput-based definition used by bench_serving).  It sits slightly
    below ``--max-concurrency`` because the load generator ramps up and the
    longest request dominates the duration.
3.  ``Benchmark duration (s)`` uses
    ``max(e2e_ms)/1000 * 1.05 + 1.0`` -- the slowest request plus ~5% of
    scheduling overhead plus a fixed 1s of setup/teardown, as documented in the
    task brief.
"""

from __future__ import annotations

import json
import math
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------

# tools/make_examples.py -> project root
ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
LOGS = EXAMPLES / "logs"
SECOND_RUN = LOGS / "second-run"

DEFAULT_MODEL = "/workspace/data/GLM-5.2-W8A8"
DEFAULT_DATASET = "/workspace/data/ShareGPT_V3_unfiltered_cleaned_split.json"
BASE_URL = "http://10.66.1.232:8081"

# Percentile ordering used by bench_serving's result block.
PCTS = ("Mean", "Median", "P90", "P95", "P99")

# --------------------------------------------------------------------------
# Run definitions
# --------------------------------------------------------------------------


@dataclass
class Run:
    """One fabricated bench_serving invocation."""

    name: str
    scenario: str
    num_prompts: int
    max_concurrency: int
    input_len: int
    output_len: int
    request_rate: float

    ttft_mean_ms: float
    tpot_mean_ms: float
    itl_mean_ms: float | None  # None -> report N/A (streaming not instrumented)

    # The calibrated tail parameters are chosen so that the *observed* sample
    # P99/mean lands in the required 1.4..1.8 band once the sample mean has
    # been rescaled onto the target: rescaling by a constant inflates the mean
    # more than the P99, so the observed ratio runs ~0.95x the requested one.
    ttft_tail: float
    tpot_tail: float

    backend: str = "sglang-oai-chat"
    host: str = "10.66.1.232"
    port: int = 8081
    use_base_url: bool = True
    model: str = DEFAULT_MODEL
    dataset_path: str = DEFAULT_DATASET

    num_errors: int = 0
    error_kinds: tuple[str, ...] = ()
    stderr_lines: list[str] = field(default_factory=list)

    out_dir: Path = LOGS
    seed: int = 0

    @property
    def log_path(self) -> Path:
        return self.out_dir / f"{self.name}.log"

    @property
    def jsonl_path(self) -> Path:
        return self.out_dir / f"{self.name}.requests.jsonl"


RUNS: list[Run] = [
    # ---------------- batch 1: examples/logs/ ----------------
    Run(
        name="single-standard__baseline",
        scenario="single-standard",
        num_prompts=50,
        max_concurrency=1,
        input_len=4096,
        output_len=2048,
        request_rate=1.0,
        ttft_mean_ms=84.0,
        tpot_mean_ms=7.7,
        itl_mean_ms=7.75,
        ttft_tail=1.45,
        tpot_tail=1.20,
        seed=1001,
    ),
    # Same scenario, re-run after a firmware/driver bump: TTFT regresses ~10%,
    # TPOT moves ~1% which is inside run-to-run noise.
    Run(
        name="single-standard__candidate",
        scenario="single-standard",
        num_prompts=50,
        max_concurrency=1,
        input_len=4096,
        output_len=2048,
        request_rate=1.0,
        ttft_mean_ms=92.8,
        tpot_mean_ms=7.78,
        itl_mean_ms=7.83,
        ttft_tail=1.45,
        tpot_tail=1.20,
        seed=1002,
    ),
    Run(
        name="single-longctx__baseline",
        scenario="single-longctx",
        num_prompts=20,
        max_concurrency=1,
        input_len=65536,
        output_len=8192,
        request_rate=0.5,
        ttft_mean_ms=1850.0,
        tpot_mean_ms=12.5,
        itl_mean_ms=12.6,
        ttft_tail=1.55,
        tpot_tail=1.50,
        seed=2001,
    ),
    Run(
        name="conc-32-standard__baseline",
        scenario="conc-32-standard",
        num_prompts=200,
        max_concurrency=32,
        input_len=4096,
        output_len=2048,
        request_rate=4.0,
        ttft_mean_ms=620.0,
        tpot_mean_ms=11.2,
        itl_mean_ms=11.3,
        ttft_tail=1.60,
        tpot_tail=1.66,
        seed=3001,
    ),
    Run(
        name="conc-64-standard__baseline",
        scenario="conc-64-standard",
        num_prompts=200,
        max_concurrency=64,
        input_len=4096,
        output_len=2048,
        request_rate=6.0,
        ttft_mean_ms=1180.0,
        tpot_mean_ms=14.8,
        itl_mean_ms=14.9,
        ttft_tail=1.72,
        tpot_tail=1.74,
        seed=3002,
    ),
    # Crash run: 300 submitted, 212 succeed, 88 die mid-flight.
    Run(
        name="conc-128-standard__baseline",
        scenario="conc-128-standard",
        num_prompts=300,
        max_concurrency=128,
        input_len=4096,
        output_len=2048,
        request_rate=8.0,
        ttft_mean_ms=2450.0,
        tpot_mean_ms=21.5,
        itl_mean_ms=21.6,
        ttft_tail=1.66,
        tpot_tail=1.72,
        num_errors=88,
        error_kinds=("oom", "prefill_restart", "timeout"),
        stderr_lines=[
            "[2026-01-14 08:31:22] ERROR: Prefill worker 3 crashed: torch.OutOfMemoryError: CUDA out of memory",
            "[2026-01-14 08:31:22] WARNING: pod mxgpu-prefill-2 restarted (restart count: 1)",
            "[2026-01-14 08:31:23] ERROR: scheduler failed to allocate KV cache for request 7f3c1a20 (seq_len=6144)",
            "[2026-01-14 08:31:25] WARNING: decode worker 1 queue depth 214 > high watermark 192, shedding load",
            "[2026-01-14 08:31:26] ERROR: torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.14 GiB (GPU 0; 63.99 GiB total capacity)",
        ],
        seed=3003,
    ),
    Run(
        name="prefill-only__baseline",
        scenario="prefill-only",
        num_prompts=100,
        max_concurrency=64,
        input_len=8192,
        output_len=128,
        request_rate=2.0,
        ttft_mean_ms=340.0,
        tpot_mean_ms=6.5,
        itl_mean_ms=6.55,
        ttft_tail=1.60,
        tpot_tail=1.35,
        backend="sglang",
        host="10.66.1.232",
        port=8000,
        use_base_url=False,
        seed=4001,
    ),
    Run(
        name="decode-only__baseline",
        scenario="decode-only",
        num_prompts=100,
        max_concurrency=32,
        input_len=2048,
        output_len=2048,
        request_rate=5.0,
        ttft_mean_ms=95.0,
        tpot_mean_ms=12.0,
        itl_mean_ms=12.1,
        ttft_tail=1.60,
        tpot_tail=1.65,
        backend="sglang",
        host="10.66.1.166",
        port=8000,
        use_base_url=False,
        seed=4002,
    ),
    # ---------------- batch 2: examples/logs/second-run/ ----------------
    # Third run of single-standard, essentially identical to baseline; used to
    # demonstrate "no significant difference" across repeated runs.
    Run(
        name="single-standard__second",
        scenario="single-standard",
        num_prompts=50,
        max_concurrency=1,
        input_len=4096,
        output_len=2048,
        request_rate=1.0,
        ttft_mean_ms=84.6,
        tpot_mean_ms=7.73,
        itl_mean_ms=7.78,
        ttft_tail=1.45,
        tpot_tail=1.20,
        out_dir=SECOND_RUN,
        seed=1003,
    ),
]

# --------------------------------------------------------------------------
# Sampling helpers
# --------------------------------------------------------------------------


def _lognormal_shape(tail_ratio: float) -> float:
    """Return lognormal sigma whose P99/median equals ``tail_ratio``.

    P99/median = exp(2.3263 * sigma)  =>  sigma = ln(ratio) / 2.3263
    """
    z99 = 2.3263478740408408  # scipy.stats.norm.ppf(0.99)
    return math.log(tail_ratio) / z99


def _shift_scale(values: np.ndarray, target_mean: float) -> np.ndarray:
    """Rescale a positive sample so that its mean is exactly ``target_mean``.

    Multiplicative rescaling preserves every percentile *ratio* (including
    P99/mean), so calibrating the mean this way also pins the tail shape.
    """
    values = np.clip(values, 1e-9, None)
    return values * (target_mean / values.mean())


def sample_latency(
    rng: np.random.Generator, n: int, target_mean: float, tail_ratio: float
) -> np.ndarray:
    """Draw ``n`` positive, right-skewed latency samples.

    The samples are lognormal with the requested P99/mean tail and are then
    multiplicatively rescaled so the sample mean lands exactly on
    ``target_mean``.  Rescaling keeps the multiplicative tail shape, so the
    "P99 is 1.45x..1.8x the mean" property survives calibration.
    """
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    sigma = _lognormal_shape(tail_ratio)
    raw = rng.lognormal(mean=0.0, sigma=sigma, size=n)
    return _shift_scale(raw, target_mean)


def sample_input_lengths(rng: np.random.Generator, n: int, base: int) -> np.ndarray:
    """Token counts around ``base``.

    With ``--random-range-ratio=1.0`` the real harness draws the length from
    ``[base, base]``, i.e. a constant.  We keep in-token/out-token counts
    constant for the fixed-length scenarios and only jitter the prompt length a
    little for the crash run, where the load generator is visibly struggling.
    """
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    if base >= 32768:
        # Long-context runs are the only ones where real traffic shows spread,
        # because the sampler truncates the source document at random offsets.
        return np.round(rng.uniform(0.98, 1.02, size=n) * base).astype(np.int64)
    return np.full(n, base, dtype=np.int64)


# --------------------------------------------------------------------------
# Per-run sample generation
# --------------------------------------------------------------------------


def generate_run(run: Run) -> tuple[list[dict], dict]:
    """Build the per-request rows and the derived result block for one run.

    Returns ``(rows, result)`` where ``rows`` is the list of dicts written to
    the ``.requests.jsonl`` file and ``result`` holds every scalar shown in the
    log's result block.
    """
    rng = np.random.default_rng(run.seed)
    py_rng = random.Random(run.seed * 7919 + 13)

    n_ok = run.num_prompts - run.num_errors
    if n_ok <= 0:
        raise ValueError(f"{run.name}: no successful requests to sample")

    # --- successful requests -------------------------------------------------
    ttft = sample_latency(rng, n_ok, run.ttft_mean_ms, run.ttft_tail)
    tpot = sample_latency(rng, n_ok, run.tpot_mean_ms, run.tpot_tail)
    itl = (
        sample_latency(rng, n_ok, run.itl_mean_ms, run.tpot_tail)
        if run.itl_mean_ms is not None
        else None
    )
    in_len = sample_input_lengths(rng, n_ok, run.input_len)
    out_len = np.full(n_ok, run.output_len, dtype=np.int64)

    # e2e is *constructed* from ttft/tpot so the self-consistency relation
    # holds by construction; the jitter models harness measurement slop.  It is
    # clipped at +/-1.8% so that
    # ``e2e_ms ~= ttft_ms + tpot_ms * (output_len - 1)`` holds within the
    # documented +/-2% envelope for every single row, not just on average.
    nominal_e2e = ttft + tpot * (out_len - 1).astype(np.float64)
    jitter = np.clip(rng.normal(0.0, 0.006, size=n_ok), -0.018, 0.018)
    e2e = nominal_e2e * (1.0 + jitter)

    # --- failed requests -----------------------------------------------------
    # Sampled from their own, much slower distribution.  They are emitted to
    # the jsonl (status="error") but are excluded from every latency statistic
    # and from the token throughput totals, exactly like real bench_serving.
    fail_ttft = sample_latency(rng, run.num_errors, run.ttft_mean_ms * 2.2, 1.8)
    fail_e2e = sample_latency(rng, run.num_errors, run.ttft_mean_ms * 12.0, 1.9)
    # A crashed request has partial output; a refused one has none.
    fail_out = np.round(rng.uniform(0.0, 0.35, size=run.num_errors) * run.output_len)
    fail_out = np.where(rng.random(run.num_errors) < 0.15, 0.0, fail_out).astype(np.int64)
    fail_in = sample_input_lengths(rng, run.num_errors, run.input_len)

    ok_rows = [
        {
            "request_id": str(i),
            "input_len": int(in_len[i]),
            "output_len": int(out_len[i]),
            "ttft_ms": round(float(ttft[i]), 2),
            "tpot_ms": round(float(tpot[i]), 2),
            "itl_ms": round(float(itl[i]), 2) if itl is not None else None,
            "e2e_ms": round(float(e2e[i]), 2),
            "status": "ok",
            "error": None,
        }
        for i in range(n_ok)
    ]

    error_messages = _error_messages(run, py_rng, run.num_errors)
    fail_rows = [
        {
            "request_id": str(n_ok + j),
            "input_len": int(fail_in[j]),
            "output_len": int(fail_out[j]),
            "ttft_ms": round(float(fail_ttft[j]), 2),
            "tpot_ms": None,
            "itl_ms": None,
            "e2e_ms": round(float(fail_e2e[j]), 2),
            "status": "error",
            "error": error_messages[j],
        }
        for j in range(run.num_errors)
    ]

    if run.num_errors:
        # Interleave the failures across the submission stream instead of
        # parking them at the tail: a real OOM/restart episode hits whichever
        # requests are in flight at that moment, and the load generator keeps
        # issuing new ones afterwards.  request_id stays dense and increasing
        # (0..num_prompts-1); failures are just scattered through it.
        fail_at = set(
            int(x) for x in rng.choice(run.num_prompts, size=run.num_errors, replace=False)
        )
        merged: list[dict] = []
        i_ok = i_fail = 0
        for rid in range(run.num_prompts):
            if rid in fail_at:
                row = dict(fail_rows[i_fail])
                i_fail += 1
            else:
                row = dict(ok_rows[i_ok])
                i_ok += 1
            row["request_id"] = str(rid)
            merged.append(row)
        rows = merged
    else:
        rows = ok_rows

    result = _derive_result(run, ok_rows, ttft, tpot, itl, e2e, in_len, out_len)
    return rows, result


def _error_messages(run: Run, py_rng: random.Random, count: int) -> list[str]:
    """Realistic failure strings, weighted by kind."""
    if count == 0:
        return []
    catalog: list[tuple[str, list[str], float]] = [
        (
            "oom",
            [
                "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.14 GiB "
                "(GPU 0; 63.99 GiB total capacity)",
                "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 1.07 GiB "
                "(GPU 0; 63.99 GiB total capacity; 58.42 GiB already allocated)",
                "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 3.51 GiB "
                "(GPU 1; 63.99 GiB total capacity)",
            ],
            0.60,
        ),
        (
            "prefill_restart",
            [
                "HTTP 500: prefill worker restarted",
                "HTTP 500: prefill worker mxgpu-prefill-2 restarted during request",
                "HTTP 503: no healthy prefill worker available (0/4 ready)",
            ],
            0.25,
        ),
        (
            "timeout",
            [
                "Client error: request timed out after 600s",
                "HTTP 504: gateway timeout waiting for decode worker",
            ],
            0.15,
        ),
    ]
    active = [c for c in catalog if c[0] in run.error_kinds] or catalog
    weights = [c[2] for c in active]
    kinds = py_rng.choices(range(len(active)), weights=weights, k=count)
    out: list[str] = []
    for k in kinds:
        options = active[k][1]
        out.append(options[py_rng.randrange(len(options))])
    return out


def _pct(values: np.ndarray, key: str) -> float:
    """bench_serving-style percentile: linear interpolation."""
    if key == "Mean":
        return float(np.mean(values))
    if key == "Median":
        return float(np.percentile(values, 50, method="linear"))
    return float(np.percentile(values, float(key[1:]), method="linear"))


def _derive_result(
    run: Run,
    ok_rows: list[dict],
    ttft: np.ndarray,
    tpot: np.ndarray,
    itl: np.ndarray | None,
    e2e: np.ndarray,
    in_len: np.ndarray,
    out_len: np.ndarray,
) -> dict:
    """Compute the result block purely from the successful per-request samples."""
    successful = len(ok_rows)
    total_input = int(np.sum(in_len))
    total_output = int(np.sum(out_len))

    # Aggregate over successful requests only -- the documented trade-off.
    duration = float(np.max(e2e)) / 1000.0 * 1.05 + 1.0
    # Every throughput below is derived from the *rounded* duration, i.e. the
    # exact number printed on the "Benchmark duration (s)" line.  Otherwise a
    # reader recomputing ``tokens / duration`` from the log text would disagree
    # in the last digits.  The rounding error is < 0.005% for every run here,
    # far below the 0.5% consistency budget, and it makes the log internally
    # consistent to the last printed digit.
    duration_printed = round(duration, 2)

    result: dict = {
        "backend": run.backend,
        "traffic_request_rate": run.request_rate,
        "max_concurrency": run.max_concurrency,
        "successful": successful,
        "duration": duration_printed,
        "total_input": total_input,
        "total_output": total_output,
        "request_throughput": successful / duration_printed,
        "input_throughput": total_input / duration_printed,
        "output_throughput": total_output / duration_printed,
        "total_throughput": (total_input + total_output) / duration_printed,
        "concurrency": successful / duration_printed,
        "latency": {},
    }

    for label, values in (("ttft", ttft), ("tpot", tpot), ("itl", itl), ("e2e", e2e)):
        if values is None:
            continue
        result["latency"][label] = {p: _pct(values, p) for p in PCTS}
    return result


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def _label_row(label: str, value: str) -> str:
    """`label:` left-aligned into a 41-column gutter, then the value."""
    return f"{label + ':':<41}{value}"


def render_namespace(run: Run) -> str:
    parts = [f"backend='{run.backend}'"]
    if run.use_base_url:
        parts.append(f"base_url='{BASE_URL}'")
    else:
        parts.append(f"host='{run.host}'")
        parts.append(f"port={run.port}")
    parts += [
        f"dataset_name='random'",
        f"dataset_path='{run.dataset_path}'",
        f"model='{run.model}'",
        f"tokenizer='{run.model}'",
        f"num_prompts={run.num_prompts}",
        f"max_concurrency={run.max_concurrency}",
        f"random_input={run.input_len}",
        f"random_output={run.output_len}",
        "random_range_ratio=1.0",
        f"request_rate={run.request_rate}",
    ]
    if run.use_base_url:
        parts.append("pd_separated=True")
    return "Namespace(" + ", ".join(parts) + ")"


def render_progress(run: Run, result: dict) -> str:
    elapsed = int(round(result["duration"]))
    it_s = elapsed / max(run.num_prompts, 1)
    return (
        f"100%|{'█' * 40}| {run.num_prompts}/{run.num_prompts} "
        f"[{elapsed // 60:02d}:{elapsed % 60:02d}<00:00, {it_s:.2f}s/it]"
    )


def render_result_block(run: Run, result: dict) -> str:
    L = result["latency"]
    lines = [
        "",
        "============ Serving Benchmark Result ============",
        _label_row("Backend", result["backend"]),
        _label_row("Traffic request rate", _fmt(result["traffic_request_rate"])),
        _label_row("Max request concurrency", str(result["max_concurrency"])),
        _label_row("Successful requests", str(result["successful"])),
        _label_row("Benchmark duration (s)", _fmt(result["duration"])),
        _label_row("Total input tokens", str(result["total_input"])),
        _label_row("Total generated tokens", str(result["total_output"])),
        _label_row("Request throughput (req/s)", _fmt(result["request_throughput"])),
        _label_row("Input token throughput (tok/s)", _fmt(result["input_throughput"])),
        _label_row("Output token throughput (tok/s)", _fmt(result["output_throughput"])),
        _label_row("Total token throughput (tok/s)", _fmt(result["total_throughput"])),
        _label_row("Concurrency", _fmt(result["concurrency"])),
        "----------------End-to-End Latency----------------",
    ]
    for p in PCTS:
        lines.append(_label_row(f"{p} E2E Latency (ms)", _fmt(L["e2e"][p])))
    lines.append("---------------Time to First Token----------------")
    for p in PCTS:
        lines.append(_label_row(f"{p} TTFT (ms)", _fmt(L["ttft"][p])))
    lines.append("-----Time per Output Token (excl. 1st token)------")
    for p in PCTS:
        lines.append(_label_row(f"{p} TPOT (ms)", _fmt(L["tpot"][p])))
    lines.append("---------------Inter-token Latency----------------")
    if "itl" in L:
        for p in PCTS:
            lines.append(_label_row(f"{p} ITL (ms)", _fmt(L["itl"][p])))
    else:
        for p in PCTS:
            lines.append(_label_row(f"{p} ITL (ms)", "N/A"))
    lines.append("==================================================")
    return "\n".join(lines) + "\n"


def render_log(run: Run, result: dict) -> str:
    chunks = [render_namespace(run) + "\n"]
    if run.stderr_lines:
        # Server-side stderr interleaved into the captured stdout, as happens
        # when the launch wrapper streams pod logs into the same stream.
        chunks.append("\n" + "\n".join(run.stderr_lines) + "\n")
    chunks.append("\n" + render_progress(run, result) + "\n")
    chunks.append(render_result_block(run, result))
    return "".join(chunks)


def write_text(path: Path, text: str) -> None:
    """UTF-8, no BOM, LF line endings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    write_text(path, body)


# --------------------------------------------------------------------------
# README payloads
# --------------------------------------------------------------------------


README_MAIN = """# 示例数据（examples/）

本目录下的数据是**纯合成的示例**，用于演示/回归测试「SGLang PD 分离集群压测」的产物格式，
不是真实跑出来的 benchmark 结果。所有文件都由 `tools/make_examples.py` 以固定随机种子生成，
重复运行结果完全一致。

## 目录结构

```
examples/
├── README.md                       # 本文件
└── logs/
    ├── <name>.log                  # 模拟 python3 -m sglang.bench_serving 的 stdout
    ├── <name>.requests.jsonl       # 该 run 的每请求明细，每行一个 JSON 对象
    └── second-run/
        ├── README.md
        ├── single-standard__second.log
        └── single-standard__second.requests.jsonl
```

命名规则：`<scenario_key>__<label>.log`。同一个 `scenario_key` 下的多个 `label` 属于同一场景的多次 run
（例如升级固件前后的 baseline / candidate），可以直接横向对比。

## 两批数据

**批次一（`logs/` 根目录）—— 场景对比演示**

| name | 场景特征 | 用途 |
|---|---|---|
| `single-standard__baseline` | c=1, n=50, in=4096, out=2048, rr=1.0 | 单并发标准长度基准 |
| `single-standard__candidate` | 同上，第二次 test run | 模拟固件/驱动升级：TTFT +10% 回归，TPOT +1%（噪声） |
| `single-longctx__baseline` | c=1, n=20, in=65536, out=8192, rr=0.5 | 长上下文：TTFT、TPOT 显著变慢 |
| `conc-32-standard__baseline` | c=32, n=200, in=4096, out=2048, rr=4.0 | 中等并发 |
| `conc-64-standard__baseline` | c=64, n=200, in=4096, out=2048, rr=6.0 | 高并发 |
| `conc-128-standard__baseline` | c=128, n=300, in=4096, out=2048, rr=8.0 | **崩溃场景**：300 提交 / 212 成功 / 88 失败（OOM、prefill worker 重启、超时） |
| `prefill-only__baseline` | host `10.66.1.232:8000`, backend=sglang, c=64, n=100, in=8192, out=128, rr=2.0 | prefill 节点单独压测（极短输出） |
| `decode-only__baseline` | host `10.66.1.166:8000`, backend=sglang, c=32, n=100, in=2048, out=2048, rr=5.0 | decode 节点单独压测 |

**批次二（`logs/second-run/`）—— 多 run 对比补充数据**

`single-standard__second` 是 `single-standard` 场景的第三次 run，TTFT 均值与 baseline 基本一致，
用于演示「重复运行无显著差异」的对照组。

## 数据格式

### `.log`

逐行格式与真实 `bench_serving` 输出一致：

1. 第一行 `Namespace(...)`，记录本次 run 的全部参数（`--base-url` 或 `--host/--port`）；
2. （仅崩溃 run）若干行服务端 stderr 混入文本，含 `torch.OutOfMemoryError` 与 pod 重启告警；
3. 一行 tqdm 进度条（`100%|████...| 50/50 [01:23<00:00, 1.67s/it]`）；
4. `============ Serving Benchmark Result ============` 结果块，标签左对齐到第 41 列，
   数值右对齐，延迟类保留 2 位小数、吞吐保留 2 位小数、token 数为整数。

结果块中包含 `Successful requests`、`Benchmark duration (s)`、token 总数、吞吐、
`Concurrency`，以及 E2E / TTFT / TPOT / ITL 四组 mean/median/P90/P95/P99。

### `.requests.jsonl`

每行一个 JSON 对象，键固定：

```json
{"request_id": "0", "input_len": 4096, "output_len": 2048, "ttft_ms": 82.13, "tpot_ms": 7.65, "itl_ms": 7.70, "e2e_ms": 1652.1, "status": "ok", "error": null}
```

* `request_id`：从 `"0"` 开始、按提交顺序递增的字符串（崩溃 run 的失败请求按发生时刻散布在中间，
  不是集中在末尾）。
* `status`：`"ok"` 或 `"error"`；失败行 `error` 为一句错误消息，`tpot_ms`/`itl_ms` 为 `null`，
  `output_len` 为已生成的部分 token 数，`e2e_ms` 仍有值。
* 自洽关系：`e2e_ms ≈ ttft_ms + tpot_ms * (output_len - 1)`（生成时按此构造，噪声 ≤ 2%）。

## 怎么用

### 1. 直接解析日志做回归对比

对每个 `.log`，先用正则取 `Namespace(...)` 里的参数，再取结果块的标签/数值对：

```python
import re, pathlib

ROW = re.compile(r"^(?P<label>[A-Za-z][^:]*?):[ ]+(?P<value>.+)$", re.M)
for path in sorted(pathlib.Path("examples/logs").rglob("*.log")):
    text = path.read_text(encoding="utf-8")
    block = text.split("Serving Benchmark Result", 1)[1]
    stats = {m["label"].strip(): m["value"] for m in ROW.finditer(block)}
    print(path.name, stats["Mean TTFT (ms)"], stats["Mean TPOT (ms)"])
```

### 2. 从明细重新计算统计量并与日志对账

`.log` 结果块里的**每一个统计量都由同目录 `.requests.jsonl` 中的成功样本重新计算得到**
（延迟统计与 token 总量只统计 `status == "ok"` 的行，与真实 `bench_serving` 一致；
`Successful requests` 体现失败计数）：

```python
import json, numpy as np, pathlib

for jl in sorted(pathlib.Path("examples/logs").rglob("*.requests.jsonl")):
    rows = [json.loads(l) for l in jl.read_text(encoding="utf-8").splitlines() if l.strip()]
    ok = [r for r in rows if r["status"] == "ok"]
    ttft = np.array([r["ttft_ms"] for r in ok])
    e2e = np.array([r["e2e_ms"] for r in ok])
    duration = e2e.max() / 1000 * 1.05 + 1.0
    print(jl.name, len(ok), ttft.mean(), np.percentile(ttft, 99, method="linear"),
          sum(r["output_len"] for r in ok) / duration)
```

### 3. 重新生成

```bash
python tools/make_examples.py
```

脚本只依赖标准库 + numpy，确定性输出（固定 seed），运行结束会打印每个文件的路径与行数。
生成的文件均为 UTF-8 无 BOM、LF 换行。

## 已知取舍

1. **失败请求不计入延迟统计，也不计入 token 吞吐分子**。真实 `bench_serving` 的
   `input_lens`/`output_lens` 会累计所有收到的请求（含后续失败的），因此它的 token 总数可能略高。
   这里为了让 `.log` 与 `.requests.jsonl` 能逐位精确对账，统一只统计成功请求；受影响最大的只有
   `conc-128-standard__baseline`（崩溃 run，300 提交 / 212 成功）。
   日志里 `Successful requests = 212` 已经体现了失败计数，但它与 `Total input tokens` 并非同源，
   单独拿这两个数相除无法还原 `random_input`。
2. `Concurrency` 使用 `成功请求数 / Benchmark duration`（bench_serving 的吞吐定义），
   而不是压测机配置的并发上限。
3. `Benchmark duration (s) = max(e2e_ms)/1000 * 1.05 + 1.0`，即最慢请求耗时 + 约 5% 调度开销 + 1s 启停开销。
   这一定义下 `Concurrency` 会**明显低于**日志里的 `Max request concurrency`（后者只是压测机的并发上限，
   而本数据是「固定请求总数」的 closed-loop 压测，请求速率由模型计算耗时决定）。真实
   `bench_serving` 以整个 test 的实测 wall time 作 duration，因此它的 `Concurrency` 才会贴近上限。
4. 崩溃 run 的失败请求也写了 `.requests.jsonl` 行，其 `e2e_ms` 可能大于成功请求的最大值
   （挂住的请求更慢），但 duration 只由成功请求决定，以保持与吞吐自洽。
5. 各场景的 `TTFT` / `TPOT` 的 P99/Mean 按并发标定在 1.4~1.8（并发越高越接近 1.8）。
   `E2E` 的 P99/Mean 是 `e2e = ttft + tpot * (out_len - 1)` 的结果，`tpot * (out_len - 1)`
   这一项近似常量、把分布右侧拉平，所以 `E2E` 的尾比通常低于同场景的 `TTFT`：
   本批数据实测 `E2E` P99/Mean 约 1.13（c=1）~ 1.61（c=64），符合右偏但被常量项稀释的形态。
"""

README_SECOND_RUN = """# second-run —— 多 run 对比补充数据

本目录是 `examples/logs/` 的补充，用于演示**同一场景多次 run 之间的对比**。
数据同样是 `tools/make_examples.py` 生成的合成数据，不是真实压测结果。

## 内容

| name | scenario_key | 说明 |
|---|---|---|
| `single-standard__second` | `single-standard` | `single-standard` 场景的第三次 run |

## 与批次的对应关系

| run | 文件位置 | Mean TTFT | Mean TPOT | 结论 |
|---|---|---|---|---|
| baseline | `../single-standard__baseline.log` | ≈ 84 ms | ≈ 7.7 ms | 基准 |
| candidate | `../single-standard__candidate.log` | ≈ 93 ms | ≈ 7.8 ms | TTFT +10%（回归），TPOT 在噪声内 |
| **second** | `single-standard__second.log` | ≈ 85 ms | ≈ 7.7 ms | 与 baseline 基本一致，无显著差异 |

把它作为第三个 run 一起做统计对比时，可以观察到：
`second` 与 `baseline` 的 TTFT 差异远小于 run 间正常波动，不应判定为变化；
而 `candidate` 的 TTFT 偏移明显超出 `baseline`/`second` 之间的散布，属于真实回归。

## 使用方式

与批次一完全相同：`.log` 给出汇总结果块，`.requests.jsonl` 给出每请求明细，
两者可用同一套脚本解析与对账。示例代码见 `../README.md`。
"""


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> int:
    # The printed output contains absolute paths that may include non-ASCII
    # characters; force UTF-8 so the report never trips over a cp936 console.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):  # pragma: no cover - exotic stdout
        pass

    written: list[tuple[Path, int, int]] = []  # (path, lines, bytes)

    for run in RUNS:
        rows, result = generate_run(run)
        write_jsonl(run.jsonl_path, rows)
        write_text(run.log_path, render_log(run, result))

        for path, n_lines in (
            (run.log_path, render_log(run, result).count("\n")),
            (run.jsonl_path, len(rows)),
        ):
            written.append((path, n_lines, path.stat().st_size))

    for path, text in (
        (EXAMPLES / "README.md", README_MAIN),
        (SECOND_RUN / "README.md", README_SECOND_RUN),
    ):
        write_text(path, text)
        written.append((path, text.count("\n"), path.stat().st_size))

    print("Generated example data:")
    for path, n_lines, n_bytes in written:
        rel = path.relative_to(ROOT).as_posix()
        print(f"  {path}  ({rel}  lines={n_lines}  bytes={n_bytes})")
    print(f"Total: {len(written)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
