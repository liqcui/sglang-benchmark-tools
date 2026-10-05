"""HiCache 多轮对话专项基准（对齐 SGLang 官方 ``benchmark/hicache/bench_multiturn.py`` 思路）。

场景：N 个并发客户端 × R 轮对话。
- 第 0 轮：请求 = 随机 base prompt（~request_length tokens）
- 第 k 轮（k≥1）：请求 = 上一轮输入 + 上一轮输出 拼接（模拟真实多轮对话，
  制造共享前缀 → 驱动 HiCache L2 cache-hit）

测量（每请求）：
- TTFT（首 token 延迟，prefill 指标）
- ``usage.cached_tokens``（缓存命中 token 数，SGLang 报告时）
- 轮次 / 客户端

归档：与 ``sglbench run`` 相同的 run 布局（runs/<run_id>/ + DuckDB），
scenario_key 来自场景目录（hicache-multiturn-*），可直接用
``sglbench compare --scenario hicache-multiturn-20x10 --last 2`` 对比 l2-off/l2-on。

用法::

    sglbench multiturn --scenario hicache-multiturn-20x10 \
        [--clients N] [--rounds R] [--request-length L] [--output-length O] \
        [--request-rate RATE] [--label ...] [--exec docker|kubectl] [--env-file ...]
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import requests

from .env import capture_env
from .models import Issue, RunRecord, Sample, Scenario
from .runner import collect_meta, run_dir_for
from .util import Paths, iso, new_run_id, utcnow, write_json, write_jsonl

# 每请求默认超时（秒）
DEFAULT_TIMEOUT_S = 600.0


@dataclass
class Turn:
    client: int
    round: int
    ok: bool
    ttft_ms: float | None = None
    total_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    http: int = 0
    error: str = ""


@dataclass
class MultiTurnResult:
    run_id: str
    scenario_key: str
    label: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    turns: list[Turn] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    command: str = ""
    notes: str = ""

    def samples(self) -> list[Sample]:
        return [
            Sample(
                request_id=f"c{tr.client:03d}-r{tr.round:02d}",
                input_len=tr.prompt_tokens or 0,
                output_len=tr.completion_tokens or 0,
                ttft_ms=tr.ttft_ms,
                tpot_ms=(tr.total_ms - tr.ttft_ms) if (tr.total_ms and tr.ttft_ms) else None,
                itl_ms=None,
                e2e_ms=tr.total_ms,
                status="ok" if tr.ok else "failed",
                error=tr.error or None,
            )
            for tr in self.turns
        ]


def _build_prompt(client: int, round_no: int, prev_input: str, prev_output: str, request_length: int) -> str:
    """多轮对话 prompt 构造。

    round 0: 固定 seed 的重复文本（可复现）；round≥1: 上一轮输入+输出 拼接补足长度。
    """
    base = "The quick brown fox jumps over the lazy dog. " * 6
    if round_no == 0:
        # 用客户端 seed 生成可复现的初始 prompt（长度约 request_length tokens）
        rng = random.Random(1000 + client)
        words = base.split()
        seq = [words[rng.randrange(len(words))] for _ in range(max(8, request_length // 4))]
        return " ".join(seq)
    # 延续对话：输入 = 上一轮输入 + 上一轮输出
    return f"{prev_input}\n{prev_output}"


def _token_estimate(text: str) -> int:
    """粗略 token 估算（bench 侧无 tokenizer 时用；精确值以 usage.prompt_tokens 为准）。"""
    return max(1, len(text) // 3)


def _do_call(
    url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    timeout: float,
) -> tuple[dict[str, Any], float]:
    t0 = time.time()
    resp = requests.post(
        url + "/v1/completions",
        json={"model": model, "prompt": prompt, "max_tokens": max_tokens, "temperature": 0},
        timeout=timeout,
    )
    dt = time.time() - t0
    return resp.json(), dt


def _run_client(
    client: int,
    url: str,
    model: str,
    rounds: int,
    request_length: int,
    max_tokens: int,
    timeout: float,
    results: list[Turn],
    lock: threading.Lock,
    barrier: "threading.Barrier | None",
    request_rate: float,
    sem: "threading.Semaphore | None" = None,
) -> None:
    """单个客户端的完整多轮对话（轮间栅栏保证逐轮对齐；max_parallel 用信号量限并发）。"""
    prev_input = ""
    prev_output = ""
    for r in range(rounds):
        if sem is not None:
            sem.acquire()
        prompt = _build_prompt(client, r, prev_input, prev_output, request_length)
        ttft: float | None = None
        total: float | None = None
        try:
            started = time.time()
            # 流式以获取 TTFT（首 token 时间）；SSE 首行即首 token
            with requests.post(
                url + "/v1/completions",
                json={
                    "model": model,
                    "prompt": prompt,
                    "max_tokens": max_tokens,
                    "temperature": 0,
                    "stream": True,
                },
                timeout=timeout,
                stream=True,
            ) as resp:
                text_parts: list[str] = []
                cached: int | None = None
                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    if ttft is None:
                        ttft = (time.time() - started) * 1000.0
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if choices and choices[0].get("delta", {}).get("content"):
                        text_parts.append(choices[0]["delta"]["content"])
                    usage = chunk.get("usage") or {}
                    if usage.get("cached_tokens") is not None:
                        cached = int(usage.get("cached_tokens", 0))
                total = (time.time() - started) * 1000.0
                completed = "".join(text_parts)
                prev_input = prompt
                prev_output = completed
                turn = Turn(
                    client=client, round=r, ok=resp.status_code == 200,
                    ttft_ms=ttft, total_ms=total,
                    prompt_tokens=_token_estimate(prompt),
                    completion_tokens=_token_estimate(completed),
                    cached_tokens=cached,
                    http=resp.status_code,
                )
        except Exception as exc:  # noqa: BLE001
            turn = Turn(client=client, round=r, ok=False, error=f"{type(exc).__name__}: {exc}")
        with lock:
            results.append(turn)
        if sem is not None:
            sem.release()
        if request_rate > 0:
            time.sleep(1.0 / request_rate)
        if barrier is not None:
            try:
                barrier.wait(timeout=timeout)
            except threading.BrokenBarrierError:
                pass


def run_multiturn(
    *,
    url: str,
    model: str,
    clients: int,
    rounds: int,
    request_length: int,
    output_length: int,
    request_rate: float,
    label: str,
    notes: str,
    scenario_key: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    env_file: str | Path | None = None,
    max_parallel: int | None = None,
) -> MultiTurnResult:
    """执行多轮对话基准并返回结果（不落盘）。"""
    started = utcnow()
    rid = new_run_id(scenario_key)
    cmd_text = (
        f"multiturn url={url} model={model} clients={clients} rounds={rounds} "
        f"request_length={request_length} output_length={output_length} request_rate={request_rate}"
    )
    max_tokens = max(1, int(output_length))

    results: list[Turn] = []
    lock = threading.Lock()
    # max_parallel>0 时用信号量限制并发活跃客户端（对齐官方 --max-parallel），
    # 此时不做轮次栅栏（非全部客户端同时活跃）；否则全部并发 + 轮栅栏。
    sem = threading.Semaphore(max_parallel) if max_parallel and max_parallel > 0 else None
    barrier = None if sem else (threading.Barrier(clients) if clients > 1 else None)
    threads = [
        threading.Thread(
            target=_run_client,
            args=(c, url, model, rounds, request_length, max_tokens, timeout_s,
                  results, lock, barrier, request_rate),
            kwargs={"sem": sem},
        )
        for c in range(clients)
    ]
    for t in threads:
        t.start()
        if request_rate > 0 and clients > 1:
            time.sleep(0.5 / request_rate)  # 客户端错峰启动
    for t in threads:
        t.join()

    finished = utcnow()
    ok_n = sum(1 for tr in results if tr.ok)
    status = "ok" if ok_n == len(results) and results else ("partial" if ok_n else "failed")

    # 逐轮统计
    metrics: dict[str, float] = {
        "success_rate": round(ok_n / len(results), 6) if results else 0.0,
        "total_requests": float(len(results)),
        "ok_requests": float(ok_n),
    }
    for r in range(rounds):
        ts = [tr for tr in results if tr.round == r and tr.ok and tr.ttft_ms is not None]
        if ts:
            vals = sorted(t.ttft_ms for t in ts)  # type: ignore[union-attr]
            metrics[f"round{r}_mean_ttft_ms"] = round(sum(vals) / len(vals), 3)
            metrics[f"round{r}_median_ttft_ms"] = round(vals[len(vals) // 2], 3)
        hits = [tr for tr in results if tr.round == r and tr.cached_tokens and tr.cached_tokens > 0]
        if ts:
            metrics[f"round{r}_cache_hit_rate"] = round(len(hits) / len(ts), 6)

    return MultiTurnResult(
        run_id=rid,
        scenario_key=scenario_key,
        label=label or scenario_key,
        status=status,
        started_at=started,
        finished_at=finished,
        turns=results,
        metrics=metrics,
        command=cmd_text,
        notes=notes,
    )


def archive_multiturn(
    paths: Paths,
    result: MultiTurnResult,
    *,
    store,
    env_file: str | Path | None = None,
    operator: str = "",
) -> str:
    """把多轮结果按 run 布局落盘 + 入库（含部署环境元数据）。"""
    rdir = run_dir_for(paths, result.run_id)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "command.txt").write_text(result.command + "\n", encoding="utf-8")

    meta = collect_meta(
        extra={
            "multiturn": True,
            "clients": len({t.client for t in result.turns}),
            "rounds": max((t.round for t in result.turns), default=0) + 1,
            "content_sha256": "",
        },
    )
    try:
        meta["deployment_env"] = capture_env(env_file).to_dict()
    except Exception:  # pragma: no cover
        meta["deployment_env"] = {"deployment": "", "error": "capture failed"}

    write_json(rdir / "meta.json", meta)
    write_jsonl(
        rdir / "requests.jsonl",
        [
            {
                "request_id": s.request_id,
                "input_len": s.input_len,
                "output_len": s.output_len,
                "ttft_ms": s.ttft_ms,
                "tpot_ms": s.tpot_ms,
                "e2e_ms": s.e2e_ms,
                "status": s.status,
                "error": s.error,
            }
            for s in result.samples()
        ],
    )

    record = RunRecord(
        run_id=result.run_id,
        scenario_key=result.scenario_key,
        label=result.label,
        status=result.status,
        source="executed",
        started_at=result.started_at,
        finished_at=result.finished_at,
        wall_s=(result.finished_at - result.started_at).total_seconds() if result.finished_at else None,
        command=result.command,
        host="",
        operator=operator,
        notes=result.notes,
        tags=["hicache", "multiturn"],
        raw_dir=str(rdir),
        params={
            "backend": "multiturn",
            "base_url": "",
            "model": "",
            "num_prompts": len({t.client for t in result.turns}),
        },
        metrics=result.metrics,
        samples=result.samples(),
        meta=meta,
    )
    write_json(rdir / "run.json", record.to_dict(include_samples=False))
    store.upsert_scenario(_scenario_for(result.scenario_key))
    store.save_run(record)
    return result.run_id


def _scenario_for(key: str) -> Scenario:
    from .scenarios import BUILTIN_SCENARIOS

    return BUILTIN_SCENARIOS.get(key) or Scenario(
        key=key, name=key, family="hicache", description="multiturn run"
    )
