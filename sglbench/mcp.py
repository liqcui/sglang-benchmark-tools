"""sglbench MCP server —— 把压测工具暴露为 MCP tools（stdio, 零依赖轻量实现）。

任何 MCP 兼容 agent（Claude Code / OpenClaw 类框架等）可经此调用 13 个 tools：
status / list_runs / show_run / sql / compare / dashboard / run_scenario /
report / rounds / campaign_report / trend / forensics / recover（两段式 dry_run 默认 true）。

协议: JSON-RPC 2.0 over stdio（MCP stdio transport, 行分隔 JSON）。
启动: ``sglbench mcp``（agent 侧配置 command=sglbench, args=["mcp"]）。
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

from .store import Store
from .util import resolve_paths

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "sglbench", "version": "0.1.0"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "sglbench_status",
        "description": "集群+DB 健康快照: router/leaders 连通、run 统计。只读非侵入，agent 决策入口。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sglbench_list_runs",
        "description": "列出历史 run（可按 scenario_key/label/status 过滤）。返回 run_id/状态/指标摘要。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scenario": {"type": "string", "description": "场景 key 过滤"},
                "label": {"type": "string", "description": "label 过滤"},
                "status": {"type": "string", "description": "ok/failed/partial"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "sglbench_show_run",
        "description": "查看单个 run 的完整记录（指标/参数/环境快照/问题）。",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
    },
    {
        "name": "sglbench_sql",
        "description": "对 DuckDB 直查（长期记忆）。只允许 SELECT/WITH 开头的只读查询。",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "SQL (SELECT ...)"}},
            "required": ["query"],
        },
    },
    {
        "name": "sglbench_compare",
        "description": "对比两个 run（指标 Δ/显著性检验）。用于 A/B 收益比较。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "baseline_run": {"type": "string"},
                "candidate_run": {"type": "string"},
            },
            "required": ["baseline_run", "candidate_run"],
        },
    },
    {
        "name": "sglbench_dashboard",
        "description": "生成跨场景对比 dashboard HTML（label 配对, 按 scenario_key 对齐, 趋势图+表格）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pair": {"type": "string", "description": "labelA=labelB"},
                "output": {"type": "string", "description": "输出 HTML 路径"},
            },
            "required": ["pair"],
        },
    },
    {
        "name": "sglbench_run_scenario",
        "description": "执行一个压测场景并归档（写 DuckDB）。较重操作; auto_retry>0 时崩溃自动等健康重试。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scenario": {"type": "string"},
                "label": {"type": "string"},
                "exec": {"type": "string", "enum": ["local", "docker", "kubectl"], "default": "docker"},
                "env_file": {"type": "string", "description": "部署环境 JSON（记入 run_environments）"},
                "set": {"type": "object", "description": "场景参数覆盖, 如 {request_rate: 5.0}"},
                "auto_retry": {"type": "integer", "default": 0},
                "timeout_s": {"type": "number", "default": 3600},
            },
            "required": ["scenario"],
        },
    },
    {
        "name": "sglbench_report",
        "description": "生成两个 run 的对比 HTML 报告（直方图/CDF/分位/逐轮曲线, 自包含）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "baseline_run": {"type": "string"},
                "candidate_run": {"type": "string"},
                "output": {"type": "string", "description": "输出 HTML 路径（默认 reports/）"},
            },
            "required": ["baseline_run", "candidate_run"],
        },
    },
    {
        "name": "sglbench_rounds",
        "description": "多轮对话逐轮指标（TTFT/cache-hit 序列）；给 candidate_run 时附 Δ 表与断崖判定。",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}, "candidate_run": {"type": "string"}},
            "required": ["run_id"],
        },
    },
    {
        "name": "sglbench_campaign_report",
        "description": "战役完整报告（index.html+SUMMARY.md+每场景明细, 对标手工 TEST-REPORT）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pair": {"type": "string", "description": "labelA=labelB（支持 * 前缀通配）"},
                "output_dir": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["pair", "output_dir"],
        },
    },
    {
        "name": "sglbench_trend",
        "description": "同一场景跨 run 指标时间线（回归跟踪; 非 ok run 标记）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scenario": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
                "metrics": {"type": "array", "items": {"type": "string"}},
                "since": {"type": "string", "description": "YYYY-MM-DD"},
                "output": {"type": "string", "description": "可选 HTML 输出路径"},
            },
            "required": ["scenario"],
        },
    },
    {
        "name": "sglbench_forensics",
        "description": "崩溃取证摘要（签名/证据目录）。collect=true 时现场补采（kubectl/ssh）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "collect": {"type": "boolean", "default": False},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "sglbench_recover",
        "description": "GPU 热复位+pod 重建（硬件级, 破坏性）。两段式：dry_run 默认 true 返回计划；"
                       "确认后显式传 dry_run=false 执行。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_pods": {"type": "boolean", "description": "自动发现异常 pod 所在节点"},
                "nodes": {"type": "array", "items": {"type": "string"}, "description": "节点名/IP"},
                "gpus": {"type": "array", "items": {"type": "integer"}, "description": "默认 [0,4]"},
                "rebuild": {"type": "boolean", "default": True},
                "dry_run": {"type": "boolean", "default": True},
            },
        },
    },
]


def _text(content: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": content}]}


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def _paths():
    return resolve_paths(None, None)


def tool_status(_: dict[str, Any]) -> dict[str, Any]:
    import urllib.request
    from .scenarios import ROUTER

    out: dict[str, Any] = {}
    try:
        with Store(_paths().db, read_only=True) as store:
            out["db_runs"] = store.count_runs()
            out["latest_run"] = store.scalar(
                "SELECT run_id FROM runs ORDER BY started_at DESC NULLS LAST LIMIT 1"
            )
    except Exception as exc:  # noqa: BLE001
        out["db_error"] = str(exc)
    try:
        with urllib.request.urlopen(ROUTER + "/v1/models", timeout=5) as resp:
            out["router_ok"] = resp.status == 200
    except Exception as exc:  # noqa: BLE001
        out["router_ok"] = False
        out["router_error"] = f"{type(exc).__name__}: {exc}"
    return _text(json.dumps(out, ensure_ascii=False, indent=2))


def tool_list_runs(a: dict[str, Any]) -> dict[str, Any]:
    with Store(_paths().db, read_only=True) as store:
        where, params = [], []
        if a.get("scenario"):
            where.append("scenario_key = ?")
            params.append(a["scenario"])
        if a.get("label"):
            where.append("label = ?")
            params.append(a["label"])
        if a.get("status"):
            where.append("status = ?")
            params.append(a["status"])
        sql = "SELECT run_id, scenario_key, label, status, started_at, sample_count FROM runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_at DESC NULLS LAST LIMIT ?"
        params.append(int(a.get("limit", 20)))
        rows = store.query(sql, params)
    return _text(json.dumps(rows, ensure_ascii=False, default=str, indent=2))


def tool_show_run(a: dict[str, Any]) -> dict[str, Any]:
    with Store(_paths().db, read_only=True) as store:
        rid = store.resolve_run(a["run_id"])
        rec = store.get_run(rid, with_samples=False)
        env = store.get_environment(rid)
    if rec is None:
        return _err(f"找不到 run: {a['run_id']}")
    return _text(json.dumps(
        {"run": rec.to_dict(include_samples=False), "environment": env},
        ensure_ascii=False, default=str, indent=2,
    ))


def tool_sql(a: dict[str, Any]) -> dict[str, Any]:
    q = (a.get("query") or "").strip()
    if not q.lower().startswith(("select", "with")):
        return _err("只允许 SELECT/WITH 只读查询")
    with Store(_paths().db, read_only=True) as store:
        rows = store.query(q)
    return _text(json.dumps(rows, ensure_ascii=False, default=str, indent=2))


def tool_compare(a: dict[str, Any]) -> dict[str, Any]:
    from .report import render_markdown
    from .stats import compare_runs
    from .scenarios import catalog

    with Store(_paths().db, read_only=True) as store:
        base = store.resolve_run(a["baseline_run"])
        cand = store.resolve_run(a["candidate_run"])
        first = store.get_run(cand, with_samples=False)
        scenario = None
        sc = catalog([])
        if first and first.scenario_key in sc:
            scenario = sc[first.scenario_key]
        report = compare_runs(store, [base, cand], baseline=base, scenario=scenario)
    return _text(render_markdown(report))


def tool_dashboard(a: dict[str, Any]) -> dict[str, Any]:
    from .dashboard import write_dashboard

    pair = (a.get("pair") or "").split("=", 1)
    if len(pair) != 2:
        return _err("pair 需形如 labelA=labelB")
    out = a.get("output") or str(_paths().reports_dir / "dashboard-mcp.html")
    with Store(_paths().db, read_only=True) as store:
        target = write_dashboard(store, out, label_pairs=[pair])
    return _text(f"dashboard 已生成: {target}")


def tool_run_scenario(a: dict[str, Any]) -> dict[str, Any]:
    from .runner import run_scenario
    from .scenarios import catalog

    paths = _paths()
    sc = catalog([])
    if a["scenario"] not in sc:
        return _err(f"未知场景: {a['scenario']}（用 sglbench_list_runs/sql 查 scenes 表或 sglbench scenarios）")
    scenario = sc[a["scenario"]]
    overrides = a.get("set") or {}
    if overrides:
        from dataclasses import replace

        clean = {k: v for k, v in overrides.items() if k in scenario.__dataclass_fields__}
        scenario = replace(scenario, **clean)
    from .cli import _wait_healthy  # 复用健康等待
    from .runner import ExecEnv

    exec_env = ExecEnv.for_mode(a.get("exec", "docker"))
    outcome = None
    attempt = 0
    max_retry = int(a.get("auto_retry", 0) or 0)
    while True:
        outcome = run_scenario(
            paths, scenario, label=a.get("label") or scenario.key,
            dump_requests=True, timeout=float(a.get("timeout_s", 3600)),
            exec_env=exec_env, env_file=a.get("env_file"),
        )
        if outcome.record.status == "ok" or attempt >= max_retry:
            break
        attempt += 1
        if not _wait_healthy(scenario):
            break
    with Store(paths.db) as store:
        store.upsert_scenario(scenario)
        store.save_run(outcome.record)
    m = outcome.record.metrics
    summary = {
        "run_id": outcome.record.run_id, "status": outcome.record.status,
        "success_rate": m.get("success_rate"), "mean_ttft_ms": m.get("mean_ttft_ms"),
        "mean_tpot_ms": m.get("mean_tpot_ms"),
        "output_token_throughput_tok_s": m.get("output_token_throughput_tok_s"),
        "attempts": attempt + 1,
    }
    return _text(json.dumps(summary, ensure_ascii=False, indent=2))


def tool_report(a: dict[str, Any]) -> dict[str, Any]:
    from .analyze import compare_runs, scenario_for_run
    from .report import build_html, write_html

    paths = _paths()
    with Store(paths.db, read_only=True) as store:
        recs = [store.get_run(a["baseline_run"], with_samples=True),
                store.get_run(a["candidate_run"], with_samples=True)]
        recs = [r for r in recs if r is not None]
        if len(recs) < 2:
            return _err("run 不存在（用 sglbench_list_runs 查）")
        scenario = scenario_for_run(recs[0])
        report = compare_runs(store, [a["baseline_run"], a["candidate_run"]],
                              baseline=a["baseline_run"], scenario=scenario)
    out = Path(a.get("output")
               or (paths.reports_dir / f"mcp-{a['baseline_run']}-vs-{a['candidate_run']}.html"))
    write_html(out, build_html(report, runs=recs))
    return _text(json.dumps({"output": str(out)}, ensure_ascii=False))


def tool_rounds(a: dict[str, Any]) -> dict[str, Any]:
    from .rounds import extract_rounds, round_delta_rows, round_verdict

    paths = _paths()
    with Store(paths.db, read_only=True) as store:
        run = store.get_run(a["run_id"], with_samples=False)
        if run is None:
            return _err(f"未找到 run: {a['run_id']}")
        payload: dict[str, Any] = {"run_id": run.run_id, "rounds": extract_rounds(run.metrics)}
        if a.get("candidate_run"):
            cand = store.get_run(a["candidate_run"], with_samples=False)
            if cand is None:
                return _err(f"未找到 run: {a['candidate_run']}")
            payload["candidate_run"] = cand.run_id
            payload["delta"] = [
                {"round": r, "base": b, "cand": c, "pct": p}
                for r, b, c, p in round_delta_rows(run.metrics, cand.metrics)
            ]
            payload["verdict"] = round_verdict(run.metrics, cand.metrics)
    return _text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def tool_campaign_report(a: dict[str, Any]) -> dict[str, Any]:
    from .campaign import write_suite

    pair = (a.get("pair") or "").split("=", 1)
    if len(pair) != 2:
        return _err("pair 需形如 labelA=labelB")
    paths = _paths()
    with Store(paths.db, read_only=True) as store:
        result = write_suite(store, a["output_dir"], label_pairs=[pair], title=a.get("title"))
    return _text(json.dumps({
        "index": str(result["index"]),
        "summary_md": str(result["summary_md"]) if result["summary_md"] else None,
        "reports": len(result["reports"]),
    }, ensure_ascii=False, indent=2))


def tool_trend(a: dict[str, Any]) -> dict[str, Any]:
    from dataclasses import asdict

    from .analyze import analyze_trend

    paths = _paths()
    with Store(paths.db, read_only=True) as store:
        series = analyze_trend(
            store, a["scenario"],
            labels=a.get("labels") or None,
            metrics=a.get("metrics") or None,
            since=a.get("since"),
        )
    payload: dict[str, Any] = {k: [asdict(p) for p in pts] for k, pts in series.items()}
    if a.get("output"):
        from .report import build_trend_html, write_html

        write_html(a["output"], build_trend_html(series, scenario_key=a["scenario"]))
        payload["html"] = a["output"]
    return _text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def tool_forensics(a: dict[str, Any]) -> dict[str, Any]:
    paths = _paths()
    collect = bool(a.get("collect"))
    with Store(paths.db, read_only=not collect) as store:
        run = store.get_run(a["run_id"], with_samples=False)
        if run is None:
            return _err(f"未找到 run: {a['run_id']}")
        if collect:
            from .forensics import collect_evidence

            report = collect_evidence(paths, run, trigger="manual")
            store.save_forensics(
                run.run_id, trigger="manual", nodes=report.nodes,
                signatures=report.signatures, evidence_dir=report.evidence_dir,
                ok=report.ok, notes="; ".join(report.notes),
            )
        row = store.get_forensics(run.run_id)
    if not row:
        return _text(json.dumps({"collected": False,
                                 "hint": "无取证记录; collect=true 现场补采"},
                                ensure_ascii=False))
    return _text(json.dumps({
        "collected": True, "run_id": row["run_id"], "trigger": row["trigger"],
        "ok": row["ok"], "collected_at": row["collected_at"],
        "evidence_dir": row["evidence_dir"], "notes": row["notes"],
        "signatures": json.loads(row["signatures_json"] or "[]"),
    }, ensure_ascii=False, indent=2, default=str))


def tool_recover(a: dict[str, Any]) -> dict[str, Any]:
    """GPU 热复位+重建。安全语义：dry_run 默认 true（先计划，确认后显式 false 执行）。"""
    from .recover import DEFAULT_GPU_INDICES, render_recovery_report, run_recovery

    dry_run = bool(a.get("dry_run", True))
    nodes = a.get("nodes") or []
    if not a.get("from_pods") and not nodes:
        return _err("至少给 from_pods=true 或 nodes=[...]")
    report = run_recovery(
        nodes=nodes or None,
        from_pods=bool(a.get("from_pods", False)),
        gpus=tuple(int(x) for x in (a.get("gpus") or DEFAULT_GPU_INDICES)),
        dry_run=dry_run,
        rebuild=bool(a.get("rebuild", True)),
    )
    payload = report.to_dict()
    payload["plan_text"] = render_recovery_report(report)
    return _text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "sglbench_status": tool_status,
    "sglbench_list_runs": tool_list_runs,
    "sglbench_show_run": tool_show_run,
    "sglbench_sql": tool_sql,
    "sglbench_compare": tool_compare,
    "sglbench_dashboard": tool_dashboard,
    "sglbench_run_scenario": tool_run_scenario,
    "sglbench_report": tool_report,
    "sglbench_rounds": tool_rounds,
    "sglbench_campaign_report": tool_campaign_report,
    "sglbench_trend": tool_trend,
    "sglbench_forensics": tool_forensics,
    "sglbench_recover": tool_recover,
}


def _handle(msg: dict[str, Any]) -> dict[str, Any] | None:
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = HANDLERS.get(name or "")
        if handler is None:
            return {"jsonrpc": "2.0", "id": mid, "result": _err(f"未知工具: {name}")}
        try:
            return {"jsonrpc": "2.0", "id": mid, "result": handler(arguments)}
        except Exception as exc:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "result": _err(
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"
            )}
    if mid is None:
        return None  # 通知类消息无需响应
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def serve() -> int:
    """stdio 主循环（行分隔 JSON-RPC）。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0
