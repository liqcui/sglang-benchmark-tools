"""DuckDB 持久化层。

为什么选 DuckDB 而不是 SQLite：压测分析是典型的"宽表 + 聚合 + 分布"负载，DuckDB
是列存 + 向量化执行，单文件、零服务、可被 pandas/Arrow 直接消费，还能直接
``read_parquet``/``read_json_auto`` 做外部数据联查。并发写不是需求（一次压测只有
一个写入者），所以放弃 SQLite 的写并发换取分析性能。

表结构
------
``schema_meta``      schema 版本与工具版本
``scenarios``        场景定义快照（key 唯一）
``runs``             一次 test run 的元数据与参数
``metrics``          长表：``(run_id, metric_key) -> value``
``request_samples``  每请求明细（分布分析的原始数据）
``samples_meta``      每请求明细的行数（用于判断是否真的落盘了）
``preflight_checks`` 连通性预检记录
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import __version__
from .models import Issue, RunRecord, RunSummary, Sample, Scenario
from .util import iso, parse_iso, utcnow

SCHEMA_VERSION = 1

_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   VARCHAR PRIMARY KEY,
        value VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scenarios (
        scenario_key  VARCHAR PRIMARY KEY,
        name          VARCHAR,
        family        VARCHAR,
        stage         VARCHAR,
        description   VARCHAR,
        identity      VARCHAR,
        spec_json     VARCHAR,
        updated_at    TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id        VARCHAR PRIMARY KEY,
        scenario_key  VARCHAR,
        label         VARCHAR,
        status        VARCHAR,
        source        VARCHAR,
        started_at    TIMESTAMP,
        finished_at   TIMESTAMP,
        wall_s        DOUBLE,
        command       VARCHAR,
        exit_code     INTEGER,
        host          VARCHAR,
        operator      VARCHAR,
        notes         VARCHAR,
        tags_json     VARCHAR,
        raw_dir       VARCHAR,
        params_json   VARCHAR,
        env_json      VARCHAR,
        artifacts_json VARCHAR,
        meta_json     VARCHAR,
        issues_json   VARCHAR,
        content_sha256 VARCHAR,
        sample_count  INTEGER,
        issue_count   INTEGER,
        created_at    TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metrics (
        run_id      VARCHAR,
        metric_key  VARCHAR,
        value       DOUBLE,
        PRIMARY KEY (run_id, metric_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS request_samples (
        run_id      VARCHAR,
        seq         INTEGER,
        request_id  VARCHAR,
        input_len   INTEGER,
        output_len  INTEGER,
        ttft_ms     DOUBLE,
        tpot_ms     DOUBLE,
        itl_ms      DOUBLE,
        e2e_ms      DOUBLE,
        status      VARCHAR,
        error       VARCHAR,
        PRIMARY KEY (run_id, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS preflight_checks (
        check_id     VARCHAR PRIMARY KEY,
        checked_at   TIMESTAMP,
        scenario_key VARCHAR,
        target       VARCHAR,
        url          VARCHAR,
        model        VARCHAR,
        ok           BOOLEAN,
        http_status  INTEGER,
        ttft_ms      DOUBLE,
        total_ms     DOUBLE,
        output_chars INTEGER,
        detail       VARCHAR
    )
    """,
    """
    CREATE OR REPLACE VIEW v_run_metrics AS
    SELECT r.run_id, r.scenario_key, r.label, r.status, r.started_at,
           m.metric_key, m.value
    FROM runs r
    JOIN metrics m USING (run_id)
    """,
)


