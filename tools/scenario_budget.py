#!/usr/bin/env python3
"""场景负荷预算与实验有效性审计（回答"这个并发/速率合不合理"）。

为什么需要它：`request_rate` 不是跨形状可比的负荷单位（512 输入的 4 req/s 与 4096 输入的
4 req/s 差 8 倍），而"并发"是否为主控变量取决于 `rate × S` 与 `max_concurrency` 的关系。
本工具把每个场景换算成 token/s 负荷、容量占比、饱和并发，并判定实验语义。

用法::

    python3 tools/scenario_budget.py                                  # 审计全部内置场景
    python3 tools/scenario_budget.py v2-conc-64-standard              # 只看指定场景
    python3 tools/scenario_budget.py --scenario-file scenarios/standard-v2.json
    python3 tools/scenario_budget.py --family v2-capacity --scenario-file scenarios/capacity-v2.json

判定规则（单变量原则）::

    闭合环：rate × S ≥ 1.2 × conc   → 实际控制量是 max_concurrency，rate 只是"填满速率"
    开放环：rate × S ≤ 0.8 × conc   → 实际控制量是 rate（客户端并发不成为约束）
    耦合  ：两者之间                → conc 与 rate 同时影响结果，不可归因（不推荐）

标记口径::

    持续负荷 = min(rate, conc/S) × 长度（闭合环下客户端推不出 rate×S 那么多）
    超墙      = 持续负荷 > 1.1 × 容量墙（>100% 即进入排队/饱和区）—— 长上下文档改判
                KV 池并发上限（full 池 ≈3 请求/rank × rank 数，默认 12），因为 65K 形状
                的瓶颈是 KV full 池而非 4K 档标定的 in-tok/s 墙
    填不满    = 闭合环但 rate×S < 1.5×conc（槽位空转，档位无效）
    并发先绑定 = 开放环但 conc < 1.5×rate×S（退化成闭合环，rate 不再是控制量）

容量与估计（现场实测校准，可用参数覆盖）::

    --in-wall   输入侧容量墙 tok/s（默认 8200，实测 ≈8.2K in-tok/s）
    --out-wall  输出侧容量墙 tok/s（默认 2900，pernode c48 实测；旧 PP1 ≈2000）
    --longctx-max-conc  长上下文并发上限（默认 12 ≈ 3 请求/rank × 4 rank）
    S_est = TTFT_est + L_out × TPOT_est，TTFT/TPOT 估计见 --ttft-ms/--tpot-ms 与内置分档表。

注意：本工具做的是**量级判定与实验语义分类**（这些结论对 S 估计不敏感），
精确容量点/最优并发仍必须实测（开放环扫 rate + 闭合环扫 conc）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sglbench.scenarios import catalog  # noqa: E402


def _safe_stdout() -> None:
    """GBK 控制台不至于因中文/箭头崩掉。"""
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass


# 分档估计（现场实测：单流/并发 p50 量级；仅用于判定实验语义，不用于出报告数字）
def latency_estimate(in_len: int, ttft_ms: float | None, tpot_ms: float | None) -> tuple[float, float]:
    if ttft_ms is not None and tpot_ms is not None:
        return ttft_ms, tpot_ms
    if in_len >= 32768:
        est = (13000.0, 9.0)      # 65K 长上下文：pernode 单流 TTFT ≈13s
    elif in_len >= 8000:
        est = (1500.0, 8.0)       # 8K prefill 密集
    elif in_len >= 2048:
        est = (1100.0, 14.0)      # 4K 通用（pernode c48：TTFT 1.05s / TPOT 14.8ms）
    else:
        est = (900.0, 8.0)        # 短请求/交互
    if ttft_ms is not None:
        est = (ttft_ms, est[1])
    if tpot_ms is not None:
        est = (est[0], tpot_ms)
    return est


def analyse(sc, *, in_wall: float, out_wall: float, longctx_max_conc: float = 12.0,
            ttft_ms: float | None = None, tpot_ms: float | None = None) -> dict:
    li, lo = sc.random_input, sc.random_output
    ttft, tpot = latency_estimate(li, ttft_ms, tpot_ms)
    s_est = ttft / 1000.0 + lo * tpot / 1000.0
    conc = sc.max_concurrency
    rate = sc.request_rate

    # 实验语义：只看 rate × S 与并发上限的几何关系（与容量墙无关）
    n_offered = rate * s_est
    if n_offered >= 1.2 * conc:
        mode = "闭合环(conc主控)"
    elif n_offered <= 0.8 * conc:
        mode = "开放环(rate主控)"
    else:
        mode = "耦合(不推荐)"

    # 持续到达率：闭合环下客户端推不出 rate×S 那么多（被 conc 限制）
    effective_rate = min(rate, conc / s_est) if s_est else rate
    in_demand = li * effective_rate
    out_demand = lo * effective_rate

    longctx = li >= 32768
    flags: list[str] = []
    open_loop = mode.startswith("开放环")
    if longctx:
        if conc > longctx_max_conc:
            flags.append(f"并发超 KV 池上限(≈{longctx_max_conc:.0f})×{conc / longctx_max_conc:.1f}")
    elif open_loop:
        if in_demand > 1.1 * in_wall:
            flags.append(f"输入进排队区×{in_demand / in_wall:.2f}")
        if out_demand > 1.1 * out_wall:
            flags.append(f"输出进排队区×{out_demand / out_wall:.2f}")
    else:
        # 闭合环是**故意**压到容量墙的，只有远超（>2×）才值得提醒
        if in_demand > 2.0 * in_wall or out_demand > 2.0 * out_wall:
            flags.append(f"闭环下已顶到容量墙(估计×{max(in_demand / in_wall, out_demand / out_wall):.1f})")
    if mode == "耦合(不推荐)":
        flags.append("conc 与 rate 同时影响结果(不可归因)")
    if open_loop and conc < 1.1 * n_offered:
        flags.append(f"并发余量不足(<1.1×rate×S)")
    if mode.startswith("闭合环") and conc > 0 and (rate * s_est) / conc < 1.25:
        flags.append(f"填满余量偏低({(rate * s_est) / conc:.2f})")
    if sc.dataset_name != "random":
        flags.append(f"长度由数据集决定({sc.dataset_name})，另按实测长度归一")

    margin = (rate * s_est) / conc if conc else float("inf")
    return {
        "key": sc.key, "conc": conc, "rate": rate, "li": li, "lo": lo,
        "n": sc.num_prompts, "s_est": s_est, "eff_rate": effective_rate,
        "n_off": n_offered, "in_demand": in_demand, "out_demand": out_demand,
        "mode": mode, "flags": flags, "family": sc.family, "tags": sc.tags,
        "longctx": longctx, "margin": margin, "open_loop": open_loop,
    }


def main(argv: list[str] | None = None) -> int:
    _safe_stdout()
    ap = argparse.ArgumentParser(description="场景负荷预算与实验有效性审计")
    ap.add_argument("keys", nargs="*", help="只审计这些场景 key（缺省=全部）")
    ap.add_argument("--scenario-file", action="append", default=[], help="追加/覆盖场景定义")
    ap.add_argument("--family", help="只审计该 family")
    ap.add_argument("--in-wall", type=float, default=8200.0, help="输入侧容量墙 tok/s")
    ap.add_argument("--out-wall", type=float, default=2900.0, help="输出侧容量墙 tok/s")
    ap.add_argument("--longctx-max-conc", type=float, default=12.0,
                    help="长上下文形状的并发上限（KV full 池 ≈3/rank × 4 rank）")
    ap.add_argument("--ttft-ms", type=float, help="统一覆盖 TTFT 估计（ms）")
    ap.add_argument("--tpot-ms", type=float, help="统一覆盖 TPOT 估计（ms）")
    args = ap.parse_args(argv)

    cat = catalog(args.scenario_file)
    keys = args.keys or sorted(cat)
    rows = []
    for key in keys:
        sc = cat.get(key)
        if sc is None:
            print(f"!! 未知场景: {key}")
            continue
        if args.family and sc.family != args.family:
            continue
        rows.append(analyse(sc, in_wall=args.in_wall, out_wall=args.out_wall,
                            longctx_max_conc=args.longctx_max_conc,
                            ttft_ms=args.ttft_ms, tpot_ms=args.tpot_ms))

    print(f"容量墙: 输入 {args.in_wall:.0f} tok/s / 输出 {args.out_wall:.0f} tok/s；"
          f"长上下文并发上限 ≈{args.longctx_max_conc:.0f}"
          f"（墙与 S 估计只做量级判定，精确容量点必须实测）\n")
    head = (f"{'scenario':32s} {'in/out':>11s} {'conc':>5s} {'rate':>5s} {'n':>4s} {'S估':>6s} "
            f"{'rate×S':>7s} {'余量':>11s} {'持续in':>7s} {'%墙':>5s} {'持续out':>8s} {'%墙':>5s} "
            f"{'语义':16s} 标记")
    print(head)
    print("-" * len(head))
    for r in rows:
        inwall = "  n/a" if r["longctx"] else f"{100 * r['in_demand'] / args.in_wall:4.0f}%"
        outwall = "  n/a" if r["longctx"] else f"{100 * r['out_demand'] / args.out_wall:4.0f}%"
        margin = (f"余×{r['margin']:.2f}" if r["open_loop"] else f"满×{r['margin']:.2f}")
        print(f"{r['key'][:32]:32s} {str(r['li']) + '/' + str(r['lo']):>11s} {r['conc']:5d} "
              f"{r['rate']:5.1f} {r['n']:4d} {r['s_est']:6.1f} {r['n_off']:7.1f} {margin:>11s} "
              f"{r['in_demand']:7.0f} {inwall:>5s} {r['out_demand']:8.0f} {outwall:>5s} "
              f"{r['mode']:16s} {'；'.join(r['flags']) or 'OK'}")

    by_mode: dict[str, int] = {}
    for r in rows:
        by_mode[r["mode"]] = by_mode.get(r["mode"], 0) + 1
    print("\n语义分布:", "；".join(f"{k} {v}" for k, v in sorted(by_mode.items())))
    keys = ("排队区", "KV", "不可归因", "余量不足")
    bad = [r for r in rows if any(k in f for f in r["flags"] for k in keys)]
    print(f"需要关注（进排队区/超 KV 池/不可归因/余量不足）: {len(bad)}/{len(rows)}")
    for r in bad:
        print(f"  - {r['key']}: {'；'.join(r['flags'])}")
    print("\n提示: 一次只动一个变量 —— 闭合环扫 conc（rate 填满），开放环扫 rate（conc 放开）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
