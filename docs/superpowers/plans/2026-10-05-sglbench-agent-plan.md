# sglbench Agent 能力实施计划（Plan 2：S4–S6）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐 agent 闭环：① GPU 复位自愈（`sglbench recover` + `run/multiturn --gpu-reset`）② 崩溃取证归档（`sglbench forensics` + 非 ok 自动采集 + `run_forensics` 表）③ 战役执行器（`sglbench campaign run`）。

**Architecture:** 新增 `remote.py`（kubectl/ssh 执行封装，命令构建为纯函数、执行器可注入 mock）→ `recover.py`（pod 异常发现 → ssh `mx-smi -y -r` 热复位 → 重建 → 验证）与 `forensics.py`（pod/事件/节点日志/GPU 取证 + 崩溃签名提取）复用 remote 通道；`campaign.py` 增 spec 解析 + 执行器（runner 可注入）。全部显式触发（已批准决策：`--gpu-reset` 标志 / `recover` 命令；MCP 侧两段式在 Plan 3 实现）。

**Tech Stack:** Python ≥3.10 标准库（subprocess/json/re/shutil）+ duckdb；unittest（`python3 -m unittest tests.test_x -v`，仓库根运行）。

**Spec:** `docs/superpowers/specs/2026-10-05-sglbench-viz-agent-openclaw-design.md`（§3 agent 设计、§7 测试、§8 S4–S6）

## Global Constraints

- 回归红线：现有 **229** 个测试保持全绿（`python3 -m unittest discover -s tests -t . -q`）。
- 测试**绝不允许**真连集群：所有 kubectl/ssh 调用必须经可注入执行器（默认 `remote.run_cmd`，测试注入 fake）；涉及恢复/取证的 CLI 级测试用 `unittest.mock.patch` 替换 `sglbench.recover.*` / `sglbench.forensics.collect_evidence`。
- node 访问通道（本机已实测）：`ssh -o BatchMode=yes ubuntu@<hostIP>` 免密；`sudo -n mx-smi` 免密；GPU 复位配方默认 `-i 0` + `-i 4`（`--gpus all` = 0..7）。
- "Resource temporarily unavailable" → 等 30-60s 重试（≤3 次/索引）——沿用血泪规程。
- 节点日志目录常量 `/var/log/sglang-dsv4rc3/`（每节点最新 `rank<N>-<mdd-hhmmss>.log`）。
- `sglbench run/multiturn --auto-retry` 语义不变；新增 `--gpu-reset`（未给时行为与现状完全一致）。
- 提交信息尾注 `Co-Authored-By: Claude Code <noreply@anthropic.com>`；中文注释风格。

---

### Task 1: `remote.py` —— kubectl/ssh 执行封装

**Files:**
- Create: `sglbench/remote.py`
- Test: `tests/test_remote.py`

**Interfaces:**
- Produces（后续任务全部依赖）:

```python
@dataclass
class CmdResult:
    ok: bool
    stdout: str
    stderr: str
    argv: list[str]
    returncode: int | None  # None=超时/不可执行

def run_cmd(argv: Sequence[str], *, timeout_s: float = 30.0) -> CmdResult
def build_ssh_argv(host: str, remote_cmd: str, *, user: str = "ubuntu", connect_timeout_s: int = 10) -> list[str]
def ssh_run(host: str, remote_cmd: str, *, user: str = "ubuntu", timeout_s: float = 30.0, runner: Callable[[Sequence[str]], CmdResult] | None = None) -> CmdResult
def kubectl_json(resource_args: Sequence[str], *, namespace: str | None = None, timeout_s: float = 30.0, runner=None) -> tuple[Any | None, CmdResult]
```

- [ ] **Step 1: Write the failing test**

```python
"""remote 执行封装单测（命令构建纯函数 + 注入执行器）。"""
from __future__ import annotations

import unittest

from sglbench.remote import CmdResult, build_ssh_argv, kubectl_json, run_cmd, ssh_run


class TestRemote(unittest.TestCase):
    def test_build_ssh_argv(self):
        argv = build_ssh_argv("10.66.3.29", "sudo -n mx-smi -L", user="ubuntu")
        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", " ".join(argv))
        self.assertIn("ubuntu@10.66.3.29", argv)
        self.assertEqual(argv[-1], "sudo -n mx-smi -L")

    def test_run_cmd_real(self):
        ok = run_cmd(["true"], timeout_s=5)
        self.assertTrue(ok.ok)
        self.assertEqual(ok.returncode, 0)
        bad = run_cmd(["false"], timeout_s=5)
        self.assertFalse(bad.ok)
        missing = run_cmd(["definitely-not-a-cmd-xyz"], timeout_s=5)
        self.assertFalse(missing.ok)
        self.assertIsNone(missing.returncode)

    def test_ssh_run_injects_runner(self):
        seen = []

        def fake(argv):
            seen.append(list(argv))
            return CmdResult(ok=True, stdout="GPU#0 ok\n", stderr="", argv=list(argv), returncode=0)

        res = ssh_run("10.66.3.29", "sudo -n mx-smi -L", runner=fake)
        self.assertTrue(res.ok)
        self.assertEqual(seen[0][0], "ssh")
        self.assertIn("10.66.3.29", seen[0][-2])

    def test_kubectl_json_parses(self):
        def fake(argv):
            return CmdResult(ok=True, stdout='{"items": [{"a": 1}]}', stderr="",
                             argv=list(argv), returncode=0)

        data, res = kubectl_json(["get", "pods", "-o", "json"], namespace="metax-ai-pd", runner=fake)
        self.assertTrue(res.ok)
        self.assertEqual(data["items"][0]["a"], 1)
        # JSON 坏掉 → (None, res)
        def bad(argv):
            return CmdResult(ok=True, stdout="not-json", stderr="", argv=list(argv), returncode=0)

        data, res = kubectl_json(["get", "pods"], runner=bad)
        self.assertIsNone(data)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_remote`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write implementation**

```python
"""kubectl / ssh 外部命令执行封装。

设计要点：**命令构建是纯函数**（易测），**执行器可注入**（测试零真实连接）。
所有函数吞掉异常并以 :class:`CmdResult` 返回，调用侧按 ok 判断。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Sequence

DEFAULT_SSH_USER = "ubuntu"
SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new")


@dataclass
class CmdResult:
    ok: bool
    stdout: str
    stderr: str
    argv: list[str]
    returncode: int | None = None  # None = 超时/无法执行


def run_cmd(argv: Sequence[str], *, timeout_s: float = 30.0) -> CmdResult:
    """执行命令；超时/不存在 → ok=False, returncode=None（不抛）。"""
    argv = [str(a) for a in argv]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
        return CmdResult(proc.returncode == 0, proc.stdout, proc.stderr, argv, proc.returncode)
    except subprocess.TimeoutExpired as exc:
        return CmdResult(False, exc.stdout or "", f"timeout after {timeout_s}s", argv, None)
    except OSError as exc:
        return CmdResult(False, "", str(exc), argv, None)


def build_ssh_argv(
    host: str, remote_cmd: str, *, user: str = DEFAULT_SSH_USER, connect_timeout_s: int = 10
) -> list[str]:
    opts = ("-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout_s}",
            "-o", "StrictHostKeyChecking=accept-new")
    return ["ssh", *opts, f"{user}@{host}", remote_cmd]


def ssh_run(
    host: str,
    remote_cmd: str,
    *,
    user: str = DEFAULT_SSH_USER,
    timeout_s: float = 30.0,
    runner: Callable[[Sequence[str]], CmdResult] | None = None,
) -> CmdResult:
    argv = build_ssh_argv(host, remote_cmd, user=user)
    fn = runner or (lambda a: run_cmd(a, timeout_s=timeout_s))
    try:
        return fn(argv)
    except Exception as exc:  # 注入执行器/网络层的任何异常都归为失败
        return CmdResult(False, "", str(exc), argv, None)


def kubectl_json(
    resource_args: Sequence[str],
    *,
    namespace: str | None = None,
    timeout_s: float = 30.0,
    runner: Callable[[Sequence[str]], CmdResult] | None = None,
) -> tuple[Any | None, CmdResult]:
    """``kubectl ... -o json`` → (解析后对象 | None, CmdResult)。"""
    argv = ["kubectl"]
    if namespace:
        argv += ["-n", namespace]
    argv += [str(a) for a in resource_args]
    fn = runner or (lambda a: run_cmd(a, timeout_s=timeout_s))
    try:
        res = fn(argv)
    except Exception as exc:
        res = CmdResult(False, "", str(exc), argv, None)
    if not res.ok:
        return None, res
    try:
        return json.loads(res.stdout or "null"), res
    except json.JSONDecodeError:
        return None, res


def kubectl_available() -> bool:
    return shutil.which("kubectl") is not None
```

（SSH_OPTS 常量与 build_ssh_argv 内 opts 重复——删掉模块级 SSH_OPTS，仅保留函数内构建。）

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_remote -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add sglbench/remote.py tests/test_remote.py
git commit -m "sglbench: remote.py kubectl/ssh 执行封装（命令纯函数 + 执行器可注入）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: `recover.py` —— pod 异常发现

**Files:**
- Create: `sglbench/recover.py`
- Test: `tests/test_recover.py`

**Interfaces:**
- Consumes: Task 1 `kubectl_json`。
- Produces:

```python
PD_LABEL = "llm-d.ai/guide=pd-disaggregation"
NAMESPACE = "metax-ai-pd"

@dataclass
class PodIssue:
    pod: str
    node: str
    host_ip: str
    reason: str  # not_ready / restarts=N / waiting:Cause / exit=CODE

def find_pod_issues(pods_json: Any) -> list[PodIssue]      # 纯函数
def fetch_pd_pods(*, namespace=NAMESPACE, runner=None) -> tuple[Any | None, "CmdResult"]
def find_unhealthy_pods(*, namespace=NAMESPACE, runner=None) -> list[PodIssue]
```

判据：pod 非 Ready（Ready condition False）或容器 `restartCount > 0` 或 waiting.reason ∈ {CrashLoopBackOff, ImagePullBackOff, RunContainerError} 或 lastState.terminated.exitCode ∉ {0, None}。

