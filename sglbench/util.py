"""通用工具：时间、路径、JSON/JSONL IO、数值格式化、run id。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

# --------------------------------------------------------------------------- #
# 时间
# --------------------------------------------------------------------------- #


def utcnow() -> datetime:
    """当前 UTC 时间（带时区）。全库统一 UTC，展示层再本地化。"""
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    """datetime -> ISO8601 字符串（秒精度、UTC、带 Z）。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def stamp_compact(dt: datetime | None = None) -> str:
    """``20260114T083000Z`` —— 可直接排序的文件名时间戳。"""
    dt = dt or utcnow()
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def humanize_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds // 60:.0f}m{seconds % 60:02.0f}s"
    return f"{seconds // 3600:.0f}h{(seconds % 3600) // 60:02.0f}m"


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Paths:
    """工具的家目录布局。

    ``<home>/bench.duckdb``  分析库
    ``<home>/runs/<run_id>/`` 不可变 run 工件
    """

    home: Path

    @property
    def db(self) -> Path:
        return self.home / "bench.duckdb"

    @property
    def runs_dir(self) -> Path:
        return self.home / "runs"

    @property
    def reports_dir(self) -> Path:
        return self.home / "reports"

    @property
    def exports_dir(self) -> Path:
        return self.home / "exports"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def ensure(self) -> "Paths":
        for p in (self.home, self.runs_dir, self.reports_dir, self.exports_dir):
            p.mkdir(parents=True, exist_ok=True)
        return self


def resolve_paths(home: str | os.PathLike[str] | None = None, db: str | os.PathLike[str] | None = None) -> Paths:
    """解析家目录：显式参数 > ``SGLBENCH_HOME`` > ``./data``。

    ``db`` 只覆盖数据库文件位置，run 工件仍在家目录下。
    """
    if home:
        base = Path(home).expanduser().resolve()
    elif os.environ.get("SGLBENCH_HOME"):
        base = Path(os.environ["SGLBENCH_HOME"]).expanduser().resolve()
    else:
        base = (Path.cwd() / "data").resolve()
    if db:
        dbp = Path(db).expanduser().resolve()
        return Paths(home=dbp.parent).ensure()
    return Paths(home=base).ensure()


def ensure_dir(path: str | os.PathLike[str]) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def slugify_filename(text: str, maxlen: int = 80) -> str:
    """把任意标签变成安全的文件名片段（保留中文）。"""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "-", str(text)).strip(" .-")
    cleaned = re.sub(r"\s+", "_", cleaned)
    return (cleaned or "unnamed")[:maxlen]


def atomic_write_text(path: str | os.PathLike[str], text: str, encoding: str = "utf-8") -> Path:
    """原子写文本：先写同目录临时文件再 rename，避免半截文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp-", suffix=p.suffix)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return p


# --------------------------------------------------------------------------- #
# JSON / JSONL
# --------------------------------------------------------------------------- #


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return iso(obj)
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "tolist"):  # numpy 标量/数组
        return obj.tolist()
    if hasattr(obj, "item"):
        return obj.item()
    return str(obj)


def dumps(obj: Any, indent: int | None = 2) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=indent, default=_json_default, sort_keys=False)


def write_json(path: str | os.PathLike[str], obj: Any, indent: int | None = 2) -> Path:
    return atomic_write_text(path, dumps(obj, indent=indent) + "\n")


def read_json(path: str | os.PathLike[str], default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return default


def iter_jsonl(path: str | os.PathLike[str]) -> Iterator[dict]:
    """逐行读 JSONL，坏行跳过并计数交给调用方。"""
    p = Path(path)
    if not p.exists():
        return
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def write_jsonl(path: str | os.PathLike[str], rows: Iterable[Mapping[str, Any]]) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp-", suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(dict(row), ensure_ascii=False, default=_json_default) + "\n")
                n += 1
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return n


def read_jsonl(path: str | os.PathLike[str]) -> list[dict]:
    return list(iter_jsonl(path))


# --------------------------------------------------------------------------- #
# 杂项
# --------------------------------------------------------------------------- #


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def short_hash(*parts: Any, n: int = 4) -> str:
    h = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8", errors="replace"))
    return h.hexdigest()[:n]


def new_run_id(scenario_key: str, when: datetime | None = None, salt: str | None = None) -> str:
    """``<scenario>-<UTC时间戳>-<4位随机>``：可排序、可读、不撞车。"""
    return f"{slugify_filename(scenario_key, 40)}-{stamp_compact(when)}-{salt or secrets.token_hex(2)}"


def copy_file(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> Path:
    d = Path(dst)
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(src), str(d))
    return d


def format_metric(value: float | None, unit: str = "") -> str:
    """按量纲做人类可读格式化，表格/图表共用。"""
    if value is None:
        return "-"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v != v:  # NaN
        return "-"
    if unit == "ms":
        return f"{v / 1000:.2f} s" if abs(v) >= 1000 else f"{v:.2f} ms"
    if unit == "s":
        return f"{v:.2f} s"
    if unit == "%":
        return f"{v * 100:.2f}%"
    if unit == "tok/s":
        return f"{v:,.1f}"
    if unit == "req/s":
        return f"{v:.2f}"
    if unit in ("count", "tokens"):
        return f"{v:,.0f}"
    if unit == "x":
        return f"{v:.3f}x"
    if float(v).is_integer():
        return f"{v:,.0f}"
    return f"{v:,.4g}"


def format_delta(value: float | None, unit: str = "", pct: float | None = None) -> str:
    if value is None:
        return "-"
    sign = "+" if value > 0 else ""
    body = f"{sign}{format_metric(value, unit)}"
    if pct is not None:
        body += f" ({sign}{pct:.2f}%)"
    return body


def parse_kv(pairs: Sequence[str] | None) -> dict[str, str]:
    """``--set a=1 b=2`` -> ``{"a": "1", "b": "2"}``。"""
    out: dict[str, str] = {}
    for item in pairs or []:
        if "=" not in item:
            raise ValueError(f"期望 key=value 形式，收到: {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def parse_duration(text: str) -> timedelta:
    """``30s`` / ``5m`` / ``2h`` / ``1d`` -> timedelta。"""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(text))
    if not m:
        raise ValueError(f"无法解析的时长: {text!r}")
    n = float(m.group(1))
    unit = m.group(2) or "s"
    return timedelta(seconds=n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])


def truncate(text: str, width: int, tail: str = "…") -> str:
    text = "" if text is None else str(text)
    if len(text) <= width:
        return text
    return text[: max(0, width - len(tail))] + tail
