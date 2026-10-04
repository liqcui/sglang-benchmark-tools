"""部署环境元数据捕获。

为了让同一场景在不同部署形态（非 HiCache PD / HiCache L2 等）下的 run 可对比，
每次压测把**服务端部署环境**结构化记录到 DuckDB ``run_environments`` 表。

数据来源（优先级从高到低）：
1. ``--env-file <json>`` 显式指定（完整覆盖）
2. ``$SGLBENCH_HOME/env.json`` 或 ``sglbench/env.defaults.json``（部分字段可被 3 覆盖）
3. 自动探测：kubectl 可用时探测各角色 pod 数 / 每节点 GPU 数 / 镜像（失败静默跳过）
4. 内置默认值（未知字段留空）

字段约定：拓扑/镜像等以 dict 描述；所有值为字符串或简单 JSON 标量，
入库时扁平化到结构化列（缺失填 NULL）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# 内置默认值：按当前集群现场（deepseek-v4-rc3-maca / deepseek-hicache lws 方案）
# --------------------------------------------------------------------------- #
DEFAULT_ENV: dict[str, Any] = {
    "cluster": "metax-c500-10.66.3",
    "namespace": "metax-ai-pd",
    "deployment": "",                 # 例: deepseek-v4-rc3-maca (non-hicache) / deepseek-hicache-l2
    "prefill_nodes": 4,
    "decode_nodes": 4,
    "gpu_per_node": 8,
    "prefill_tp": 8,
    "prefill_dp": 1,
    "prefill_pp": 4,
    "decode_tp": 8,
    "decode_dp": 4,
    "decode_pp": 1,
    "prefill_image": "",
    "decode_image": "",
    "router_image": "",
    "launch_mode": "official",
    "hicache_enabled": False,
    "hicache_io_backend": "",
    "hicache_ratio": "",
    "hicache_mem_layout": "",
    "hicache_storage_backend": "",
    "hicache_write_policy": "",
    "swa_full_tokens_ratio": "",
    "context_length": 131072,
    "chunked_prefill_size": 4096,
    "eplb_enabled": False,
    "eplb_file": "",
    "maca_version": "",
    "driver_version": "",
    "engine_version": "",
    "notes": "",
}


@dataclass
class DeploymentEnv:
    """服务端部署环境快照（扁平 dict 形式存储）。"""

    data: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)

    def to_json(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, text: str) -> "DeploymentEnv":
        return cls(json.loads(text))


def _read_json_file(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _defaults_file() -> Path:
    home = os.environ.get("SGLBENCH_HOME")
    if home:
        p = Path(home) / "env.json"
        if p.exists():
            return p
    return Path(__file__).parent / "env.defaults.json"


def _kubectl_json(args: list[str]) -> list[dict[str, Any]] | None:
    """kubectl 输出 JSON 列表；失败返回 None（静默）。"""
    if not shutil.which("kubectl"):
        return None
    try:
        out = subprocess.run(
            ["kubectl"] + args,
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
        return data if isinstance(data, list) else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def _auto_detect() -> dict[str, Any]:
    """kubectl 自动探测（尽力而为，失败即忽略）。"""
    detected: dict[str, Any] = {}
    ns = os.environ.get("SGLBENCH_NAMESPACE", "metax-ai-pd")
    pods = _kubectl_json(
        ["-n", ns, "get", "pods", "-o", "json",
         "-l", "llm-d.ai/guide=pd-disaggregation"]
    )
    if pods is None:
        return detected

    roles: dict[str, list[dict[str, Any]]] = {}
    images: set[str] = set()
    for p in pods:
        role = (p.get("metadata", {}).get("labels", {}) or {}).get("llm-d.ai/role", "")
        if role in ("prefill", "decode"):
            roles.setdefault(role, []).append(p)
            for c in p.get("spec", {}).get("containers", []):
                images.add(c.get("image", ""))
    if roles:
        detected["prefill_nodes"] = len(roles.get("prefill", []))
        detected["decode_nodes"] = len(roles.get("decode", []))
    if images:
        img = sorted(i for i in images if i)
        if img:
            detected["prefill_image"] = img[0] if len(img) == 1 else ""
            joined = " ".join(img).lower()
            detected["hicache_enabled"] = "hicache" in joined or "r4-hicache" in joined
    return detected


def capture_env(env_file: str | Path | None = None, **overrides: Any) -> DeploymentEnv:
    """合并出最终环境快照。

    顺序：默认值 → 默认文件 → 显式 env-file → kubectl 自动探测 → 显式 overrides。
    """
    merged: dict[str, Any] = dict(DEFAULT_ENV)
    merged.update(_read_json_file(_defaults_file()))
    merged.update(_read_json_file(env_file))
    merged.update(_auto_detect())
    for k, v in overrides.items():
        if v is not None and v != "":
            merged[k] = v
    return DeploymentEnv(merged)