- [ ] **Step 1: Write the failing test**

```python
"""recover pod 异常发现单测（fixture JSON, 零真实 kubectl）。"""
from __future__ import annotations

import unittest

from sglbench.recover import find_pod_issues

PODS = {
    "items": [
        {   # Ready 且无重启 → 无异常
            "metadata": {"name": "prefill-hicache-0-0", "labels": {}},
            "status": {
                "hostIP": "10.66.3.29",
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [{"name": "engine", "ready": True, "restartCount": 0, "state": {"running": {}}}],
            },
            "spec": {"nodeName": "mxgpu-3-29"},
        },
        {   # 非 Ready
            "metadata": {"name": "prefill-hicache-0-1"},
            "status": {
                "hostIP": "10.66.3.31",
                "conditions": [{"type": "Ready", "status": "False"}],
                "containerStatuses": [{"name": "engine", "ready": False, "restartCount": 0, "state": {"running": {}}}],
            },
            "spec": {"nodeName": "mxgpu-3-31"},
        },
        {   # CrashLoopBackOff + 退出码
            "metadata": {"name": "decode-hicache-0-2"},
            "status": {
                "hostIP": "10.66.3.32",
                "conditions": [{"type": "Ready", "status": "False"}],
                "containerStatuses": [{
                    "name": "engine", "ready": False, "restartCount": 3,
                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                    "lastState": {"terminated": {"exitCode": 137}},
                }],
            },
            "spec": {"nodeName": "mxgpu-3-32"},
        },
    ]
}


class TestFindPodIssues(unittest.TestCase):
    def test_detects(self):
        issues = find_pod_issues(PODS)
        self.assertEqual([i.pod for i in issues], ["prefill-hicache-0-1", "decode-hicache-0-2"])
        self.assertEqual(issues[0].host_ip, "10.66.3.31")
        self.assertIn("not_ready", issues[0].reason)
        self.assertIn("CrashLoopBackOff", issues[1].reason)
        self.assertIn("exit=137", issues[1].reason)

    def test_empty_and_garbage(self):
        self.assertEqual(find_pod_issues({"items": []}), [])
        self.assertEqual(find_pod_issues(None), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_recover`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write implementation**（`sglbench/recover.py` 第一段）

```python
"""崩溃恢复：pod 异常发现 → GPU 热复位（ssh mx-smi）→ 重建 → 验证。

通道实测（本机 10.66.2.1）：``ssh -o BatchMode=yes ubuntu@<hostIP>`` 免密 +
``sudo -n mx-smi`` 免密。仅显式触发（``--gpu-reset`` 标志 / ``sglbench recover``）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

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
    state = cs.get("state") or {}
    waiting = (state.get("waiting") or {}).get("reason")
    if waiting in ("CrashLoopBackOff", "ImagePullBackOff", "RunContainerError"):
        return f"waiting:{waiting}"
    last = ((cs.get("lastState") or {}).get("terminated") or {})
    code = last.get("exitCode")
    if code not in (None, 0):
        return f"exit={code}"
    return None


def find_pod_issues(pods_json: Any) -> list[PodIssue]:
    """从 ``kubectl get pods -o json`` 输出里找出异常 pod（纯函数）。"""
    issues: list[PodIssue] = []
    for pod in (pods_json or {}).get("items", []) or []:
        meta = pod.get("metadata") or {}
        status = pod.get("status") or {}
        spec = pod.get("spec") or {}
        name = meta.get("name", "")
        node = spec.get("nodeName", "")
        host_ip = status.get("hostIP", "")
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
            issues.append(PodIssue(pod=name, node=node, host_ip=host_ip, reason=",".join(reasons)))
    return issues


def fetch_pd_pods(
    *, namespace: str = NAMESPACE, runner=None
) -> tuple[Any | None, CmdResult]:
    return kubectl_json(
        ["get", "pods", "-l", PD_LABEL, "-o", "json"], namespace=namespace, runner=runner
    )


def find_unhealthy_pods(*, namespace: str = NAMESPACE, runner=None) -> list[PodIssue]:
    data, _res = fetch_pd_pods(namespace=namespace, runner=runner)
    return find_pod_issues(data)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_recover -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add sglbench/recover.py tests/test_recover.py
git commit -m "sglbench: recover pod 异常发现（Ready/重启/等待原因/退出码判据）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: `recover.py` —— GPU 热复位 + 节点验证

**Files:**
- Modify: `sglbench/recover.py`（追加）
- Test: `tests/test_recover.py`（追加）

**Interfaces:**
- Produces:

```python
@dataclass
class GpuResetResult:
    host: str
    index: int
    ok: bool
    attempts: int
    last_output: str

def build_reset_cmd(index: int) -> str                       # "sudo -n mx-smi -y -r -i {i}"
def reset_gpu(host_ip, index, *, ssh_user="ubuntu", runner=None, sleeper=time.sleep,
              attempts=RESET_ATTEMPTS) -> GpuResetResult
def build_gpu_list_cmd() -> str                              # "sudo -n mx-smi -L"
def verify_node(host_ip, *, ssh_user="ubuntu", runner=None) -> tuple[bool, str]
```

`reset_gpu`：输出含 RETRYABLE_OUTPUT → `sleeper(RETRY_WAIT_S)` 后重试；非 0 退出或超时同样按 attempts 重试但只在 retryable 场景等待（其它直接失败）。

- [ ] **Step 1: Write the failing test**（追加）

```python
import time as _time

from sglbench.remote import CmdResult
from sglbench.recover import build_reset_cmd, reset_gpu, verify_node


class TestGpuReset(unittest.TestCase):
    def test_build_cmd(self):
        self.assertEqual(build_reset_cmd(4), "sudo -n mx-smi -y -r -i 4")

    def test_transient_then_success(self):
        calls, sleeps = [], []

        def fake(argv):
            calls.append(argv)
            if len(calls) == 1:
                return CmdResult(False, "", "Resource temporarily unavailable", list(argv), 1)
            return CmdResult(True, "reset done", "", list(argv), 0)

        r = reset_gpu("10.66.3.29", 0, runner=fake, sleeper=sleeps.append)
        self.assertTrue(r.ok)
        self.assertEqual(r.attempts, 2)
        self.assertEqual(sleeps, [45.0])

    def test_nonretryable_fail(self):
        def fake(argv):
            return CmdResult(False, "", "unknown error", list(argv), 1)

        r = reset_gpu("10.66.3.29", 0, runner=fake, sleeper=lambda s: None)
        self.assertFalse(r.ok)
        self.assertEqual(r.attempts, 1)  # 非 retryable 不重试

    def test_exhausted_retries(self):
        def fake(argv):
            return CmdResult(False, "", "Resource temporarily unavailable", list(argv), 1)

        r = reset_gpu("10.66.3.29", 4, runner=fake, sleeper=lambda s: None)
        self.assertFalse(r.ok)
        self.assertEqual(r.attempts, 3)

    def test_verify_node(self):
        def fake(argv):
            return CmdResult(True, "GPU#0 ... Available (UUID: x)\nGPU#1 ... Available\n", "", list(argv), 0)

        ok, text = verify_node("10.66.3.29", runner=fake)
        self.assertTrue(ok)
        self.assertIn("Available", text)

        def fake2(argv):
            return CmdResult(True, "GPU#0 ... Unavailable\n", "", list(argv), 0)

        ok, _ = verify_node("10.66.3.29", runner=fake2)
        self.assertFalse(ok)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_recover`
Expected: FAIL — ImportError (build_reset_cmd)

- [ ] **Step 3: Write implementation**（追加）

```python
import time
from typing import Callable


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
    sleeper: Callable[[float], None] | None = None,
    attempts: int = RESET_ATTEMPTS,
) -> GpuResetResult:
    """单个 GPU 热复位；"Resource temporarily unavailable" 等待后重试。"""
    sleep = sleeper or time.sleep
    last = ""
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
```

注意：`attempt` 在 break 后引用——把 `attempt = 0` 预置在循环前，或使用 `enumerate` 后在末尾统一返回（上面 `for attempt in ...` 在 break 时变量已绑定，Python 合法；`attempts=0` 防御化 max(1,·) 已处理）。

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_recover -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add sglbench/recover.py tests/test_recover.py
git commit -m "sglbench: recover GPU 热复位（retryable 等待重试/非错即停）+ 节点验证

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: `recover.py` 编排 + `sglbench recover` CLI

**Files:**
- Modify: `sglbench/recover.py`（编排 + 计划/报告渲染）、`sglbench/cli.py`（parser + `cmd_recover`）
- Test: `tests/test_recover.py`（编排）、`tests/test_cli.py`（CLI dry-run）

**Interfaces:**
- Produces:

```python
@dataclass
class NodeRecovery:
    host_ip: str
    node: str
    issues: list[PodIssue]
    resets: list[GpuResetResult] = field(default_factory=list)
    healthy: bool | None = None
    healthy_text: str = ""
    rebuilt: list[str] = field(default_factory=list)

@dataclass
class RecoveryReport:
    dry_run: bool
    nodes: list[NodeRecovery] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    @property
    def ok(self) -> bool

def plan_recovery(hosts: Sequence[tuple[str, str, list[PodIssue]]], gpus) -> list[str]  # 人类可读计划行
def run_recovery(*, nodes=None, from_pods=False, gpus=DEFAULT_GPU_INDICES, dry_run=False,
                 rebuild=True, ssh_user="ubuntu", namespace=NAMESPACE,
                 kubectl_runner=None, ssh_runner=None, sleeper=None) -> RecoveryReport
def render_recovery_report(report: RecoveryReport) -> str
```

**CLI**：`sglbench recover [--from-pods | --node mxgpu-3-29 ...] [--gpus 0,4|all] [--dry-run] [--no-rebuild] [--ssh-user ubuntu] [--json]`。
`--from-pods` 与 `--node` 至少给一个；`--node` 接受节点名（mxgpu-3-29 → hostIP 末段 .29 推导：`10.66.3.<末段>`；`--node` 也可直接给 IP）。
重建 = `kubectl delete pod <name> -n metax-ai-pd --wait=false`（LWS 自动重建组）。

- [ ] **Step 1: Write the failing tests**（recover 编排，追加 `tests/test_recover.py`）

```python
from sglbench.recover import RecoveryReport, run_recovery


