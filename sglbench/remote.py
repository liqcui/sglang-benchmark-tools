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
    opts = (
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={connect_timeout_s}",
        "-o", "StrictHostKeyChecking=accept-new",
    )
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
