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
# 集群常量（新集群现场值，2026-10-02 实测核实；按现场环境改这里或 --scenario-file 覆盖）
# --------------------------------------------------------------------------- #
# 注意：所有路径都是**容器内**路径（压测在 sglang-benchmark 容器 / bench pod 内执行，
# 宿主 /data 挂载为 /workspace/data，数据集在 /workspace/ 下 —— 与旧文档路径不同）。

ROUTER = "http://10.66.3.32:8001"          # sglang router（GLM/DeepSeek 统一压测入口，hostNetwork）
EPP_GATEWAY = "http://10.233.106.77:8081"  # EPP 网关（pod 网络，仅集群内可达；当前仅路由 DeepSeek）
PREFILL_MASTER = ("10.66.3.29", 8000)      # 当前 prefill leader（会漂移，压测前用 preflight 探测）
DECODE_MASTER = ("10.66.3.35", 8200)       # 当前 decode leader（会漂移；8200=decode modelserver）
GLM = "/workspace/data/GLM-5.2-W8A8"
GLM_51 = "/workspace/data/GLM-5.1-W8A8"
DEEPSEEK = "/workspace/data/DeepSeek-V4-Flash-FlexSMQ-AWQ-W8A8"  # 与 router /v1/models 的 served id 一致
DATASET = "/workspace/ShareGPT_V3_unfiltered_cleaned_split.json"

OFFLINE_ENV: dict[str, str] = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
}

_SLA_BASE: dict[str, float] = {"min_success_rate": 1.0, "max_failed_requests": 0}


def _sla(**kw: float) -> dict[str, float]:
    return {**_SLA_BASE, **kw}


def _scenario(
    key: str,
    name: str,
    family: str,
    description: str,
    model: str,
    *,
    num_prompts: int,
    max_concurrency: int,
    random_input: int,
    random_output: int,
    request_rate: float,
    sla: Mapping[str, float],
    tags: list[str] | None = None,
    dataset_name: str = "random",
    backend: str = "sglang",
    base_url: str | None = ROUTER,
    host: str | None = None,
    port: int | None = None,
    pd_separated: bool = True,
    extra_args: list[str] | None = None,
) -> Scenario:
    """PD 分离全链路场景的统一构造器（默认走 router，backend=sglang + --pd-separated）。

    单阶段诊断场景（prefill/decode-only）用 host/port 直连、不带 --pd-separated。
    """
    return Scenario(
        key=key,
        name=name,
        family=family,
        description=description,
        backend=backend,
        base_url=base_url,
        host=host,
        port=port,
        model=model,
        tokenizer=model,
        dataset_name=dataset_name,
        dataset_path=DATASET,
        num_prompts=num_prompts,
        max_concurrency=max_concurrency,
        random_input=random_input,
        random_output=random_output,
        random_range_ratio=1.0,
        request_rate=request_rate,
        pd_separated=pd_separated,
        extra_args=list(extra_args or []),
        env=dict(OFFLINE_ENV),
        sla=dict(sla),
        tags=tags or [],
    )


def _glm(key: str, name: str, family: str, description: str, **kw: Any) -> Scenario:
    """GLM-5.2-W8A8 场景（GLM 尚未部署到新集群，案例代码已就绪，部署后即可跑）。

    GLM 走 ``sglang-oai-chat`` backend（对齐生产调用形态；与 DeepSeek 的 ``sglang``
    backend 区分开，但链路相同：都经 router + --pd-separated）。
    """
    return _scenario(key, name, family, description, model=GLM, backend="sglang-oai-chat", **kw)


def _deepseek(key: str, name: str, family: str, description: str, **kw: Any) -> Scenario:
    """DeepSeek-V4-Flash-FlexSMQ-AWQ-W8A8 场景（当前集群实际服务的模型）。"""
    return _scenario(key, name, family, description, model=DEEPSEEK, **kw)


