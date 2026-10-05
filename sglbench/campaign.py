"""战役汇总报告（suite）—— 把"同一批场景, 两个部署对比"产出为完整测试报告。

对标人工维护的 docs/TEST-REPORT-*.md：受控条件表（run_environments 差异）+
场景汇总（TTFT/TPOT）+ 吞吐量完整宽表 + 多轮汇总 + 稳定性 + 结论草稿。
index.html / SUMMARY.md / 每场景明细 由 write_suite 落盘。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .analyze import DEFAULT_THRESHOLD_PCT
from .rounds import round_verdict
from .store import Store

# 受控条件表展示列（run_environments 列名 → 中文显示名）
ENV_DISPLAY_KEYS: list[tuple[str, str]] = [
    ("deployment", "部署"), ("prefill_tp", "prefill tp"), ("prefill_dp", "prefill dp"),
    ("prefill_pp", "prefill pp"), ("decode_tp", "decode tp"), ("decode_dp", "decode dp"),
    ("decode_pp", "decode pp"), ("launch_mode", "启动模式"),
    ("prefill_image", "prefill 镜像"), ("decode_image", "decode 镜像"),
    ("hicache_enabled", "HiCache"), ("hicache_io_backend", "io_backend"),
    ("hicache_ratio", "ratio"), ("hicache_mem_layout", "mem_layout"),
    ("hicache_storage_backend", "storage"), ("hicache_write_policy", "write_policy"),
    ("swa_full_tokens_ratio", "swa ratio"), ("maca_version", "MACA"),
    ("driver_version", "驱动"), ("engine_version", "引擎"),
]

# 吞吐宽表列（metric_key, 显示名, higher_better）
THROUGHPUT_KEYS: list[tuple[str, str, bool]] = [
    ("input_token_throughput_tok_s", "输入 tok/s", True),
    ("output_token_throughput_tok_s", "输出 tok/s", True),
    ("total_token_throughput_tok_s", "总 tok/s", True),
    ("peak_output_token_throughput_tok_s", "峰值输出 tok/s", True),
]


def delta_pct(off: float | None, on: float | None) -> float | None:
    """(on-off)/off*100%；任一缺失或 off=0 → None。"""
    if off is None or on is None or not off:
        return None
    return (on - off) / off * 100.0


def verdict_of(
    pct: float | None, *, higher_better: bool, threshold: float = DEFAULT_THRESHOLD_PCT
) -> str:
    """按指标方向给出 ✅/❌/➖/— 判定。"""
    if pct is None:
        return "—"
    good = pct >= threshold if higher_better else pct <= -threshold
    bad = pct <= -threshold if higher_better else pct >= threshold
    return "✅" if good else ("❌" if bad else "➖")


@dataclass
class ScenarioSummary:
    key: str
    name: str
    off_run: str
    on_run: str
    off: dict[str, float]
    on: dict[str, float]
    status_off: str = "ok"
    status_on: str = "ok"
    verdict: str = "—"
    round_verdict: dict[str, Any] | None = None


@dataclass
class SuiteData:
    title: str
    pair_titles: tuple[str, str]
    env_rows: list[tuple[str, str, str, bool]] = field(default_factory=list)
    summaries: list[ScenarioSummary] = field(default_factory=list)
    conclusions: list[str] = field(default_factory=list)


def _scenario_names(store: Store, keys: Sequence[str]) -> dict[str, str]:
    if not keys:
        return {}
    rows = store.query(
        "SELECT scenario_key, name FROM scenarios WHERE scenario_key IN ("
        + ",".join("?" * len(keys)) + ")",
        list(keys),
    )
    return {r["scenario_key"]: r["name"] or "" for r in rows}


def build_suite(
    store: Store,
    *,
    label_pairs: Sequence[Sequence[str]] | None = None,
    run_pairs: Sequence[Sequence[str]] | None = None,
    title: str | None = None,
    pair_titles: tuple[str, str] | None = None,
) -> SuiteData:
    """从 DuckDB 组装 suite 数据（配对逻辑与 dashboard 一致：label→每场景最新 ok run）。"""
    from .dashboard import resolve_pairs  # 延迟导入避免环

    pairs = resolve_pairs(store, label_pairs=label_pairs, run_pairs=run_pairs)
    pt = pair_titles
    if pt is None and label_pairs:
        pt = (label_pairs[0][0], label_pairs[0][1])
    pt = pt or ("A", "B")
    names = _scenario_names(store, [str(p["name"]) for p in pairs])

    summaries: list[ScenarioSummary] = []
    for p in sorted(pairs, key=lambda x: str(x["name"])):
        key = str(p["name"])
        off_m, on_m = p["metrics_off"], p["metrics_on"]
        t = delta_pct(off_m.get("mean_ttft_ms"), on_m.get("mean_ttft_ms"))
        rv = round_verdict(off_m, on_m)
        summaries.append(
            ScenarioSummary(
                key=key,
                name=names.get(key, ""),
                off_run=p["off_run"],
                on_run=p["on_run"],
                off=off_m,
                on=on_m,
                status_off=str(p.get("status_off", "ok")),
                status_on=str(p.get("status_on", "ok")),
                verdict=verdict_of(t, higher_better=False),
                round_verdict=(rv if rv["verdict"] != "无逐轮数据" else None),
            )
        )

    env_rows: list[tuple[str, str, str, bool]] = []
    if pairs:
        off_env = store.get_environment(pairs[0]["off_run"]) or {}
        on_env = store.get_environment(pairs[0]["on_run"]) or {}
        if off_env or on_env:
            for col, disp in ENV_DISPLAY_KEYS:
                a, b = off_env.get(col), on_env.get(col)
                fa = "—" if a is None or a == "" else str(a)
                fb = "—" if b is None or b == "" else str(b)
                env_rows.append((disp, fa, fb, fa != fb))

    suite = SuiteData(
        title=title or f"跨场景对比: {pt[0]} vs {pt[1]}",
        pair_titles=pt,
        env_rows=env_rows,
        summaries=summaries,
    )
    suite.conclusions = suite_conclusions(suite)
    return suite


def suite_conclusions(suite: SuiteData) -> list[str]:
    """结论草稿：按指标计数 + 显著项 + 稳定性 + 多轮亮点。"""
    out: list[str] = []
    metric_specs = [
        ("mean_ttft_ms", "TTFT", False),
        ("mean_tpot_ms", "TPOT", False),
        ("total_token_throughput_tok_s", "总吞吐", True),
    ]
    for key, disp, higher in metric_specs:
        good = bad = flat = 0
        notable: list[str] = []
        for s in suite.summaries:
            d = delta_pct(s.off.get(key), s.on.get(key))
            if d is None:
                continue
            v = verdict_of(d, higher_better=higher)
            if v == "✅":
                good += 1
            elif v == "❌":
                bad += 1
            else:
                flat += 1
            if abs(d) >= 10.0:
                notable.append(f"{s.key} {d:+.1f}%")
        if good or bad or flat:
            line = f"{disp}: 改善 {good} / 劣化 {bad} / 持平 {flat}（±{DEFAULT_THRESHOLD_PCT:g}% 阈值）"
            if notable:
                line += "；显著项(|Δ|≥10%): " + "、".join(notable)
            out.append(line)
    ok_bad = [s.key for s in suite.summaries if s.status_off != "ok" or s.status_on != "ok"]
    if ok_bad:
        out.append("稳定性: 以下场景存在非 ok run —— " + "、".join(ok_bad))
    elif suite.summaries:
        out.append(f"稳定性: {len(suite.summaries)} 个场景两侧全部 ok。")
    rounds_hi = [
        f"{s.key} round1 {s.round_verdict['round1_pct']:+.1f}%"
        for s in suite.summaries
        if s.round_verdict and s.round_verdict.get("round1_pct") is not None
        and s.round_verdict["round1_pct"] <= -10
    ]
    if rounds_hi:
        out.append("多轮冷热转换（round1）显著改善: " + "、".join(rounds_hi) + "。")
    return out
