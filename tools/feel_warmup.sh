#!/usr/bin/env bash
# feel_warmup.sh v2 — 体感保活器（消灭「闲置后冷态首发」短板）
#
# 实验依据（2026-10-15）：
#   · 闲置 10min → 首请求 TTFT 仍热 (0.25s)；闲置 ~27min → 冷态复现 (2.7s)
#   → 热态衰减阈值 ∈ (10, 27] 分钟；**保活间隔取 5min（300s）≈ 阈值 1/5，安全裕度 5x**
#   · router /health 探针不触碰生成路径——保活必须打 /v1/chat/completions
#   · 每轮打 2 发（router 负载均衡下覆盖多引擎落点）
# 用法: nohup bash tools/feel_warmup.sh [间隔秒=300] [base_url] [model] &
# 停止: kill $(cat /tmp/feel-warmup.pid)
INTERVAL="${1:-300}"
BASE="${2:-http://10.66.3.32:8001/v1}"
MODEL="${3:-DeepSeek-V4-Flash}"
echo $$ > /tmp/feel-warmup.pid
LOG=/tmp/feel-warmup.log
echo "[$(date '+%F %T')] warmup v2 start interval=${INTERVAL}s base=${BASE}" >> "$LOG"
ping_once() {
  t0=$(date +%s.%N)
  code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' -X POST "${BASE}/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"$1\"}],\"max_tokens\":1,\"temperature\":0}")
  t1=$(date +%s.%N)
  echo "[$(date '+%F %T')] ping$2 code=${code} t=$(echo "$t1 $t0" | awk '{printf "%.2f", $1-$2}')s" >> "$LOG"
}
while true; do
  ping_once "warm-ping-a" 1
  sleep 2
  ping_once "warm-ping-b" 2
  sleep "$INTERVAL"
done
