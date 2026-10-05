"""sglbench MCP server —— 把压测工具暴露为 MCP tools（stdio, 零依赖轻量实现）。

任何 MCP 兼容 agent（Claude Code / OpenClaw 类框架等）可经此调用：
    status / list_runs / show_run / sql / compare / dashboard / run_scenario

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


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "sglbench_status": tool_status,
    "sglbench_list_runs": tool_list_runs,
    "sglbench_show_run": tool_show_run,
    "sglbench_sql": tool_sql,
    "sglbench_compare": tool_compare,
    "sglbench_dashboard": tool_dashboard,
    "sglbench_run_scenario": tool_run_scenario,
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
