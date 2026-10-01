"""内置场景目录 —— 与《SGLang PD 分离集群压测说明》中的命令逐条对应。

场景是**稳定标识**：对比的前提是同一个 ``scenario_key``。修改场景参数会让历史
run 失去可比性，因此参数一旦发布就不应随意改动；确需变更时请新增场景 key。

除了这里的内置目录，也可以用 ``--scenarios <file.json|file.toml>`` 追加/覆盖场景，
或把文件放到 ``$SGLBENCH_HOME/scenarios.json`` 自动加载。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .models import Scenario

# --------------------------------------------------------------------------- #
# 集群常量（按现场环境改这里，或改用 --scenarios 覆盖）
# --------------------------------------------------------------------------- #

GATEWAY = "http://10.66.1.232:8081"
PREFILL_NODE = ("10.66.1.232", 8000)
DECODE_NODE = ("10.66.1.166", 8000)
GLM = "/workspace/data/GLM-5.2-W8A8"
GLM_51 = "/workspace/data/GLM-5.1-W8A8"
DEEPSEEK = "/data/DeepSeek-V4-Flash-FlexSMQ-AWQ-W8A8"
DATASET = "/workspace/data/ShareGPT_V3_unfiltered_cleaned_split.json"

OFFLINE_ENV: dict[str, str] = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
}

_SLA_BASE: dict[str, float] = {"min_success_rate": 1.0, "max_failed_requests": 0}


def _sla(**kw: float) -> dict[str, float]:
    return {**_SLA_BASE, **kw}


def _glm_scenario(
    key: str,
    name: str,
    family: str,
    description: str,
    *,
    num_prompts: int,
    max_concurrency: int,
    random_input: int,
    random_output: int,
    request_rate: float,
    sla: Mapping[str, float],
    tags: list[str] | None = None,
    model: str = GLM,
    tokenizer: str | None = None,
) -> Scenario:
    return Scenario(
        key=key,
        name=name,
        family=family,
        description=description,
        backend="sglang-oai-chat",
        base_url=GATEWAY,
        model=model,
        tokenizer=tokenizer or model,
        dataset_name="random",
        dataset_path=DATASET,
        num_prompts=num_prompts,
        max_concurrency=max_concurrency,
        random_input=random_input,
        random_output=random_output,
        random_range_ratio=1.0,
        request_rate=request_rate,
        pd_separated=True,
        env=dict(OFFLINE_ENV),
        sla=dict(sla),
        tags=tags or [],
    )


def _catalog() -> dict[str, Scenario]:
    items: list[Scenario] = [
        # ---------------- 单并发（稳定性基线） ----------------
        _glm_scenario(
            "single-standard",
            "单并发·常规文本（日常基线）",
            "single",
            "max-concurrency=1 串行请求，产出集群最优 TTFT/TPOT 延迟基线。",
            num_prompts=50,
            max_concurrency=1,
            random_input=4096,
            random_output=2048,
            request_rate=1.0,
            sla=_sla(max_p99_ttft_ms=200.0, max_p99_tpot_ms=15.0, min_output_token_throughput_tok_s=100.0),
            tags=["baseline", "gate"],
        ),
        _glm_scenario(
            "single-deepseek-standard",
            "单并发·常规文本（DeepSeek-V4-Flash）",
            "single",
            "同上，模型换成 DeepSeek-V4-Flash-FlexSMQ-AWQ-W8A8。",
            num_prompts=50,
            max_concurrency=1,
            random_input=4096,
            random_output=2048,
            request_rate=1.0,
            sla=_sla(max_p99_ttft_ms=250.0, max_p99_tpot_ms=18.0),
            tags=["baseline"],
            model=DEEPSEEK,
        ),
        # ---------------- 长上下文 ----------------
        _glm_scenario(
            "single-longctx",
            "单并发·超长上下文（约 200K）",
            "long-context",
            "max-concurrency=1，输入 65536 / 输出 8192，校验长上下文 Prefill 与 KV 传输。",
            num_prompts=20,
            max_concurrency=1,
            random_input=65536,
            random_output=8192,
            request_rate=0.5,
            sla=_sla(max_p99_ttft_ms=6000.0, max_p99_tpot_ms=30.0),
            tags=["long-context"],
        ),
        _glm_scenario(
            "conc-32-longctx",
            "32 并发·超长上下文压力",
            "long-context",
            "32 并发 + 65536/8192，验证高并发下 PD 架构与 KV 传输效率。",
            num_prompts=100,
            max_concurrency=32,
            random_input=65536,
            random_output=8192,
            request_rate=2.0,
            sla=_sla(max_p99_ttft_ms=20000.0, max_p99_tpot_ms=60.0),
            tags=["long-context", "stress"],
        ),
        # ---------------- 多并发梯度 ----------------
        _glm_scenario(
            "conc-32-standard",
            "32 并发·常规文本（Decode 满配）",
            "concurrency",
            "匹配 Decode 集群理论最大并发，验证解码满负载稳定性。",
            num_prompts=200,
            max_concurrency=32,
            random_input=4096,
            random_output=2048,
            request_rate=4.0,
            sla=_sla(max_p99_ttft_ms=1500.0, max_p99_tpot_ms=25.0),
            tags=["sweep", "gate"],
        ),
        _glm_scenario(
            "conc-64-standard",
            "64 并发·常规文本（Prefill 满配）",
            "concurrency",
            "匹配 Prefill 集群预批最大并发，验证大批次算力吞吐。",
            num_prompts=200,
            max_concurrency=64,
            random_input=4096,
            random_output=2048,
            request_rate=6.0,
            sla=_sla(max_p99_ttft_ms=2500.0, max_p99_tpot_ms=35.0),
            tags=["sweep", "gate"],
        ),
        _glm_scenario(
            "conc-128-standard",
            "128 并发·常规文本（极限承压）",
            "concurrency",
            "request-rate=8.0 的极限压测；已知会触发 Prefill 崩溃，用作失败检测样例。",
            num_prompts=300,
            max_concurrency=128,
            random_input=4096,
            random_output=2048,
            request_rate=8.0,
            sla=_sla(max_p99_ttft_ms=4000.0, max_p99_tpot_ms=60.0),
            tags=["sweep", "stress", "known-failure"],
        ),
        _glm_scenario(
            "conc-128-safe",
            "128 并发·安全边界（request-rate=5.0）",
            "concurrency",
            "极限并发下的安全水位，用于与 8.0 崩溃点对比。",
            num_prompts=300,
            max_concurrency=128,
            random_input=4096,
            random_output=2048,
            request_rate=5.0,
            sla=_sla(max_p99_ttft_ms=4000.0, max_p99_tpot_ms=60.0),
            tags=["sweep", "stress"],
        ),
        _glm_scenario(
            "conc-128-deepseek-safe",
            "128 并发·安全边界（DeepSeek-V4-Flash）",
            "concurrency",
            "DeepSeek 在 128 并发下的安全 request-rate（5.0）。",
            num_prompts=300,
            max_concurrency=128,
            random_input=4096,
            random_output=2048,
            request_rate=5.0,
            sla=_sla(max_p99_ttft_ms=5000.0, max_p99_tpot_ms=80.0),
            tags=["sweep", "stress"],
            model=DEEPSEEK,
        ),
        _glm_scenario(
            "conc-256-standard",
            "256 并发·超高并发极限",
            "concurrency",
            "探测集群吞吐瓶颈与显存容错；request-rate=32.0 会导致 Decode 重启。",
            num_prompts=300,
            max_concurrency=256,
            random_input=4096,
            random_output=2048,
            request_rate=16.0,
            sla=_sla(max_p99_ttft_ms=8000.0, max_p99_tpot_ms=100.0),
            tags=["stress", "known-failure"],
        ),
    ]

    # ---------------- 单阶段独立压测（绕过网关，定位单集群瓶颈） ----------------
    items.append(
        Scenario(
            key="prefill-only",
            name="Prefill 集群独立压测",
            family="single-stage",
            description="绕过网关直连 Prefill modelserver，短输出以放大 Prefill 算力权重。",
            backend="sglang",
            base_url=None,
            host=PREFILL_NODE[0],
            port=PREFILL_NODE[1],
            model=GLM,
            tokenizer=GLM,
            dataset_name="random",
            dataset_path=DATASET,
            num_prompts=100,
            max_concurrency=64,
            random_input=8192,
            random_output=128,
            random_range_ratio=1.0,
            request_rate=2.0,
            pd_separated=False,
            env=dict(OFFLINE_ENV),
            sla=_sla(max_p99_ttft_ms=800.0, max_p99_tpot_ms=20.0),
            tags=["diagnostic"],
        )
    )
    items.append(
        Scenario(
            key="decode-only",
            name="Decode 集群独立压测",
            family="single-stage",
            description="绕过网关直连 Decode routing-proxy，校验纯解码吞吐与时延。",
            backend="sglang",
            base_url=None,
            host=DECODE_NODE[0],
            port=DECODE_NODE[1],
            model=GLM,
            tokenizer=GLM,
            dataset_name="random",
            dataset_path=DATASET,
            num_prompts=100,
            max_concurrency=32,
            random_input=2048,
            random_output=2048,
            random_range_ratio=1.0,
            request_rate=5.0,
            pd_separated=False,
            env=dict(OFFLINE_ENV),
            sla=_sla(max_p99_ttft_ms=300.0, max_p99_tpot_ms=25.0),
            tags=["diagnostic"],
        )
    )
    return {s.key: s for s in items}


BUILTIN_SCENARIOS: dict[str, Scenario] = _catalog()

FAMILIES: dict[str, str] = {
    "single": "单并发基线",
    "concurrency": "多并发梯度承压",
    "long-context": "超长上下文",
    "single-stage": "单阶段独立压测",
    "custom": "自定义",
}


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #


def _scenario_from_mapping(data: Mapping[str, Any]) -> Scenario:
    scenario = Scenario.from_dict(data)
    scenario.env = {**OFFLINE_ENV, **dict(scenario.env or {})}
    return scenario


def load_scenario_file(path: str | os.PathLike[str]) -> dict[str, Scenario]:
    """读取 JSON 或 TOML 场景文件。

    支持两种结构::

        {"scenarios": [ {...}, {...} ]}      # 列表
        {"my-scenario": {...}, ...}          # 以 key 为键的映射
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"场景文件不存在: {p}")
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".toml":
        try:
            import tomllib  # py>=3.11
        except ModuleNotFoundError:  # pragma: no cover
            raise RuntimeError("当前 Python 不支持 TOML，请改用 JSON 场景文件") from None
        raw: Any = tomllib.loads(text)
    else:
        raw = json.loads(text)

    if isinstance(raw, dict) and "scenarios" in raw:
        entries = raw["scenarios"]
        if isinstance(entries, dict):
            entries = [{"key": k, **v} for k, v in entries.items()]
    elif isinstance(raw, dict):
        entries = [{"key": k, **v} for k, v in raw.items() if isinstance(v, dict)]
    elif isinstance(raw, list):
        entries = raw
    else:
        raise ValueError(f"无法识别的场景文件结构: {p}")

    out: dict[str, Scenario] = {}
    for entry in entries:
        scenario = _scenario_from_mapping(entry)
        out[scenario.key] = scenario
    return out