class TestRecoveryOrchestration(unittest.TestCase):
    def _pod_issues(self):
        from sglbench.recover import PodIssue
        return [PodIssue(pod="prefill-0-1", node="mxgpu-3-31", host_ip="10.66.3.31", reason="not_ready")]

    def test_dry_run_plans_only(self):
        calls = []
        rep = run_recovery(
            nodes=None, from_pods=True, gpus=(0, 4), dry_run=True,
            kubectl_runner=lambda a: (calls.append(list(a)) or __import__("sglbench.remote", fromlist=["CmdResult"]).CmdResult(False, "", "no kubectl in dry-run", list(a), None)),
            ssh_runner=lambda a: calls.append(list(a)),
        )
        # dry-run 不执行任何 ssh / kubectl 写操作（kubectl 只读发现允许）
        self.assertTrue(rep.dry_run)
        self.assertEqual([c for c in calls if c[0] == "ssh"], [])

    def test_full_recovery_with_injected_runners(self):
        from sglbench.remote import CmdResult

        deletes = []

        def kubectl(argv):
            if "get" in argv:
                import json
                return CmdResult(True, json.dumps({"items": [
                    {"metadata": {"name": "prefill-0-1"},
                     "spec": {"nodeName": "mxgpu-3-31"},
                     "status": {"hostIP": "10.66.3.31",
                                "conditions": [{"type": "Ready", "status": "False"}],
                                "containerStatuses": []}},
                ]}), "", list(argv), 0)
            deletes.append(list(argv))
            return CmdResult(True, "", "", list(argv), 0)

        def ssh(argv):
            cmd = argv[-1]
            if "mx-smi -L" in cmd:
                return CmdResult(True, "GPU#0 Available\nGPU#1 Available\n", "", list(argv), 0)
            return CmdResult(True, "reset ok", "", list(argv), 0)

        rep = run_recovery(from_pods=True, gpus=(0, 4), kubectl_runner=kubectl,
                           ssh_runner=ssh, sleeper=lambda s: None)
        self.assertTrue(rep.ok)
        self.assertEqual(len(rep.nodes), 1)
        node = rep.nodes[0]
        self.assertEqual([r.index for r in node.resets], [0, 4])
        self.assertTrue(all(r.ok for r in node.resets))
        self.assertTrue(node.healthy)
        self.assertIn("prefill-0-1", node.rebuilt)
        self.assertTrue(any("delete" in " ".join(d) for d in deletes))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_recover`
Expected: FAIL — ImportError (run_recovery)

- [ ] **Step 3: Write implementation**（追加）

```python
def _nodes_from_args(
    nodes: Sequence[str] | None, from_pods: bool, *, namespace: str, kubectl_runner
) -> list[tuple[str, str, list[PodIssue]]]:
    """→ [(host_ip, node_name, issues)]。--node 只给名字时无 issues。"""
    out: list[tuple[str, str, list[PodIssue]]] = []
    if from_pods:
        data, _res = fetch_pd_pods(namespace=namespace, runner=kubectl_runner)
        by_host: dict[str, list[PodIssue]] = {}
        for issue in find_pod_issues(data):
            by_host.setdefault(issue.host_ip, []).append(issue)
        for host, issues in by_host.items():
            out.append((host, issues[0].node, issues))
    for spec in nodes or []:
        if "/" in spec or spec.replace(".", "").isdigit():
            host = spec
            name = spec
        else:  # mxgpu-3-31 → 10.66.3.31
            name = spec
            tail = spec.rsplit("-", 1)[-1]
            host = f"10.66.3.{tail}" if tail.isdigit() else spec
        out.append((host, name, []))
    # 去重（同 host 合并 issues）
    merged: dict[str, tuple[str, str, list[PodIssue]]] = {}
    for host, name, issues in out:
        if host in merged:
            prev_name, _n, prev_issues = merged[host]
            merged[host] = (prev_name, name, prev_issues + issues)
        else:
            merged[host] = (host, name, issues)
    return [(h, n, i) for h, (nn, n_ignored, i) in merged.items()] if False else list(merged.values())
```

（简化：上面 merged 值结构改成 `(name, issues)`；实现时写干净版本，语义 = 去重合并。）

```python
def run_recovery(
    *,
    nodes: Sequence[str] | None = None,
    from_pods: bool = False,
    gpus: Sequence[int] = DEFAULT_GPU_INDICES,
    dry_run: bool = False,
    rebuild: bool = True,
    ssh_user: str = "ubuntu",
    namespace: str = NAMESPACE,
    kubectl_runner=None,
    ssh_runner=None,
    sleeper=None,
) -> RecoveryReport:
    targets = _nodes_from_args(nodes, from_pods, namespace=namespace, kubectl_runner=kubectl_runner)
    report = RecoveryReport(dry_run=dry_run)
    if not targets:
        report.notes.append("未发现异常 pod，且未指定 --node —— 无需恢复。")
        return report
    for host, node, issues in targets:
        nr = NodeRecovery(host_ip=host, node=node, issues=issues)
        if dry_run:
            report.nodes.append(nr)
            continue
        for idx in gpus:
            nr.resets.append(
                reset_gpu(host, idx, ssh_user=ssh_user, runner=ssh_runner, sleeper=sleeper)
            )
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
            lines.append(f"[计划] {head}：复位 GPU {', '.join(str(r.index) for r in n.resets) or '全部'}；"
                         f"重建 pod {', '.join(i.pod for i in n.issues) or '(无)'}")
            continue
        gpu_txt = ", ".join(f"#{r.index}:{'ok' if r.ok else 'FAIL'}(x{r.attempts})" for r in n.resets)
        lines.append(
            f"{head}：GPU {gpu_txt}；验证={'healthy' if n.healthy else 'UNHEALTHY'}"
            f"；重建 {len(n.rebuilt)} pod"
        )
    if report.notes:
        lines += ["", *[f"注: {x}" for x in report.notes]]
    return "\n".join(lines) or "无需动作。"
```

`plan_recovery` 供 dry-run CLI 打印：对 `targets` 生成上面 `[计划]` 行（实现时把 dry-run 分支也走 `render_recovery_report` 即可，`plan_recovery` 可省——**以 render_recovery_report 为唯一出口**，删掉 plan_recovery 接口）。

CLI：

```python
def cmd_recover(args: argparse.Namespace) -> int:
    from .recover import DEFAULT_GPU_INDICES, render_recovery_report, run_recovery

    gpus = tuple(range(8)) if args.gpus == "all" else tuple(
        int(x) for x in (args.gpus or "0,4").split(",") if x.strip()
    )
    if not args.from_pods and not args.node:
        raise UsageError("至少给 --from-pods 或 --node <mxgpu-3-XX>")
    report = run_recovery(
        nodes=args.node, from_pods=args.from_pods, gpus=gpus,
        dry_run=args.dry_run, rebuild=not args.no_rebuild, ssh_user=args.ssh_user,
    )
    if args.json:
        print(json.dumps(asdict_report(report), ensure_ascii=False, indent=2, default=str))
    else:
        print(render_recovery_report(report))
    return EXIT_OK if report.ok or report.dry_run else EXIT_FAIL
```

parser：

```python
    # recover —— GPU 热复位 + 重建（显式触发; 硬件级动作）
    p = sub.add_parser("recover", help="GPU 热复位+pod 重建（ssh mx-smi; --dry-run 先看计划）")
    _add_common(p)
    p.add_argument("--from-pods", action="store_true", help="自动发现异常 pod 所在节点")
    p.add_argument("--node", action="append", default=[], help="指定节点（mxgpu-3-XX 或 IP; 可重复）")
    p.add_argument("--gpus", default="0,4", help="复位 GPU 索引（默认 0,4; all=0..7）")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-rebuild", action="store_true", help="只复位不删 pod")
    p.add_argument("--ssh-user", default="ubuntu")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_recover)
```

`RecoveryReport` 加 `to_dict()`（asdict + nodes 展平），CLI `--json` 用它。

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_recover -v`
Expected: PASS（含编排 2 新用例）

- [ ] **Step 5: CLI dry-run 测试**（`tests/test_cli.py` 追加）

```python
    def test_recover_dry_run_needs_target(self) -> None:
        with temp_home() as paths:
            code, _out, err = run_cli("recover", "--home", str(paths.home))
            self.assertEqual(code, 2)
            self.assertIn("--from-pods", err)

    def test_recover_dry_run_with_node(self) -> None:
        with temp_home() as paths:
            code, out, err = run_cli(
                "recover", "--home", str(paths.home), "--dry-run", "--node", "mxgpu-3-29"
            )
            self.assertEqual(code, 0, err)
            self.assertIn("10.66.3.29", out)
            self.assertIn("计划", out)
```

（`--dry-run --node` 不执行任何 ssh/kubectl —— `run_recovery` dry_run 分支直接返回节点列表。）

- [ ] **Step 6: Commit**

