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


# --------------------------------------------------------------------------- #
# GPU 热复位
# --------------------------------------------------------------------------- #


@dataclass
class GpuResetResult:
    host: str
    index: int
    ok: bool
    attempts: int
    last_output: str


def build_reset_cmd(index: int) -> str:
    return f"sudo -n mx-smi -y -r -i {index}"


def reset_gpu(
    host_ip: str,
    index: int,
    *,
    ssh_user: str = "ubuntu",
    runner=None,
    sleeper=None,
    attempts: int = RESET_ATTEMPTS,
) -> GpuResetResult:
    """单个 GPU 热复位；"Resource temporarily unavailable" 等待后重试（≤attempts）。"""
    import time

    sleep = sleeper or time.sleep
    last = ""
    attempt = 0
    for attempt in range(1, max(1, attempts) + 1):
        res = ssh_run(host_ip, build_reset_cmd(index), user=ssh_user, timeout_s=120, runner=runner)
        last = (res.stdout + "\n" + res.stderr).strip()
        if res.ok:
            return GpuResetResult(host_ip, index, True, attempt, last)
        if not any(pat in last for pat in RETRYABLE_OUTPUT):
            break  # 非可重试错误不浪费时间
        if attempt < attempts:
            sleep(RETRY_WAIT_S)
    return GpuResetResult(host_ip, index, False, attempt, last)


def build_gpu_list_cmd() -> str:
    return "sudo -n mx-smi -L"


def verify_node(host_ip: str, *, ssh_user: str = "ubuntu", runner=None) -> tuple[bool, str]:
    """``mx-smi -L`` 全部 Available → True。"""
    res = ssh_run(host_ip, build_gpu_list_cmd(), user=ssh_user, timeout_s=60, runner=runner)
    text = (res.stdout + "\n" + res.stderr).strip()
    if not res.ok:
        return False, text
    ok = "Unavailable" not in text and "Available" in text
    return ok, text