def _to_ts(dt: datetime | None) -> datetime | None:
    """入库统一存 UTC 的 naive TIMESTAMP。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _from_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return parse_iso(str(value))


@dataclass
class PreflightCheck:
    check_id: str
    checked_at: datetime
    target: str
    url: str
    ok: bool
    model: str = ""
    scenario_key: str = ""
    http_status: int | None = None
    ttft_ms: float | None = None
    total_ms: float | None = None
    output_chars: int | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "checked_at": iso(self.checked_at),
            "scenario_key": self.scenario_key,
            "target": self.target,
            "url": self.url,
            "model": self.model,
            "ok": self.ok,
            "http_status": self.http_status,
            "ttft_ms": self.ttft_ms,
            "total_ms": self.total_ms,
            "output_chars": self.output_chars,
            "detail": self.detail,
        }


class Store:
    """DuckDB 仓储。用 ``with Store(path) as store:`` 管理生命周期。"""

    def __init__(self, path: str | Path, read_only: bool = False, auto_init: bool = True):
        import duckdb  # 延迟导入，未安装 duckdb 时其它子命令仍可使用

        self.path = Path(path)
        self.read_only = read_only
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.path.exists():
            raise FileNotFoundError(f"数据库不存在: {self.path}（先执行 sglbench db init）")
        try:
            self.con = duckdb.connect(str(self.path), read_only=read_only)
        except Exception as exc:  # pragma: no cover - 锁冲突等
            raise RuntimeError(
                f"无法打开 DuckDB 数据库 {self.path}: {exc}\n"
                "提示：DuckDB 同一文件只允许一个写入进程，请确认没有其它 sglbench 正在写入。"
            ) from exc
        if auto_init and not read_only:
            self.init_schema()

    # ---- 生命周期 ----
    def close(self) -> None:
        try:
            self.con.close()
        except Exception:  # pragma: no cover
            pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- schema ----
    def init_schema(self) -> None:
        for ddl in _DDL:
            self.con.execute(ddl)
        self.con.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            [str(SCHEMA_VERSION)],
        )
        self.con.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('tool_version', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            [__version__],
        )

    def schema_version(self) -> int | None:
        row = self.con.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        return int(row[0]) if row else None

    # ---- 通用查询 ----
    def query(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        cur = self.con.execute(sql, list(params or []))
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        row = self.con.execute(sql, list(params or [])).fetchone()
        return row[0] if row else None

    def to_pandas(self, sql: str, params: Sequence[Any] | None = None):
        return self.con.execute(sql, list(params or [])).df()

    # ---- 场景 ----
    def upsert_scenario(self, scenario: Scenario) -> None:
        self.con.execute(
            """
            INSERT INTO scenarios (scenario_key, name, family, stage, description, identity, spec_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (scenario_key) DO UPDATE SET
                name = EXCLUDED.name,
                family = EXCLUDED.family,
                stage = EXCLUDED.stage,
                description = EXCLUDED.description,
                identity = EXCLUDED.identity,
                spec_json = EXCLUDED.spec_json,
                updated_at = EXCLUDED.updated_at
            """,
            [
                scenario.key,
                scenario.name,
                scenario.family,
                scenario.stage,
                scenario.description,
                scenario.identity(),
                json.dumps(scenario.to_dict(), ensure_ascii=False),
                _to_ts(utcnow()),
            ],
        )

    def upsert_scenarios(self, scenarios: Iterable[Scenario]) -> int:
        n = 0
        for s in scenarios:
            self.upsert_scenario(s)
            n += 1
        return n

    def list_scenarios(self) -> list[dict[str, Any]]:
        return self.query(
            """
            SELECT s.*,
                   (SELECT COUNT(*) FROM runs r WHERE r.scenario_key = s.scenario_key) AS run_count
            FROM scenarios s
            ORDER BY s.family, s.scenario_key
            """
        )

    # ---- run 写入 ----
    def save_run(self, run: RunRecord) -> str:
        """幂等写入：同 run_id 覆盖（先删后插），保证重跑 ingest 不产生重复。"""
        self.delete_run(run.run_id, commit=False)
        self.con.execute(
            """
            INSERT INTO runs (run_id, scenario_key, label, status, source, started_at, finished_at,
                              wall_s, command, exit_code, host, operator, notes, tags_json, raw_dir,
                              params_json, env_json, artifacts_json, meta_json, issues_json,
                              content_sha256, sample_count, issue_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run.run_id,
                run.scenario_key,
                run.label,
                run.status,
                run.source,
                _to_ts(run.started_at),
                _to_ts(run.finished_at),
                run.wall_s,
                run.command,
                run.exit_code,
                run.host,
                run.operator,
                run.notes,
                json.dumps(run.tags, ensure_ascii=False),
                run.raw_dir,
                json.dumps(run.params, ensure_ascii=False, default=str),
                json.dumps(run.env, ensure_ascii=False),
                json.dumps(run.artifacts, ensure_ascii=False),
                json.dumps(run.meta, ensure_ascii=False, default=str),
                json.dumps([i.to_dict() for i in run.issues], ensure_ascii=False),
                run.meta.get("content_sha256"),
                len(run.samples),
                len(run.issues),
                _to_ts(utcnow()),
            ],
        )

        if run.metrics:
            self.con.executemany(
                "INSERT INTO metrics (run_id, metric_key, value) VALUES (?, ?, ?)",
                [(run.run_id, k, float(v)) for k, v in run.metrics.items()],
            )
        if run.samples:
            rows = []
            for seq, sample in enumerate(run.samples):
                rows.append(
                    (
                        run.run_id,
                        seq,
                        sample.request_id or str(seq),
                        sample.input_len,
                        sample.output_len,
                        sample.ttft_ms,
                        sample.tpot_ms,
                        sample.itl_ms,
                        sample.e2e_ms,
                        sample.status,
                        (sample.error or "")[:1000] or None,
                    )
                )
            self.con.executemany(
                """
                INSERT INTO request_samples (run_id, seq, request_id, input_len, output_len,
                                             ttft_ms, tpot_ms, itl_ms, e2e_ms, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return run.run_id

    def delete_run(self, run_id: str, commit: bool = True) -> bool:
        exists = self.has_run(run_id)
        for table in ("metrics", "request_samples", "runs"):
            self.con.execute(f"DELETE FROM {table} WHERE run_id = ?", [run_id])
        return exists

    def has_run(self, run_id: str) -> bool:
        return bool(self.scalar("SELECT COUNT(*) FROM runs WHERE run_id = ?", [run_id]))

    def count_runs(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM runs") or 0)

    # ---- run 读取 ----
    def _row_to_summary(self, row: Mapping[str, Any], metrics: dict[str, float] | None = None) -> RunSummary:
        return RunSummary(
            run_id=row["run_id"],
            scenario_key=row["scenario_key"],
            label=row["label"] or "",
            status=row["status"],
            started_at=_from_ts(row["started_at"]),
            metrics=metrics or {},
            sample_count=int(row["sample_count"] or 0),
            issue_count=int(row["issue_count"] or 0),
            source=row["source"] or "executed",
        )

    def list_runs(
        self,
        scenario_key: str | None = None,
        family: str | None = None,
        status: str | None = None,
        limit: int | None = None,
        ascending: bool = False,
        with_metrics: bool = True,
    ) -> list[RunSummary]:
        sql = ["SELECT r.* FROM runs r"]
        params: list[Any] = []
        where: list[str] = []
        if family:
            sql.append("JOIN scenarios s ON s.scenario_key = r.scenario_key")
            where.append("s.family = ?")
            params.append(family)
        if scenario_key:
            where.append("r.scenario_key = ?")
            params.append(scenario_key)
        if status:
            where.append("r.status = ?")
            params.append(status)
        if where:
            sql.append("WHERE " + " AND ".join(where))
        sql.append(
            f"ORDER BY r.started_at {'ASC' if ascending else 'DESC'} NULLS LAST, r.run_id {'ASC' if ascending else 'DESC'}"
        )
        if limit:
            sql.append(f"LIMIT {int(limit)}")
        rows = self.query("\n".join(sql), params)

        metrics_by_run: dict[str, dict[str, float]] = {}
        if with_metrics and rows:
            ids = [r["run_id"] for r in rows]
            placeholders = ", ".join("?" for _ in ids)
            for m in self.query(
                f"SELECT run_id, metric_key, value FROM metrics WHERE run_id IN ({placeholders})",
                ids,
            ):
                metrics_by_run.setdefault(m["run_id"], {})[m["metric_key"]] = m["value"]
        return [self._row_to_summary(r, metrics_by_run.get(r["run_id"])) for r in rows]

    def latest_run(self, scenario_key: str) -> str | None:
        row = self.con.execute(
            "SELECT run_id FROM runs WHERE scenario_key = ? "
            "ORDER BY started_at DESC NULLS LAST, run_id DESC LIMIT 1",
            [scenario_key],
        ).fetchone()
        return row[0] if row else None

    def find_run_by_content(self, scenario_key: str, digest: str) -> str | None:
        """按内容指纹查重：同一份日志重复 ingest 时避免制造重复 run。"""
        if not digest:
            return None
        row = self.con.execute(
            "SELECT run_id FROM runs WHERE scenario_key = ? AND content_sha256 = ? "
            "ORDER BY started_at DESC NULLS LAST LIMIT 1",
            [scenario_key, digest],
        ).fetchone()
        return row[0] if row else None

    def run_ids_for_scenario(self, scenario_key: str, limit: int | None = None) -> list[str]:
        sql = (
            "SELECT run_id FROM runs WHERE scenario_key = ? "
            "ORDER BY started_at DESC NULLS LAST, run_id DESC"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r["run_id"] for r in self.query(sql, [scenario_key])]

    def resolve_run(self, token: str) -> str:
        """把用户输入解析成 run_id：精确 / 唯一前缀 / ``latest`` / ``<scenario>:<n>``。"""
        if token in ("latest", "last"):
            row = self.con.execute(
                "SELECT run_id FROM runs ORDER BY started_at DESC NULLS LAST LIMIT 1"
            ).fetchone()
            if not row:
                raise KeyError("数据库里没有任何 run")
            return row[0]
        if self.has_run(token):
            return token
        if ":" in token:
            # 支持 ``<scenario_key>:<n>``：n=1 表示最新一次（与 shell 历史直觉一致）
            scenario, _, ordinal = token.rpartition(":")
            if ordinal.isdigit() and scenario:
                ids = self.run_ids_for_scenario(scenario)
                idx = int(ordinal)
                if 1 <= idx <= len(ids):
                    return ids[idx - 1]
                if ids:
                    raise KeyError(f"场景 {scenario} 只有 {len(ids)} 个 run，取不到第 {idx} 个")
        matches = self.query("SELECT run_id FROM runs WHERE run_id LIKE ? ORDER BY run_id", [f"{token}%"])
        if len(matches) == 1:
            return matches[0]["run_id"]
        if not matches:
            raise KeyError(f"找不到 run: {token!r}")
        raise KeyError(f"前缀 {token!r} 匹配到多个 run: " + ", ".join(m["run_id"] for m in matches[:8]))

    def resolve_runs(self, tokens: Sequence[str]) -> list[str]:
        return [self.resolve_run(t) for t in tokens]

    def get_run(self, run_id: str, with_samples: bool = True) -> RunRecord | None:
        rows = self.query("SELECT * FROM runs WHERE run_id = ?", [run_id])
        if not rows:
            return None
        row = rows[0]
        metrics = {
            m["metric_key"]: m["value"]
            for m in self.query("SELECT metric_key, value FROM metrics WHERE run_id = ?", [run_id])
        }
        issues = [Issue(**i) for i in json.loads(row["issues_json"] or "[]")]
        samples: list[Sample] = []
        if with_samples:
            samples = [
                Sample(
                    request_id=s["request_id"],
                    input_len=s["input_len"],
                    output_len=s["output_len"],
                    ttft_ms=s["ttft_ms"],
                    tpot_ms=s["tpot_ms"],
                    itl_ms=s["itl_ms"],
                    e2e_ms=s["e2e_ms"],
                    status=s["status"] or "ok",
                    error=s["error"],
                )
                for s in self.query(
                    """
                    SELECT request_id, input_len, output_len, ttft_ms, tpot_ms, itl_ms, e2e_ms, status, error
                    FROM request_samples WHERE run_id = ? ORDER BY seq
                    """,
                    [run_id],
                )
            ]
        return RunRecord(
            run_id=row["run_id"],
            scenario_key=row["scenario_key"],
            label=row["label"] or "",
            status=row["status"],
            source=row["source"] or "executed",
            started_at=_from_ts(row["started_at"]),
            finished_at=_from_ts(row["finished_at"]),
            wall_s=row["wall_s"],
            command=row["command"] or "",
            exit_code=row["exit_code"],
            host=row["host"] or "",
            operator=row["operator"] or "",
            notes=row["notes"] or "",
            tags=list(json.loads(row["tags_json"] or "[]")),
            raw_dir=row["raw_dir"] or "",
            params=dict(json.loads(row["params_json"] or "{}")),
            env=dict(json.loads(row["env_json"] or "{}")),
            metrics=metrics,
            samples=samples,
            issues=issues,
            artifacts=dict(json.loads(row["artifacts_json"] or "{}")),
            meta=dict(json.loads(row["meta_json"] or "{}")),
            schema_version=self.schema_version() or SCHEMA_VERSION,
        )

    def metrics(self, run_id: str) -> dict[str, float]:
        return {
            m["metric_key"]: m["value"]
            for m in self.query("SELECT metric_key, value FROM metrics WHERE run_id = ?", [run_id])
        }

    def samples(self, run_id: str) -> list[Sample]:
        run = self.get_run(run_id, with_samples=True)
        return run.samples if run else []

    def sample_values(self, run_id: str, field_name: str, ok_only: bool = True) -> list[float]:
        """只取某字段的数值列（SQL 侧过滤），分布图的快速通道。"""
        clause = "AND status IN ('ok','success','succeeded','200','true')" if ok_only else ""
        rows = self.query(
            f"SELECT {field_name} AS v FROM request_samples WHERE run_id = ? AND {field_name} IS NOT NULL {clause}",
            [run_id],
        )
        return [float(r["v"]) for r in rows if r["v"] is not None]

    # ---- 预检 ----
    def save_preflight(self, check: PreflightCheck) -> str:
        self.con.execute(
            """
            INSERT INTO preflight_checks (check_id, checked_at, scenario_key, target, url, model,
                                          ok, http_status, ttft_ms, total_ms, output_chars, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (check_id) DO UPDATE SET
                checked_at = EXCLUDED.checked_at, ok = EXCLUDED.ok,
                http_status = EXCLUDED.http_status, ttft_ms = EXCLUDED.ttft_ms,
                total_ms = EXCLUDED.total_ms, output_chars = EXCLUDED.output_chars,
                detail = EXCLUDED.detail
            """,
            [
                check.check_id,
                _to_ts(check.checked_at),
                check.scenario_key,
                check.target,
                check.url,
                check.model,
                check.ok,
                check.http_status,
                check.ttft_ms,
                check.total_ms,
                check.output_chars,
                check.detail,
            ],
        )
        return check.check_id

    def list_preflight(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM preflight_checks ORDER BY checked_at DESC NULLS LAST LIMIT ?", [limit]
        )

    # ---- 导出 ----
    def export(self, out_dir: str | Path, fmt: str = "parquet") -> list[Path]:
        """把核心表导出成文件，便于长期归档或丢进 BI 工具。"""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        tables = ("runs", "metrics", "request_samples", "scenarios", "v_run_metrics")
        written: list[Path] = []
        fmt = fmt.lower()
        if fmt not in ("parquet", "csv", "json"):
            raise ValueError(f"不支持的导出格式: {fmt}（可选 parquet/csv/json）")

        if fmt == "json":
            for table in ("runs", "metrics", "request_samples", "scenarios"):
                target = out / f"{table}.json"
                target.write_text(
                    json.dumps(self.query(f"SELECT * FROM {table}"), ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
                written.append(target)
            return written

        options = "(FORMAT PARQUET)" if fmt == "parquet" else "(FORMAT CSV, HEADER)"
        suffix = "parquet" if fmt == "parquet" else "csv"
        for table in tables:
            target = out / f"{table}.{suffix}"
            literal = str(target).replace("'", "''")
            self.con.execute(f"COPY (SELECT * FROM {table}) TO '{literal}' {options}")
            written.append(target)
        return written