def catalog(
    extra_files: list[str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> dict[str, Scenario]:
    """内置目录 + ``$SGLBENCH_HOME/scenarios.json`` + 显式文件（后者覆盖前者）。"""
    result: dict[str, Scenario] = dict(BUILTIN_SCENARIOS)

    candidates: list[Path] = []
    if home:
        candidates += [Path(home) / "scenarios.json", Path(home) / "scenarios.toml"]
    if os.environ.get("SGLBENCH_HOME"):
        candidates += [
            Path(os.environ["SGLBENCH_HOME"]) / "scenarios.json",
            Path(os.environ["SGLBENCH_HOME"]) / "scenarios.toml",
        ]
    for f in extra_files or []:
        candidates.append(Path(f))

    for path in candidates:
        if path and path.exists():
            result.update(load_scenario_file(path))
    return result


def get_scenario(key: str, scenarios: Mapping[str, Scenario] | None = None) -> Scenario:
    """按 key 取场景，支持唯一前缀匹配；未命中时报错并给出候选。"""
    table = dict(scenarios or BUILTIN_SCENARIOS)
    if key in table:
        return table[key]
    matches = [k for k in table if k.startswith(key)]
    if len(matches) == 1:
        return table[matches[0]]
    hint = "、".join(sorted(matches)[:10]) if matches else "、".join(sorted(table)[:10])
    raise KeyError(f"未知场景 {key!r}；候选: {hint}")


def family_of(key: str) -> str:
    s = BUILTIN_SCENARIOS.get(key)
    return s.family if s else "custom"


def sweep_key(scenario: Scenario) -> str:
    """同一"扫描维度"的分组键：同模型 + 同输入输出规模 + 同链路形态。"""
    return f"{scenario.family}|{scenario.stage}|{scenario.model}|{scenario.random_input}x{scenario.random_output}"
