#!/usr/bin/env python3
"""feel_test.py — PD 集群「体感速率」测试器（chat 流式实测）

测什么（体感三要素）：
  1. TTFT   发问→首 token 的等待（"响应快不快"）
  2. ITL    流式相邻 chunk 间隔的 中位/p95/p99（"打字流畅不流畅"，p99=卡顿感）
  3. 出字速率 端到端字符/秒（"看得爽不爽"）

用法：
  python3 tools/feel_test.py                            # 默认 3P5D router 全档电池
  python3 tools/feel_test.py --base-url http://IP:8001/v1 --model DeepSeek-V4-Flash
  python3 tools/feel_test.py --quick                    # 只跑单流中篇一格

判读标尺（经验档位）：
  TTFT：<0.5s 即时 · 0.5-1s 好 · 1-3s 可接受 · >3s 明显等待
  ITL 中位：<20ms 流畅(~65字/s+) · 20-50ms 尚可 · >50ms 顿
  p99 ITL：>1s = 可感知大卡顿（>0.3s 已能被用户察觉）
输出：控制台表格 + /tmp/feel-results.json（供入库/文档引用）
"""
import argparse, json, subprocess, time


def stream_measure(base_url: str, model: str, messages, max_tokens: int, tag: str, note: str = ""):
    payload = json.dumps({"model": model, "messages": messages, "max_tokens": max_tokens, "stream": True})
    t0 = time.time()
    p = subprocess.Popen(
        ["curl", "-sN", "-X", "POST", f"{base_url}/chat/completions",
         "-H", "Content-Type: application/json", "-d", payload],
        stdout=subprocess.PIPE, text=True)
    first = None; chunks = 0; prev = None; gaps = []; out = ""
    for line in p.stdout:
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if body == "[DONE]":
            break
        try:
            d = json.loads(body)
        except Exception:
            continue
        delta = d["choices"][0].get("delta") or {}
        c = delta.get("content")
        if c:
            now = time.time()
            if first is None:
                first = now
            if prev is not None:
                gaps.append(now - prev)
            prev = now; chunks += 1; out += c
        if d["choices"][0].get("finish_reason"):
            break
    p.wait()
    ttft = (first - t0) if first else -1
    total = (prev - t0) if prev else -1
    g = sorted(gaps)
    pct = lambda q: g[min(int(len(g) * q), len(g) - 1)] * 1000 if g else 0
    gen_t = max(total - ttft, 0.001)
    chars = len(out)
    r = dict(tag=tag, note=note, ttft=round(ttft, 3), chunks=chunks,
             itl_med=round(pct(0.5), 1), itl_p95=round(pct(0.95), 1), itl_p99=round(pct(0.99), 1),
             total=round(total, 2), chars=chars, cps=round(chars / gen_t, 1),
             tok_ps_est=round(chars / 1.5 / gen_t, 1))
    print(f"[{tag}] {note}")
    print(f"   TTFT={r['ttft']:.3f}s | chunks={chunks} | ITL 中位={r['itl_med']:.0f}ms "
          f"p95={r['itl_p95']:.0f}ms p99={r['itl_p99']:.0f}ms")
    print(f"   端到端={r['total']:.2f}s | 输出={chars}字 | 出字={r['cps']:.0f}字/s (≈{r['tok_ps_est']:.0f} tok/s)")
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://10.66.3.32:8001/v1")
    ap.add_argument("--model", default="DeepSeek-V4-Flash")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default="/tmp/feel-results.json")
    args = ap.parse_args()

    R = []
    R.append(stream_measure(args.base_url, args.model,
                            [{"role": "user", "content": "用一句话介绍杭州"}], 80, "A1-短问答#0", "热身(弃)"))
    if not args.quick:
        R.append(stream_measure(args.base_url, args.model,
                                [{"role": "user", "content": "用一句话介绍杭州"}], 80, "A1-短问答", "50字级"))
    R.append(stream_measure(args.base_url, args.model,
                            [{"role": "user", "content": "写一段150字左右关于深夜城市的散文"}], 400, "A2-中篇", "150字级"))
    if not args.quick:
        R.append(stream_measure(args.base_url, args.model,
                                [{"role": "user", "content": "写一篇400字左右的短文，主题：开源软件的意义"}], 900,
                                "A3-长篇", "400字级"))
        # 多轮对话：3 轮同会话（历史累积），看轮间 TTFT 稳定性
        msgs = [{"role": "user", "content": "我们聊聊杭州这个城市"}]
        R.append(stream_measure(args.base_url, args.model, list(msgs), 200, "B-多轮R1", "冷启动轮"))
        msgs.append({"role": "assistant", "content": "好的，杭州是浙江省会，以西湖闻名……"})
        msgs.append({"role": "user", "content": "它有哪些值得一去的老街？"})
        R.append(stream_measure(args.base_url, args.model, list(msgs), 200, "B-多轮R2", "带历史轮"))
        msgs.append({"role": "assistant", "content": "杭州有河坊街、南宋御街等……"})
        msgs.append({"role": "user", "content": "帮我规划半天游的路线"})
        R.append(stream_measure(args.base_url, args.model, list(msgs), 200, "B-多轮R3", "带历史轮"))

    json.dump(R, open(args.out, "w"), ensure_ascii=False, indent=1)
    print(f"\n=== 汇总（{len([x for x in R if '#' not in x['tag']])} 有效行；明细→{args.out}）===")
    for r in R:
        if "#" in r["tag"]:
            continue
        print(f"{r['tag']}: TTFT {r['ttft']:.2f}s | ITL中位 {r['itl_med']:.0f}ms | "
              f"出字 {r['cps']:.0f}字/s | 端到端 {r['total']:.2f}s")


if __name__ == "__main__":
    main()
