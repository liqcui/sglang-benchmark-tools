"""崩溃取证：非 ok run 的现场采集与签名提取。

证据（best-effort，单项失败不中断）→ ``data/runs/<run_id>/forensics/``：
pods.json / events.txt / logs/<node>.tail / gpu.txt / signatures.json；
摘要落 ``run_forensics`` 表（agent 可直接 SQL 归因）。
"""
from __future__ import annotations

import re
from typing import Any

# (签名名, 正则) —— 与血泪规程一致；顺序即输出顺序
SIGNATURES: list[tuple[str, str]] = [
    ("xnack", r"xnack\(0x8\)|xnack 0x8"),
    ("memory_violation", r"memory violation\(0x4\)"),
    ("mc_error_illegal_address", r"mcErrorIllegalAddress"),
    ("mccl_wrong_magic", r"wrong[- ]magic"),
    ("connection_reset", r"Connection reset by peer"),
    ("assertion_error", r"AssertionError"),
    ("subprocess_crashed", r"Subprocess .* crashed"),
    ("not_implemented", r"NotImplementedError"),
    ("kv_cache_attr_error", r"AttributeError.*kv_cache"),
    ("address_in_use", r"address already in use"),
]


def extract_signatures(text: str | None, *, source: str = "") -> list[dict[str, Any]]:
    """逐签名取**首个**命中行（name/source/line_no/line）。"""
    hits: list[dict[str, Any]] = []
    for name, pattern in SIGNATURES:
        rx = re.compile(pattern)
        for i, line in enumerate((text or "").splitlines(), start=1):
            if rx.search(line):
                hits.append(
                    {"name": name, "source": source, "line_no": i, "line": line.strip()[:300]}
                )
                break
    return hits