```bash
git add sglbench/recover.py sglbench/cli.py tests/test_recover.py tests/test_cli.py
git commit -m "sglbench: recover 编排 + CLI（--from-pods/--node/--gpus/--dry-run/--no-rebuild）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: `--gpu-reset` 自愈集成（run / multiturn）

**Files:**
- Modify: `sglbench/cli.py`（`_maybe_gpu_recover` 助手 + 两处 auto-retry 循环插入 + parser flags）
- Test: `tests/test_cli.py`（mock 验证触发语义）

**Interfaces:**
- Produces: `_maybe_gpu_recover(args) -> str | None`（`--gpu-reset` 未开启 → None 不动作；开启 → 调 `recover.run_recovery(from_pods=True, rebuild=True)`，返回一行摘要文本，供重试日志）。
- `run`/`multiturn` parser 增 `--gpu-reset`（store_true），`run`/`multiturn` 的 `--auto-retry` 重试路径在 `_wait_healthy` **之前**调用 `_maybe_gpu_recover`。

- [ ] **Step 1: Write the failing test**

```python
    def test_gpu_reset_gate(self) -> None:
        from unittest import mock

        from sglbench import cli as cli_mod

        args = type("A", (), {"gpu_reset": False})()
        self.assertIsNone(cli_mod._maybe_gpu_recover(args))

        called = {}

        def fake_run_recovery(**kw):
            called.update(kw)

            class R:
                ok = True
                nodes = []
                notes = []

            return R()

        with mock.patch("sglbench.recover.run_recovery", side_effect=fake_run_recovery):
            args = type("A", (), {"gpu_reset": True})()
            text = cli_mod._maybe_gpu_recover(args)
        self.assertTrue(called.get("from_pods"))
        self.assertTrue(called.get("rebuild"))
        self.assertIsNotNone(text)

    def test_run_gpu_reset_flag_parses(self) -> None:
        code, out, _ = run_cli("run", "--scenario", "conc-128-safe", "--dry-run", "--gpu-reset")
        self.assertEqual(code, 0)  # dry-run 不触发恢复
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cli -k gpu`
Expected: FAIL — `_maybe_gpu_recover` 不存在 / argparse 不识别 --gpu-reset

- [ ] **Step 3: Implement**

```python
def _maybe_gpu_recover(args: argparse.Namespace) -> str | None:
    """崩溃自愈的硬件阶段（仅 --gpu-reset 显式开启时触发）。

    复位异常 pod 所在节点的 GPU（默认 -i 0 + -i 4），随后删除异常 pod 由 LWS 重建。
    返回一行摘要（无动作返回 None）。
    """
    if not getattr(args, "gpu_reset", False):
        return None
    from .recover import render_recovery_report, run_recovery

    _eprint("  [gpu-reset] 检测异常 pod → GPU 热复位 → 重建 …")
    report = run_recovery(from_pods=True, rebuild=True)
    summary = render_recovery_report(report)
    _eprint("  [gpu-reset] " + summary.replace("\n", "\n  [gpu-reset] "))
    return summary
```

在 `cmd_run` 重试循环（`attempt += 1` 之后、`_wait_healthy` 之前）与 `cmd_multiturn` 同构位置插入：

```python
            _maybe_gpu_recover(args)
```

parser（run 与 multiturn 各加一行）：

```python
    p.add_argument("--gpu-reset", action="store_true",
                   help="崩溃重试前自动 GPU 热复位+重建（与 --auto-retry 配合; 硬件级动作）")
```

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_cli -k gpu -v` → PASS；随后全量回归（见 Task 9 统一跑）。

- [ ] **Step 5: Commit**

```bash
git add sglbench/cli.py tests/test_cli.py
git commit -m "sglbench: run/multiturn --gpu-reset（显式开启的复位→重建→等健康自愈闭环）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: `forensics.py` —— 崩溃签名提取

**Files:**
- Create: `sglbench/forensics.py`
- Test: `tests/test_forensics.py`

**Interfaces:**
- Produces:

```python
SIGNATURES: list[tuple[str, str]]   # (name, regex)
def extract_signatures(text: str, *, source: str = "") -> list[dict]
    # [{"name", "source", "line_no", "line"}], 同签名只记首个命中
```

签名表（血泪沉淀）：`xnack(0x8)`、`memory violation(0x4)`、`mcErrorIllegalAddress`、`MCCL wrong-magic`、`Connection reset by peer`、`AssertionError`、`Subprocess .* crashed`、`NotImplementedError`、`AttributeError.*kv_cache`、`address already in use`。

- [ ] **Step 1: Write the failing test**

```python
"""forensics 签名提取单测（fixture 日志片段）。"""
from __future__ import annotations

import unittest

from sglbench.forensics import extract_signatures

LOG = """\
[2026-10-05 08:01:02] server started
[2026-10-05 08:14:11] [MXC][E]memory violation(0x4) at 0x7f... (trapping pipeID 3)
[2026-10-05 08:14:11] Gloo: Connection reset by peer
[2026-10-05 08:14:12] [MXC][E]memory violation(0x4) again
[2026-10-05 08:14:13] Subprocess scheduler_0 crashed exit -6
"""