def _stage_scenario(
    key: str,
    name: str,
    family: str,
    description: str,
    model: str,
    *,
    host: str,
    port: int,
    num_prompts: int,
    max_concurrency: int,
    random_input: int,
    random_output: int,
    request_rate: float,
    sla: Mapping[str, float],
    tags: list[str] | None = None,
) -> Scenario:
    """绕过网关直连单阶段节点（leader 漂移后需同步 PREFILL_MASTER/DECODE_MASTER）。"""
    return _scenario(
        key, name, family, description, model,
        host=host, port=port, base_url=None, pd_separated=False,
        num_prompts=num_prompts, max_concurrency=max_concurrency,
        random_input=random_input, random_output=random_output,
        request_rate=request_rate, sla=sla, tags=tags,
    )


def _catalog() -> dict[str, Scenario]:
    # 6 个标准场景 × 2 个模型（GLM/DeepSeek）。速率已按实测崩溃边界校准：
    #   DeepSeek 128 并发 @ rate=8.0 → prefill 崩溃（pod 重启），安全边界 5.0
    #   DeepSeek 256 并发 @ rate=32.0 → decode 重启，标准档 16.0
    #   GLM 未实测，与 DeepSeek 同档保守取值（部署后需重新校准）
    items: list[Scenario] = [
        # ================ ① 常规文本单并发（稳定性基线） ================
        _glm(
            "single-standard", "单并发·常规文本（GLM）", "single",
            "max-concurrency=1 串行请求，产出集群最优 TTFT/TPOT 延迟基线。",
            num_prompts=50, max_concurrency=1, random_input=4096, random_output=2048,
            request_rate=1.0,
            sla=_sla(max_p99_ttft_ms=200.0, max_p99_tpot_ms=15.0, min_output_token_throughput_tok_s=100.0),
            tags=["baseline", "gate"],
        ),
        _deepseek(
            "single-deepseek-standard", "单并发·常规文本（DeepSeek-V4-Flash）", "single",
            "同 GLM 场景，模型换 DeepSeek-V4-Flash。SLA 按 4096 输入放大（首轮基线后校准）。",
            num_prompts=50, max_concurrency=1, random_input=4096, random_output=2048,
            request_rate=1.0,
            sla=_sla(max_p99_ttft_ms=5000.0, max_p99_tpot_ms=60.0, min_output_token_throughput_tok_s=50.0),
            tags=["baseline", "gate"],
        ),
        # ================ ② 超长上下文单并发（200K 场景基线） ================
        _glm(
            "single-longctx", "单并发·超长上下文（GLM，约 200K）", "long-context",
            "max-concurrency=1，输入 65536 / 输出 8192，校验长上下文 Prefill 与 KV 传输。",
            num_prompts=20, max_concurrency=1, random_input=65536, random_output=8192,
            request_rate=0.5,
            sla=_sla(max_p99_ttft_ms=6000.0, max_p99_tpot_ms=30.0),
            tags=["long-context"],
        ),
        _deepseek(
            "single-longctx-deepseek", "单并发·超长上下文（DeepSeek，约 200K）", "long-context",
            "65536/8192 单并发基线；SLA 按 65K 输入放大。",
            num_prompts=20, max_concurrency=1, random_input=65536, random_output=8192,
            request_rate=0.5,
            sla=_sla(max_p99_ttft_ms=60000.0, max_p99_tpot_ms=100.0),
            tags=["long-context"],
        ),
        # ================ ③ 低并发 32（Decode 满配） ================
        _glm(
            "conc-32-standard", "32 并发·常规文本（GLM，Decode 满配）", "concurrency",
            "匹配 Decode 集群理论最大并发，验证解码满负载稳定性。",
            num_prompts=200, max_concurrency=32, random_input=4096, random_output=2048,
            request_rate=4.0,
            sla=_sla(max_p99_ttft_ms=1500.0, max_p99_tpot_ms=25.0),
            tags=["sweep", "gate"],
        ),
        _deepseek(
            "conc-32-deepseek", "32 并发·常规文本（DeepSeek，Decode 满配）", "concurrency",
            "Decode 满配并发，生产常规水位。",
            num_prompts=200, max_concurrency=32, random_input=4096, random_output=2048,
            request_rate=4.0,
            sla=_sla(max_p99_ttft_ms=6000.0, max_p99_tpot_ms=70.0),
            tags=["sweep", "gate"],
        ),
        # ================ ④ 中并发 64（Prefill 满配） ================
        _glm(
            "conc-64-standard", "64 并发·常规文本（GLM，Prefill 满配）", "concurrency",
            "匹配 Prefill 集群预批最大并发，验证大批次算力吞吐。",
            num_prompts=200, max_concurrency=64, random_input=4096, random_output=2048,
            request_rate=6.0,
            sla=_sla(max_p99_ttft_ms=2500.0, max_p99_tpot_ms=35.0),
            tags=["sweep", "gate"],
        ),
        _deepseek(
            "conc-64-deepseek", "64 并发·常规文本（DeepSeek，Prefill 满配）", "concurrency",
            "Prefill 满配并发，大批次吞吐最优水位。",
            num_prompts=200, max_concurrency=64, random_input=4096, random_output=2048,
            request_rate=6.0,
            sla=_sla(max_p99_ttft_ms=10000.0, max_p99_tpot_ms=90.0),
            tags=["sweep", "gate"],
        ),
        # ================ ⑤ 高并发 128（集群承压上限，安全档） ================
        _glm(
            "conc-128-safe", "128 并发·安全档（GLM）", "concurrency",
            "极限并发安全边界 rate=5.0；rate=8.0 实测崩 prefill（见 conc-128-crash-probe）。",
            num_prompts=300, max_concurrency=128, random_input=4096, random_output=2048,
            request_rate=5.0,
            sla=_sla(max_p99_ttft_ms=4000.0, max_p99_tpot_ms=60.0),
            tags=["sweep", "stress"],
        ),
        _deepseek(
            "conc-128-deepseek-safe", "128 并发·安全档（DeepSeek）", "concurrency",
            "**测试边界**：DeepSeek 128 并发 @ rate=8.0 实测 prefill 崩溃（pod 重启），"
            "安全边界 = 5.0。本场景即安全档；崩溃复现见 conc-128-crash-probe。",
            num_prompts=300, max_concurrency=128, random_input=4096, random_output=2048,
            request_rate=5.0,
            sla=_sla(max_p99_ttft_ms=15000.0, max_p99_tpot_ms=120.0),
            tags=["sweep", "stress"],
        ),
        # ================ ⑥ 多并发超长上下文压力 ================
        _glm(
            "conc-32-longctx", "32 并发·超长上下文压力（GLM）", "long-context",
            "32 并发 + 65536/8192，验证高并发下 PD 架构与 KV 传输效率。",
            num_prompts=100, max_concurrency=32, random_input=65536, random_output=8192,
            request_rate=2.0,
            sla=_sla(max_p99_ttft_ms=20000.0, max_p99_tpot_ms=60.0),
            tags=["long-context", "stress"],
        ),
        _deepseek(
            "conc-32-longctx-deepseek", "32 并发·超长上下文压力（DeepSeek）", "long-context",
            "65K 输入 + 32 并发，校验高并发下 KV 传输与显存管控。",
            num_prompts=100, max_concurrency=32, random_input=65536, random_output=8192,
            request_rate=2.0,
            sla=_sla(max_p99_ttft_ms=120000.0, max_p99_tpot_ms=180.0),
            tags=["long-context", "stress"],
        ),
    ]

    # ---------------- 崩溃边界探针（复现留证，勿用于生产门禁） ----------------
    items.append(
        _deepseek(
            "conc-128-crash-probe", "128 并发·崩溃探针（DeepSeek @ rate=8.0）", "concurrency",
            "**实测边界复现**：128 并发 @ request-rate=8.0 → prefill 崩溃 pod 重启，"
            "安全边界 5.0。本场景预期失败（known-failure），用于崩溃留证与边界验证。",
            num_prompts=300, max_concurrency=128, random_input=4096, random_output=2048,
            request_rate=8.0,
            sla=_sla(max_p99_ttft_ms=20000.0, max_p99_tpot_ms=150.0),
            tags=["stress", "known-failure"],
        )
    )

    # ---------------- 超高并发极限（256；rate=32.0 实测崩 decode） ----------------
    items.append(
        _glm(
            "conc-256-standard", "256 并发·超高并发极限（GLM）", "concurrency",
            "**测试边界**：request-rate=32.0 实测导致 decode 重启，标准档 16.0。",
            num_prompts=300, max_concurrency=256, random_input=4096, random_output=2048,
            request_rate=16.0,
            sla=_sla(max_p99_ttft_ms=8000.0, max_p99_tpot_ms=100.0),
            tags=["stress", "known-failure"],
        )
    )
    items.append(
        _deepseek(
            "conc-256-deepseek", "256 并发·超高并发极限（DeepSeek）", "concurrency",
            "**测试边界**：rate=32.0 实测导致 decode 重启，标准档 16.0。",
            num_prompts=300, max_concurrency=256, random_input=4096, random_output=2048,
            request_rate=16.0,
            sla=_sla(max_p99_ttft_ms=25000.0, max_p99_tpot_ms=200.0),
            tags=["stress", "known-failure"],
        )
    )

    # ---------------- ShareGPT 短上下文（对齐 OPTIMIZATION.md 压测协议） ----------------
    items.append(
        _deepseek(
            "conc-64-sharegpt-deepseek", "64 并发·ShareGPT 短上下文（DeepSeek）", "concurrency",
            "ShareGPT 512/128、rate=20，与 llm-d OPTIMIZATION.md 协议一致（1238 tok/s 基准对照）。",
            num_prompts=100, max_concurrency=64, random_input=512, random_output=128,
            request_rate=20.0, dataset_name="sharegpt",
            sla=_sla(max_p99_ttft_ms=3000.0, max_p99_tpot_ms=80.0, min_output_token_throughput_tok_s=800.0),
            tags=["sweep", "opt-baseline"],
        )
    )

    # ---------------- 单阶段独立压测（绕过网关，定位单集群瓶颈） ----------------
    items.append(
        _stage_scenario(
            "prefill-only", "Prefill 集群独立压测（GLM）", "single-stage",
            "绕过网关直连 Prefill leader（PREFILL_MASTER），短输出放大 Prefill 算力权重。",
            GLM, host=PREFILL_MASTER[0], port=PREFILL_MASTER[1],
            num_prompts=100, max_concurrency=64, random_input=8192, random_output=128,
            request_rate=2.0,
            sla=_sla(max_p99_ttft_ms=800.0, max_p99_tpot_ms=20.0),
            tags=["diagnostic"],
        )
    )
    items.append(
        _stage_scenario(
            "prefill-only-deepseek", "Prefill 集群独立压测（DeepSeek）", "single-stage",
            "直连 prefill leader；leader 漂移后需同步 PREFILL_MASTER 常量。",
            DEEPSEEK, host=PREFILL_MASTER[0], port=PREFILL_MASTER[1],
            num_prompts=100, max_concurrency=64, random_input=8192, random_output=128,
            request_rate=2.0,
            sla=_sla(max_p99_ttft_ms=3000.0, max_p99_tpot_ms=50.0),
            tags=["diagnostic"],
        )
    )
    items.append(
        _stage_scenario(
            "decode-only", "Decode 集群独立压测（GLM）", "single-stage",
            "绕过网关直连 Decode modelserver（DECODE_MASTER:8200），校验纯解码吞吐。",
            GLM, host=DECODE_MASTER[0], port=DECODE_MASTER[1],
            num_prompts=100, max_concurrency=32, random_input=2048, random_output=2048,
            request_rate=5.0,
            sla=_sla(max_p99_ttft_ms=300.0, max_p99_tpot_ms=25.0),
            tags=["diagnostic"],
        )
    )
    items.append(
        _stage_scenario(
            "decode-only-deepseek", "Decode 集群独立压测（DeepSeek）", "single-stage",
            "直连 decode modelserver；leader 漂移后需同步 DECODE_MASTER 常量。",
            DEEPSEEK, host=DECODE_MASTER[0], port=DECODE_MASTER[1],
            num_prompts=100, max_concurrency=32, random_input=2048, random_output=2048,
            request_rate=5.0,
            sla=_sla(max_p99_ttft_ms=2000.0, max_p99_tpot_ms=60.0),
            tags=["diagnostic"],
        )
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # HiCache L2 稳定边界场景（2026-10-04 实测边界: 输入 token 速率 ≤~12K tok/s
    #   稳定, ≥~20K 必崩 MACA xnack）。mini-l2-* 全部取 ≤8K tok/s 留足余量。
    #   非 HiCache 与 HiCache 部署跑同一批 key 即可对比（label 区分 l2-off/l2-on）。
    # ═══════════════════════════════════════════════════════════════════════════
    items.append(
        _deepseek(
            "mini-l2-32c", "mini-l2: 32 并发·稳定边界（DeepSeek）", "hicache",
            "HiCache 稳定边界基准：32c @ rate 3.0 = ~8K input tok/s（稳定区下沿留余量）。"
            "用于 l2-off/l2-on 同 key 对比。",
            num_prompts=200, max_concurrency=32, random_input=4096, random_output=2048,
            request_rate=3.0,
            sla=_sla(max_p99_ttft_ms=8000.0, max_p99_tpot_ms=120.0),
            tags=["hicache", "l2", "mini-l2", "stable-boundary"],
        )
    )
    items.append(
        _deepseek(
            "mini-l2-64c", "mini-l2: 64 并发·稳定边界（DeepSeek）", "hicache",
            "HiCache 稳定边界基准：64c @ rate 2.0 = ~8K input tok/s。",
            num_prompts=200, max_concurrency=64, random_input=4096, random_output=2048,
            request_rate=2.0,
            sla=_sla(max_p99_ttft_ms=8000.0, max_p99_tpot_ms=120.0),
            tags=["hicache", "l2", "mini-l2", "stable-boundary"],
        )
    )
    items.append(
        _deepseek(
            "mini-l2-128c", "mini-l2: 128 并发·稳定边界（DeepSeek）", "hicache",
            "HiCache 稳定边界基准：128c @ rate 1.0 = ~4K input tok/s（远低于稳定线）。",
            num_prompts=300, max_concurrency=128, random_input=4096, random_output=2048,
            request_rate=1.0,
            sla=_sla(max_p99_ttft_ms=15000.0, max_p99_tpot_ms=120.0),
            tags=["hicache", "l2", "mini-l2", "stable-boundary"],
        )
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # HiCache 多轮对话专项（对齐 SGLang 官方 bench_multiturn 思路：N 客户端 × R 轮，
    #   每轮输入 = 上一轮输入+输出 拼接 → 逐轮 TTFT / cache-hit 率）
    #   执行方式: sglbench multiturn --scenario <key> [--clients N --rounds R ...]
    #   num_prompts=客户端数, max_concurrency=并发客户端, random_input=单轮请求长度,
    #   random_output=单轮输出长度, request_rate=每客户端速率。
    # ═══════════════════════════════════════════════════════════════════════════
    items.append(
        _deepseek(
            "hicache-multiturn-20x10", "HiCache 多轮对话 20 客户端×10 轮", "hicache",
            "多轮对话专项（官方 bench_multiturn 参数: --num-clients 20 --num-rounds 10 "
            "--request-length 2048 --output-length 16）。逐轮 TTFT/cache-hit 对比 l2-off/l2-on。",
            num_prompts=20, max_concurrency=20, random_input=2048, random_output=16,
            request_rate=2.0,
            sla=_sla(max_p99_ttft_ms=15000.0),
            tags=["hicache", "l2", "multiturn"],
        )
    )
    items.append(
        _deepseek(
            "hicache-multiturn-80x10", "HiCache 多轮对话 80 客户端×10 轮", "hicache",
            "多轮对话专项（官方 H800 基准参数: --num-clients 80 --num-rounds 10 "
            "--request-length 2048 --output-length 1）。",
            num_prompts=80, max_concurrency=80, random_input=2048, random_output=1,
            request_rate=1.0,
            sla=_sla(max_p99_ttft_ms=20000.0),
            tags=["hicache", "l2", "multiturn"],
        )
    )

    # ── HiCache L3（Mooncake Store）多轮对话场景 ──
    # 对应官方 4 配置矩阵的 "L1+L2+Mooncake" 与 "Populated Mooncake" 两档。
    # ⚠ 需要 L3 部署（r4-hicache + mooncake master/store, storage_backend=mooncake,
    #   envs/hicache-l3.json）；当前集群 L3 被 MACA xnack 崩溃阻塞（2026-09-27 定性），
    #   场景先行就绪，驱动修复后即可运行与 L2 对比。
    # 4 配置矩阵与场景/label 对应：
    #   GPU only          → 非 HiCache 部署（dsv4rc3）+ 本场景, label l2-off
    #   L1+L2             → hicache-l2 部署, label l2-on
    #   L1+L2+L3          → hicache-l3 部署 + hicache-l3-multiturn-*, label l3-on
    #   Populated L3      → hicache-l3 部署 + hicache-l3-populated-*, label l3-pre
    items.append(
        _deepseek(
            "hicache-l3-multiturn-20x10", "HiCache L3 多轮对话 20 客户端×10 轮（Mooncake）", "hicache",
            "L1+L2+L3 档（storage_backend=mooncake, prefetch=wait_complete）；"
            "需要 L3 部署（envs/hicache-l3.json）。对齐官方 A10 参数。",
            num_prompts=20, max_concurrency=20, random_input=2048, random_output=16,
            request_rate=2.0,
            sla=_sla(max_p99_ttft_ms=15000.0),
            tags=["hicache", "l3", "multiturn", "mooncake"],
        )
    )
    items.append(
        _deepseek(
            "hicache-l3-multiturn-80x10", "HiCache L3 多轮对话 80 客户端×10 轮（Mooncake）", "hicache",
            "L1+L2+L3 档；对齐官方 H800 参数（output-length 1, 80 clients）。"
            "需要 L3 部署（envs/hicache-l3.json）。",
            num_prompts=80, max_concurrency=80, random_input=2048, random_output=1,
            request_rate=1.0,
            sla=_sla(max_p99_ttft_ms=20000.0),
            tags=["hicache", "l3", "multiturn", "mooncake"],
        )
    )
    items.append(
        _deepseek(
            "hicache-l3-populated-20x10", "HiCache L3 预填充多轮对话 20 客户端×10 轮（Populated）", "hicache",
            "Populated Mooncake 档：与 l3-multiturn 相同参数，区别是测试前 KV 已预加载到 "
            "Mooncake store（先跑一轮预热对话再正式跑，或由部署侧预填充）。"
            "需要 L3 部署（envs/hicache-l3.json）。",
            num_prompts=20, max_concurrency=20, random_input=2048, random_output=16,
            request_rate=2.0,
            sla=_sla(max_p99_ttft_ms=15000.0),
            tags=["hicache", "l3", "multiturn", "mooncake", "populated"],
        )
    )
    return {s.key: s for s in items}


BUILTIN_SCENARIOS: dict[str, Scenario] = _catalog()

FAMILIES: dict[str, str] = {
    "single": "单并发基线",
    "concurrency": "多并发梯度承压",
    "long-context": "超长上下文",
    "single-stage": "单阶段独立压测",
    "hicache": "HiCache 专项（L2 稳定边界 / L2+L3 多轮对话）",
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
