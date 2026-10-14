"""MCP server 工具面测试（patch _paths 指向临时库; 零真实集群）。"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from sglbench import mcp as mcp_mod
from sglbench.models import RunRecord
from sglbench.store import Store

from .helpers import temp_home


def make_run(store, run_id, *, scenario="mini-l2-64c", label="A", metrics=None, env=None,
             status="ok", day=1):
    rec = RunRecord(run_id=run_id, scenario_key=scenario, label=label, status=status,
                    started_at=datetime(2026, 10, day, tzinfo=timezone.utc),
                    metrics=dict(metrics or {}))
    if env:
        rec.meta["deployment_env"] = env
    store.save_run(rec)
    return rec


class FakePaths:
    def __init__(self, home):
        self.home = home
        self.db = home / "bench.duckdb"
        self.runs_dir = home / "runs"
        self.reports_dir = home / "reports"


class McpToolsTest(unittest.TestCase):
    def setUp(self):
        self._th = temp_home()
        self.paths_ctx = self._th.__enter__()
        store = Store(self.paths_ctx.db)
        make_run(store, "b1", label="l2off", metrics={
            "mean_ttft_ms": 7.0, "round0_mean_ttft_ms": 606.0,
            "round1_mean_ttft_ms": 5178.0, "round3_mean_ttft_ms": 1700.0,
        })
        make_run(store, "c1", label="l2pp1", metrics={
            "mean_ttft_ms": 8.0, "round0_mean_ttft_ms": 656.0,
            "round1_mean_ttft_ms": 1410.0, "round3_mean_ttft_ms": 1720.0,
        })
        store.close()
        self.fake = FakePaths(self.paths_ctx.home)
        orig = mcp_mod._paths
        mcp_mod._paths = lambda: self.fake  # 测试期替换
        self.addCleanup(setattr, mcp_mod, "_paths", orig)
        self.addCleanup(self._th.__exit__, None, None, None)

    def call(self, name, **args):
        resp = mcp_mod.HANDLERS[name](args)
        text = resp["content"][0]["text"]
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"text": text}
        payload["_isError"] = bool(resp.get("isError"))
        return payload

    def test_tool_count_and_schemas(self):
        self.assertEqual(len(mcp_mod.TOOLS), 13)
        names = {t["name"] for t in mcp_mod.TOOLS}
        for n in ("sglbench_report", "sglbench_rounds", "sglbench_campaign_report",
                  "sglbench_trend", "sglbench_forensics", "sglbench_recover"):
            self.assertIn(n, names)

    def test_rounds_tool(self):
        payload = self.call("sglbench_rounds", run_id="b1", candidate_run="c1")
        self.assertEqual(payload["rounds"]["1"]["mean_ttft_ms"], 5178.0)
        self.assertAlmostEqual(
            payload["delta"][1]["pct"], (1410.0 - 5178.0) / 5178.0 * 100.0, places=3
        )
        self.assertEqual(payload["verdict"]["verdict"], "✅ round1 改善")

    def test_trend_tool(self):
        payload = self.call("sglbench_trend", scenario="mini-l2-64c")
        self.assertIn("mean_ttft_ms", payload)
        self.assertEqual(len(payload["mean_ttft_ms"]), 2)

    def test_campaign_report_tool(self):
        payload = self.call("sglbench_campaign_report", pair="l2off=l2pp1",
                            output_dir=str(self.fake.home / "suite"))
        self.assertIn("index", payload)
        self.assertEqual(payload["reports"], 1)

    def test_forensics_tool_no_collect(self):
        payload = self.call("sglbench_forensics", run_id="b1")
        self.assertFalse(payload.get("collected"))

    def test_report_tool(self):
        payload = self.call("sglbench_report", baseline_run="b1", candidate_run="c1",
                            output=str(self.fake.home / "r.html"))
        self.assertTrue(str(payload["output"]).endswith("r.html"))

    def test_recover_default_dry_run(self):
        payload = self.call("sglbench_recover", nodes=["mxgpu-3-29"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["nodes"][0]["host_ip"], "10.66.3.29")
        self.assertIn("计划", payload["plan_text"])

    def test_recover_no_target_err(self):
        payload = self.call("sglbench_recover")
        self.assertTrue(payload["_isError"] or "至少" in payload.get("text", ""))

    def test_recover_execute_requires_explicit(self):
        from unittest import mock

        called = {}

        def fake(**kw):
            called.update(kw)

            class R:
                ok = True
                dry_run = False
                nodes = []
                notes = []

                def to_dict(self):
                    return {"ok": True, "dry_run": False, "nodes": []}

            return R()

        with mock.patch("sglbench.recover.run_recovery", side_effect=fake):
            payload = self.call("sglbench_recover", nodes=["mxgpu-3-29"], dry_run=False)
        self.assertFalse(payload["dry_run"])
        self.assertFalse(called.get("dry_run"))  # 显式 false 透传


if __name__ == "__main__":
    unittest.main()
