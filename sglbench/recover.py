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


# --------------------------------------------------------------------------- #
# 恢复编排
# --------------------------------------------------------------------------- #


@dataclass
class NodeRecovery:
    host_ip: str
    node: str
    issues: list[PodIssue] = field(default_factory=list)
    resets: list[GpuResetResult] = field(default_factory=list)
    healthy: bool | None = None
    healthy_text: str = ""
    rebuilt: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if any(not r.ok for r in self.resets):
            return False
        return self.healthy is not False


@dataclass
class RecoveryReport:
    dry_run: bool
    nodes: list[NodeRecovery] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(n.ok for n in self.nodes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "ok": self.ok,
            "notes": list(self.notes),
            "nodes": [
                {
                    "node": n.node,
                    "host_ip": n.host_ip,
                    "issues": [{"pod": i.pod, "reason": i.reason} for i in n.issues],
                    "resets": [
                        {"index": r.index, "ok": r.ok, "attempts": r.attempts,
                         "last_output": r.last_output[:200]}
                        for r in n.resets
                    ],
                    "healthy": n.healthy,
                    "rebuilt": list(n.rebuilt),
                }
                for n in self.nodes
            ],
        }


def _node_to_host(spec: str) -> tuple[str, str]:
    """``mxgpu-3-31`` → ("mxgpu-3-31", "10.66.3.31")；直接 IP 原样返回。"""
    if spec.replace(".", "").isdigit():
        return spec, spec
    tail = spec.rsplit("-", 1)[-1]
    if tail.isdigit():
        return spec, f"10.66.3.{tail}"
    return spec, spec


def _nodes_from_args(
    nodes, from_pods: bool, *, namespace: str, kubectl_runner
) -> list[tuple[str, str, list[PodIssue]]]:
    """→ [(host_ip, node_name, issues)]（同 host 去重合并）。"""
    out: dict[str, tuple[str, list[PodIssue]]] = {}
    if from_pods:
        data, _res = fetch_pd_pods(namespace=namespace, runner=kubectl_runner)
        for issue in find_pod_issues(data):
            host = issue.host_ip or issue.node
            name, issues = out.get(host, (issue.node or host, []))
            out[host] = (name, issues + [issue])
    for spec in nodes or []:
        name, host = _node_to_host(str(spec))
        prev = out.get(host)
        out[host] = (name, prev[1] if prev else [])
    return [(host, name, issues) for host, (name, issues) in out.items()]


def run_recovery(
    *,
    nodes=None,
    from_pods: bool = False,
    gpus=DEFAULT_GPU_INDICES,
    dry_run: bool = False,
    rebuild: bool = True,
    ssh_user: str = "ubuntu",
    namespace: str = NAMESPACE,
    kubectl_runner=None,
    ssh_runner=None,
    sleeper=None,
) -> RecoveryReport:
    """恢复编排：发现 → 复位 → 验证 → 重建（dry_run 只产计划不动作）。"""
    targets = _nodes_from_args(nodes, from_pods, namespace=namespace, kubectl_runner=kubectl_runner)
    report = RecoveryReport(dry_run=dry_run)
    if not targets:
        report.notes.append("未发现异常 pod，且未指定 --node —— 无需恢复。")
        return report
    for host, node, issues in targets:
        nr = NodeRecovery(host_ip=host, node=node, issues=issues)
        for idx in gpus:
            if dry_run:
                nr.resets.append(GpuResetResult(host, int(idx), True, 0, "(dry-run 未执行)"))
            else:
                nr.resets.append(
                    reset_gpu(host, int(idx), ssh_user=ssh_user, runner=ssh_runner, sleeper=sleeper)
                )
        if not dry_run:
            nr.healthy, nr.healthy_text = verify_node(host, ssh_user=ssh_user, runner=ssh_runner)
            if rebuild and issues:
                for issue in issues:
                    res = kubectl_json(
                        ["delete", "pod", issue.pod, "--wait=false"],
                        namespace=namespace, runner=kubectl_runner,
                    )[1]
                    if res.ok:
                        nr.rebuilt.append(issue.pod)
                    else:
                        report.notes.append(f"删除 pod {issue.pod} 失败: {res.stderr[:120]}")
        report.nodes.append(nr)
    return report


def render_recovery_report(report: RecoveryReport) -> str:
    lines: list[str] = []
    for n in report.nodes:
        head = f"{n.node} ({n.host_ip})"
        if report.dry_run:
            idxs = ", ".join(str(r.index) for r in n.resets) or "默认"
            pods = ", ".join(i.pod for i in n.issues) or "(无)"
            lines.append(f"[计划] {head}：复位 GPU {idxs}；重建 pod {pods}")
            continue
        gpu_txt = ", ".join(f"#{r.index}:{'ok' if r.ok else 'FAIL'}(x{r.attempts})" for r in n.resets)
        lines.append(
            f"{head}：GPU {gpu_txt}；验证={'healthy' if n.healthy else 'UNHEALTHY'}"
            f"；重建 {len(n.rebuilt)} pod"
        )
    if report.notes:
        lines.append("")
        lines += [f"注: {x}" for x in report.notes]
    return "\n".join(lines) or "无需动作。"
