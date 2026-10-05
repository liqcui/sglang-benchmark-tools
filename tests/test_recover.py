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


if __name__ == "__main__":
    unittest.main()
