"""forensics 签名提取与现场采集单测。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from sglbench.forensics import extract_signatures
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


if __name__ == "__main__":
    unittest.main()
