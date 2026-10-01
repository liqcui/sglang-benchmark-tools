"""测试前置预检：确认 PD 全链路可用，避免"无效压测"。

对应文档第六节的 curl 连通性测试，但比 curl 多测三件事：

1. **TTFT**：流式首个 token 的到达时间 —— Prefill 算力 + KV 传输链路是否真的通；
2. **流式完整性**：能不能一路收到 ``[DONE]``，而不是中途断流；
3. **结构化入库**：预检结果也进 DuckDB，事后可以把"预检失败的那次压测"筛出来。

只用标准库 ``urllib``，不引入 requests。
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from .models import Scenario
from .store import PreflightCheck
from .util import new_run_id, utcnow

DEFAULT_PROMPT = "简要说明PD分离架构优势"
DEFAULT_MAX_TOKENS = 64


@dataclass
class PreflightResult:
    target: str
    url: str
    model: str
    ok: bool
    checked_at: datetime = field(default_factory=utcnow)
    scenario_key: str = ""
    http_status: int | None = None
    ttft_ms: float | None = None
    total_ms: float | None = None
    output_chars: int = 0
    detail: str = ""
    chunk_count: int = 0
    stream_completed: bool = False
    sample_output: str = ""

    def to_check(self) -> PreflightCheck:
        return PreflightCheck(
            check_id=new_run_id(f"preflight-{self.target}", self.checked_at),
            checked_at=self.checked_at,
            target=self.target,
            url=self.url,
            ok=self.ok,
            model=self.model,
            scenario_key=self.scenario_key,
            http_status=self.http_status,
            ttft_ms=self.ttft_ms,
            total_ms=self.total_ms,
            output_chars=self.output_chars,
            detail=self.detail,
        )

    def to_dict(self) -> dict[str, Any]:
        data = self.to_check().to_dict()
        data.update(
            {
                "chunk_count": self.chunk_count,
                "stream_completed": self.stream_completed,
                "sample_output": self.sample_output[:200],
            }
        )
        return data


def _build_request(url: str, model: str, prompt: str, max_tokens: int, stream: bool, api_key: str | None) -> urllib.request.Request:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": int(max_tokens),
        "stream": bool(stream),
    }
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )


def _chat_url(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    if base.endswith("/v1/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def check_endpoint(
    endpoint: str,
    *,
    model: str,
    prompt: str = DEFAULT_PROMPT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    stream: bool = True,
    timeout: float = 30.0,
    api_key: str | None = None,
    scenario_key: str = "",
    target: str = "",
    insecure: bool = False,
) -> PreflightResult:
    """对单个端点做一次真实对话请求（默认流式），测 TTFT 与流式完整性。"""
    url = _chat_url(endpoint)
    label = target or endpoint
    result = PreflightResult(target=label, url=url, model=model, ok=False, scenario_key=scenario_key)

    context = None
    if insecure and url.startswith("https"):
        context = ssl._create_unverified_context()  # noqa: S323 - 内网自签证书场景

    request = _build_request(url, model, prompt, max_tokens, stream, api_key)
    started = time.monotonic()
    first_token_at: float | None = None
    text_parts: list[str] = []
    chunks = 0

    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:  # noqa: S310 - 内网固定地址
            result.http_status = getattr(response, "status", None) or response.getcode()
            if not stream:
                body = response.read().decode("utf-8", errors="replace")
                result.total_ms = (time.monotonic() - started) * 1000.0
                data = json.loads(body) if body.strip().startswith("{") else {}
                for choice in data.get("choices", []):
                    text_parts.append(str(choice.get("message", {}).get("content", "")))
                result.stream_completed = True
            else:
                for raw in response:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        result.stream_completed = True
                        break
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    chunks += 1
                    for choice in obj.get("choices", []):
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            if first_token_at is None:
                                first_token_at = time.monotonic()
                            text_parts.append(str(piece))
                    if first_token_at is None and obj.get("choices"):
                        # 有 chunk 但没内容（例如只带 role）也算链路已通
                        first_token_at = time.monotonic()
        result.total_ms = (time.monotonic() - started) * 1000.0
        if first_token_at is not None:
            result.ttft_ms = (first_token_at - started) * 1000.0
        result.chunk_count = chunks
        result.output_chars = len("".join(text_parts))
        result.sample_output = "".join(text_parts)[:400]
        result.ok = bool(result.http_status == 200 and (result.output_chars > 0 or chunks > 0))
        if not result.ok:
            result.detail = f"HTTP {result.http_status}，但未收到有效内容（chunk={chunks}）"
    except urllib.error.HTTPError as exc:
        result.http_status = exc.code
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        result.detail = f"HTTP {exc.code}: {body or exc.reason}"
        result.total_ms = (time.monotonic() - started) * 1000.0
    except urllib.error.URLError as exc:
        result.detail = f"连接失败: {exc.reason}"
        result.total_ms = (time.monotonic() - started) * 1000.0
    except (TimeoutError, OSError) as exc:
        result.detail = f"超时/网络错误: {exc}"
        result.total_ms = (time.monotonic() - started) * 1000.0
    except Exception as exc:  # noqa: BLE001 - 预检不应该让命令崩掉
        result.detail = f"未预期错误: {type(exc).__name__}: {exc}"
        result.total_ms = (time.monotonic() - started) * 1000.0

    if result.ok and not result.detail:
        result.detail = (
            f"TTFT {result.ttft_ms:.1f} ms" if result.ttft_ms is not None else "OK"
        ) + f"；收到 {result.chunk_count} 个 chunk / {result.output_chars} 字符"
        if stream and not result.stream_completed:
            result.detail += "（未见 [DONE]，流可能被中断）"
    return result


def targets_for(scenario: Scenario) -> list[tuple[str, str]]:
    """返回该场景相关的 (标签, 端点) 列表。PD 场景额外探测 Prefill/Decode 直连。"""
    out: list[tuple[str, str]] = []
    if scenario.base_url:
        out.append((f"gateway({scenario.key})", scenario.base_url))
        if scenario.pd_separated:
            from .scenarios import DECODE_NODE, PREFILL_NODE

            out.append(("prefill-node", f"http://{PREFILL_NODE[0]}:{PREFILL_NODE[1]}"))
            out.append(("decode-node", f"http://{DECODE_NODE[0]}:{DECODE_NODE[1]}"))
    elif scenario.host and scenario.port:
        out.append((scenario.key, f"http://{scenario.host}:{scenario.port}"))
    return out


def check_scenario(
    scenario: Scenario,
    *,
    prompt: str = DEFAULT_PROMPT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: float = 30.0,
    stream: bool = True,
    api_key: str | None = None,
    include_nodes: bool = True,
    insecure: bool = False,
    model: str | None = None,
) -> list[PreflightResult]:
    """对一个场景做预检（含 PD 两个直连节点）。"""
    pairs = targets_for(scenario)
    if not include_nodes:
        pairs = pairs[:1]
    results = []
    for label, endpoint in pairs:
        results.append(
            check_endpoint(
                endpoint,
                model=model or scenario.model,
                prompt=prompt,
                max_tokens=max_tokens,
                timeout=timeout,
                stream=stream,
                api_key=api_key,
                scenario_key=scenario.key,
                target=label,
                insecure=insecure,
            )
        )
    return results


def render_console(results: Sequence[PreflightResult]) -> str:
    from .report import render_table

    rows = []
    for r in results:
        rows.append(
            [
                r.target,
                r.url,
                str(r.http_status or "-"),
                f"{r.ttft_ms:.1f}" if r.ttft_ms is not None else "-",
                f"{r.total_ms:.1f}" if r.total_ms is not None else "-",
                str(r.output_chars),
                "✅ OK" if r.ok else "❌ FAIL",
                r.detail[:60],
            ]
        )
    if not rows:
        return "没有需要预检的端点。"
    table = render_table(
        ["目标", "URL", "HTTP", "TTFT(ms)", "总耗时(ms)", "输出字符", "结果", "说明"],
        rows,
        ["left", "left", "right", "right", "right", "right", "left", "left"],
    )
    ok = sum(1 for r in results if r.ok)
    return f"{table}\n预检汇总: {ok}/{len(results)} 通过"
