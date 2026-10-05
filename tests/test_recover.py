"""recover pod 异常发现单测（fixture JSON, 零真实 kubectl）。"""
from __future__ import annotations

import json
import unittest

from sglbench.recover import (
    build_reset_cmd,
    find_pod_issues,
    render_recovery_report,
    reset_gpu,
    run_recovery,
    verify_node,
)
from sglbench.remote import CmdResult

PODS = {
    "items": [
        {   # Ready 且无重启 → 无异常
            "metadata": {"name": "prefill-hicache-0-0", "labels": {}},
            "status": {
                "hostIP": "10.66.3.29",
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [
                    {"name": "engine", "ready": True, "restartCount": 0, "state": {"running": {}}}
                ],
            },
            "spec": {"nodeName": "mxgpu-3-29"},
        },
        {   # 非 Ready
            "metadata": {"name": "prefill-hicache-0-1"},
            "status": {
                "hostIP": "10.66.3.31",
                "conditions": [{"type": "Ready", "status": "False"}],
                "containerStatuses": [
                    {"name": "engine", "ready": False, "restartCount": 0, "state": {"running": {}}}
                ],
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
        self.assertIn("restarts=3", issues[1].reason)
        self.assertIn("CrashLoopBackOff", issues[1].reason)
        self.assertIn("exit=137", issues[1].reason)

    def test_restart_only_detected(self):
        pods = {"items": [{
            "metadata": {"name": "p1"},
            "status": {
                "hostIP": "10.66.3.33",
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [
                    {"name": "engine", "ready": True, "restartCount": 1, "state": {"running": {}}}
                ],
            },
            "spec": {"nodeName": "mxgpu-3-33"},
        }]}
        issues = find_pod_issues(pods)
        self.assertEqual(len(issues), 1)
        self.assertIn("restarts=1", issues[0].reason)

    def test_empty_and_garbage(self):
        self.assertEqual(find_pod_issues({"items": []}), [])
        self.assertEqual(find_pod_issues(None), [])


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

    def test_nonretryable_fail_no_retry(self):
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
            return CmdResult(
                True, "GPU#0 ... Available (UUID: x)\nGPU#1 ... Available\n", "", list(argv), 0
            )

        ok, text = verify_node("10.66.3.29", runner=fake)
        self.assertTrue(ok)
        self.assertIn("Available", text)

        def fake2(argv):
            return CmdResult(True, "GPU#0 ... Unavailable\n", "", list(argv), 0)

        ok, _ = verify_node("10.66.3.29", runner=fake2)
        self.assertFalse(ok)

        def fake3(argv):
            return CmdResult(False, "", "ssh: connect refused", list(argv), 255)

        ok, text = verify_node("10.66.3.29", runner=fake3)
        self.assertFalse(ok)
        self.assertIn("connect refused", text)


class TestRecoveryOrchestration(unittest.TestCase):
    def test_dry_run_plans_only(self):
        calls = []

        def kubectl(argv):
            calls.append(list(argv))
            return CmdResult(False, "", "no kubectl in dry-run test", list(argv), None)

        rep = run_recovery(
            from_pods=True, dry_run=True,
            kubectl_runner=kubectl,
            ssh_runner=lambda a: calls.append(list(a)),
        )
        self.assertTrue(rep.dry_run)
        self.assertEqual([c for c in calls if c[0] == "ssh"], [])  # 无任何 ssh 动作

    def test_dry_run_with_explicit_node(self):
        rep = run_recovery(nodes=["mxgpu-3-29"], dry_run=True)
        self.assertEqual(len(rep.nodes), 1)
        self.assertEqual(rep.nodes[0].host_ip, "10.66.3.29")
        text = render_recovery_report(rep)
        self.assertIn("[计划]", text)
        self.assertIn("10.66.3.29", text)

    def test_full_recovery_with_injected_runners(self):
        deletes = []

        def kubectl(argv):
            if "get" in argv:
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

        rep = run_recovery(
            from_pods=True, gpus=(0, 4),
            kubectl_runner=kubectl, ssh_runner=ssh, sleeper=lambda s: None,
        )
        self.assertTrue(rep.ok)
        self.assertEqual(len(rep.nodes), 1)
        node = rep.nodes[0]
        self.assertEqual([r.index for r in node.resets], [0, 4])
        self.assertTrue(all(r.ok for r in node.resets))
        self.assertTrue(node.healthy)
        self.assertIn("prefill-0-1", node.rebuilt)
        self.assertTrue(any("delete" in " ".join(d) for d in deletes))

    def test_failed_reset_marks_report_not_ok(self):
        def kubectl(argv):
            return CmdResult(False, "", "no", list(argv), None)

        def ssh(argv):
            cmd = argv[-1]
            if "mx-smi -L" in cmd:
                return CmdResult(True, "GPU#0 Available\n", "", list(argv), 0)
            return CmdResult(False, "", "boom", list(argv), 1)

        rep = run_recovery(
            nodes=["10.66.3.29"], gpus=(0,),
            kubectl_runner=kubectl, ssh_runner=ssh, sleeper=lambda s: None,
        )
        self.assertFalse(rep.ok)
        self.assertFalse(rep.nodes[0].resets[0].ok)


if __name__ == "__main__":
    unittest.main()