class TestSignatures(unittest.TestCase):
    def test_extract(self):
        hits = extract_signatures(LOG, source="mxgpu-3-31")
        names = [h["name"] for h in hits]
        self.assertIn("memory_violation", names)
        self.assertIn("connection_reset", names)
        self.assertIn("subprocess_crashed", names)
        mv = next(h for h in hits if h["name"] == "memory_violation")
        self.assertEqual(mv["line_no"], 2)          # 首个命中行（1-based）
        self.assertEqual(mv["source"], "mxgpu-3-31")
        self.assertIn("memory violation(0x4)", mv["line"])
        self.assertEqual(names.count("memory_violation"), 1)  # 只记首个

    def test_empty(self):
        self.assertEqual(extract_signatures(""), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_forensics`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write implementation**（`sglbench/forensics.py` 第一段）

```python
"""崩溃取证：非 ok run 的现场采集与签名提取。

证据（best-effort，单项失败不中断）→ ``data/runs/<run_id>/forensics/``：
pods.json / events.txt / logs/<node>.tail / gpu.txt / signatures.json；
摘要落 ``run_forensics`` 表（agent 可直接 SQL 归因）。
"""
from __future__ import annotations

import re
from typing import Any

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


def extract_signatures(text: str, *, source: str = "") -> list[dict[str, Any]]:
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_forensics -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add sglbench/forensics.py tests/test_forensics.py
git commit -m "sglbench: forensics 崩溃签名提取（xnack/mv0x4/MCCL 等 10 条, 只记首个命中）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: `run_forensics` 表（schema v2）

**Files:**
- Modify: `sglbench/store.py`（`SCHEMA_VERSION = 1 → 2`；`_DDL` 追加表；`save_forensics` / `get_forensics` / `list_forensics`）
- Test: `tests/test_store.py`（追加）

**Interfaces:**
- Produces:

```python
CREATE TABLE IF NOT EXISTS run_forensics (
    run_id VARCHAR PRIMARY KEY, collected_at VARCHAR, trigger VARCHAR,
    nodes_json VARCHAR, signatures_json VARCHAR, evidence_dir VARCHAR,
    ok BOOLEAN, notes VARCHAR
)
store.save_forensics(run_id, *, trigger, nodes, signatures, evidence_dir, ok, notes="") -> None
store.get_forensics(run_id) -> dict | None
store.list_forensics(limit: int = 20) -> list[dict]
```

- [ ] **Step 1: Write the failing test**（追加 `tests/test_store.py`）

```python
    def test_forensics_roundtrip(self) -> None:
        with temp_home() as paths, Store(paths.db) as store:
            self.assertEqual(store.get_forensics("nope"), None)
            store.save_forensics(
                "run-1", trigger="auto",
                nodes=[{"node": "mxgpu-3-31", "host_ip": "10.66.3.31"}],
                signatures=[{"name": "xnack", "source": "mxgpu-3-31", "line_no": 3, "line": "..."}],
                evidence_dir="/tmp/ev", ok=True, notes="partial: gpu.txt 缺失",
            )
            row = store.get_forensics("run-1")
            self.assertEqual(row["trigger"], "auto")
            self.assertTrue(row["ok"])
            self.assertIn("xnack", row["signatures_json"])
            self.assertIn("mxgpu-3-31", row["nodes_json"])
            rows = store.list_forensics(limit=5)
            self.assertEqual(rows[0]["run_id"], "run-1")
            # schema 版本提升
            self.assertEqual(store.schema_version(), 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_store -k forensics`
Expected: FAIL — no attribute 'save_forensics'

- [ ] **Step 3: Implement**（store.py）

i. `SCHEMA_VERSION = 1` → `SCHEMA_VERSION = 2`。
ii. `_DDL` 列表追加：

```python
    """
    CREATE TABLE IF NOT EXISTS run_forensics (
        run_id          VARCHAR PRIMARY KEY,
        collected_at    VARCHAR,
        trigger         VARCHAR,
        nodes_json      VARCHAR,
        signatures_json VARCHAR,
        evidence_dir    VARCHAR,
        ok              BOOLEAN,
        notes           VARCHAR
    )
    """,
```

iii. 方法（放 `save_environment` 附近）：

```python
    # ---- 崩溃取证 ----
    def save_forensics(
        self,
        run_id: str,
        *,
        trigger: str,
        nodes: Any,
        signatures: Any,
        evidence_dir: str,
        ok: bool,
        notes: str = "",
        commit: bool = True,
    ) -> None:
        from .util import iso, utcnow

        self.con.execute(
            "INSERT OR REPLACE INTO run_forensics "
            "(run_id, collected_at, trigger, nodes_json, signatures_json, evidence_dir, ok, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run_id, iso(utcnow()), trigger,
                json.dumps(nodes, ensure_ascii=False, default=str),
                json.dumps(signatures, ensure_ascii=False, default=str),
                evidence_dir, bool(ok), notes,
            ],
        )
        if commit:
            self.con.execute("COMMIT")

    def get_forensics(self, run_id: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM run_forensics WHERE run_id = ?", [run_id])
        return rows[0] if rows else None

    def list_forensics(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM run_forensics ORDER BY collected_at DESC NULLS LAST LIMIT ?", [limit]
        )
```

（`json` 已在 store.py 导入；`util.iso/utcnow` 的导入方式按文件现有风格——store.py 顶部若无则函数内 import 保稳。）

iv. `db rebuild` 路径（store.py:721 附近 tables 元组）追加 `"run_forensics"`。

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_store -q` → PASS（注意 schema 版本断言已同步为 2；如现有测试断言 schema_version==1，一并更新为 2 并在此提交说明）

- [ ] **Step 5: Commit**

```bash
git add sglbench/store.py tests/test_store.py
git commit -m "sglbench: run_forensics 表（schema v2）+ save/get/list 方法

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 8: `forensics.collect_evidence` —— 现场采集

**Files:**
- Modify: `sglbench/forensics.py`（追加）
- Test: `tests/test_forensics.py`（追加）

**Interfaces:**
- Consumes: Task 1 `remote`、Task 2 `find_pod_issues`、Task 6 `extract_signatures`。
- Produces:

```python
NODE_LOG_DIR = "/var/log/sglang-dsv4rc3"

@dataclass
class ForensicsReport:
    ok: bool                      # 全部证据收集成功
    evidence_dir: str
    nodes: list[dict]             # [{"node","host_ip","reason"}]
    signatures: list[dict]
    notes: list[str]              # 每项失败记一条（partial 不中断）

def collect_evidence(paths, run, *, trigger="manual", namespace=NAMESPACE,
                     kubectl_runner=None, ssh_runner=None, timeout_s=90.0,
                     log_dir=NODE_LOG_DIR) -> ForensicsReport
```

采集步骤（各步 try/except，失败 → notes 追加、`ok=False` 继续）：
1. `pods.json` ← `kubectl get pods -l PD_LABEL -o json`（原始 stdout 落盘）；
2. 由 pod 异常 + run 时间窗 → 涉及节点集合；
3. `events.txt` ← `kubectl get events -n ns --sort-by=.lastTimestamp`（尾部 200 行）；
4. 每节点 `logs/<node>.tail` ← `ssh sudo bash -lc 'f=$(ls -t <log_dir>/*.log | head -1); echo "== $f"; tail -n 2000 "$f"'`；
5. `gpu.txt` ← 每节点 `sudo -n mx-smi -L`；
6. `signatures.json` = 对 logs/*.tail + events.txt 跑 `extract_signatures`。
7. `run.meta` 无有用信息时不依赖 run 字段（仅用 run_id 定位目录）。

- [ ] **Step 1: Write the failing test**（追加；全 mock）

```python
import json
import tempfile
from pathlib import Path

from sglbench.forensics import ForensicsReport, collect_evidence
from sglbench.remote import CmdResult


class FakePaths:
    def __init__(self, home: Path):
        self.home = home
        self.runs_dir = home / "runs"


class FakeRun:
    def __init__(self, rid="run-1"):
        self.run_id = rid
        self.meta = {}


class TestCollectEvidence(unittest.TestCase):
    def _runners(self, fail_gpu=False):
        pods = {"items": [
            {"metadata": {"name": "prefill-0-1"}, "spec": {"nodeName": "mxgpu-3-31"},
             "status": {"hostIP": "10.66.3.31",
                        "conditions": [{"type": "Ready", "status": "False"}],
                        "containerStatuses": []}},
        ]}

        def kubectl(argv):
            cmd = " ".join(argv)
            if "get pods" in cmd:
                return CmdResult(True, json.dumps(pods), "", list(argv), 0)
            if "get events" in cmd:
                return CmdResult(True, "10:00 CrashLoopBackOff prefill-0-1\n", "", list(argv), 0)
            return CmdResult(False, "", "unexpected", list(argv), 1)

        def ssh(argv):
            cmd = argv[-1]
            if "mx-smi -L" in cmd:
                if fail_gpu:
                    raise RuntimeError("ssh boom")
                return CmdResult(True, "GPU#0 Available\n", "", list(argv), 0)
            return CmdResult(True, "== rank1.log\n[E]xnack(0x8) boom\n", "", list(argv), 0)

        return kubectl, ssh

    def test_collect_full(self):
        import os
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            os.environ["SGLBENCH_HOME"] = home
            try:
                kubectl, ssh = self._runners()
                rep = collect_evidence(FakePaths(Path(home)), FakeRun(), kubectl_runner=kubectl, ssh_runner=ssh)
            finally:
                os.environ.pop("SGLBENCH_HOME", None)
            ev = Path(rep.evidence_dir)
            self.assertTrue(rep.ok)
            for f in ("pods.json", "events.txt", "gpu.txt", "signatures.json"):
                self.assertTrue((ev / f).exists(), f)
            self.assertTrue((ev / "logs" / "mxgpu-3-31.tail").exists())
            self.assertTrue(any(s["name"] == "xnack" for s in rep.signatures))

    def test_collect_partial_tolerates_ssh_failure(self):
        import os
        with tempfile.TemporaryDirectory() as home:
            os.environ["SGLBENCH_HOME"] = home
            try:
                kubectl, ssh = self._runners(fail_gpu=True)
                rep = collect_evidence(FakePaths(Path(home)), FakeRun(), kubectl_runner=kubectl, ssh_runner=ssh)
            finally:
                os.environ.pop("SGLBENCH_HOME", None)
            self.assertFalse(rep.ok)             # 有失败项
            self.assertTrue(rep.notes)           # 记录了原因
            self.assertTrue(Path(rep.evidence_dir).exists())  # 其余证据仍在
```

`rep.evidence_dir` 定位：`<paths.home>/runs/<run_id>/forensics`——FakePaths 提供 `home`；真实 `Paths` 有 `runs_dir`。collect 内部用 `getattr(paths, "runs_dir", paths.home / "runs")` 兼容两形态。

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_forensics -k collect`
Expected: FAIL — ImportError

- [ ] **Step 3: Write implementation**（追加 forensics.py）

```python
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .remote import kubectl_json, ssh_run

NODE_LOG_DIR = "/var/log/sglang-dsv4rc3"
EVENTS_TAIL_LINES = 200
LOG_TAIL_LINES = 2000


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
    """采集崩溃现场（best-effort；总时长受 timeout_s 约束）。"""
    from .recover import PD_LABEL, find_pod_issues  # 复用判据

    deadline = time.monotonic() + timeout_s
    ev = _runs_dir(paths) / run.run_id / "forensics"
    ev.mkdir(parents=True, exist_ok=True)
    rep = ForensicsReport(ok=True, evidence_dir=str(ev))

    # ① pods
    pods_raw, pods_res = kubectl_json(
        ["get", "pods", "-l", PD_LABEL, "-o", "json"], namespace=namespace, runner=kubectl_runner
    )
    if pods_res.ok:
        (ev / "pods.json").write_text(pods_res.stdout, encoding="utf-8")
    else:
        rep.ok = False
        rep.notes.append(f"pods.json 采集失败: {pods_res.stderr[:120]}")

    issues = find_pod_issues(pods_raw)
    # 节点集合：异常 pod 优先；无异常则取全部 PD pod 节点（崩溃可能已重启→全采）
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
        res = kubectl_json(["get", "events", "-o", "wide"], namespace=namespace, runner=kubectl_runner)[1]
        if res.ok:
            tail = "\n".join(res.stdout.splitlines()[-EVENTS_TAIL_LINES:])
            (ev / "events.txt").write_text(tail, encoding="utf-8")
        else:
            rep.ok = False
            rep.notes.append(f"events 采集失败: {res.stderr[:120]}")

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
            f'sudo -n bash -lc \'f=$(ls -t {log_dir}/*.log 2>/dev/null | head -1); '
            f'echo "== $f"; tail -n {LOG_TAIL_LINES} "$f"\''
        )
        res = ssh_run(host, cmd, timeout_s=60, runner=ssh_runner)
        if res.ok:
            (logs_dir / f"{node}.tail").write_text(res.stdout, encoding="utf-8")
        else:
            rep.ok = False
            rep.notes.append(f"{node} 日志采集失败: {(res.stderr or res.stdout)[:120]}")
        gres = ssh_run(host, "sudo -n mx-smi -L", timeout_s=30, runner=ssh_runner)
        gpu_lines.append(f"== {node} ({host})\n{(gres.stdout or gres.stderr).strip()}")
        if not gres.ok:
            rep.ok = False
            rep.notes.append(f"{node} GPU 状态采集失败")
    if gpu_lines:
        (ev / "gpu.txt").write_text("\n".join(gpu_lines) + "\n", encoding="utf-8")

    # ④ 签名
    for node in (crash_hosts or hosts):
        f = logs_dir / f"{node}.tail"
        if f.exists():
            rep.signatures.extend(extract_signatures(f.read_text(encoding="utf-8", errors="replace"), source=node))
    evf = ev / "events.txt"
    if evf.exists():
        rep.signatures.extend(extract_signatures(evf.read_text(encoding="utf-8", errors="replace"), source="events"))
    (ev / "signatures.json").write_text(
        json.dumps(rep.signatures, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return rep
```

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_forensics -v` → PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add sglbench/forensics.py tests/test_forensics.py
git commit -m "sglbench: forensics 现场采集（pods/events/节点日志/GPU/签名, best-effort 超时保护）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 9: 自动取证钩子 + `sglbench forensics` CLI + show 集成

**Files:**
- Modify: `sglbench/cli.py`（`_maybe_forensics` 助手 + run/multiturn 非 ok 钩子 + `--no-forensics` flags + `cmd_forensics` + `show` 摘要 + parser）
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `_maybe_forensics(args, paths, store, record) -> str | None`——`record.status == "ok"` 或 `args.no_forensics` → None；否则 `collect_evidence(trigger="auto")` + `store.save_forensics(...)` + 返回摘要行；钩子失败静默（不拖累压测流程）。

- [ ] **Step 1: Write the failing test**

```python
    def test_maybe_forensics_gate(self) -> None:
        from unittest import mock

        from sglbench import cli as cli_mod

        class R:
            run_id = "r1"
            status = "ok"

        args = type("A", (), {"no_forensics": False})()
        self.assertIsNone(cli_mod._maybe_forensics(args, None, None, R()))  # ok → 不取证

        class Bad(R):
            status = "partial"

        class Rep:
            ok = False
            notes = ["x"]
            signatures = [{"name": "xnack"}]
            nodes = [{"node": "n"}]
            evidence_dir = "/tmp/ev"
            def summary_line(self):
                return "取证 partial: 节点 1 个, 签名[xnack]"

        saved = {}
        class Store:
            def save_forensics(self, run_id, **kw):
                saved["run_id"] = run_id
                saved.update(kw)

        with mock.patch("sglbench.forensics.collect_evidence", return_value=Rep()):
            text = cli_mod._maybe_forensics(args, None, Store(), Bad())
        self.assertIsNotNone(text)
        self.assertIn("xnack", text)
        self.assertEqual(saved["run_id"], "r1")
        self.assertEqual(saved["trigger"], "auto")

        args2 = type("A", (), {"no_forensics": True})()
        self.assertIsNone(cli_mod._maybe_forensics(args2, None, Store(), Bad()))  # 显式关闭
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cli -k forensics`
Expected: FAIL

- [ ] **Step 3: Implement（cli.py）**

```python
def _maybe_forensics(args, paths, store, record) -> str | None:
    """run 非 ok 时的自动取证（best-effort；--no-forensics 关闭）。"""
    if getattr(args, "no_forensics", False) or record is None or record.status == "ok":
        return None
    if paths is None:  # 纯单元路径（无产物目录）或 --no-store
        return None
    try:
        from .forensics import collect_evidence

        report = collect_evidence(paths, record, trigger="auto")
        if store is not None:
            store.save_forensics(
                record.run_id, trigger="auto", nodes=report.nodes,
                signatures=report.signatures, evidence_dir=report.evidence_dir,
                ok=report.ok, notes="; ".join(report.notes),
            )
        _eprint(f"  [forensics] {report.summary_line()} → {report.evidence_dir}")
        return report.summary_line()
    except Exception as exc:  # 取证永不阻断主流程
        _eprint(f"  [forensics] 采集异常（忽略）: {exc}")
        return None
```

（`paths is None` 分支让单测可传 None——但上面单测传 `paths=None` 会提前返回 None！测试里应传一个 dummy `object()`。修正测试：`cli_mod._maybe_forensics(args, object(), Store(), Bad())`。mock 的 collect_evidence 不碰 paths，安全。）

钩子接入：`cmd_run` 每个 outcome 在 `store.save_run(...)` 之后：

```python
        fnote = _maybe_forensics(args, paths, store, outcome.record)
```

（multiturn 同理，在 archive 后。）

`cmd_forensics`：

```python
def cmd_forensics(args: argparse.Namespace) -> int:
    from .forensics import collect_evidence

    paths = _paths(args)
    with Store(paths.db) as store:
        record = store.get_run(args.run, with_samples=False)
        if record is None:
            _eprint(f"❌ 未找到 run: {args.run}")
            return EXIT_FAIL
        report = collect_evidence(paths, record, trigger="manual")
        store.save_forensics(
            record.run_id, trigger="manual", nodes=report.nodes,
            signatures=report.signatures, evidence_dir=report.evidence_dir,
            ok=report.ok, notes="; ".join(report.notes),
        )
    if args.json:
        print(json.dumps(
            {"ok": report.ok, "evidence_dir": report.evidence_dir, "nodes": report.nodes,
             "signatures": report.signatures, "notes": report.notes},
            ensure_ascii=False, indent=2, default=str,
        ))
    else:
        print(report.summary_line())
        for s in report.signatures:
            print(f"  [{s['name']}] {s['source']}: {s['line']}")
        if report.notes:
            print("提示:", "; ".join(report.notes))
    print(f"证据目录: {report.evidence_dir}")
    return EXIT_OK
```

`cmd_show` 尾部（打印 run 详情后）追加：

```python
    fx = store.get_forensics(run_id)  # run_id 变量按 cmd_show 实际命名调整
    if fx:
        print(f"── 崩溃取证（{fx['trigger']}）: {fx['collected_at']}  ")
        ... # 打印 signatures_json 摘要（json.loads → name 列表）+ evidence_dir
```

parser：

```python
    # forensics
    p = sub.add_parser("forensics", help="崩溃取证（采集现场+签名提取; 非 ok run 自动触发）")
    _add_common(p)
    p.add_argument("run", help="run_id（可前缀匹配）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_forensics)
```

run/multiturn parser 各加：`p.add_argument("--no-forensics", action="store_true", help="非 ok 时不做自动取证")`。

- [ ] **Step 4: Run tests + 全量回归**

Run: `python3 -m unittest tests.test_cli -k forensics -v` → PASS
Run: `python3 -m unittest discover -s tests -t . -q` → OK（全部）

- [ ] **Step 5: Commit**

```bash
git add sglbench/cli.py tests/test_cli.py
git commit -m "sglbench: 非 ok 自动取证钩子 + forensics CLI + show 取证摘要

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 10: campaign spec 解析 + 跳过决策

**Files:**
- Modify: `sglbench/campaign.py`（追加 spec 数据类 + 解析 + 跳过判断）
- Test: `tests/test_campaign.py`（追加）

**Interfaces:**
- Produces:

```python
@dataclass
class SideSpec:
    label: str
    env_file: str | None = None

@dataclass
class ScenarioEntry:
    key: str
    mode: str = "bench"          # bench / multiturn
    max_parallel: int | None = None
    set: dict[str, Any] = field(default_factory=dict)
    repeats: int = 1

@dataclass
class CampaignSpec:
    name: str
    sides: dict[str, SideSpec]
    scenarios: list[ScenarioEntry]
    exec: str = "docker"
    timeout: str = "30m"
    auto_retry: int = 3
    gpu_reset: bool = False
    title: str = ""
    output_dir: str = ""

def load_spec(path) -> CampaignSpec                 # 校验：name/sides(≥1)/scenarios 非空; side 必须含 label
def parse_scenario_entry(raw) -> ScenarioEntry      # "key" 或 {"key":..., "mode":...}
def should_skip(store, entry, side: SideSpec) -> bool
    # True ⇔ 存在 (scenario_key=entry.key, label=side.label, status='ok') 的 run，
    #   且（side.env_file is None 或其 run_environments 快照与最近一次 ok run 相同[即该 run 就是用此 env 跑的]）
```

跳过语义简化（实现）：取 (key, label) 最新 ok run；若不存在 → False；若 env_file 给定 → 读取 env json 的 `deployment` 字段与 run_environments.deployment 比对（指纹最稳的单一字段），一致 → True。

- [ ] **Step 1: Write the failing test**

```python
import json as _json
import tempfile as _tempfile
from pathlib import Path as _Path

from sglbench.campaign import CampaignSpec, load_spec, parse_scenario_entry, should_skip, SideSpec


class CampaignSpecTest(unittest.TestCase):
    def test_parse_entry(self):
        self.assertEqual(parse_scenario_entry("mini-l2-64c").key, "mini-l2-64c")
        e = parse_scenario_entry({"key": "hicache-multiturn-80x10-r16", "mode": "multiturn",
                                  "max_parallel": 4, "set": {"request_rate": 16.0}})
        self.assertEqual(e.mode, "multiturn")
        self.assertEqual(e.max_parallel, 4)
        self.assertEqual(e.set["request_rate"], 16.0)

    def test_load_spec_validation(self):
        with _tempfile.TemporaryDirectory() as td:
            p = _Path(td) / "c.json"
            p.write_text(_json.dumps({"name": "x", "sides": {"baseline": {"label": "A"}},
                                      "scenarios": ["mini-l2-64c"]}), encoding="utf-8")
            spec = load_spec(p)
            self.assertEqual(spec.sides["baseline"].label, "A")
            self.assertEqual(spec.scenarios[0].key, "mini-l2-64c")
            p.write_text(_json.dumps({"name": "x", "sides": {}, "scenarios": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_spec(p)
            p.write_text("{ not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_spec(p)

    def test_should_skip(self):
        with temp_home() as paths, Store(paths.db) as store:
            mk_run(store, "r1", scenario="mini-l2-64c", label="A", day=1,
                   env={"deployment": "dep-1"}, metrics={"mean_ttft_ms": 1.0})
            self.assertTrue(should_skip(store, parse_scenario_entry("mini-l2-64c"), SideSpec("A", None)))
            self.assertTrue(should_skip(store, parse_scenario_entry("mini-l2-64c"),
                                        SideSpec("A", None)))
            # env 指纹不同 → 不跳过
            with _tempfile.TemporaryDirectory() as td:
                bad_env = _Path(td) / "env.json"
                bad_env.write_text(_json.dumps({"deployment": "dep-2"}), encoding="utf-8")
                self.assertFalse(should_skip(store, parse_scenario_entry("mini-l2-64c"),
                                             SideSpec("A", str(bad_env))))
            self.assertFalse(should_skip(store, parse_scenario_entry("mini-l2-999x"), SideSpec("A", None)))
```

（mk_run 是 test_campaign 已有 helper；env 参数即 meta deployment_env。）

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_campaign -k Spec`
Expected: FAIL

- [ ] **Step 3: Implement（campaign.py 追加，spec 区放文件尾部）**

```python
# --------------------------------------------------------------------------- #
# 战役执行器（spec 解析 / 跳过决策 / 执行）
# --------------------------------------------------------------------------- #


@dataclass
class SideSpec:
    label: str
    env_file: str | None = None


@dataclass
class ScenarioEntry:
    key: str
    mode: str = "bench"
    max_parallel: int | None = None
    set: dict[str, Any] = field(default_factory=dict)
    repeats: int = 1


@dataclass
class CampaignSpec:
    name: str
    sides: dict[str, SideSpec]
    scenarios: list[ScenarioEntry]
    exec: str = "docker"
    timeout: str = "30m"
    auto_retry: int = 3
    gpu_reset: bool = False
    title: str = ""
    output_dir: str = ""


def parse_scenario_entry(raw: Any) -> ScenarioEntry:
    if isinstance(raw, str):
        return ScenarioEntry(key=raw)
    if isinstance(raw, dict) and raw.get("key"):
        return ScenarioEntry(
            key=str(raw["key"]),
            mode=str(raw.get("mode", "bench")),
            max_parallel=raw.get("max_parallel"),
            set=dict(raw.get("set") or {}),
            repeats=int(raw.get("repeats", 1)),
        )
    raise ValueError(f"非法的 scenarios 条目: {raw!r}（应为 \"key\" 或 {{\"key\": ...}}）")


def load_spec(path: str | Path) -> CampaignSpec:
    import json

    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"战役 spec 读取失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("战役 spec 必须是 JSON 对象")
    sides_raw = data.get("sides") or {}
    if not sides_raw:
        raise ValueError("战役 spec 需要至少一个 sides 条目（如 baseline/candidate）")
    sides: dict[str, SideSpec] = {}
    for name, sv in sides_raw.items():
        if not isinstance(sv, dict) or not sv.get("label"):
            raise ValueError(f"side {name!r} 需要 label 字段")
        sides[name] = SideSpec(label=str(sv["label"]), env_file=sv.get("env_file"))
    scenarios = [parse_scenario_entry(x) for x in (data.get("scenarios") or [])]
    if not scenarios:
        raise ValueError("战役 spec 的 scenarios 不能为空")
    return CampaignSpec(
        name=str(data.get("name") or p.stem),
        sides=sides,
        scenarios=scenarios,
        exec=str(data.get("exec", "docker")),
        timeout=str(data.get("timeout", "30m")),
        auto_retry=int(data.get("auto_retry", 3)),
        gpu_reset=bool(data.get("gpu_reset", False)),
        title=str(data.get("title", "")),
        output_dir=str(data.get("output_dir", "")),
    )


def should_skip(store: Store, entry: ScenarioEntry, side: SideSpec) -> bool:
    """(key, side.label) 已有 ok run（且 env 指纹一致）→ 跳过。"""
    rows = store.query(
        "SELECT run_id FROM runs WHERE scenario_key = ? AND label = ? AND status = 'ok' "
        "ORDER BY started_at DESC NULLS LAST LIMIT 1",
        [entry.key, side.label],
    )
    if not rows:
        return False
    if not side.env_file:
        return True
    import json

    try:
        want = json.loads(Path(side.env_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True  # env 文件读不了不阻塞（执行时仍会传）
    env = store.get_environment(rows[0]["run_id"]) or {}
    if not env:
        return False  # run 无环境快照 → 无法证明一致, 重跑
    return str(env.get("deployment") or "") == str(want.get("deployment") or "")
```

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_campaign -q` → OK

- [ ] **Step 5: Commit**

```bash
git add sglbench/campaign.py tests/test_campaign.py
git commit -m "sglbench: campaign spec 解析 + 跳过决策（env 指纹一致才跳过）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 11: campaign 执行器

**Files:**
- Modify: `sglbench/campaign.py`（执行器）
- Test: `tests/test_campaign.py`

**Interfaces:**
- Produces:

```python
@dataclass
class ScenarioRunOutcome:
    key: str
    mode: str
    action: str            # skipped / ok / failed / partial
    run_id: str = ""
    detail: str = ""

@dataclass
class CampaignResult:
    side: str
    outcomes: list[ScenarioRunOutcome] = field(default_factory=list)
    @property
    def failures(self) -> list[ScenarioRunOutcome]

def run_campaign(
    paths, spec: CampaignSpec, *, side: str, only: Sequence[str] | None = None,
    force: bool = False, dry_run: bool = False, store=None,
    bench_runner=None,      # callable(entry, side) -> ScenarioRunOutcome（可注入 mock）
    multiturn_runner=None,  # callable(entry, side) -> ScenarioRunOutcome
) -> CampaignResult
def render_campaign_plan(spec, side, entries_rows) -> str   # 计划表（dry-run/开始前打印）
```

- `only`：`fnmatch` 匹配 entry.key；未命中的条目剔除。
- 跳过判断：`force` 时不跳过；dry_run 时只标注 skip/run。
- bench_runner/multiturn_runner 默认实现（`_default_bench_runner` / `_default_multiturn_runner`）内部调 `cli` 的同款路径：直接复用 runner.run_scenario / multiturn.run_multiturn + `_wait_healthy`。为防循环依赖，默认实现放在 cli.py 里注册？——**决策**：默认实现放 campaign.py 内，通过延迟导入 `from .runner import run_scenario, ExecEnv`、`from .multiturn import archive_multiturn, run_multiturn`，健康等待用轻量轮询（`_poll_healthy(url, timeout)`，requests 标准库 urllib）。auto_retry 循环与 gpu_reset 在默认实现内处理（调 recover.run_recovery）。测试全部注入 fake runner，不触网。

- [ ] **Step 1: Write the failing test**

```python
from sglbench.campaign import CampaignResult, run_campaign, render_campaign_plan


class CampaignRunTest(unittest.TestCase):
    def _spec(self):
        return CampaignSpec(
            name="t", sides={"baseline": SideSpec("A", None)}, 
            scenarios=[ScenarioEntry("mini-l2-64c"), ScenarioEntry("hicache-multiturn-20x10", mode="multiturn", max_parallel=2)],
        )

    def test_dry_run_plan(self):
        with temp_home() as paths, Store(paths.db) as store:
            result = run_campaign(paths, self._spec(), side="baseline", dry_run=True, store=store)
        self.assertEqual([o.action for o in result.outcomes], ["run", "run"])

    def test_skip_and_execute(self):
        with temp_home() as paths, Store(paths.db) as store:
            mk_run(store, "r1", scenario="mini-l2-64c", label="A", day=1, metrics={"mean_ttft_ms": 1.0})
            calls = []

            def bench(entry, side):
                calls.append(("bench", entry.key))
                from sglbench.campaign import ScenarioRunOutcome
                return ScenarioRunOutcome(key=entry.key, mode="bench", action="ok", run_id="new-1")

            def mt(entry, side):
                calls.append(("mt", entry.key))
                from sglbench.campaign import ScenarioRunOutcome
                return ScenarioRunOutcome(key=entry.key, mode="multiturn", action="failed", run_id="new-2", detail="partial")

            result = run_campaign(paths, self._spec(), side="baseline", store=store,
                                  bench_runner=bench, multiturn_runner=mt)
        self.assertEqual([o.action for o in result.outcomes], ["skipped", "failed"])
        self.assertEqual(calls, [("mt", "hicache-multiturn-20x10")])
        self.assertEqual(len(result.failures), 1)

    def test_only_filter_and_force(self):
        with temp_home() as paths, Store(paths.db) as store:
            mk_run(store, "r1", scenario="mini-l2-64c", label="A", day=1, metrics={"mean_ttft_ms": 1.0})

            def bench(entry, side):
                from sglbench.campaign import ScenarioRunOutcome
                return ScenarioRunOutcome(key=entry.key, mode="bench", action="ok", run_id="n")

            result = run_campaign(paths, self._spec(), side="baseline", only=["mini-*"],
                                  force=True, store=store, bench_runner=bench)
        self.assertEqual([o.key for o in result.outcomes], ["mini-l2-64c"])
        self.assertEqual(result.outcomes[0].action, "ok")  # force 覆盖跳过
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_campaign -k CampaignRun`
Expected: FAIL

- [ ] **Step 3: Implement（campaign.py 追加）**

```python
from fnmatch import fnmatch
from datetime import datetime, timezone


@dataclass
class ScenarioRunOutcome:
    key: str
    mode: str
    action: str            # skipped / ok / failed / partial
    run_id: str = ""
    detail: str = ""


@dataclass
class CampaignResult:
    side: str
    outcomes: list[ScenarioRunOutcome] = field(default_factory=list)

    @property
    def failures(self) -> list[ScenarioRunOutcome]:
        return [o for o in self.outcomes if o.action in ("failed", "partial")]


def _default_bench_runner(paths, spec, side_spec, store):
    def run(entry: ScenarioEntry, side: str) -> ScenarioRunOutcome:
        from .runner import ExecEnv, run_scenario
        from .scenarios import get_scenario

        scenario = get_scenario(entry.key)
        if entry.set:
            from .runner import apply_overrides

            scenario = apply_overrides(scenario, entry.set)
        attempt = 0
        while True:
            outcome = run_scenario(
                paths, scenario, label=side_spec.label,
                env_file=side_spec.env_file,
                exec_env=ExecEnv.for_mode(spec.exec),
                timeout=_parse_timeout_s(spec.timeout),
            )
            record = outcome.record
            if store is not None:
                store.save_run(record)
            if record.status == "ok" or attempt >= spec.auto_retry:
                return ScenarioRunOutcome(
                    key=entry.key, mode="bench",
                    action="ok" if record.status == "ok" else record.status,
                    run_id=record.run_id,
                )
            attempt += 1
            _campaign_recover_and_wait(scenario, spec, side_spec)

    return run


def _parse_timeout_s(text: str) -> float | None:
    from .util import parse_duration

    try:
        return parse_duration(text).total_seconds()
    except Exception:
        return None


def _campaign_recover_and_wait(scenario, spec: CampaignSpec, side_spec) -> None:
    """重试前：按 spec 决定是否 GPU 复位；随后轮询端点健康。"""
    import time
    import urllib.request

    if spec.gpu_reset:
        from .recover import run_recovery

        report = run_recovery(from_pods=True, rebuild=True)
        print(f"  [campaign] gpu-reset: ok={report.ok} nodes={len(report.nodes)}")
    url = scenario.health_url or scenario.endpoint
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return
        except Exception:
            pass
        time.sleep(20)
```

（`scenario.health_url/endpoint` 以 `models.Scenario` 实际字段为准——实现时核对；bench 场景健康检查沿用 cli `_wait_healthy` 的 URL 推导逻辑，**若可复用则直接从 cli 导入 `_wait_healthy`**（cli→campaign 无环：campaign 已从 dashboard 延迟导入；cli 导入 campaign 是延迟的。直接 `from .cli import _wait_healthy` 会造成 cli↔campaign 双向延迟导入——`_wait_healthy` 延迟导入在函数内即可，无环。）默认实现优先：`from .cli import _wait_healthy; _wait_healthy(scenario, timeout_s=900)`。

```python
def _default_multiturn_runner(paths, spec, side_spec, store):
    def run(entry: ScenarioEntry, side: str) -> ScenarioRunOutcome:
        from .cli import _wait_healthy
        from .multiturn import archive_multiturn, run_multiturn
        from .scenarios import get_scenario

        scenario = get_scenario(entry.key)
        attempt = 0
        while True:
            result = run_multiturn(
                url=scenario.endpoint, model=scenario.model,
                clients=scenario.num_prompts,
                rounds=int(getattr(scenario, "rounds", 10) or 10),
                request_length=scenario.random_input,
                output_length=scenario.random_output,
                request_rate=scenario.request_rate,
                label=side_spec.label, notes="[campaign]",
                scenario_key=scenario.key,
                env_file=side_spec.env_file,
                max_parallel=entry.max_parallel,
            )
            rid = archive_multiturn(paths, result, store=store, env_file=side_spec.env_file) if store else ""
            status = "ok" if (getattr(result, "success_rate", 1.0) or 0) >= 0.999 else "partial"
            if status == "ok" or attempt >= spec.auto_retry:
                return ScenarioRunOutcome(key=entry.key, mode="multiturn", action=status, run_id=rid)
            attempt += 1
            if spec.gpu_reset:
                from .recover import run_recovery

                run_recovery(from_pods=True, rebuild=True)
            _wait_healthy(scenario, timeout_s=900)

    return run
```

（实现时以 multiturn.run_multiturn/archive_multiturn 与 Scenario 的真实字段/参数名为准核对一遍——multiturn 场景 key 的 clients/rounds 从 scenarios catalog 取还是从 spec set 取：catalog 已有 `hicache-multiturn-*-*` 定义，直接 `get_scenario(key)` 即可。）

```python
def render_campaign_plan(spec: CampaignSpec, side: str, rows: Sequence[tuple[str, str, str]]) -> str:
    lines = [f"战役: {spec.name} | side={side} (label={spec.sides[side].label}) | exec={spec.exec} | gpu_reset={spec.gpu_reset}"]
    for key, mode, action in rows:
        lines.append(f"  [{'跳过' if action == 'skipped' else '执行' if action == 'run' else action}] {key} ({mode})")
    return "\n".join(lines)


def run_campaign(
    paths,
    spec: CampaignSpec,
    *,
    side: str,
    only: Sequence[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    store=None,
    bench_runner=None,
    multiturn_runner=None,
) -> CampaignResult:
    if side not in spec.sides:
        raise ValueError(f"未知 side: {side}（可选: {', '.join(spec.sides)}）")
    side_spec = spec.sides[side]
    entries = [
        e for e in spec.scenarios if not only or any(fnmatch(e.key, pat) for pat in only)
    ]
    result = CampaignResult(side=side)
    bench = bench_runner or _default_bench_runner(paths, spec, side_spec, store)
    mt = multiturn_runner or _default_multiturn_runner(paths, spec, side_spec, store)

    rows: list[tuple[str, str, str]] = []
    for entry in entries:
        skip = (not force) and store is not None and should_skip(store, entry, side_spec)
        rows.append((entry.key, entry.mode, "skipped" if skip else "run"))
    if dry_run:
        result.outcomes = [
            ScenarioRunOutcome(key=k, mode=m, action=("skipped" if a == "skipped" else "run"))
            for k, m, a in rows
        ]
        return result

    print(render_campaign_plan(spec, side, rows))
    for entry, (_k, _m, action) in zip(entries, rows):
        if action == "skipped":
            print(f"  ↺ {entry.key} 已有 ok run，跳过（--force 重跑）")
            result.outcomes.append(ScenarioRunOutcome(key=entry.key, mode=entry.mode, action="skipped"))
            continue
        print(f"  ▶ {entry.key} ({entry.mode}) …")
        fn = mt if entry.mode == "multiturn" else bench
        outcome = fn(entry, side_spec.label)
        print(f"    → {outcome.action} {outcome.run_id} {outcome.detail}".rstrip())
        result.outcomes.append(outcome)
    return result
```

（`_default_*_runner` 内部调 runner/scenario 真实字段——实现步骤里先 `grep def apply_overrides` 与 `Scenario` 字段核对：health_url 是否存在。若不存在用 endpoint 推导，参考 cli `_wait_healthy`。）

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_campaign -q` → OK

- [ ] **Step 5: Commit**

```bash
git add sglbench/campaign.py tests/test_campaign.py
git commit -m "sglbench: campaign 执行器（skip/force/only/dry-run + 注入式 runner）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 12: `campaign run` CLI + 完成后自动出报告

**Files:**
- Modify: `sglbench/cli.py`（`campaign run` parser + `cmd_campaign_run`）
- Test: `tests/test_cli.py`（dry-run + spec 校验）

**Interfaces:**
- Produces: `sglbench campaign run <spec.json> --side S [--only P]... [--force] [--dry-run] [--report-dir DIR] [--json]`。
- 结束时（非 dry-run）：若 `--report-dir`（或 spec.output_dir）给出 → 调 `write_suite` 生成完整报告；否则打印后续命令提示。

- [ ] **Step 1: Write the failing test**

```python
    def test_campaign_run_dry_run(self) -> None:
        import json as _json

        with temp_home() as paths:
            spec = paths.home / "spec.json"
            spec.write_text(_json.dumps({
                "name": "t", "sides": {"baseline": {"label": "l2off-opt"}},
                "scenarios": ["mini-l2-64c", {"key": "hicache-multiturn-20x10", "mode": "multiturn"}],
            }), encoding="utf-8")
            code, out, err = run_cli(
                "campaign", "run", str(spec), "--side", "baseline",
                "--home", str(paths.home), "--dry-run",
            )
            self.assertEqual(code, 0, err)
            self.assertIn("mini-l2-64c", out)
            # side 不存在 → 报错
            code, _out, err = run_cli(
                "campaign", "run", str(spec), "--side", "nope",
                "--home", str(paths.home), "--dry-run",
            )
            self.assertEqual(code, 1)
            self.assertIn("未知 side", err)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cli -k campaign_run`
Expected: FAIL

- [ ] **Step 3: Implement**

parser（campaign 子命令下追加）：

```python
    crun = csub.add_parser("run", help="执行战役（一侧: 当前部署下的全部场景, 带自愈）")
    _add_common(crun)
    crun.add_argument("spec", help="战役 spec JSON 路径")
    crun.add_argument("--side", required=True, help="执行哪一侧（spec.sides 的键）")
    crun.add_argument("--only", action="append", default=[], help="场景过滤（fnmatch, 可重复）")
    crun.add_argument("--force", action="store_true", help="已有 ok run 也重跑")
    crun.add_argument("--dry-run", action="store_true", help="只打印计划")
    crun.add_argument("--report-dir", help="完成/结束后自动生成完整报告到该目录")
    crun.add_argument("--json", action="store_true")
    crun.set_defaults(func=cmd_campaign_run)
```

```python
def cmd_campaign_run(args: argparse.Namespace) -> int:
    from .campaign import load_spec, render_campaign_plan, run_campaign

    paths = _paths(args)
    try:
        spec = load_spec(args.spec)
    except ValueError as exc:
        _eprint(f"❌ {exc}")
        return EXIT_FAIL
    with Store(paths.db) as store:
        if args.dry_run:
            result = run_campaign(paths, spec, side=args.side, only=args.only,
                                  force=args.force, dry_run=True, store=store)
            rows = [(o.key, o.mode, o.action) for o in result.outcomes]
            print(render_campaign_plan(spec, args.side, rows))
            return EXIT_OK
        try:
            result = run_campaign(paths, spec, side=args.side, only=args.only,
                                  force=args.force, store=store)
        except ValueError as exc:
            _eprint(f"❌ {exc}")
            return EXIT_FAIL
        report_dir = args.report_dir or spec.output_dir
        if report_dir:
            other = [s for s in spec.sides if s != args.side]
            if other:
                # 对侧 label 前缀通配配对（与 campaign report 相同机制）
                pairs = [(spec.sides[other[0]].label + "*", spec.sides[args.side].label + "*")]
                try:
                    from .campaign import write_suite

                    res = write_suite(store, report_dir, label_pairs=pairs, title=spec.title or spec.name)
                    print(f"报告: {res['index']}（{len(res['reports'])} 场景）")
                except Exception as exc:
                    _eprint(f"⚠️ 自动报告生成失败（数据可能不全）: {exc}")
    failures = result.failures
    if args.json:
        print(json.dumps({
            "side": result.side,
            "outcomes": [asdict(o) for o in result.outcomes],
            "failures": len(failures),
        }, ensure_ascii=False, indent=2, default=str))
    print(f"战役完成: {len(result.outcomes)} 场景, 失败 {len(failures)}"
          + ("（" + "、".join(o.key for o in failures) + "）" if failures else ""))
    return EXIT_FAIL if failures else EXIT_OK
```

（`asdict` 从 dataclasses 导入——cli.py 内函数级 import。）

- [ ] **Step 4: Run tests + 全量回归 + 提交**

Run: `python3 -m unittest tests.test_cli -k campaign_run -v` → PASS
Run: `python3 -m unittest discover -s tests -t . -q` → OK

```bash
git add sglbench/cli.py tests/test_cli.py
git commit -m "sglbench: campaign run CLI（--side/--only/--force/--dry-run + 收尾自动完整报告）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## 自审记录

1. **Spec 覆盖**：§3.1 recover（T1–T5：远程封装/发现/复位/编排/dry-run/CLI/自愈集成，显式触发决策落实在 `_maybe_gpu_recover` 门控）✓；§3.2 执行器（T10–T12：spec/跳过/env 指纹/一侧一次/dry-run/收尾报告）✓；§3.3 forensics（T6–T9：签名/表 schema v2/采集/自动钩子/CLI/show）✓；错误处理原则（各步 best-effort、超时硬上限、退出码）落在 T4/T8/T9/T11 实现内 ✓。
2. **占位符扫描**：Task 4 `_nodes_from_args` 与 Task 11 `_default_*_runner` 标注"实现时以真实字段核对"——核对点已指名（`Scenario` 字段、`apply_overrides`、`_wait_healthy`），逻辑与断言已成文；无 TBD。
3. **类型一致性**：`CmdResult/ssh_run/kubectl_json`（T1）被 T2/T3/T8 一致引用；`PodIssue`（T2）→ `find_pod_issues` 在 T8 复用（`from .recover import PD_LABEL, find_pod_issues`，T8 实现已写 imports）；`ScenarioRunOutcome/CampaignResult`（T11）在 T12 CLI 一致使用；`SideSpec/ScenarioEntry/CampaignSpec`（T10）在 T11/T12 一致。`store.save_forensics` 签名（T7）与 T9 调用一致。
