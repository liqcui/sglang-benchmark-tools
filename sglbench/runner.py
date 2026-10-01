"""执行与归档：起压测进程、采集原始工件、构造 RunRecord。

工件布局（**不可变**，数据库只是它的索引）::

    <home>/runs/<run_id>/
        run.json         归一化记录（指标、参数、问题、工件索引）
        command.txt      完整命令行（可直接复制到集群执行）
        stdout.log       压测原始输出（含 stderr）
        requests.jsonl   每请求明细（若 SGLang 支持且启用）
        meta.json        运行环境快照（python/sglang/平台/git）

``ingest`` 走同一条落盘路径：把外部日志**复制**进来而不是引用原路径，这样日志被
轮转/删除后历史 run 依然完整可复现。
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .models import Issue, RunRecord, Sample, Scenario
from .parse import ParsedLog, default_requests_path, load_samples, parse_bench_log
from .util import (
    Paths,
    ensure_dir,
    iso,
    new_run_id,
    sha256_text,
    utcnow,
    write_json,
    write_jsonl,
)

BENCH_MODULE = "sglang.bench_serving"
DETAIL_FLAG = "--output-file"
EXPORT_TARGET_DIR = "/var/log"

_probe_cache: dict[tuple[str, str, str], bool] = {}


# --------------------------------------------------------------------------- #
# 命令行构造
# --------------------------------------------------------------------------- #


def python_executable(explicit: str | None = None) -> str:
    """默认用 ``python3``（集群现场），本地没有时回退到当前解释器。"""
    if explicit:
        return explicit
    if os.name != "nt" and shutil.which("python3"):
        return "python3"
    if shutil.which("python3"):
        return "python3"
    return sys.executable


def build_command(
    scenario: Scenario,
    *,
    python: str | None = None,
    detail_path: str | Path | None = None,
    detail_flag: str = DETAIL_FLAG,
    module: str = BENCH_MODULE,
    overrides: Mapping[str, Any] | None = None,
) -> list[str]:
    """按文档里的参数顺序生成 bench_serving 命令行。"""
    sc = apply_overrides(scenario, overrides or {})
    cmd: list[str] = [python_executable(python), "-m", module]
    cmd += ["--backend", sc.backend]
    if sc.base_url:
        cmd += ["--base-url", sc.base_url]
    elif sc.host and sc.port:
        cmd += ["--host", str(sc.host), "--port", str(sc.port)]
    cmd += ["--dataset-name", sc.dataset_name]
    if sc.dataset_path and sc.dataset_name != "random-shared":
        cmd += ["--dataset-path", sc.dataset_path]
    cmd += ["--tokenizer", sc.tokenizer]
    cmd += ["--model", sc.model]
    cmd += ["--num-prompts", str(sc.num_prompts)]
    cmd += ["--max-concurrency", str(sc.max_concurrency)]
    cmd += ["--random-input", str(sc.random_input)]
    cmd += ["--random-output", str(sc.random_output)]
    cmd += ["--random-range-ratio", str(sc.random_range_ratio)]
    if sc.pd_separated:
        cmd += ["--pd-separated"]
    cmd += ["--request-rate", str(sc.request_rate)]
    cmd += list(sc.extra_args)
    if detail_path:
        cmd += [detail_flag, str(detail_path)]
    return cmd


def command_string(cmd: Sequence[str]) -> str:
    """平台无关的可复现命令行文本（统一 POSIX 引号风格，便于贴到集群）。"""
    return " ".join(shlex.quote(str(part)) for part in cmd)


def _q(token: str) -> str:
    """shell 引用；含 ``$`` 的占位符用双引号以便在目标机上展开。"""
    if "$" in token:
        return '"' + token.replace('"', '\\"') + '"'
    return shlex.quote(token)


def _shell_lines(cmd: Sequence[str]) -> list[str]:
    """把 ``--flag value`` 保持在同一行，一条参数一行，便于阅读与 diff。"""
    lines: list[str] = []
    index = 0
    # 解释器 + ``-m`` + 模块名 保持在同一行
    if len(cmd) >= 3 and str(cmd[1]) == "-m":
        lines.append(" ".join(_q(str(c)) for c in cmd[:3]))
        index = 3
    while index < len(cmd):
        token = str(cmd[index])
        if token.startswith("--") and index + 1 < len(cmd) and not str(cmd[index + 1]).startswith("--"):
            lines.append(f"{_q(token)} {_q(str(cmd[index + 1]))}")
            index += 2
        else:
            lines.append(_q(token))
            index += 1
    return lines


def render_shell(
    scenario: Scenario,
    cmd: Sequence[str],
    *,
    log_path: str | None = None,
    background: bool = True,
) -> str:
    """渲染成可以直接在集群上跑的 shell 片段（含离线环境变量）。"""
    target = log_path or f"{EXPORT_TARGET_DIR}/{scenario.key}.log"
    lines = []
    for key, value in (scenario.env or {}).items():
        lines.append(f"export {key}={value}")
    lines.append(f"export TOKENIZER={scenario.tokenizer}")
    body = " \\\n      ".join(_shell_lines(cmd))
    if background:
        lines.append(f"nohup {body} \\\n      >{target} 2>&1 &")
    else:
        lines.append(f"{body} \\\n      >{target} 2>&1")
    return "\n".join(lines)


def apply_overrides(scenario: Scenario, overrides: Mapping[str, Any]) -> Scenario:
    """用 CLI 覆盖项生成场景副本（不修改原对象，保证场景目录不被污染）。"""
    if not overrides:
        return scenario
    from dataclasses import replace

    clean: dict[str, Any] = {}
    valid = set(Scenario.__dataclass_fields__)  # type: ignore[attr-defined]
    for key, value in overrides.items():
        if value is None:
            continue
        key = key.replace("-", "_")
        if key not in valid:
            continue
        clean[key] = value
    return replace(scenario, **clean)


# --------------------------------------------------------------------------- #
# 能力探测
# --------------------------------------------------------------------------- #


def probe_detail_flag(
    python: str | None = None,
    module: str = BENCH_MODULE,
    flag: str = DETAIL_FLAG,
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = 90.0,
) -> bool:
    """探测当前 SGLang 是否支持每请求明细导出（不同版本参数名不一样）。

    结果在本进程内缓存；探测失败（例如没装 sglang）时返回 ``False`` 并只影响
    "能不能画直方图"，不影响压测本身。
    """
    exe = python_executable(python)
    cache_key = (exe, module, flag)
    if cache_key in _probe_cache:
        return _probe_cache[cache_key]
    supported = False
    try:
        proc = subprocess.run(
            [exe, "-m", module, "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=dict(env) if env else None,
        )
        text = (proc.stdout or "") + (proc.stderr or "")
        supported = flag in text
    except (OSError, subprocess.SubprocessError):
        supported = False
    _probe_cache[cache_key] = supported
    return supported


# --------------------------------------------------------------------------- #
# 进程执行
# --------------------------------------------------------------------------- #


@dataclass
class ExecResult:
    exit_code: int | None = None
    wall_s: float = 0.0
    timed_out: bool = False
    interrupted: bool = False
    log_text: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


def execute(
    cmd: Sequence[str],
    *,
    log_path: str | Path,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    on_line: Callable[[str], None] | None = None,
) -> ExecResult:
    """执行命令，实时把 stdout+stderr 落到 ``log_path`` 并在内存里留一份全文。"""
    log_file = Path(log_path)
    ensure_dir(log_file.parent)
    result = ExecResult(started_at=utcnow())
    buffers: list[str] = []

    merge_env = dict(os.environ)
    merge_env.update({str(k): str(v) for k, v in (env or {}).items()})

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            [str(c) for c in cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=merge_env,
            cwd=str(cwd) if cwd else None,
        )
    except OSError as exc:
        result.error = f"无法启动进程: {exc}"
        result.exit_code = -1
        result.finished_at = utcnow()
        log_file.write_text(result.error + "\n", encoding="utf-8")
        result.log_text = result.error
        return result

    def reader() -> None:
        assert proc.stdout is not None
        with log_file.open("w", encoding="utf-8", errors="replace", newline="\n") as fh:
            for line in proc.stdout:
                fh.write(line)
                fh.flush()
                buffers.append(line)
                if on_line:
                    try:
                        on_line(line.rstrip("\n"))
                    except Exception:  # noqa: BLE001 - 回调不应影响采集
                        pass

    thread = threading.Thread(target=reader, name="sglbench-reader", daemon=True)
    thread.start()

    deadline = (started + timeout) if timeout else None
    try:
        while proc.poll() is None:
            if deadline and time.monotonic() > deadline:
                result.timed_out = True
                proc.kill()
                break
            time.sleep(0.2)
        proc.wait(timeout=30)
    except KeyboardInterrupt:
        result.interrupted = True
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
    finally:
        thread.join(timeout=10)

    result.wall_s = time.monotonic() - started
    result.exit_code = proc.returncode
    result.finished_at = utcnow()
    result.log_text = "".join(buffers)
    if result.timed_out:
        result.error = f"执行超时（>{timeout:.0f}s），已强制终止子进程"
    elif result.interrupted:
        result.error = "被用户中断（Ctrl+C）"
    return result


# --------------------------------------------------------------------------- #
# 工件落盘
# --------------------------------------------------------------------------- #


def run_dir_for(paths: Paths, run_id: str) -> Path:
    return ensure_dir(paths.run_dir(run_id))


def write_artifacts(
    paths: Paths,
    record: RunRecord,
    *,
    log_text: str | None = None,
    command: str | None = None,
    samples: Sequence[Sample] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> RunRecord:
    """把 run 的原始工件与归一化记录写进 ``<home>/runs/<run_id>/``。"""
    rdir = run_dir_for(paths, record.run_id)
    artifacts: dict[str, str] = dict(record.artifacts)

    if log_text is not None:
        target = rdir / "stdout.log"
        target.write_text(log_text, encoding="utf-8", newline="\n")
        artifacts["stdout"] = str(target)

    if command is not None:
        target = rdir / "command.txt"
        target.write_text(command.rstrip("\n") + "\n", encoding="utf-8", newline="\n")
        artifacts["command"] = str(target)

    if samples:
        target = rdir / "requests.jsonl"
        write_jsonl(target, [s.to_row(record.run_id, i) for i, s in enumerate(samples)])
        artifacts["requests"] = str(target)

    payload_meta = dict(record.meta)
    if meta:
        payload_meta.update(meta)
    target = rdir / "meta.json"
    write_json(target, payload_meta)
    artifacts["meta"] = str(target)

    record.raw_dir = str(rdir)
    record.artifacts = artifacts
    target = rdir / "run.json"
    artifacts["run"] = str(target)
    record.artifacts = artifacts
    write_json(target, record.to_dict(include_samples=False))
    return record


def collect_meta(
    *,
    python: str | None = None,
    module: str = BENCH_MODULE,
    cwd: str | Path | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """运行环境快照：可复现性靠它，而不是靠记忆。"""
    meta: dict[str, Any] = {
        "tool": "sglbench",
        "hostname": platform.node(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "python_executable": python_executable(python),
        "bench_module": module,
        "cwd": str(cwd or Path.cwd()),
        "captured_at": iso(utcnow()),
        "operator": _default_operator(),
    }
    meta["git_sha"] = _git_sha(cwd)
    meta["sglang_version"] = _module_version(python, "sglang")
    meta.update(dict(extra or {}))
    return meta


def _default_operator() -> str:
    for key in ("SGLBENCH_OPERATOR", "USER", "USERNAME", "LOGNAME"):
        value = os.environ.get(key)
        if value:
            return value
    return "unknown"


def _git_sha(cwd: str | Path | None) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(cwd) if cwd else None,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()[:40]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _module_version(python: str | None, module: str) -> str | None:
    try:
        proc = subprocess.run(
            [python_executable(python), "-c", f"import {module} as m; print(getattr(m, '__version__', 'unknown'))"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


# --------------------------------------------------------------------------- #
# 从解析结果构造 RunRecord
# --------------------------------------------------------------------------- #


def build_record(
    *,
    run_id: str,
    scenario: Scenario,
    parsed: ParsedLog,
    status: str,
    source: str = "executed",
    label: str = "",
    command: str = "",
    exit_code: int | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    wall_s: float | None = None,
    operator: str = "",
    notes: str = "",
    tags: Sequence[str] | None = None,
    meta: Mapping[str, Any] | None = None,
    extra_issues: Sequence[Issue] = (),
) -> RunRecord:
    params = dict(scenario.benchmark_params())
    # 日志回显优先：它代表进程真正收到的参数（也是参数漂移的现场证据）
    params.update({k: v for k, v in (parsed.params or {}).items() if v is not None})

    metrics = dict(parsed.metrics)
    if "successful_requests" not in metrics and parsed.samples:
        metrics["successful_requests"] = float(sum(1 for s in parsed.samples if s.ok))

    return RunRecord(
        run_id=run_id,
        scenario_key=scenario.key,
        label=label or scenario.key,
        status=status,
        source=source,
        started_at=started_at,
        finished_at=finished_at,
        wall_s=round(wall_s, 3) if wall_s is not None else None,
        command=command,
        exit_code=exit_code,
        host=platform.node(),
        operator=operator or _default_operator(),
        notes=notes,
        tags=list(tags or scenario.tags),
        params=params,
        env=dict(scenario.env),
        metrics=metrics,
        samples=list(parsed.samples),
        issues=[*parsed.issues, *extra_issues],
        meta=dict(meta or {}),
    )


def infer_status(
    parsed: ParsedLog,
    *,
    exit_code: int | None = None,
    timed_out: bool = False,
    interrupted: bool = False,
) -> str:
    """状态机：failed / partial / ok。

    ``partial`` 专门用于"跑完了但请求没全成功"——这在 PD 分离场景里往往意味着
    某个 Prefill/Decode pod 已经在重启，必须能一眼看出来。
    """
    if timed_out or interrupted:
        return "failed"
    if exit_code not in (0, None):
        return "failed"
    if not parsed.block_found:
        return "failed"
    rate = parsed.metrics.get("success_rate")
    if rate is not None and rate < 0.999:
        return "partial"
    severe = [i for i in parsed.issues if i.severity == "error" and i.kind in ("oom", "crash", "restart", "http_error")]
    if severe:
        return "partial"
    return "ok"


# --------------------------------------------------------------------------- #
# 对外主流程
# --------------------------------------------------------------------------- #


@dataclass
class RunOutcome:
    record: RunRecord
    exec_result: ExecResult | None = None
    parsed: ParsedLog | None = None
    run_dir: Path | None = None


def run_scenario(
    paths: Paths,
    scenario: Scenario,
    *,
    label: str = "",
    notes: str = "",
    tags: Sequence[str] | None = None,
    run_id: str | None = None,
    python: str | None = None,
    module: str = BENCH_MODULE,
    detail_flag: str = DETAIL_FLAG,
    dump_requests: bool = True,
    timeout: float | None = None,
    cwd: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
    on_line: Callable[[str], None] | None = None,
    log_path: str | Path | None = None,
) -> RunOutcome:
    """执行一次场景压测并落盘、入库前的完整采集。"""
    paths.ensure()
    sc = apply_overrides(scenario, overrides or {})
    rid = run_id or new_run_id(sc.key)
    rdir = run_dir_for(paths, rid)
    raw_log = Path(log_path) if log_path else rdir / "stdout.log"

    env = {**os.environ.copy()}
    env.update(sc.env)

    detail_path: Path | None = None
    used_detail_flag: str | None = None
    if dump_requests:
        if probe_detail_flag(python, module, detail_flag, env=env):
            detail_path = rdir / "requests.jsonl"
            used_detail_flag = detail_flag

    # 注意：明细文件由 bench_serving 自己写；我们随后会读它并重写成标准格式
    cmd = build_command(
        sc,
        python=python,
        detail_path=detail_path,
        detail_flag=detail_flag,
        module=module,
        overrides=None,
    )
    cmd_text = command_string(cmd)

    exec_result = execute(cmd, log_path=raw_log, env=env, cwd=cwd, timeout=timeout, on_line=on_line)

    samples: list[Sample] = []
    if detail_path and detail_path.exists():
        samples = load_samples(detail_path)
    else:
        guess = default_requests_path(raw_log)
        if guess.exists():
            samples = load_samples(guess)

    params_for_parse = dict(sc.benchmark_params())
    parsed = parse_bench_log(exec_result.log_text, params=params_for_parse, samples=samples)

    status = infer_status(parsed, exit_code=exec_result.exit_code, timed_out=exec_result.timed_out, interrupted=exec_result.interrupted)
    extra_issues: list[Issue] = []
    if exec_result.error:
        extra_issues.append(Issue(kind="exec", severity="error", message=exec_result.error))
    if exec_result.exit_code not in (0, None):
        extra_issues.append(Issue(kind="exit_code", severity="error", message=f"进程退出码 {exec_result.exit_code}"))

    meta = collect_meta(
        python=python,
        module=module,
        cwd=cwd,
        extra={
            "detail_flag": used_detail_flag,
            "dump_requests": bool(detail_path),
            "content_sha256": sha256_text(exec_result.log_text),
        },
    )

    record = build_record(
        run_id=rid,
        scenario=sc,
        parsed=parsed,
        status=status,
        source="executed",
        label=label,
        command=cmd_text,
        exit_code=exec_result.exit_code,
        started_at=exec_result.started_at,
        finished_at=exec_result.finished_at,
        wall_s=exec_result.wall_s,
        notes=notes,
        tags=tags,
        meta=meta,
        extra_issues=extra_issues,
    )

    # 归一化明细：确保 requests.jsonl 的字段名统一（不同 SGLang 版本字段名不同）
    if samples:
        write_jsonl(rdir / "requests.jsonl", [s.to_row(rid, i) for i, s in enumerate(samples)])

    record = write_artifacts(
        paths,
        record,
        log_text=exec_result.log_text,
        command=cmd_text,
        samples=samples,
        meta=meta,
    )
    return RunOutcome(record=record, exec_result=exec_result, parsed=parsed, run_dir=rdir)


def ingest_log(
    paths: Paths,
    log_path: str | Path,
    scenario: Scenario,
    *,
    requests_path: str | Path | None = None,
    label: str = "",
    notes: str = "",
    tags: Sequence[str] | None = None,
    run_id: str | None = None,
    started_at: datetime | None = None,
    command: str | None = None,
    operator: str = "",
    meta_extra: Mapping[str, Any] | None = None,
) -> RunOutcome:
    """把已有日志（集群上 nohup 跑出来的）导入成一次 run。

    始终把原日志**复制**进 run 目录：这样日志被轮转或删除后，历史 run 依然完整
    可复现 —— 这是 benchmark 数据管理里最容易踩的坑。
    """
    src = Path(log_path)
    if not src.exists():
        raise FileNotFoundError(f"日志不存在: {src}")
    paths.ensure()
    rid = run_id or new_run_id(scenario.key)
    rdir = run_dir_for(paths, rid)

    text = src.read_text(encoding="utf-8", errors="replace")

    detail_src = Path(requests_path) if requests_path else default_requests_path(src)
    samples: list[Sample] = load_samples(detail_src) if detail_src.exists() else []

    parsed = parse_bench_log(
        text,
        params=dict(scenario.benchmark_params()),
        samples=samples,
    )
    status = infer_status(parsed)

    stat = src.stat()
    started = started_at or datetime.fromtimestamp(stat.st_mtime, tz=utcnow().tzinfo)
    cmd_text = command or (src.with_suffix(".cmd").read_text(encoding="utf-8").strip() if src.with_suffix(".cmd").exists() else "")
    meta = collect_meta(
        extra={
            "ingested_from": str(src),
            "ingested_at": iso(utcnow()),
            "requests_from": str(detail_src) if detail_src.exists() else None,
            "source_mtime": iso(started),
            "content_sha256": sha256_text(text),
            **(dict(meta_extra or {})),
        }
    )

    record = build_record(
        run_id=rid,
        scenario=scenario,
        parsed=parsed,
        status=status,
        source="ingested",
        label=label or src.stem,
        command=cmd_text,
        exit_code=None,
        started_at=started,
        finished_at=None,
        wall_s=parsed.metrics.get("benchmark_duration_s"),
        operator=operator,
        notes=notes,
        tags=tags,
        meta=meta,
    )

    record = write_artifacts(
        paths,
        record,
        log_text=text,
        command=cmd_text,
        samples=samples,
        meta=meta,
    )
    return RunOutcome(record=record, exec_result=None, parsed=parsed, run_dir=rdir)


def discover_logs(root: str | Path, pattern: str = "*.log") -> list[Path]:
    """递归找一个目录下的候选日志（``ingest-dir`` 用）。"""
    base = Path(root)
    if base.is_file():
        return [base]
    return sorted(p for p in base.rglob(pattern) if p.is_file())


def label_from_log_path(path: str | Path) -> str:
    """``single-standard__candidate.log`` -> ``candidate``。"""
    stem = Path(path).stem
    if "__" in stem:
        return stem.split("__", 1)[1]
    return stem
