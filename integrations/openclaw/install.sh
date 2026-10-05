#!/usr/bin/env bash
# sglbench → OpenClaw 一键安装：Skill 包 + MCP 配置（幂等, 可重复执行）。
#
# 用法:
#   install.sh --workspace <OpenClaw workspace> [--repo <sglbench 仓库>] [--check]
#
#   --check 只校验 SKILL.md 与生成器同步（CI 用, 不做安装动作）。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKSPACE=""
CHECK=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --check) CHECK=1; shift ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

PY="${PYTHON:-python3}"

if [[ "$CHECK" == "1" ]]; then
  (cd "$REPO" && "$PY" tools/gen_openclaw_skill.py --check)
  echo "✅ check 通过（未做任何安装动作）"
  exit 0
fi

if [[ -z "$WORKSPACE" ]]; then
  echo "用法: install.sh --workspace <OpenClaw workspace> [--repo <sglbench 仓库>] [--check]" >&2
  exit 2
fi

# 1) 生成/校验 SKILL.md（防漂移）
(cd "$REPO" && "$PY" tools/gen_openclaw_skill.py)

# 2) 安装 Skill 包（RULES.md + SKILL.md 一起）
mkdir -p "$WORKSPACE/skills"
rm -rf "$WORKSPACE/skills/sglbench"
cp -r "$REPO/integrations/openclaw/skills/sglbench" "$WORKSPACE/skills/sglbench"
echo "skill  → $WORKSPACE/skills/sglbench/SKILL.md"

# 3) 生成 MCP 配置（绝对路径; 13 个 tools）
cat > "$WORKSPACE/mcp-config.json" <<EOF
{
  "mcpServers": {
    "sglbench": {
      "command": "$PY",
      "args": ["-m", "sglbench", "mcp"],
      "cwd": "$REPO"
    }
  }
}
EOF
echo "mcp    → $WORKSPACE/mcp-config.json"

cat <<'TIP'

接入步骤:
  1) 将 mcp-config.json 内容并入 OpenClaw 的 MCP 配置（Agents API / 插件体系）
  2) 冒烟: cd <repo> && python3 -m sglbench status   （JSON 输出即链路通）
  3) 13 个 MCP tools 生效；sglbench_recover 为两段式（dry_run 默认 true 先给计划）
  4) campaign 执行器（长任务）走 CLI: python3 -m sglbench campaign run <spec> --side <S>
TIP
