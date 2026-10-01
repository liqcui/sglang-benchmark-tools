"""``sglbench`` 命令行入口。

子命令一览::

    scenarios   查看内置/自定义场景目录
    preflight   压测前连通性预检（对应文档第六节）
    run         执行一次/多次场景压测并落盘入库
    ingest      把已有日志（集群 nohup 产物）导入成 run
    list        列出历史 run
    show        查看单个 run 的指标与异常
    compare     同一场景多次 test run 的对比分析
    sweep       并发梯度扫描（吞吐上限/最优水位）
    report      生成自包含 HTML + Markdown + JSON 报告
    gate        按 SLA 判定通过/失败（可用于 CI 卡点）
    export      导出 parquet/csv/json 归档
    sql         直接对 DuckDB 跑 SQL
    db          数据库初始化/信息
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import __version__
from .analyze import (
    ComparisonReport,
    analyze_sweep,
    compare_runs,
    evaluate_gates,
)
from .models import Scenario
from .report import (
    build_html,
    render_console,
    render_markdown,
    render_sweep_console,
    render_table,
    write_html,
)
from .runner import (
    BENCH_MODULE,
    DETAIL_FLAG,
    build_command,
    discover_logs,
    ingest_log,
    label_from_log_path,
    render_shell,
    run_scenario,
)
from .scenarios import FAMILIES, catalog, get_scenario
from .store import Store
from .util import (
    Paths,
    format_metric,
    iso,
    parse_duration,
    parse_kv,
    resolve_paths,
    utcnow,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


class UsageError(Exception):
    """参数/用法错误：打印提示并返回退出码 2（不抛 SystemExit，便于被调用方复用）。"""


# --------------------------------------------------------------------------- #
# 公共参数
# --------------------------------------------------------------------------- #


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", help="sglbench 家目录（默认 ./data，或环境变量 SGLBENCH_HOME）")
    parser.add_argument("--db", help="DuckDB 文件路径（覆盖默认 <home>/bench.duckdb）")
    parser.add_argument(
        "--scenario-file",
        action="append",
        default=[],
        metavar="FILE",
        help="追加/覆盖场景定义的 JSON/TOML 文件（可重复）",
    )
    parser.add_argument("--quiet", action="store_true", help="只输出必要信息")


def _paths(args: argparse.Namespace) -> Paths:
    return resolve_paths(getattr(args, "home", None), getattr(args, "db", None))


def _catalog(args: argparse.Namespace) -> dict[str, Scenario]:
    return catalog(list(getattr(args, "scenario_file", []) or []), home=_paths(args).home)


def _scenario(args: argparse.Namespace, key: str) -> Scenario:
    return get_scenario(key, _catalog(args))


def _eprint(*parts: Any) -> None:
    print(*parts, file=sys.stderr)


# --------------------------------------------------------------------------- #
# --set 覆盖项
# --------------------------------------------------------------------------- #


def _coerce(current: Any, raw: str) -> Any:
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "y", "on")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(float(raw))
    if isinstance(current, float):
        return float(raw)
    if current is None:
        try:
            return int(raw)
        except ValueError:
            try:
                return float(raw)
            except ValueError:
                return raw
    return raw


def apply_set(scenario: Scenario, pairs: Mapping[str, str]) -> Scenario:
    """把 ``--set max_concurrency=64`` 应用到场景（类型按现有字段推断）。"""
    from dataclasses import replace

    clean: dict[str, Any] = {}
    valid = set(Scenario.__dataclass_fields__)  # type: ignore[attr-defined]
    for key, raw in pairs.items():
        field = key.replace("-", "_")
        if field not in valid:
            raise UsageError(f"未知的场景字段: {key}（可用: {', '.join(sorted(valid))}）")
        clean[field] = _coerce(getattr(scenario, field), raw)
    return replace(scenario, **clean) if clean else scenario


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def cmd_run(args: argparse.Namespace) -> int:
    paths = _paths(args)
    scenario = _scenario(args, args.scenario)
    if args.set:
        scenario = apply_set(scenario, parse_kv(args.set))
    store = None if args.no_store else Store(paths.db)

    timeout = parse_duration(args.timeout).total_seconds() if args.timeout else None

    cmd = build_command(
        scenario,
        python=args.python,
        module=args.module,
        # dry-run 只用于预览/复制到集群，路径用可替换的占位符而不是本机绝对路径
        detail_path=("$RUN_DIR/requests.jsonl" if not args.no_dump_requests else None),
        detail_flag=args.detail_flag,
    )

    if args.dry_run:
        if args.json:
            print(json.dumps({"command": list(cmd), "scenario": scenario.to_dict()}, ensure_ascii=False, indent=2))
        else:
            print(f"# 场景: {scenario.key} — {scenario.name}")
            print(render_shell(scenario, cmd))
        if store:
            store.upsert_scenario(scenario)
            store.close()
        return EXIT_OK

    if store:
        store.upsert_scenario(scenario)

    repeats = max(1, int(args.repeat))
    failures = 0
    outcomes = []
    for index in range(repeats):
        label = args.label or scenario.key
        if repeats > 1:
            label = f"{label}#{index + 1}"
        _eprint(f"[{index + 1}/{repeats}] 执行 {scenario.key} …")
        outcome = run_scenario(
            paths,
            scenario,
            label=label,
            notes=args.notes or "",
            tags=args.tag,
            run_id=args.run_id if repeats == 1 else None,
            python=args.python,
            module=args.module,
            detail_flag=args.detail_flag,
            dump_requests=not args.no_dump_requests,
            timeout=timeout,
            overrides=None,
            on_line=None if args.quiet else (lambda line: _eprint(f"  | {line}") if args.verbose else None),
        )
        outcomes.append(outcome)
        if store:
            store.save_run(outcome.record)
        if outcome.record.status != "ok":
            failures += 1
        metrics = outcome.record.metrics
        summary = f"    → run_id={outcome.record.run_id} status={outcome.record.status}"
        if "successful_requests" in metrics:
            summary += (
                f" 成功请求={metrics['successful_requests']:.0f}"
                f" MeanTTFT={format_metric(metrics.get('mean_ttft_ms'), 'ms')}"
                f" MeanTPOT={format_metric(metrics.get('mean_tpot_ms'), 'ms')}"
            )
        _eprint(summary)
    if store:
        store.close()

    if args.fail_on_bad and failures:
        _eprint(f"❌ {failures}/{repeats} 次运行未达到 ok 状态")
        return EXIT_FAIL
    return EXIT_OK


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #


def _ingest_one(
    args: argparse.Namespace,
    paths: Paths,
    catalog_map: Mapping[str, Scenario],
    log_path: Path,
    scenario_key: str,
    store: Store | None = None,
) -> tuple[str, str]:
    """导入单个日志。``dedupe`` 生效时按内容指纹跳过重复导入（幂等）。"""
    scenario = get_scenario(scenario_key, catalog_map)
    if store is not None and not args.force:
        from .util import sha256_text

        digest = sha256_text(log_path.read_text(encoding="utf-8", errors="replace"))
        existing = store.find_run_by_content(scenario.key, digest)
        if existing:
            _eprint(f"↺ {log_path.name} 内容与已有 run {existing} 相同，跳过（--force 可强制重新导入）")
            return existing, "duplicate"
    outcome = ingest_log(
        paths,
        log_path,
        scenario,
        requests_path=args.requests,
        label=args.label or label_from_log_path(log_path),
        notes=args.notes or "",
        tags=args.tag,
        run_id=args.run_id,
        command=args.command,
        operator=args.operator or "",
        meta_extra={"ingested_via": "sglbench ingest"},
    )
    return outcome.record.run_id, outcome.record.status


def cmd_ingest(args: argparse.Namespace) -> int:
    paths = _paths(args)
    catalog_map = _catalog(args)
    store = None if args.no_store else Store(paths.db)

    if args.infer:
        # 目录批量导入：文件名约定 <scenario_key>__<label>.log
        targets: list[tuple[Path, str]] = []
        for log_path in discover_logs(args.path[0], args.pattern):
            if args.requests:
                detail = Path(args.requests)
            else:
                detail = log_path.with_suffix("").with_name(log_path.stem + ".requests.jsonl")
            if not detail.exists():
                # 没有每请求明细也能导入，只是失去分布级分析能力
                pass
            key = log_path.stem.split("__", 1)[0]
            if key not in catalog_map:
                _eprint(f"⚠️  跳过 {log_path.name}：无法从文件名推断场景 {key!r}")
                continue
            targets.append((log_path, key))
        if not targets:
            _eprint("没有可导入的日志。")
            if store:
                store.close()
            return EXIT_FAIL
    else:
        if not args.scenario:
            raise UsageError("非 --infer 模式下必须指定 --scenario")
        targets = [(Path(p), args.scenario) for p in args.path]

    rows: list[list[str]] = []
    errored = 0
    skipped = 0
    not_ok = 0
    for log_path, key in targets:
        try:
            run_id, status = _ingest_one(args, paths, catalog_map, log_path, key, store)
        except (OSError, KeyError, ValueError, RuntimeError) as exc:
            errored += 1
            rows.append([log_path.name, key, "-", f"导入失败: {exc}"])
            continue
        if status == "duplicate":
            skipped += 1
            rows.append([log_path.name, key, run_id, "已存在(跳过)"])
            continue
        if store:
            store.upsert_scenario(get_scenario(key, catalog_map))
            # 统一从落盘的 run.json 还原记录后入库，保证"库 == 磁盘工件"
            record = ingest_log_cached(paths, run_id)
            if record is not None:
                store.save_run(record)
        rows.append([log_path.name, key, run_id, status])
        if status != "ok":
            not_ok += 1

    if store:
        store.close()

    if not args.quiet:
        print(render_table(["日志", "场景", "run_id", "状态"], rows, ["left", "left", "left", "left"]))
    imported = len(rows) - errored - skipped
    print(f"导入完成: {imported} 成功导入 / {errored} 导入失败 / {skipped} 跳过（内容重复）", file=sys.stderr)
    if not_ok:
        # 数据本身是 partial/failed 不算导入错误 —— 恰恰是我们要留档的证据
        print(f"⚠️  其中 {not_ok} 个 run 的压测结果未达 ok 状态，用 `sglbench show <run_id>` 查看异常日志", file=sys.stderr)
    return EXIT_FAIL if errored else EXIT_OK


def ingest_log_cached(paths: Paths, run_id: str):
    """从 run.json 还原 RunRecord（含每请求明细），供入库与报告复用。"""
    from .models import Issue, RunRecord, Sample
    from .parse import load_samples
    from .util import parse_iso, read_json

    run_dir = paths.run_dir(run_id)
    data = read_json(run_dir / "run.json", default=None)
    if not data:
        return None
    samples: list[Sample] = []
    requests_file = run_dir / "requests.jsonl"
    if requests_file.exists():
        samples = load_samples(requests_file)
    return RunRecord(
        run_id=data["run_id"],
        scenario_key=data["scenario_key"],
        label=data.get("label") or "",
        status=data.get("status", "ok"),
        source=data.get("source", "ingested"),
        started_at=parse_iso(data.get("started_at")),
        finished_at=parse_iso(data.get("finished_at")),
        wall_s=data.get("wall_s"),
        command=data.get("command") or "",
        exit_code=data.get("exit_code"),
        host=data.get("host") or "",
        operator=data.get("operator") or "",
        notes=data.get("notes") or "",
        tags=list(data.get("tags") or []),
        raw_dir=data.get("raw_dir") or str(run_dir),
        params=dict(data.get("params") or {}),
        env=dict(data.get("env") or {}),
        metrics={k: float(v) for k, v in (data.get("metrics") or {}).items()},
        samples=samples,
        issues=[Issue(**i) for i in (data.get("issues") or [])],
        artifacts=dict(data.get("artifacts") or {}),
        meta=dict(data.get("meta") or {}),
    )


# --------------------------------------------------------------------------- #
# list / show
# --------------------------------------------------------------------------- #


def cmd_list(args: argparse.Namespace) -> int:
    paths = _paths(args)
    with Store(paths.db) as store:
        scenario_key = None
        if args.scenario:
            scenario_key = _scenario(args, args.scenario).key
        runs = store.list_runs(
            scenario_key=scenario_key,
            family=args.family,
            status=args.status,
            limit=args.limit,
            ascending=False,
        )
    if args.json:
        print(json.dumps([_summary_dict(r) for r in runs], ensure_ascii=False, indent=2))
        return EXIT_OK

    rows = []
    for r in runs:
        rows.append(
            [
                r.run_id,
                r.scenario_key,
                r.label or "-",
                iso(r.started_at)[:19] if r.started_at else "-",
                r.status,
                format_metric(r.metrics.get("success_rate"), "%"),
                format_metric(r.metrics.get("mean_ttft_ms"), "ms"),
                format_metric(r.metrics.get("p99_ttft_ms"), "ms"),
                format_metric(r.metrics.get("mean_tpot_ms"), "ms"),
                format_metric(r.metrics.get("output_token_throughput_tok_s"), "tok/s"),
                str(r.sample_count),
                str(r.issue_count),
            ]
        )
    if not rows:
        print("数据库里还没有 run。先执行 `sglbench ingest` 或 `sglbench run`。")
        return EXIT_OK
    print(
        render_table(
            ["run_id", "场景", "标签", "时间(UTC)", "状态", "成功率", "MeanTTFT", "P99TTFT", "MeanTPOT", "输出吞吐", "样本", "异常"],
            rows,
            ["left", "left", "left", "left", "left", "right", "right", "right", "right", "right", "right", "right"],
        )
    )
    return EXIT_OK


def _summary_dict(run: Any) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "scenario_key": run.scenario_key,
        "label": run.label,
        "status": run.status,
        "started_at": iso(run.started_at) if run.started_at else None,
        "sample_count": run.sample_count,
        "issue_count": run.issue_count,
        "source": run.source,
        "metrics": run.metrics,
    }


def cmd_show(args: argparse.Namespace) -> int:
    paths = _paths(args)
    with Store(paths.db) as store:
        run_id = store.resolve_run(args.run)
        record = store.get_run(run_id, with_samples=True)
        if record is None:
            _eprint(f"找不到 run: {run_id}")
            return EXIT_FAIL
        gates = evaluate_gates(_scenario(args, record.scenario_key) if _has_scenario(args, record.scenario_key) else None, record.metrics, record.issues)

    if args.json:
        payload = record.to_dict(include_samples=False)
        payload["gates"] = [g.to_dict() for g in gates]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    print("=" * 78)
    print(f"run_id      : {record.run_id}")
    print(f"场景        : {record.scenario_key}")
    print(f"标签        : {record.label}")
    print(f"状态/来源   : {record.status} / {record.source}")
    print(f"时间(UTC)   : {iso(record.started_at)} → {iso(record.finished_at)}  用时 {record.wall_s}")
    print(f"主机/操作者 : {record.host} / {record.operator}")
    print(f"工件目录    : {record.raw_dir}")
    print("=" * 78)

    if record.command:
        print("命令:")
        print(f"  {record.command}")

    metrics = record.metrics
    print("\n核心指标:")
    keys = [
        "successful_requests",
        "success_rate",
        "benchmark_duration_s",
        "request_throughput_req_s",
        "output_token_throughput_tok_s",
        "total_token_throughput_tok_s",
        "mean_ttft_ms",
        "median_ttft_ms",
        "p99_ttft_ms",
        "mean_tpot_ms",
        "median_tpot_ms",
        "p99_tpot_ms",
        "mean_itl_ms",
        "p99_itl_ms",
        "mean_e2e_latency_ms",
        "p99_e2e_latency_ms",
        "tail_ratio_ttft",
        "tail_ratio_tpot",
        "itl_cv",
    ]
    from .models import spec_for

    for key in keys:
        if key in metrics:
            print(f"  {spec_for(key).label:<26} {format_metric(metrics[key], spec_for(key).unit)}")

    if gates:
        print("\nSLA 门禁:")
        for g in gates:
            mark = {"pass": "✅", "fail": "❌", "unknown": "❔"}[g.status]
            print(f"  {mark} {g.label:<24} 实测 {g.actual_text:<14} 阈值 {g.threshold_text}")

    if record.issues:
        print(f"\n日志异常 ({len(record.issues)} 条):")
        for issue in record.issues[:20]:
            print(f"  [{issue.severity}] {issue.kind}: {issue.message[:150]}")

    if record.samples:
        from .stats import describe

        print(f"\n每请求分布（n={len(record.samples)}）:")
        for field, label in (("ttft_ms", "TTFT"), ("tpot_ms", "TPOT"), ("itl_ms", "ITL"), ("e2e_ms", "E2E")):
            values = [getattr(s, field) for s in record.ok_samples if getattr(s, field) is not None]
            stats = describe(values)
            if not stats:
                continue
            print(
                f"  {label:<5} mean={stats['mean']:.2f}  p50={stats['median']:.2f}  p90={stats['p90']:.2f}  "
                f"p99={stats['p99']:.2f}  max={stats['max']:.2f}  cv={stats['cv']:.3f}"
            )
    return EXIT_OK


def _has_scenario(args: argparse.Namespace, key: str) -> bool:
    try:
        get_scenario(key, _catalog(args))
        return True
    except KeyError:
        return False


# --------------------------------------------------------------------------- #
# compare / report / gate
# --------------------------------------------------------------------------- #


def _select_runs(store: Store, args: argparse.Namespace, catalog_map: Mapping[str, Scenario]) -> list[str]:
    ids: list[str] = []
    if getattr(args, "last", None):
        if not args.scenario:
            raise UsageError("--last 需要同时指定 --scenario")
        key = get_scenario(args.scenario, catalog_map).key
        # run_ids_for_scenario 返回时间倒序；反转让基线=最早的一次，方向更直观
        ids = list(reversed(store.run_ids_for_scenario(key, limit=args.last)))
    for token in getattr(args, "runs", []) or []:
        ids.append(store.resolve_run(token))
    if not ids:
        raise UsageError("需要指定 run（位置参数）或 --scenario + --last N")
    seen: list[str] = []
    for rid in ids:
        if rid not in seen:
            seen.append(rid)
    return seen


def _sla_overrides(args: argparse.Namespace) -> dict[str, float] | None:
    """把 ``--set-sla min_success_rate=0.99`` 解析成 ``{key: float}``。"""
    raw = parse_kv(getattr(args, "set_sla", None) or [])
    if not raw:
        return None
    out: dict[str, float] = {}
    for key, value in raw.items():
        try:
            out[key] = float(value)
        except ValueError:
            raise UsageError(f"--set-sla 的值必须是数字: {key}={value}") from None
    return out


def _build_comparison(store: Store, args: argparse.Namespace, catalog_map: Mapping[str, Scenario]) -> tuple[ComparisonReport, list[Any]]:
    run_ids = _select_runs(store, args, catalog_map)
    baseline = store.resolve_run(args.baseline) if getattr(args, "baseline", None) else run_ids[0]
    if baseline in run_ids:
        run_ids = [baseline] + [r for r in run_ids if r != baseline]
    scenario = None
    first = store.get_run(run_ids[0], with_samples=False)
    if first is not None and first.scenario_key in catalog_map:
        scenario = catalog_map[first.scenario_key]
    report = compare_runs(
        store,
        run_ids,
        baseline=baseline,
        scenario=scenario,
        threshold_pct=args.threshold_pct,
        with_distributions=not getattr(args, "no_distributions", False),
        resamples=getattr(args, "resamples", 2000),
        extra_sla=_sla_overrides(args),
        all_metrics=getattr(args, "all_metrics", False),
    )
    records = [store.get_run(rid, with_samples=True) for rid in run_ids]
    return report, [r for r in records if r is not None]


def cmd_compare(args: argparse.Namespace) -> int:
    paths = _paths(args)
    catalog_map = _catalog(args)
    with Store(paths.db) as store:
        report, _ = _build_comparison(store, args, catalog_map)

    if args.format == "json":
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str))
    elif args.format == "markdown":
        print(render_markdown(report))
    else:
        print(render_console(report))
    return EXIT_OK if report.all_gates_pass or not args.fail_on_gate else EXIT_FAIL


def cmd_report(args: argparse.Namespace) -> int:
    paths = _paths(args)
    catalog_map = _catalog(args)
    with Store(paths.db) as store:
        report, records = _build_comparison(store, args, catalog_map)
        sweep = None
        if args.sweep:
            sweep = analyze_sweep(store, family=args.sweep_family, latest_per_scenario=not args.all_runs)

    out = Path(args.output) if args.output else paths.reports_dir / f"{report.scenario_key}-{report.baseline_run}-vs-{len(report.candidate_runs)}.html"
    html = build_html(report, title=args.title, runs=records, sweep=sweep, report_path=out)
    write_html(out, html)
    print(f"HTML 报告: {out}")

    if args.markdown:
        Path(args.markdown).write_text(render_markdown(report), encoding="utf-8", newline="\n")
        print(f"Markdown   : {args.markdown}")
    if args.json:
        Path(args.json).write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"JSON       : {args.json}")
    return EXIT_OK


def cmd_gate(args: argparse.Namespace) -> int:
    paths = _paths(args)
    catalog_map = _catalog(args)
    with Store(paths.db) as store:
        if args.run:
            run_ids = [store.resolve_run(args.run)]
        else:
            key = get_scenario(args.scenario, catalog_map).key
            run_ids = [store.latest_run(key)] if store.latest_run(key) else []
            if not run_ids:
                _eprint(f"场景 {key} 还没有 run")
                return EXIT_FAIL
        extra_sla = _sla_overrides(args)

        rows = []
        failed = 0
        for run_id in run_ids:
            record = store.get_run(run_id, with_samples=False)
            if record is None:
                continue
            scenario = catalog_map.get(record.scenario_key)
            gates = evaluate_gates(scenario, record.metrics, record.issues, extra_sla=extra_sla)
            for g in gates:
                if g.status == "fail":
                    failed += 1
                rows.append(
                    [
                        record.label or record.run_id,
                        g.label,
                        g.actual_text,
                        g.threshold_text,
                        {"pass": "✅ PASS", "fail": "❌ FAIL", "unknown": "❔ N/A"}[g.status],
                        g.detail,
                    ]
                )
    if args.json:
        print(json.dumps({"failed": failed, "checks": rows}, ensure_ascii=False, indent=2))
    else:
        print(render_table(["run", "门禁项", "实测", "阈值", "结果", "说明"], rows, ["left", "left", "right", "right", "left", "left"]))
        print(f"门禁结果: {'❌ 失败' if failed else '✅ 通过'}（失败 {failed} 项）")
    return EXIT_FAIL if failed else EXIT_OK


# --------------------------------------------------------------------------- #
# sweep / scenarios / preflight / export / sql / db
# --------------------------------------------------------------------------- #


def cmd_sweep(args: argparse.Namespace) -> int:
    paths = _paths(args)
    catalog_map = _catalog(args)
    with Store(paths.db) as store:
        keys = [get_scenario(k, catalog_map).key for k in (args.scenario or [])]
        report = analyze_sweep(
            store,
            scenario_keys=keys or None,
            family=args.family,
            latest_per_scenario=not args.all_runs,
        )
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str))
        return EXIT_OK
    if not report.points:
        print("没有可用于扫描的 run。先导入或执行若干并发梯度的场景。")
        return EXIT_OK
    print(render_sweep_console(report))
    return EXIT_OK


def cmd_scenarios(args: argparse.Namespace) -> int:
    paths = _paths(args)
    catalog_map = _catalog(args)
    if args.action == "show":
        scenario = get_scenario(args.key, catalog_map)
        print(json.dumps(scenario.to_dict(), ensure_ascii=False, indent=2))
        cmd = build_command(scenario, python=args.python, module=args.module)
        print("\n执行命令:")
        print(render_shell(scenario, cmd))
        return EXIT_OK

    if args.action == "export":
        target = Path(args.output or (paths.home / "scenarios.json"))
        target.write_text(
            json.dumps({"scenarios": [s.to_dict() for s in catalog_map.values()]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"已导出 {len(catalog_map)} 个场景: {target}")
        return EXIT_OK

    rows = []
    for scenario in catalog_map.values():
        rows.append(
            [
                scenario.key,
                FAMILIES.get(scenario.family, scenario.family),
                str(scenario.max_concurrency),
                str(scenario.num_prompts),
                f"{scenario.random_input}/{scenario.random_output}",
                str(scenario.request_rate),
                "是" if scenario.pd_separated else "否",
                scenario.name,
            ]
        )
    print(render_table(["key", "族", "并发", "请求数", "输入/输出", "速率", "PD分离", "名称"], rows,
                       ["left", "left", "right", "right", "right", "right", "center", "left"]))
    print(f"\n共 {len(rows)} 个场景。族说明: " + "；".join(f"{k}={v}" for k, v in FAMILIES.items()))
    return EXIT_OK


def cmd_preflight(args: argparse.Namespace) -> int:
    from .preflight import check_endpoint, check_scenario, render_console as render_preflight

    paths = _paths(args)
    catalog_map = _catalog(args)
    store = None if args.no_store else Store(paths.db)

    if args.url:
        results = [
            check_endpoint(
                url,
                model=args.model or "default",
                prompt=args.prompt,
                max_tokens=args.max_tokens,
                timeout=args.timeout_s,
                stream=not args.no_stream,
            )
            for url in args.url
        ]
    else:
        if not args.scenario:
            raise UsageError("请指定 --scenario 或 --url")
        scenario = get_scenario(args.scenario, catalog_map)
        results = check_scenario(
            scenario,
            prompt=args.prompt,
            max_tokens=args.max_tokens,
            timeout=args.timeout_s,
            stream=not args.no_stream,
            include_nodes=not args.no_nodes,
            model=args.model,
        )
        if store:
            store.upsert_scenario(scenario)

    if store:
        for result in results:
            store.save_preflight(result.to_check())
        store.close()

    if args.json:
        print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2, default=str))
    else:
        print(render_preflight(results))
    return EXIT_OK if all(r.ok for r in results) else EXIT_FAIL


def _copy_literal(path: Path) -> str:
    """DuckDB COPY 目标路径只能用字面量，这里做最小的单引号转义。"""
    return "'" + str(path).replace("'", "''") + "'"


def cmd_export(args: argparse.Namespace) -> int:
    paths = _paths(args)
    out = Path(args.output) if args.output else paths.exports_dir / f"export-{utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    with Store(paths.db) as store:
        if args.sql:
            out.mkdir(parents=True, exist_ok=True)
            if args.format == "parquet":
                target = out / "query.parquet"
                store.con.execute(f"COPY ({args.sql}) TO {_copy_literal(target)} (FORMAT PARQUET)")
            elif args.format == "csv":
                target = out / "query.csv"
                store.con.execute(f"COPY ({args.sql}) TO {_copy_literal(target)} (FORMAT CSV, HEADER)")
            else:
                target = out / "query.json"
                target.write_text(
                    json.dumps(store.query(args.sql), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
                )
            written = [target]
        else:
            written = store.export(out, fmt=args.format)
    for path in written:
        print(path)
    print(f"导出 {len(written)} 个文件 → {out}")
    return EXIT_OK


def cmd_sql(args: argparse.Namespace) -> int:
    paths = _paths(args)
    with Store(paths.db, read_only=False) as store:
        rows = store.query(args.query)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK
    if not rows:
        print("(空结果)")
        return EXIT_OK
    headers = list(rows[0].keys())
    print(render_table(headers, [[str(r.get(h, "")) for h in headers] for r in rows]))
    print(f"\n{len(rows)} 行")
    return EXIT_OK


def cmd_db(args: argparse.Namespace) -> int:
    paths = _paths(args)
    if args.action == "path":
        print(paths.db)
        return EXIT_OK
    with Store(paths.db) as store:
        if args.action == "info":
            tables = store.query(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name"
            )
            rows = []
            for t in tables:
                name = t["table_name"]
                count = store.scalar(f"SELECT COUNT(*) FROM {name}")
                rows.append([name, str(count)])
            print(f"数据库: {paths.db}")
            print(f"schema 版本: {store.schema_version()}")
            print(render_table(["表/视图", "行数"], rows, ["left", "right"]))
            return EXIT_OK
    print(f"数据库已初始化: {paths.db}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sglbench",
        description="SGLang PD 分离集群压测执行 / 归档 / 对比 / 可视化工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
        "  sglbench scenarios\n"
        "  sglbench preflight --scenario single-standard\n"
        "  sglbench run --scenario conc-64-standard --dry-run\n"
        "  sglbench ingest examples/logs/*.log --infer\n"
        "  sglbench compare --scenario single-standard --last 2\n"
        "  sglbench report --scenario single-standard --last 2 -o compare.html --sweep\n",
    )
    parser.add_argument("--version", action="version", version=f"sglbench {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # scenarios
    p = sub.add_parser("scenarios", help="查看场景目录")
    _add_common(p)
    p.add_argument("action", nargs="?", choices=["list", "show", "export"], default="list")
    p.add_argument("key", nargs="?", help="show 时的场景 key")
    p.add_argument("-o", "--output", help="export 的输出路径")
    p.add_argument("--python", help="生成命令时使用的解释器（默认 python3）")
    p.add_argument("--module", default=BENCH_MODULE)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_scenarios)

    # preflight
    p = sub.add_parser("preflight", help="压测前连通性预检")
    _add_common(p)
    p.add_argument("--scenario", help="按场景探测（含 Prefill/Decode 直连节点）")
    p.add_argument("--url", action="append", help="直接指定端点（可重复）")
    p.add_argument("--model", help="模型名（默认取场景定义）")
    p.add_argument("--prompt", default="简要说明PD分离架构优势")
    p.add_argument("--max-tokens", type=int, default=64)
    p.add_argument("--timeout-s", type=float, default=30.0)
    p.add_argument("--no-stream", action="store_true", help="用非流式请求")
    p.add_argument("--no-nodes", action="store_true", help="只探测网关，不探测单节点")
    p.add_argument("--no-store", action="store_true", help="不写入数据库")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_preflight)

    # run
    p = sub.add_parser("run", help="执行场景压测")
    _add_common(p)
    p.add_argument("--scenario", required=True)
    p.add_argument("--label", help="本次 run 的标签（默认场景 key）")
    p.add_argument("--notes", help="备注")
    p.add_argument("--tag", action="append", default=[], help="标签（可重复）")
    p.add_argument("--run-id", help="指定 run_id（做 CI 固定 ID 时有用）")
    p.add_argument("--repeat", type=int, default=1, help="重复执行次数（做方差研究）")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="覆盖场景参数，如 max_concurrency=64")
    p.add_argument("--python", help="解释器（默认 python3）")
    p.add_argument("--module", default=BENCH_MODULE)
    p.add_argument("--detail-flag", default=DETAIL_FLAG, help="每请求明细导出参数名")
    p.add_argument("--no-dump-requests", action="store_true", help="不尝试导出每请求明细")
    p.add_argument("--timeout", help="超时（如 30m / 2h）")
    p.add_argument("--dry-run", action="store_true", help="只打印命令，不执行")
    p.add_argument("--json", action="store_true", help="dry-run 时以 JSON 输出命令")
    p.add_argument("--no-store", action="store_true")
    p.add_argument("--fail-on-bad", action="store_true", help="有非 ok 的 run 时返回非零（CI 用）")
    p.add_argument("--verbose", action="store_true", help="实时回显压测输出")
    p.set_defaults(func=cmd_run)

    # ingest
    p = sub.add_parser("ingest", help="导入已有日志")
    _add_common(p)
    p.add_argument("path", nargs="+", help="日志文件或目录")
    p.add_argument("--scenario", help="场景 key")
    p.add_argument("--infer", action="store_true", help="批量导入目录，从文件名 <scenario>__<label>.log 推断场景")
    p.add_argument("--pattern", default="*.log", help="--infer 时的文件匹配（默认 *.log）")
    p.add_argument("--requests", help="显式指定每请求明细文件")
    p.add_argument("--label", help="run 标签")
    p.add_argument("--notes")
    p.add_argument("--tag", action="append", default=[])
    p.add_argument("--run-id")
    p.add_argument("--command", help="记录对应的执行命令文本")
    p.add_argument("--operator", help="操作者")
    p.add_argument("--force", action="store_true", help="即使内容指纹重复也重新导入")
    p.add_argument("--no-store", action="store_true", help="只落盘不入库")
    p.set_defaults(func=cmd_ingest)

    # list
    p = sub.add_parser("list", help="列出历史 run")
    _add_common(p)
    p.add_argument("--scenario")
    p.add_argument("--family")
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    # show
    p = sub.add_parser("show", help="查看单个 run")
    _add_common(p)
    p.add_argument("run")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    # compare
    p = sub.add_parser("compare", help="对比同一场景的多次 test run")
    _add_common(p)
    p.add_argument("runs", nargs="*", help="run_id（可前缀匹配、可用 latest / <scenario>:<n>）")
    p.add_argument("--scenario", help="配合 --last 使用")
    p.add_argument("--last", type=int, help="取该场景最近 N 次 run")
    p.add_argument("--baseline", help="指定基线 run（默认取第一个）")
    p.add_argument("--threshold-pct", type=float, default=5.0, help="噪声阈值（默认 5%%）")
    p.add_argument("--all-metrics", action="store_true", help="对比全部指标（默认只对比关键指标）")
    p.add_argument("--no-distributions", action="store_true", help="跳过分布级检验")
    p.add_argument("--resamples", type=int, default=2000, help="bootstrap 重采样次数")
    p.add_argument("--set-sla", action="append", default=[], metavar="KEY=VALUE", help="额外 SLA 门禁")
    p.add_argument("--format", choices=["table", "markdown", "json"], default="table")
    p.add_argument("--fail-on-gate", action="store_true", help="门禁失败时返回非零")
    p.set_defaults(func=cmd_compare)

    # report
    p = sub.add_parser("report", help="生成可视化报告")
    _add_common(p)
    p.add_argument("runs", nargs="*")
    p.add_argument("--scenario")
    p.add_argument("--last", type=int)
    p.add_argument("--baseline")
    p.add_argument("--threshold-pct", type=float, default=5.0)
    p.add_argument("--all-metrics", action="store_true", help="报告中对比全部指标")
    p.add_argument("--no-distributions", action="store_true")
    p.add_argument("--resamples", type=int, default=2000)
    p.add_argument("--set-sla", action="append", default=[])
    p.add_argument("-o", "--output", help="HTML 输出路径")
    p.add_argument("--title")
    p.add_argument("--markdown", help="同时导出 Markdown")
    p.add_argument("--json", help="同时导出 JSON")
    p.add_argument("--sweep", action="store_true", help="附带并发梯度扫描章节")
    p.add_argument("--sweep-family", default="concurrency")
    p.add_argument("--all-runs", action="store_true", help="扫描时使用全部 run（默认每个场景取最新）")
    p.set_defaults(func=cmd_report)

    # sweep
    p = sub.add_parser("sweep", help="并发梯度扫描分析")
    _add_common(p)
    p.add_argument("--scenario", action="append", help="指定参与扫描的场景（可重复）")
    p.add_argument("--family", default="concurrency")
    p.add_argument("--all-runs", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sweep)

    # gate
    p = sub.add_parser("gate", help="按 SLA 判定通过/失败")
    _add_common(p)
    p.add_argument("--scenario", help="场景 key")
    p.add_argument("--run", help="指定 run（默认该场景最新一次）")
    p.add_argument("--set-sla", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_gate)

    # export
    p = sub.add_parser("export", help="导出数据归档")
    _add_common(p)
    p.add_argument("-o", "--output", help="输出目录")
    p.add_argument("--format", choices=["parquet", "csv", "json"], default="parquet")
    p.add_argument("--sql", help="只导出该查询结果")
    p.set_defaults(func=cmd_export)

    # sql
    p = sub.add_parser("sql", help="对 DuckDB 执行 SQL")
    _add_common(p)
    p.add_argument("query")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sql)

    # db
    p = sub.add_parser("db", help="数据库管理")
    _add_common(p)
    p.add_argument("action", nargs="?", choices=["init", "info", "path"], default="init")
    p.set_defaults(func=cmd_db)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or EXIT_OK)
    except UsageError as exc:
        _eprint(f"❌ {exc}")
        return EXIT_USAGE
    except BrokenPipeError:  # pragma: no cover
        return EXIT_OK
    except KeyboardInterrupt:  # pragma: no cover
        _eprint("\n已中断。")
        return 130
    except (KeyError, FileNotFoundError, ValueError, RuntimeError) as exc:
        _eprint(f"❌ {exc}")
        return EXIT_FAIL
    except ModuleNotFoundError as exc:
        _eprint(f"❌ 缺少依赖: {exc}。请先 `pip install duckdb`。")
        return EXIT_FAIL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
