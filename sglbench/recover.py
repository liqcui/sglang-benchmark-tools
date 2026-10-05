"""崩溃恢复：pod 异常发现 → GPU 热复位（ssh mx-smi）→ 重建 → 验证。

通道实测（本机 10.66.2.1）：``ssh -o BatchMode=yes ubuntu@<hostIP>`` 免密 +
``sudo -n mx-smi`` 免密。仅显式触发（``--gpu-reset`` 标志 / ``sglbench recover``）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .remote import CmdResult, kubectl_json, ssh_run

PD_LABEL = "llm-d.ai/guide=pd-disaggregation"
NAMESPACE = "metax-ai-pd"
DEFAULT_GPU_INDICES = (0, 4)          # 血泪规程：-i 0 + -i 4
RETRYABLE_OUTPUT = ("Resource temporarily unavailable",)
RESET_ATTEMPTS = 3
RETRY_WAIT_S = 45.0                   # 30-60s 区间取中


@dataclass
class PodIssue:
    pod: str
    node: str
    host_ip: str
    reason: str


def _container_reason(cs: dict[str, Any]) -> str | None:
    reasons: list[str] = []
    state = cs.get("state") or {}
    waiting = (state.get("waiting") or {}).get("reason")
    if waiting in ("CrashLoopBackOff", "ImagePullBackOff", "RunContainerError"):
        reasons.append(f"waiting:{waiting}")
    last = ((cs.get("lastState") or {}).get("terminated") or {})
    code = last.get("exitCode")
    if code not in (None, 0):
        reasons.append(f"exit={code}")
    return ",".join(reasons) or None


def find_pod_issues(pods_json: Any) -> list[PodIssue]:
    """从 ``kubectl get pods -o json`` 输出里找出异常 pod（纯函数）。"""
    issues: list[PodIssue] = []
    for pod in (pods_json or {}).get("items", []) or []:
        meta = pod.get("metadata") or {}
        status = pod.get("status") or {}
        spec = pod.get("spec") or {}
        reasons: list[str] = []
        ready_cond = next(
            (c for c in (status.get("conditions") or []) if c.get("type") == "Ready"), None
        )
        if ready_cond is not None and ready_cond.get("status") != "True":
            reasons.append("not_ready")
        for cs in status.get("containerStatuses") or []:
            if cs.get("restartCount", 0) and cs["restartCount"] > 0:
                reasons.append(f"restarts={cs['restartCount']}")
            r = _container_reason(cs)
            if r:
                reasons.append(r)
        if reasons:
            issues.append(
                PodIssue(
                    pod=meta.get("name", ""),
                    node=spec.get("nodeName", ""),
                    host_ip=status.get("hostIP", ""),
                    reason=",".join(reasons),
                )
            )
    return issues


def fetch_pd_pods(*, namespace: str = NAMESPACE, runner=None) -> tuple[Any | None, CmdResult]:
    return kubectl_json(
        ["get", "pods", "-l", PD_LABEL, "-o", "json"], namespace=namespace, runner=runner
    )


def find_unhealthy_pods(*, namespace: str = NAMESPACE, runner=None) -> list[PodIssue]:
    data, _res = fetch_pd_pods(namespace=namespace, runner=runner)
    return find_pod_issues(data)
