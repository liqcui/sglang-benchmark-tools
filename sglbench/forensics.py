"""崩溃取证：非 ok run 的现场采集与签名提取。

证据（best-effort，单项失败不中断）→ ``data/runs/<run_id>/forensics/``：
pods.json / events.txt / logs/<node>.tail / gpu.txt / signatures.json；
摘要落 ``run_forensics`` 表（agent 可直接 SQL 归因）。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .remote import kubectl_json, ssh_run

NODE_LOG_DIR = "/var/log/sglang-dsv4rc3"
EVENTS_TAIL_LINES = 200
LOG_TAIL_LINES = 2000

# (签名名, 正则) —— 与血泪规程一致；顺序即输出顺序
SIGNATURES: list[tuple[str, str]] = [
    ("xnack", r"xnack\(0x8\)|xnack 0x8"),
    ("memory_violation", r"memory violation\(0x4\)"),
    ("mc_error_illegal_address", r"mcErrorIllegalAddress"),
    ("mccl_wrong_magic", r"wrong[- ]magic"),
    ("connection_reset", r"Connection reset by peer"),
    ("assertion_error", r"AssertionError"),
    ("subprocess_crashed", r"Subprocess .* crashed"),
    ("not_implemented", r"NotImplementedError"),
    ("kv_cache_attr_error", r"AttributeError.*kv_cache"),
    ("address_in_use", r"address already in use"),
]


def extract_signatures(text: str | None, *, source: str = "") -> list[dict[str, Any]]:
    """逐签名取**首个**命中行（name/source/line_no/line）。"""
    hits: list[dict[str, Any]] = []
    for name, pattern in SIGNATURES:
        rx = re.compile(pattern)
        for i, line in enumerate((text or "").splitlines(), start=1):
            if rx.search(line):
                hits.append(
                    {"name": name, "source": source, "line_no": i, "line": line.strip()[:300]}
                )
                break
    return hits


# --------------------------------------------------------------------------- #
# 现场采集
# --------------------------------------------------------------------------- #


@dataclass
class ForensicsReport:
    ok: bool
    evidence_dir: str
    nodes: list[dict] = field(default_factory=list)
    signatures: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        sigs = ",".join(sorted({s["name"] for s in self.signatures})) or "无签名"
        return f"取证 {'完整' if self.ok else 'partial'}: 节点 {len(self.nodes)} 个, 签名[{sigs}]"


def _runs_dir(paths) -> Path:
    runs_dir = getattr(paths, "runs_dir", None)
    if runs_dir:
        return Path(runs_dir)
    return Path(paths.home) / "runs"


def collect_evidence(
    paths,
    run,
    *,
    trigger: str = "manual",
    namespace: str = "metax-ai-pd",
    kubectl_runner=None,
    ssh_runner=None,
    timeout_s: float = 90.0,
    log_dir: str = NODE_LOG_DIR,
) -> ForensicsReport:
    """采集崩溃现场（best-effort；总时长受 timeout_s 约束；永不抛异常）。"""
    from .recover import PD_LABEL, find_pod_issues  # 复用判据

    deadline = time.monotonic() + timeout_s
    ev = _runs_dir(paths) / run.run_id / "forensics"
    ev.mkdir(parents=True, exist_ok=True)
    rep = ForensicsReport(ok=True, evidence_dir=str(ev))

    # ① pods
    pods_raw, pods_res = kubectl_json(
        ["get", "pods", "-l", PD_LABEL, "-o", "json"], namespace=namespace, runner=kubectl_runner
    )
    if pods_res.ok and pods_raw is not None:
        (ev / "pods.json").write_text(pods_res.stdout, encoding="utf-8")
    else:
        rep.ok = False
        rep.notes.append(f"pods.json 采集失败: {(pods_res.stderr or pods_res.stdout)[:120]}")

    issues = find_pod_issues(pods_raw)
    hosts: dict[str, str] = {}
    for pod in (pods_raw or {}).get("items", []) or []:
        st = pod.get("status") or {}
        node = (pod.get("spec") or {}).get("nodeName", "")
        host = st.get("hostIP", "")
        if host:
            hosts.setdefault(node, host)
    crash_hosts = {i.node: i.host_ip for i in issues}
    rep.nodes = [
        {"node": n, "host_ip": h, "reason": next((i.reason for i in issues if i.node == n), "")}
        for n, h in (crash_hosts or hosts).items()
    ]

    # ② events
    if time.monotonic() < deadline:
        res = kubectl_json(
            ["get", "events", "-o", "wide"], namespace=namespace, runner=kubectl_runner
        )[1]
        if res.ok:
            tail = "\n".join(res.stdout.splitlines()[-EVENTS_TAIL_LINES:])
            (ev / "events.txt").write_text(tail, encoding="utf-8")
        else:
            rep.ok = False
            rep.notes.append(f"events 采集失败: {(res.stderr or res.stdout)[:120]}")

    # ③ 节点日志 + GPU
    logs_dir = ev / "logs"
    logs_dir.mkdir(exist_ok=True)
    gpu_lines: list[str] = []
    for node, host in (crash_hosts or hosts).items():
        if time.monotonic() >= deadline:
            rep.notes.append("超时：部分节点日志未采集")
            rep.ok = False
            break
        cmd = (
            f"sudo -n bash -lc 'f=$(ls -t {log_dir}/*.log 2>/dev/null | head -1); "
            f'echo "== $f"; tail -n {LOG_TAIL_LINES} "$f"\''
        )
        res = ssh_run(host, cmd, timeout_s=60, runner=ssh_runner)
        if res.ok:
            (logs_dir / f"{node}.tail").write_text(res.stdout, encoding="utf-8")
        else:
            rep.ok = False
            rep.notes.append(f"{node} 日志采集失败: {(res.stderr or res.stdout)[:120]}")
        gres = ssh_run(host, "sudo -n mx-smi -L", timeout_s=30, runner=ssh_runner)
        if gres.ok:
            gpu_lines.append(f"== {node} ({host})\n{gres.stdout.strip()}")
        else:
            rep.ok = False
            rep.notes.append(f"{node} GPU 状态采集失败")

    if gpu_lines:
        (ev / "gpu.txt").write_text("\n".join(gpu_lines) + "\n", encoding="utf-8")

    # ④ 签名
    for node in (crash_hosts or hosts):
        f = logs_dir / f"{node}.tail"
        if f.exists():
            rep.signatures.extend(
                extract_signatures(f.read_text(encoding="utf-8", errors="replace"), source=node)
            )
    evf = ev / "events.txt"
    if evf.exists():
        rep.signatures.extend(
            extract_signatures(evf.read_text(encoding="utf-8", errors="replace"), source="events")
        )
    (ev / "signatures.json").write_text(
        json.dumps(rep.signatures, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return rep
