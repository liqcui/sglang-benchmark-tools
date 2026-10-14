#!/usr/bin/env python3
"""落库 run 的"长度口径"审计：查出输出长度不受控（EOS 早停）导致的不可比。

为什么需要它：`--random-output-len` 只是**上限**，生成会被 EOS 提前终止；随机 token 提示词
下的 EOS 触发率随 run 大幅漂移。实测同一场景（conc-64-deepseek，标称 4096/2048）跨 run 的
实际输出长度在 **0.11–1.00 × 标称** 之间波动 —— 在 rate 受限（下游未饱和）时，输出吞吐
≈ rate × 实际输出长度，于是 tok/s 会随长度同比例漂移，**跨 run 直接比较 tok/s 是错的**。

用法::

    python3 tools/audit_run_lengths.py                        # 全量审计（只列异常）
    python3 tools/audit_run_lengths.py --all                  # 逐 run 全列
    python3 tools/audit_run_lengths.py --scenario conc-64-deepseek
    python3 tools/audit_run_lengths.py --home data --tol 0.02

判定::

    in/nom   输入长度是否符合标称（本集群 --random-range-ratio 1.0 实测=定长）
    out/nom  实际输出/标称；< 1-tol 即长度未受控（EOS 早停或请求被中断）
    n/nreq   样本数/请求数；不足说明 run 中断（崩溃/超时）
    等效满长  仅在 rate 受限时成立：out_tok_s × (标称/实际)
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics as st
import sys
from pathlib import Path

OK_STATUS = (None, "ok", "success", "succeeded", "200", "true")


def _safe_stdout() -> None:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass


def read_lengths(path: str) -> tuple[list[int], list[int]]:
    ins: list[int] = []
    outs: list[int] = []
    if not os.path.exists(path):
        return ins, outs
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if row.get("status") not in OK_STATUS:
                continue
            if row.get("input_len"):
                ins.append(int(row["input_len"]))
            if row.get("output_len"):
                outs.append(int(row["output_len"]))
    return ins, outs


def main(argv: list[str] | None = None) -> int:
    _safe_stdout()
    ap = argparse.ArgumentParser(description="落库 run 的长度口径审计")
    ap.add_argument("--home", default="data", help="sglbench home（默认 data）")
    ap.add_argument("--scenario", help="只看该场景 key（前缀匹配）")
    ap.add_argument("--tol", type=float, default=0.02, help="长度偏差容忍度（默认 2%）")
    ap.add_argument("--all", action="store_true", help="逐 run 全列（默认只列异常）")
    args = ap.parse_args(argv)

    recs = []
    for rj in sorted(glob.glob(os.path.join(args.home, "runs", "*", "run.json"))):
        d = os.path.dirname(rj)
        try:
            run = json.load(open(rj, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        key = run.get("scenario_key") or "?"
        if args.scenario and not key.startswith(args.scenario):
            continue
        p = run.get("params") or {}
        ins, outs = read_lengths(os.path.join(d, "requests.jsonl"))
        nom_in = p.get("random_input") or 0
        nom_out = p.get("random_output") or 0
        m = run.get("metrics") or {}
        rec = {
            "dir": os.path.basename(d), "key": key,
            "date": (run.get("started_at") or "")[:10],
            "status": run.get("status"),
            "nom_in": nom_in, "nom_out": nom_out,
            "n": len(outs), "nreq": p.get("num_prompts") or 0,
            "in_mean": st.mean(ins) if ins else 0,
            "out_mean": st.mean(outs) if outs else 0,
            "out_min": min(outs) if outs else 0,
            "out_med": st.median(outs) if outs else 0,
            "out_max": max(outs) if outs else 0,
            "out_uniq": len(set(outs)),
            "out_tok_s": m.get("output_token_throughput_tok_s"),
            "dataset": p.get("dataset_name"),
        }
        rec["in_ratio"] = (rec["in_mean"] / nom_in) if nom_in else None
        rec["out_ratio"] = (rec["out_mean"] / nom_out) if nom_out else None
        recs.append(rec)

    def is_bad(r: dict) -> bool:
        if r["dataset"] not in (None, "random") or not r["nom_out"] or not r["n"]:
            return False
        if r["out_ratio"] is not None and r["out_ratio"] < 1 - args.tol:
            return True
        if r["in_ratio"] is not None and abs(r["in_ratio"] - 1) > args.tol:
            return True
        return bool(r["nreq"]) and r["n"] < r["nreq"]

    show = recs if args.all else [r for r in recs if is_bad(r)]
    print(f"审计 {len(recs)} 个 run；异常 {sum(1 for r in recs if is_bad(r))} 个"
          f"（容忍度 {args.tol:.0%}）\n")
    head = (f"{'run':50s} {'nom in/out':>11s} {'n/nreq':>9s} {'in/nom':>7s} {'out/nom':>7s} "
            f"{'out min/中位/max':>18s} {'out tok/s':>9s} {'等效满长':>8s}")
    if show:
        print(head)
        print("-" * len(head))
    for r in show:
        norm = (r["out_tok_s"] / r["out_ratio"]) if (r["out_tok_s"] and r["out_ratio"]) else None
        flag = ""
        if r["nom_out"] and r["out_ratio"] is not None and r["out_ratio"] < 1 - args.tol:
            flag = "  <== 长度未受控"
        if r["nreq"] and r["n"] < r["nreq"]:
            flag += "  <== 样本不足(中断)"
        print(f"{r['dir'][:50]:50s} {str(r['nom_in']) + '/' + str(r['nom_out']):>11s} "
              f"{str(r['n']) + '/' + str(r['nreq']):>9s} "
              f"{(r['in_ratio'] if r['in_ratio'] else 0):7.3f} "
              f"{(r['out_ratio'] if r['out_ratio'] else 0):7.3f} "
              f"{str(r['out_min']) + '/' + str(int(r['out_med'])) + '/' + str(r['out_max']):>18s} "
              f"{(r['out_tok_s'] or 0):9.1f} {(norm or 0):8.1f}{flag}")

    print("\n按场景汇总（只看长度异常 run 的占比与漂移区间）:")
    agg: dict[str, list[float]] = {}
    for r in recs:
        if r["dataset"] not in (None, "random") or not r["out_ratio"]:
            continue
        agg.setdefault(r["key"], []).append(r["out_ratio"])
    for key, ratios in sorted(agg.items(), key=lambda kv: -len(kv[1])):
        drift = [x for x in ratios if x < 1 - args.tol]
        print(f"  {key[:34]:34s} runs={len(ratios):3d}  漂移={len(drift):3d}  "
              f"out/nom {min(ratios):.2f}–{max(ratios):.2f}")
    print("\n结论：out/nom 明显 <1 的 run，其 tok/s 与满长 run 不可直接比较；"
          "加 --ignore-eos 锁死输出长度，或按『等效满长』列归一后再比。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
