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

        def bad(argv):
            return CmdResult(ok=True, stdout="not-json", stderr="", argv=list(argv), returncode=0)

        data, res = kubectl_json(["get", "pods"], runner=bad)
        self.assertIsNone(data)
        self.assertTrue(res.ok)  # 命令本身成功, 只是解析失败

    def test_runner_exception_becomes_failure(self):
        def boom(argv):
            raise RuntimeError("net down")

        res = ssh_run("10.66.3.29", "true", runner=boom)
        self.assertFalse(res.ok)
        self.assertIn("net down", res.stderr)


if __name__ == "__main__":
    unittest.main()
