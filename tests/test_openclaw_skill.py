"""OpenClaw SKILL.md 生成同步测试（防漂移）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

from gen_openclaw_skill import SKILL_PATH, generate_skill  # noqa: E402


class SkillSyncTest(unittest.TestCase):
    def test_generated_matches_committed(self):
        self.assertTrue(SKILL_PATH.exists(), f"缺失: {SKILL_PATH}（跑 tools/gen_openclaw_skill.py）")
        self.assertEqual(
            generate_skill(), SKILL_PATH.read_text(encoding="utf-8"),
            "SKILL.md 与生成器不同步——运行 python3 tools/gen_openclaw_skill.py 后提交",
        )

    def test_contains_autogen_sections(self):
        text = generate_skill()
        self.assertIn("## 场景目录", text)
        self.assertIn("## 命令参考", text)
        self.assertIn("mini-l2-64c", text)      # 场景表来自 scenarios 目录
        self.assertIn("campaign", text)          # 命令参考来自 CLI parser
        self.assertIn("--gpu-reset", text)
        self.assertIn("RULES.md", text)          # 提示改 RULES.md 重新生成


if __name__ == "__main__":
    unittest.main()
