"""forensics 签名提取与现场采集单测。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sglbench.forensics import collect_evidence, extract_signatures
from sglbench.remote import CmdResult

LOG = """\
[2026-10-05 08:01:02] server started
[2026-10-05 08:14:11] [MXC][E]memory violation(0x4) at 0x7f... (trapping pipeID 3)
[2026-10-05 08:14:11] Gloo: Connection reset by peer
[2026-10-05 08:14:12] [MXC][E]memory violation(0x4) again
[2026-10-05 08:14:13] Subprocess scheduler_0 crashed exit -6
"""


class TestSignatures(unittest.TestCase):
    def test_extract(self):
        hits = extract_signatures(LOG, source="mxgpu-3-31")
        names = [h["name"] for h in hits]
        self.assertIn("memory_violation", names)
        self.assertIn("connection_reset", names)
        self.assertIn("subprocess_crashed", names)
        mv = next(h for h in hits if h["name"] == "memory_violation")
        self.assertEqual(mv["line_no"], 2)          # 首个命中行（1-based）
        self.assertEqual(mv["source"], "mxgpu-3-31")
        self.assertIn("memory violation(0x4)", mv["line"])
        self.assertEqual(names.count("memory_violation"), 1)  # 只记首个

    def test_xnack_and_mccl(self):
        hits = extract_signatures(
            "xnack(0x8) atu\nMCCL wrong-magic from 10.0.0.1\n", source="n1"
        )
        names = [h["name"] for h in hits]
        self.assertIn("xnack", names)
        self.assertIn("mccl_wrong_magic", names)

    def test_empty(self):
        self.assertEqual(extract_signatures(""), [])
        self.assertEqual(extract_signatures(None), [])


class _FakePaths:
    def __init__(self, home: Path):
        self.home = home


class _FakeRun:
    def __init__(self, rid="run-1"):
        self.run_id = rid
        self.meta = {}


class TestCollectEvidence(unittest.TestCase):
    def _runners(self, fail_gpu=False):
        pods = {"items": [
            {"metadata": {"name": "prefill-0-1"}, "spec": {"nodeName": "mxgpu-3-31"},
             "status": {"hostIP": "10.66.3.31",
                        "conditions": [{"type": "Ready", "status": "False"}],
                        "containerStatuses": []}},
        ]}

        def kubectl(argv):
            cmd = " ".join(argv)
            if "get pods" in cmd:
                return CmdResult(True, json.dumps(pods), "", list(argv), 0)
            if "get events" in cmd:
                return CmdResult(True, "10:00 CrashLoopBackOff prefill-0-1\n", "", list(argv), 0)
            return CmdResult(False, "", "unexpected", list(argv), 1)

        def ssh(argv):
            cmd = argv[-1]
            if "mx-smi -L" in cmd:
                if fail_gpu:
                    raise RuntimeError("ssh boom")
                return CmdResult(True, "GPU#0 Available\n", "", list(argv), 0)
            return CmdResult(True, "== rank1.log\n[E]xnack(0x8) boom\n", "", list(argv), 0)

        return kubectl, ssh

    def test_collect_full(self):
        with tempfile.TemporaryDirectory() as home:
            kubectl, ssh = self._runners()
            rep = collect_evidence(
                _FakePaths(Path(home)), _FakeRun(), kubectl_runner=kubectl, ssh_runner=ssh
            )
            ev = Path(rep.evidence_dir)
            self.assertTrue(rep.ok)
            for f in ("pods.json", "events.txt", "gpu.txt", "signatures.json"):
                self.assertTrue((ev / f).exists(), f)
            self.assertTrue((ev / "logs" / "mxgpu-3-31.tail").exists())
            self.assertTrue(any(s["name"] == "xnack" for s in rep.signatures))
            self.assertIn("完整", rep.summary_line())

    def test_collect_partial_tolerates_ssh_failure(self):
        with tempfile.TemporaryDirectory() as home:
            kubectl, ssh = self._runners(fail_gpu=True)
            rep = collect_evidence(
                _FakePaths(Path(home)), _FakeRun(), kubectl_runner=kubectl, ssh_runner=ssh
            )
            self.assertFalse(rep.ok)                       # 有失败项
            self.assertTrue(rep.notes)                     # 记录了原因
            self.assertTrue(Path(rep.evidence_dir).exists())  # 其余证据仍在
            self.assertTrue((Path(rep.evidence_dir) / "pods.json").exists())
            self.assertIn("partial", rep.summary_line())

    def test_collect_kubectl_down_still_writes_dir(self):
        def kubectl(argv):
            return CmdResult(False, "", "no cluster", list(argv), 1)

        def ssh(argv):
            return CmdResult(False, "", "no ssh", list(argv), 1)

        with tempfile.TemporaryDirectory() as home:
            rep = collect_evidence(
                _FakePaths(Path(home)), _FakeRun(), kubectl_runner=kubectl, ssh_runner=ssh
            )
            self.assertFalse(rep.ok)
            self.assertTrue(Path(rep.evidence_dir).exists())
            self.assertTrue((Path(rep.evidence_dir) / "signatures.json").exists())


if __name__ == "__main__":
    unittest.main()
