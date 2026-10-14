#!/usr/bin/env python3
"""生成 OpenClaw Skill 包里的 SKILL.md（防漂移：场景目录 / 命令参考自动渲染）。

结构 = frontmatter + ``RULES.md``（人工维护段）+ 场景目录表（scenarios.py 实时）
+ 命令参考（cli.build_parser() 实时）。手改 SKILL.md 会被同步测试拦下——
改规范请改 RULES.md 后重跑本脚本。

用法::

    python3 tools/gen_openclaw_skill.py          # 重新生成并写盘
    python3 tools/gen_openclaw_skill.py --check  # 校验同步（CI/测试用, 不同步 exit 1）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SKILL_DIR = REPO / "integrations" / "openclaw" / "skills" / "sglbench"
RULES_PATH = SKILL_DIR / "RULES.md"
SKILL_PATH = SKILL_DIR / "SKILL.md"

FRONTMATTER = """---
name: sglbench
description: PD 集群压测与 HiCache 收益对比。当用户要求压测/对比/查看集群测试数据（L2/L3 收益、崩溃边界、性能回归）时使用。工具仓库 ~/sglang-benchmark-tools。生成文件——勿手改；改 RULES.md 后重跑 tools/gen_openclaw_skill.py。
---
"""


def _scenario_table() -> str:
    from sglbench.scenarios import BUILTIN_SCENARIOS

    lines = ["## 场景目录（自动生成 · 来源 scenarios.py，key 稳定勿改）", "",
             "| key | 说明 | 并发 | rate |", "|---|---|---|---|"]
    for s in BUILTIN_SCENARIOS.values():
        lines.append(f"| `{s.key}` | {s.name} | {s.max_concurrency} | {s.request_rate} |")
    return "\n".join(lines)


def _options_line(choice) -> str:
    opts = []
    for action in choice._actions:  # noqa: SLF001
        if action.dest in ("help", "command", "campaign_cmd"):
            continue
        if not action.option_strings:
            continue
        flag = "/".join(action.option_strings)
        takes_value = action.nargs != 0 and action.const is None
        opts.append(flag + (" <值>" if takes_value else ""))
    return ("参数: " + " · ".join(f"`{o}`" for o in opts)) if opts else ""


def _cli_reference() -> str:
    import argparse as _argparse

    from sglbench.cli import build_parser

    parser = build_parser()
    sub_action = next(
        a for a in parser._actions if isinstance(a, _argparse._SubParsersAction)  # noqa: SLF001
    )
    help_map = {a.dest: (a.help or "") for a in sub_action._choices_actions}  # noqa: SLF001
    lines = ["## 命令参考（自动生成 · 来源 cli.py）", ""]
    for name, choice in sub_action.choices.items():
        lines.append(f"### `sglbench {name}`")
        help_text = help_map.get(name, "")
        if help_text:
            lines.append(help_text)
        opt = _options_line(choice)
        if opt:
            lines.append(opt)
        # 一层嵌套子命令（如 campaign report / campaign run）
        nested = next(
            (a for a in choice._actions if isinstance(a, _argparse._SubParsersAction)),  # noqa: SLF001
            None,
        )
        if nested is not None:
            nested_help = {a.dest: (a.help or "") for a in nested._choices_actions}  # noqa: SLF001
            for n_name, n_choice in nested.choices.items():
                lines.append("")
                lines.append(f"#### `sglbench {name} {n_name}`")
                if nested_help.get(n_name):
                    lines.append(nested_help[n_name])
                n_opt = _options_line(n_choice)
                if n_opt:
                    lines.append(n_opt)
        lines.append("")
    return "\n".join(lines)


def generate_skill() -> str:
    rules = RULES_PATH.read_text(encoding="utf-8").strip()
    return FRONTMATTER + "\n" + rules + "\n\n" + _scenario_table() + "\n\n" + _cli_reference() + ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="校验同步（不同步 exit 1）")
    args = ap.parse_args(argv)
    text = generate_skill()
    if args.check:
        current = SKILL_PATH.read_text(encoding="utf-8") if SKILL_PATH.exists() else ""
        if current != text:
            print(f"❌ {SKILL_PATH} 与生成器不同步（重跑 tools/gen_openclaw_skill.py）",
                  file=sys.stderr)
            return 1
        print("✅ SKILL.md 同步")
        return 0
    SKILL_PATH.write_text(text, encoding="utf-8", newline="\n")
    print(f"已生成 {SKILL_PATH}（{len(text)} 字符）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
