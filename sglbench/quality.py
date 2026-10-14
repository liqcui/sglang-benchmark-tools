"""精度 / 质量评测（lm-eval 子集）—— 压测的「配套回路」。

为什么需要这一层
----------------
性能压测只回答「多快」，回答不了「多准」。一次量化、预填充、KV 精度、
引擎升级的改动，可能让吞吐漂亮地上升，却把准确率悄悄拉低 —— 只测性能的
套件发现不了这种退化。所以把「跑一个小规模 lm-eval 子集」接到压测收尾，
形成一个闭合的质量门。

与既有能力的关系
----------------
- ``RunRecord``（runs 表）记录「多快」；``QualityEval``（quality_evals 表）记录「多准」。
- 二者通过 ``run_id`` 可选关联：同一配置下「性能↑ 精度是否↓」可联合判读。
- 归档布局与 run 平行：``<home>/evals/<eval_id>/``。

三个关键约束
------------
1. **可注入 runner**：默认经 ``runner.execute`` 调 ``python -m lm_eval``；
   测试注入 fake runner —— 无需真装 lm-eval、无需真 GPU 就能验证全链路。
2. **best-effort**：作为压测收尾钩子时**永不阻断主流程**；失败只记 ``status=failed``。
3. **口径固化**：limit / num_fewshot / apply_chat_template / 子集任务全部入库，
   保证跨 run 可比（这是精度回归的前提）。

用法::

    sglbench eval --scenario prod-standard-c64 --subset smoke
    sglbench eval --scenario prod-standard-c64 --tasks gsm8k,ifeval --limit 100
    sglbench run --scenario prod-standard-c64 --eval-after smoke
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .models import QualityEval, QualityMetric, Scenario
from .util import Paths, dumps, iso, new_run_id, utcnow, write_json

# --------------------------------------------------------------------------- #
# 子集定义
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvalSubset:
    """一个命名的评测子集：任务清单 + 口径（few-shot / limit / chat 模板）。"""

    name: str
    tasks: tuple[str, ...]
    num_fewshot: int | None = 0
    limit: float | None = 200
    apply_chat_template: bool = True
    description: str = ""
    batch_size: int | None = None
    num_concurrent: int | None = 8

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tasks": list(self.tasks),
            "num_fewshot": self.num_fewshot,
            "limit": self.limit,
            "apply_chat_template": self.apply_chat_template,
            "description": self.description,
        }


# 内置子集：覆盖「快 → 全」三档。任务都选纯生成/判别式、不需代码沙箱的，保证
# 在离线集群也能跑。
DEFAULT_SUBSETS: dict[str, EvalSubset] = {
    "smoke": EvalSubset(
        name="smoke",
        tasks=("gsm8k", "ifeval"),
        num_fewshot=0,
        limit=200,
        description="5 分钟精度冒烟：数学推理 + 指令遵循（每个 run 收尾首选）",
    ),
    "regression": EvalSubset(
        name="regression",
        tasks=("gsm8k", "ifeval", "arc_challenge", "ceval-valid"),
        num_fewshot=0,
        limit=500,
        description="版本回归：中英推理 + 指令遵循 + 知识（~30 分钟）",
    ),
    "full": EvalSubset(
        name="full",
        tasks=("gsm8k", "ifeval", "arc_challenge", "mmlu", "ceval-valid", "hellaswag"),
        num_fewshot=5,
        limit=None,
        description="完整评测（不做 limit，耗时长，仅发版前跑）",
    ),
}

DEFAULT_SUBSET_NAME = "smoke"


def default_subsets_path(home: Path | None = None) -> Path:
    """默认子集配置的候选路径（按优先级）：

    ① ``<home>/eval-subsets.json``（家目录，运行期落地）
    ② ``<cwd>/envs/eval-subsets.json``（仓库版本化配置）
    """
    if home is not None:
        return Path(home) / "eval-subsets.json"
    base = Path(os.environ.get("SGLBENCH_HOME") or Path.cwd() / "data")
    return base / "eval-subsets.json"


def _candidate_paths(path: str | Path | None, home: Path | None) -> list[Path]:
    if path:
        return [Path(path)]
    cands: list[Path] = []
    if home is not None:
        cands.append(Path(home) / "eval-subsets.json")
    else:
        cands.append(default_subsets_path(None))
    cands.append(Path.cwd() / "envs" / "eval-subsets.json")
    return cands


def _subset_from_mapping(name: str, raw: Mapping[str, Any]) -> EvalSubset:
    tasks = raw.get("tasks") or []
    if isinstance(tasks, str):
        tasks = [t.strip() for t in tasks.split(",") if t.strip()]
    limit = raw.get("limit")
    if limit is not None:
        limit = float(limit)
    nfs = raw.get("num_fewshot")
    return EvalSubset(
        name=name,
        tasks=tuple(str(t) for t in tasks),
        num_fewshot=None if nfs is None else int(nfs),
        limit=limit,
        apply_chat_template=bool(raw.get("apply_chat_template", True)),
        description=str(raw.get("description", "")),
        batch_size=(int(raw["batch_size"]) if raw.get("batch_size") else None),
        num_concurrent=(int(raw["num_concurrent"]) if raw.get("num_concurrent") else None),
    )


def load_subsets(path: str | Path | None = None, *, home: Path | None = None) -> dict[str, EvalSubset]:
    """加载子集定义；未给 path 时按候选顺序找，都找不到就回退内置。

    文件里的同名字集会**覆盖**内置定义（便于现场微调），其余内置保留。
    """
    subsets = dict(DEFAULT_SUBSETS)
    target = next((p for p in _candidate_paths(path, home) if p.exists()), None)
    if target is not None:
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            data = None
        if isinstance(data, Mapping):
            block = data.get("subsets", data)
            if isinstance(block, Mapping):
                for name, raw in block.items():
                    if isinstance(raw, Mapping):
                        subsets[str(name)] = _subset_from_mapping(str(name), raw)
    return subsets


def resolve_subset(
    name: str | None,
    subsets: Mapping[str, EvalSubset],
    *,
    tasks: Sequence[str] | None = None,
    num_fewshot: int | None = None,
    limit: float | None = None,
    apply_chat_template: bool | None = None,
) -> EvalSubset:
    """解析出最终子集：以命名子集为底，命令行参数覆盖。"""
    if tasks:
        base = replace(
            subsets.get(name or DEFAULT_SUBSET_NAME, DEFAULT_SUBSETS[DEFAULT_SUBSET_NAME]),
            name=name or "custom",
        )
    elif name:
        if name not in subsets:
            raise KeyError(f"未知评测子集: {name}（可选: {', '.join(sorted(subsets))}）")
        base = subsets[name]
    else:
        base = subsets[DEFAULT_SUBSET_NAME]

    overrides: dict[str, Any] = {}
    if tasks:
        overrides["tasks"] = tuple(str(t) for t in tasks)
        overrides["name"] = name or "custom"
    if num_fewshot is not None:
        overrides["num_fewshot"] = int(num_fewshot)
    if limit is not None:
        overrides["limit"] = float(limit)
    if apply_chat_template is not None:
        overrides["apply_chat_template"] = bool(apply_chat_template)
    return replace(base, **overrides) if overrides else base


# --------------------------------------------------------------------------- #
# 命令构造
# --------------------------------------------------------------------------- #


def lm_eval_target(scenario: Scenario, kind: str | None = None) -> tuple[str, str]:
    """场景 → lm-eval ``--model`` 与 ``base_url``。

    ``kind`` 显式指定 ``chat`` / ``completions``（命令行 ``--endpoint-kind``）；
    缺省按 ``scenario.backend`` 推断：含 "chat" 走 ``local-chat-completions``，
    否则走 ``local-completions``。

    两种都合法：instruct 模型常用 ``local-completions`` + ``--apply_chat_template``
    （把 chat 模板渲染成 prompt 后打 completions 端点），也可直接打 chat 端点。
    """
    endpoint = scenario.endpoint.rstrip("/")
    if kind == "chat":
        return "local-chat-completions", f"{endpoint}/v1/chat/completions"
    if kind == "completions":
        return "local-completions", f"{endpoint}/v1/completions"
    backend = (scenario.backend or "").lower()
    if "chat" in backend:
        return "local-chat-completions", f"{endpoint}/v1/chat/completions"
    return "local-completions", f"{endpoint}/v1/completions"


def _fmt_num(v: float | None) -> str:
    if v is None:
        return ""
    f = float(v)
    return str(int(f)) if f.is_integer() else str(f)


def build_eval_command(
    scenario: Scenario,
    subset: EvalSubset,
    *,
    output_path: str,
    python: str = "python3",
    lm_eval_module: str = "lm_eval",
    model: str | None = None,
    tokenizer: str | None = None,
    endpoint_kind: str | None = None,
    extra_args: Sequence[str] | None = None,
    timeout_s: float | None = None,
) -> list[str]:
    """构造 ``python -m lm_eval ...`` 命令（纯函数，便于 dry-run 与测试）。"""
    model_kind, base_url = lm_eval_target(scenario, endpoint_kind)
    model_args = [
        f"base_url={base_url}",
        f"model={model or scenario.model}",
        f"tokenizer={tokenizer or scenario.tokenizer}",
        "max_retries=2",
        f"timeout={int(timeout_s or 600)}",
    ]
    if subset.num_concurrent:
        model_args.append(f"num_concurrent={int(subset.num_concurrent)}")

    cmd: list[str] = [
        python,
        "-m",
        lm_eval_module,
        "--model",
        model_kind,
        "--model_args",
        ",".join(model_args),
        "--tasks",
        ",".join(subset.tasks),
        "--output_path",
        str(output_path),
        "--log_samples",
    ]
    if subset.num_fewshot is not None:
        cmd += ["--num_fewshot", str(int(subset.num_fewshot))]
    if subset.limit is not None:
        cmd += ["--limit", _fmt_num(subset.limit)]
    if subset.apply_chat_template:
        cmd += ["--apply_chat_template"]
    if subset.batch_size:
        cmd += ["--batch_size", str(int(subset.batch_size))]
    cmd += list(extra_args or [])
    return cmd


def command_string(cmd: Sequence[str]) -> str:
    """可读的命令行字符串（供归档/展示；不保证可原样 shell 执行）。"""
    out: list[str] = []
    for token in cmd:
        t = str(token)
        out.append(t if t and all(c.isalnum() or c in "-_./=,:@+" for c in t) else f"'{t}'")
    return " ".join(out)


# --------------------------------------------------------------------------- #
# 结果解析
# --------------------------------------------------------------------------- #


def find_results_file(output_path: str | Path) -> Path | None:
    """在输出目录下递归找最新的 ``results*.json``（lm-eval 会写进子目录）。"""
    base = Path(output_path)
    if not base.exists():
        return None
    candidates = [p for p in base.rglob("results*.json") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def parse_results_payload(payload: Any) -> dict[str, Any]:
    """从一个 results 负载里取出「任务 → 指标」段（容忍多种外层包装）。"""
    if not isinstance(payload, Mapping):
        return {}
    results = payload.get("results")
    if isinstance(results, Mapping):
        return dict(results)
    # 容错：有的版本直接给 {"task": {...}} 或包在 "task_results"
    tr = payload.get("task_results")
    if isinstance(tr, Mapping):
        return dict(tr)
    return dict(payload)


def metrics_from_results(results: Mapping[str, Any]) -> list[QualityMetric]:
    """把 lm-eval 的 results 段展平成指标列表（跳过 stderr / 别名等非数值项）。"""
    out: list[QualityMetric] = []
    for task, task_res in results.items():
        if not isinstance(task_res, Mapping):
            continue
        hib = task_res.get("higher_is_better")
        for metric, val in task_res.items():
            if metric in ("alias", "higher_is_better", "samples"):
                continue
            if metric.endswith("_stderr") or metric.endswith("_stderr,none"):
                continue
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue
            is_hib = True
            if isinstance(hib, Mapping):
                is_hib = bool(hib.get(metric, True))
            elif isinstance(hib, bool):
                is_hib = hib
            out.append(
                QualityMetric(
                    metric_key=f"{task}:{metric}",
                    value=float(val),
                    task=str(task),
                    metric=str(metric),
                    higher_is_better=is_hib,
                )
            )
    return out


def compare_quality(base: Mapping[str, float], cur: Mapping[str, float]) -> dict[str, float]:
    """逐指标差值 ``cur - base``；只保留两侧都有的指标。"""
    return {k: round(float(cur[k]) - float(base[k]), 6) for k in cur if k in base}


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #

# 默认 runner 的返回：约定为带 exit_code / timed_out / log_text 等属性的对象。
Runner = Callable[..., Any]


def _default_runner(
    cmd: Sequence[str],
    *,
    log_path: str | Path,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> Any:
    from .runner import execute

    return execute(cmd, log_path=log_path, env=env, timeout=timeout)


def _infer_eval_status(exit_code: int | None, results: Mapping[str, Any], *, timed_out: bool = False) -> str:
    if timed_out:
        return "failed"
    if exit_code not in (0, None):
        return "failed"
    if not results:
        return "partial"  # 退出码 0 但没拿到结果
    return "ok"


def run_eval(
    scenario: Scenario,
    subset: EvalSubset,
    *,
    eval_id: str | None = None,
    label: str = "",
    linked_run_id: str = "",
    python: str | None = None,
    out_dir: str | Path | None = None,
    extra_args: Sequence[str] | None = None,
    runner: Runner | None = None,
    timeout_s: float | None = None,
    lm_eval_module: str = "lm_eval",
    env: Mapping[str, str] | None = None,
    endpoint_kind: str | None = None,
    baseline: QualityEval | None = None,
    notes: str = "",
) -> QualityEval:
    """执行一次精度评测并返回 ``QualityEval``（不落库；归档交给 ``archive_eval``）。

    ``runner`` 缺省用 ``runner.execute`` 起子进程；测试注入 fake runner 即可
    在无 lm-eval、无 GPU 环境下验证全链路。
    """
    eid = eval_id or new_run_id(f"eval-{scenario.key}")
    out = Path(out_dir) if out_dir else Path.cwd() / "evals" / eid
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / "lm_eval.log"

    py = python or "python3"
    cmd = build_eval_command(
        scenario,
        subset,
        output_path=str(out),
        python=py,
        lm_eval_module=lm_eval_module,
        endpoint_kind=endpoint_kind,
        extra_args=extra_args,
        timeout_s=timeout_s,
    )
    cmd_text = command_string(cmd)

    run = (runner or _default_runner)
    exec_result = run(cmd, log_path=log_path, env=env, timeout=timeout_s)

    results: dict[str, Any] = {}
    rpath = find_results_file(out)
    if rpath is not None:
        try:
            results = parse_results_payload(json.loads(rpath.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            results = {}

    metrics = metrics_from_results(results)
    status = _infer_eval_status(
        getattr(exec_result, "exit_code", None),
        results,
        timed_out=bool(getattr(exec_result, "timed_out", False)),
    )

    ev = QualityEval(
        eval_id=eid,
        scenario_key=scenario.key,
        label=label or scenario.key,
        subset=subset.name,
        run_id=linked_run_id,
        status=status,
        endpoint=scenario.endpoint,
        model=scenario.model,
        tokenizer=scenario.tokenizer,
        lm_eval_cmd=cmd_text,
        tasks=list(subset.tasks),
        num_fewshot=subset.num_fewshot,
        limit_n=subset.limit,
        started_at=getattr(exec_result, "started_at", None) or utcnow(),
        finished_at=getattr(exec_result, "finished_at", None) or utcnow(),
        wall_s=getattr(exec_result, "wall_s", None),
        results=results,
        metrics=metrics,
        raw_dir=str(out),
        baseline_id=(baseline.eval_id if baseline else ""),
        deltas=(
            compare_quality({m.metric_key: m.value for m in baseline.metrics},
                            {m.metric_key: m.value for m in metrics})
            if baseline
            else {}
        ),
        notes=notes,
    )
    return ev


# --------------------------------------------------------------------------- #
# 归档
# --------------------------------------------------------------------------- #


def archive_eval(paths: Paths, ev: QualityEval, *, store: Any = None, log_text: str = "") -> str:
    """把评测结果按 ``<home>/evals/<eval_id>/`` 落盘并入库。"""
    from .env import capture_env

    paths.ensure()
    edir = paths.eval_dir(ev.eval_id)
    edir.mkdir(parents=True, exist_ok=True)
    (edir / "command.txt").write_text(ev.lm_eval_cmd + "\n", encoding="utf-8")
    if log_text:
        (edir / "lm_eval.log").write_text(log_text, encoding="utf-8")

    meta = {
        "eval_id": ev.eval_id,
        "scenario_key": ev.scenario_key,
        "subset": ev.subset,
        "endpoint": ev.endpoint,
        "model": ev.model,
        "started_at": iso(ev.started_at),
        "finished_at": iso(ev.finished_at),
    }
    try:
        meta["deployment_env"] = capture_env(None).to_dict()
    except Exception:  # noqa: BLE001
        meta["deployment_env"] = {"deployment": "", "error": "capture failed"}

    write_json(edir / "meta.json", meta)
    write_json(edir / "results.json", ev.results)
    write_json(edir / "eval.json", ev.to_dict())
    ev.raw_dir = str(edir)

    if store is not None:
        store.upsert_scenario(_scenario_for(ev.scenario_key))
        store.save_quality_eval(ev)
    return ev.eval_id


def _scenario_for(key: str) -> Scenario:
    from .scenarios import BUILTIN_SCENARIOS

    return BUILTIN_SCENARIOS.get(key) or Scenario(
        key=key, name=key, family="quality", description="quality eval"
    )


def latest_baseline(
    store: Any, scenario_key: str, subset: str, *, exclude_id: str = ""
) -> QualityEval | None:
    """取同场景同子集下**最近一次 ok** 的评测作为基线（排除自身）。"""
    rows = store.list_quality_evals(limit=20, scenario_key=scenario_key, subset=subset)
    for row in rows:
        if row["eval_id"] == exclude_id:
            continue
        if (row.get("status") or "") == "ok":
            return store.get_quality_eval(row["eval_id"])
    return None


def run_eval_after(
    paths: Paths,
    scenario: Scenario,
    *,
    subset_name: str = DEFAULT_SUBSET_NAME,
    eval_file: str | Path | None = None,
    store: Any = None,
    linked_run_id: str = "",
    label: str = "",
    python: str | None = None,
    timeout_s: float | None = None,
    runner: Runner | None = None,
    endpoint_kind: str | None = None,
    notes: str = "auto（压测收尾）",
    on_line: Callable[[str], None] | None = None,
) -> QualityEval | None:
    """压测收尾钩子：跑一个子集评测并归档。

    **best-effort**：任何异常（lm-eval 未安装 / 端点不可达 / 解析失败）都被吞掉，
    返回 ``None``，绝不阻断主压测流程。这符合「性能测试不能被质量检查拖垮」的取舍。
    """
    def _say(msg: str) -> None:
        if on_line:
            try:
                on_line(msg)
            except Exception:  # noqa: BLE001
                pass

    try:
        subsets = load_subsets(eval_file, home=paths.home)
        subset = resolve_subset(subset_name, subsets)
        eval_id = new_run_id(f"eval-{scenario.key}")
        out_dir = paths.eval_dir(eval_id)
        baseline = latest_baseline(store, scenario.key, subset.name) if store is not None else None
        ev = run_eval(
            scenario, subset,
            eval_id=eval_id,
            label=label or scenario.key,
            linked_run_id=linked_run_id,
            python=python,
            out_dir=out_dir,
            timeout_s=timeout_s,
            runner=runner,
            endpoint_kind=endpoint_kind,
            baseline=baseline,
            notes=notes,
        )
        log_path = out_dir / "lm_eval.log"
        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        archive_eval(paths, ev, store=store, log_text=log_text)
        _say(f"  [eval] {render_eval_console(ev, baseline).strip()}")
        return ev
    except Exception as exc:  # noqa: BLE001 — 钩子永不阻断主流程
        _say(f"  [eval] 跳过（{type(exc).__name__}: {exc}）")
        return None


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


def render_eval_plan(ev: QualityEval) -> str:
    lines = [
        f"精度评测: {ev.eval_id}",
        f"  场景={ev.scenario_key}  子集={ev.subset}  任务={','.join(ev.tasks)}",
        f"  模型={ev.model}  端点={ev.endpoint}",
        f"  命令: {ev.lm_eval_cmd}",
    ]
    return "\n".join(lines)


def _pct(v: float) -> str:
    return f"{v * 100:.2f}%"


def render_eval_console(ev: QualityEval, baseline: QualityEval | None = None) -> str:
    mark = {"ok": "✅", "partial": "⚠️", "failed": "❌", "skipped": "↺"}.get(ev.status, "•")
    lines = [
        f"{mark} {ev.eval_id}  子集={ev.subset}  状态={ev.status}"
        + (f"  用时={ev.wall_s:.0f}s" if ev.wall_s else ""),
    ]
    if not ev.metrics:
        lines.append("  （无指标：lm-eval 未产出结果，见 raw_dir/lm_eval.log）")
        return "\n".join(lines)
    base_scores = {m.metric_key: m.value for m in baseline.metrics} if baseline else {}
    for m in ev.metrics:
        delta = ev.deltas.get(m.metric_key)
        tail = ""
        if delta is not None:
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "=")
            sign = "+" if delta > 0 else ""
            bs = base_scores.get(m.metric_key)
            tail = f"   {arrow} {sign}{delta * 100:.2f}pp" + (f"（基线 {_pct(bs)}）" if bs is not None else "")
        lines.append(f"  {m.metric_key:34s} {_pct(m.value):>8s}{tail}")
    return "\n".join(lines)


def render_list_console(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "（暂无精度评测记录）"
    lines = [f"{'eval_id':46s}{'subset':12s}{'status':9s}{'started_at':22s}label"]
    for r in rows:
        lines.append(
            f"{str(r.get('eval_id') or ''):46s}"
            f"{str(r.get('subset') or ''):12s}"
            f"{str(r.get('status') or ''):9s}"
            f"{str(r.get('started_at') or '')[:19]:22s}"
            f"{r.get('label') or ''}"
        )
    return "\n".join(lines)


def write_eval_report(path: str | Path, evs: Sequence[QualityEval]) -> Path:
    """产出可存档的 JSON 汇总（列表对比用）。"""
    payload = [e.to_dict() for e in evs]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dumps(payload), encoding="utf-8")
    return p
