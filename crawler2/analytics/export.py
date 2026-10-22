"""Durable store → validated, revisioned Parquet exports (design §4-§6).

One run:

1. **scan** every requested source table in ``segments`` token ranges; each
   segment's mapped rows are staged as gzip NDJSON and its counters as a
   checkpoint, both renamed into place only when the segment completes (a
   failed segment is retried from its start, a failed run can be resumed);
2. **build** each dataset in a disk-backed DuckDB database: collapse exact
   duplicates, refuse key conflicts and null keys, write Parquet partitioned
   by UTC ``date`` of the dataset's time column;
3. **reconcile** the Parquet read-back with the staged rows (rows, keys,
   missing/unexpected keys, an order-independent content digest) and the
   counters with the scan (design §6 invariants);
4. **publish** under a lock: if the export key's current revision has the
   same content id the run is ``unchanged`` and nothing is written; otherwise
   the staged directory, manifest included, is renamed to ``rev-NNNN`` (atomic
   on one filesystem) and ``CURRENT`` is replaced to point at it.

Readers only ever follow ``CURRENT`` or name a revision; a revision directory
exists only complete with its manifest, so a failed run can never be read as a
published export. Nothing outside ``<root>`` is written and the source is only
read.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import shutil
import socket
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from crawler2.analytics.datasets import (
    DATASETS,
    EXCLUDED,
    SOURCES,
    Dataset,
    Malformed,
    SourceTable,
    parse_ts,
)
from crawler2.analytics.source import RowSource
from crawler2.storage.errors import StorageUnavailableError

EXPORTER = "analytics-export/v1"
MANIFEST_FORMAT = "crawler2.analytics.export/v1"
MANIFEST = "_manifest.json"
CURRENT = "CURRENT"
LOCK = ".publish.lock"
UNKNOWN_DATE = "unknown"

_MAPPING_ERRORS = (Malformed, KeyError, TypeError, ValueError, AttributeError)


class ExportError(RuntimeError):
    """The run failed; nothing was published (the staging directory is kept for resume)."""


class PublishLockedError(ExportError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(UTC).isoformat()


def _write_json(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True, slots=True)
class ExportOptions:
    segments: int = 64
    max_retries: int = 3
    retry_backoff_s: float = 2.0
    duckdb_memory_limit: str = "1GB"
    duckdb_threads: int = 2
    rejected_samples: int = 20
    verify_counts: bool = False
    """Re-count every segment after the scan (doubles the read cost; reports drift)."""


@dataclass(frozen=True, slots=True)
class ExportScope:
    source: str
    until: datetime
    """Exclusive upper bound on each windowed dataset's time column (the export boundary)."""
    since: datetime | None
    datasets: tuple[str, ...]

    def document(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "since": _iso(self.since),
            "until": _iso(self.until),
            "datasets": {n: DATASETS[n].schema_version for n in self.datasets},
        }

    @property
    def key(self) -> str:
        """Stable identity of (source, window, datasets + schema versions)."""
        return hashlib.sha256(canonical(self.document())).hexdigest()[:16]

    @classmethod
    def from_document(cls, doc: dict[str, Any]) -> ExportScope:
        since = parse_ts(doc["since"])
        until = parse_ts(doc["until"])
        if until is None:
            raise ExportError("run state has no boundary (until)")
        return cls(doc["source"], until, since, tuple(sorted(doc["datasets"])))


@dataclass(slots=True)
class ExportResult:
    run_id: str
    export_key: str
    outcome: str
    """published | unchanged | failed"""
    revision: int | None = None
    revision_dir: Path | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def resolve_datasets(names: Sequence[str] | None) -> tuple[str, ...]:
    if not names:
        return tuple(sorted(DATASETS))
    unknown = sorted(set(names) - set(DATASETS))
    if unknown:
        raise ValueError(f"unknown dataset(s): {', '.join(unknown)}; known: {sorted(DATASETS)}")
    return tuple(sorted(set(names)))


class Exporter:
    def __init__(
        self,
        source: RowSource,
        root: Path,
        *,
        options: ExportOptions | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        code_version: str = "",
    ) -> None:
        self.source = source
        self.root = root
        self.options = options or ExportOptions()
        self._clock = clock
        self._sleep = sleep
        self._code_version = code_version

    # --- public ---------------------------------------------------------------------

    def export(
        self,
        *,
        until: datetime | None = None,
        since: datetime | None = None,
        datasets: Sequence[str] | None = None,
        resume: str | None = None,
    ) -> ExportResult:
        if resume is not None:
            run_dir = self.root / "_staging" / resume
            if not (run_dir / "run.json").exists():
                raise ExportError(f"no resumable run {resume} under {self.root / '_staging'}")
            state = json.loads((run_dir / "run.json").read_text())
            scope = ExportScope.from_document(state["scope"])
            if scope.source != self.source.label:
                raise ExportError(f"run {resume} reads {scope.source}, not {self.source.label}")
            run_id = resume
            segments = int(state["segments"])
        else:
            started = self._clock()
            boundary = (until or started).astimezone(UTC)
            if since is not None and since >= boundary:
                raise ValueError("since must be before until")
            scope = ExportScope(self.source.label, boundary, since, resolve_datasets(datasets))
            run_id = f"{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
            run_dir = self.root / "_staging" / run_id
            run_dir.mkdir(parents=True)
            segments = self.options.segments
            state = {
                "run_id": run_id,
                "export_key": scope.key,
                "scope": scope.document(),
                "segments": segments,
                "started_at": _iso(started),
                "status": "running",
                "host": socket.gethostname(),
                "exporter": EXPORTER,
            }
            _write_json(run_dir / "run.json", state)
        state["status"] = "running"
        state.setdefault("attempts", 0)
        state["attempts"] += 1
        _write_json(run_dir / "run.json", state)
        try:
            result = self._run(run_id, run_dir, scope, segments, state)
        except Exception as exc:
            state |= {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            state["failed_at"] = _iso(self._clock())
            _write_json(run_dir / "run.json", state)
            self._record_run(run_id, scope.key, "failed", None, None, state)
            raise
        return result

    # --- run ------------------------------------------------------------------------

    def _run(
        self,
        run_id: str,
        run_dir: Path,
        scope: ExportScope,
        segments: int,
        state: dict[str, Any],
    ) -> ExportResult:
        wanted = set(scope.datasets)
        tables = [s for s in SOURCES if wanted & {d.name for d in s.datasets}]
        datasets_doc: dict[str, Any] = {}
        drift: dict[str, Any] = {}
        for table in tables:
            requested = tuple(d for d in table.datasets if d.name in wanted)
            if not self.source.available(table.table):
                for d in requested:
                    datasets_doc[d.name] = self._unavailable(d, table)
                continue
            for segment in range(segments):
                self._segment(run_dir, scope, table, requested, segment, segments)
            if self.options.verify_counts:
                drift[table.table] = self._verify_counts(run_dir, table, segments)
        scan_finished = self._clock()
        data_dir = run_dir / "data"
        if data_dir.exists():  # a previous attempt of this run failed during build
            shutil.rmtree(data_dir)
        data_dir.mkdir()
        con = self._duckdb(run_dir)
        try:
            for table in tables:
                for d in table.datasets:
                    if d.name in wanted and d.name not in datasets_doc:
                        counters = self._counters(run_dir, table, d, segments)
                        datasets_doc[d.name] = self._build(
                            con, run_dir, data_dir, table, d, counters
                        )
        finally:
            con.close()
        failed = sorted(n for n, doc in datasets_doc.items() if doc["status"] == "failed")
        if failed:
            details = {n: datasets_doc[n]["problems"] for n in failed}
            raise ExportError(f"validation failed for {failed}: {details}")
        content_id = hashlib.sha256(
            canonical(
                {
                    n: [doc["schema_version"], doc["status"], doc.get("rows"), doc.get("digest")]
                    for n, doc in sorted(datasets_doc.items())
                }
            )
        ).hexdigest()
        manifest = {
            "format": MANIFEST_FORMAT,
            "export_key": scope.key,
            "run_id": run_id,
            "content_id": content_id,
            "status": "complete",
            "scope": scope.document(),
            "boundary": {
                "kind": "time-window over full token-range scans (not a snapshot)",
                "until_exclusive": _iso(scope.until),
                "since_inclusive": _iso(scope.since),
                "scan_started_at": state["started_at"],
                "scan_finished_at": _iso(scan_finished),
                "segments": segments,
                "read_consistency": "LOCAL_QUORUM per page",
                "count_drift": drift or None,
            },
            "code": {
                "exporter": EXPORTER,
                "crawler2": self._code_version,
                "duckdb": duckdb.__version__,
            },
            "started_at": state["started_at"],
            "datasets": datasets_doc,
            "excluded_sources": EXCLUDED,
        }
        return self._publish(run_id, run_dir, data_dir, scope, manifest)

    # --- scan -----------------------------------------------------------------------

    def _segment(
        self,
        run_dir: Path,
        scope: ExportScope,
        table: SourceTable,
        datasets: tuple[Dataset, ...],
        segment: int,
        of: int,
    ) -> None:
        checkpoint = run_dir / "segments" / table.table / f"seg-{segment:05d}.json"
        if checkpoint.exists():
            return
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        attempt = 0
        while True:
            try:
                counters = self._scan_segment(run_dir, scope, table, datasets, segment, of)
                break
            except StorageUnavailableError:
                attempt += 1
                if attempt > self.options.max_retries:
                    raise
                self._sleep(self.options.retry_backoff_s * attempt)
        for d in datasets:
            tmp = self._rows_path(run_dir, d, segment, tmp=True)
            os.replace(tmp, self._rows_path(run_dir, d, segment))
        _write_json(checkpoint, {"retries": attempt, **counters})

    def _rows_path(self, run_dir: Path, d: Dataset, segment: int, *, tmp: bool = False) -> Path:
        name = f"seg-{segment:05d}.jsonl.gz" + (".tmp" if tmp else "")
        return run_dir / "rows" / d.name / name

    def _scan_segment(
        self,
        run_dir: Path,
        scope: ExportScope,
        table: SourceTable,
        datasets: tuple[Dataset, ...],
        segment: int,
        of: int,
    ) -> dict[str, Any]:
        counts = {d.name: Counter[str]() for d in datasets}
        samples: dict[str, list[str]] = {d.name: [] for d in datasets}
        source_rows = 0
        with contextlib.ExitStack() as stack:
            out = {}
            for d in datasets:
                path = self._rows_path(run_dir, d, segment, tmp=True)
                path.parent.mkdir(parents=True, exist_ok=True)
                out[d.name] = stack.enter_context(
                    gzip.open(path, "wt", encoding="utf-8", compresslevel=1)
                )
            for row in self.source.scan(table, segment, of):
                source_rows += 1
                for d in datasets:
                    c = counts[d.name]
                    try:
                        mapped = list(d.mapper(row))
                    except _MAPPING_ERRORS as exc:
                        c["rejected"] += 1
                        if len(samples[d.name]) < self.options.rejected_samples:
                            pk = {k: str(row.get(k)) for k in table.partition_key if k in row}
                            samples[d.name].append(f"{pk or '?'}: {type(exc).__name__}: {exc}")
                        continue
                    for r in mapped:
                        c["candidates"] += 1
                        when = r.get(d.time_column) if d.time_column else None
                        if d.time_column is not None:
                            moment = parse_ts(when)
                            if moment is None:
                                c["unknown_time"] += 1
                            elif moment >= scope.until or (
                                scope.since is not None and moment < scope.since
                            ):
                                c["out_of_window"] += 1
                                continue
                        c["extracted"] += 1
                        out[d.name].write(json.dumps(r, separators=(",", ":"), ensure_ascii=False))
                        out[d.name].write("\n")
        return {
            "source_rows": source_rows,
            "datasets": {n: dict(c) for n, c in counts.items()},
            "rejected_samples": samples,
        }

    def _counters(
        self, run_dir: Path, table: SourceTable, d: Dataset, segments: int
    ) -> dict[str, Any]:
        total: Counter[str] = Counter()
        samples: list[str] = []
        source_rows = retries = 0
        for segment in range(segments):
            path = run_dir / "segments" / table.table / f"seg-{segment:05d}.json"
            seg = json.loads(path.read_text())
            source_rows += seg["source_rows"]
            retries += seg["retries"]
            total.update(seg["datasets"].get(d.name, {}))
            samples.extend(seg["rejected_samples"].get(d.name, []))
        keys = ("candidates", "rejected", "out_of_window", "unknown_time", "extracted")
        return {
            "source_rows": source_rows,
            "segment_retries": retries,
            **{k: total.get(k, 0) for k in keys},
            "rejected_samples": samples[: self.options.rejected_samples],
        }

    def _verify_counts(self, run_dir: Path, table: SourceTable, segments: int) -> dict[str, int]:
        scanned = recount = 0
        for segment in range(segments):
            seg = json.loads(
                (run_dir / "segments" / table.table / f"seg-{segment:05d}.json").read_text()
            )
            scanned += seg["source_rows"]
            recount += self.source.count(table, segment, segments)
        return {"scanned": scanned, "recounted": recount, "delta": recount - scanned}

    def _unavailable(self, d: Dataset, table: SourceTable) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "schema_version": d.schema_version,
            "source": {"table": table.table, "columns": list(table.columns)},
            "reason": f"table {table.table} does not exist in {self.source.label}"
            + (f" ({table.note})" if table.note else ""),
        }

    # --- build + reconcile ----------------------------------------------------------

    def _duckdb(self, run_dir: Path) -> duckdb.DuckDBPyConnection:
        db = run_dir / "stage.duckdb"
        if db.exists():
            db.unlink()
        con = duckdb.connect(str(db))
        tmp = run_dir / "duckdb-tmp"
        tmp.mkdir(exist_ok=True)
        con.execute("SET TimeZone = 'UTC'")
        con.execute(f"SET memory_limit = {sql_str(self.options.duckdb_memory_limit)}")
        con.execute(f"SET threads = {int(self.options.duckdb_threads)}")
        con.execute(f"SET temp_directory = {sql_str(str(tmp))}")
        return con

    def _build(
        self,
        con: duckdb.DuckDBPyConnection,
        run_dir: Path,
        data_dir: Path,
        table: SourceTable,
        d: Dataset,
        counters: dict[str, Any],
    ) -> dict[str, Any]:
        raw, dedup = f"raw_{d.name}", f"ds_{d.name}"
        cols = ", ".join(f"{quote(n)} {t}" for n, t in d.columns)
        files = sorted((run_dir / "rows" / d.name).glob("seg-*.jsonl.gz"))
        con.execute(f"DROP TABLE IF EXISTS {raw}")
        con.execute(f"CREATE TABLE {raw} ({cols})")
        if files:
            spec = "{" + ", ".join(f"{sql_str(n)}: {sql_str(t)}" for n, t in d.columns) + "}"
            paths = "[" + ", ".join(sql_str(str(p)) for p in files) + "]"
            con.execute(
                f"INSERT INTO {raw} SELECT * FROM read_json({paths}, "
                f"format = 'newline_delimited', columns = {spec}, compression = 'gzip')"
            )
        names = ", ".join(quote(n) for n in d.column_names)
        keys = ", ".join(quote(k) for k in d.key)
        con.execute(f"DROP TABLE IF EXISTS {dedup}")
        con.execute(f"CREATE TABLE {dedup} AS SELECT DISTINCT {names} FROM {raw}")
        staged = self._one(con, f"SELECT count(*) FROM {raw}")
        distinct_rows = self._one(con, f"SELECT count(*) FROM {dedup}")
        distinct_keys = self._one(
            con, f"SELECT count(*) FROM (SELECT DISTINCT {keys} FROM {dedup})"
        )
        null_cond = " OR ".join(f"{quote(k)} IS NULL" for k in d.key)
        null_keys = self._one(con, f"SELECT count(*) FROM {dedup} WHERE {null_cond}")
        conflicts = distinct_rows - distinct_keys
        conflict_samples = [
            str(r)
            for r in con.execute(
                f"SELECT {keys} FROM {dedup} GROUP BY {keys} HAVING count(*) > 1 "
                f"ORDER BY {keys} LIMIT 5"
            ).fetchall()
        ]
        out_dir = data_dir / d.name / f"schema_version={d.schema_version}"
        out_dir.mkdir(parents=True)
        order = ", ".join(quote(c) for c in d.sort)
        if d.time_column is not None:
            date = (
                f"coalesce(strftime({quote(d.time_column)}, '%Y-%m-%d'), {sql_str(UNKNOWN_DATE)})"
            )
            select = f"SELECT {names}, {date} AS date FROM {dedup}"
            if distinct_rows:
                con.execute(
                    f"COPY ({select} ORDER BY date, {order}) TO {sql_str(str(out_dir))} "
                    "(FORMAT PARQUET, PARTITION_BY (date), WRITE_PARTITION_COLUMNS true, "
                    "COMPRESSION zstd, FILENAME_PATTERN 'part-{i}')"
                )
            else:
                con.execute(
                    f"COPY ({select}) TO {sql_str(str(out_dir / 'part-0.parquet'))} "
                    "(FORMAT PARQUET, COMPRESSION zstd)"
                )
        else:
            con.execute(
                f"COPY (SELECT {names} FROM {dedup} ORDER BY {order}) TO "
                f"{sql_str(str(out_dir / 'part-0.parquet'))} (FORMAT PARQUET, COMPRESSION zstd)"
            )
        written = sorted(out_dir.rglob("*.parquet"))
        file_docs = []
        for path in written:
            rows = self._one(con, f"SELECT count(*) FROM read_parquet({sql_str(str(path))})")
            part = path.parent.name
            file_docs.append(
                {
                    "path": str(path.relative_to(data_dir)),
                    "date": part.removeprefix("date=") if part.startswith("date=") else None,
                    "rows": rows,
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
        pq = parquet_relation(written)
        readback = self._one(con, f"SELECT count(*) FROM {pq}")
        readback_keys = self._one(con, f"SELECT count(*) FROM (SELECT DISTINCT {keys} FROM {pq})")
        missing = self._one(
            con,
            f"SELECT count(*) FROM (SELECT {keys} FROM {dedup}) ANTI JOIN "
            f"(SELECT {keys} FROM {pq}) USING ({keys})",
        )
        unexpected = self._one(
            con,
            f"SELECT count(*) FROM (SELECT {keys} FROM {pq}) ANTI JOIN "
            f"(SELECT {keys} FROM {dedup}) USING ({keys})",
        )
        digest = content_digest(con, dedup, d)
        readback_digest = content_digest(con, pq, d)
        counts = {
            **{k: v for k, v in counters.items() if k != "rejected_samples"},
            "staged": staged,
            "duplicates_collapsed": staged - distinct_rows,
            "key_conflicts": conflicts,
            "null_keys": null_keys,
            "written": distinct_rows,
            "readback": readback,
            "readback_distinct_keys": readback_keys,
            "missing_keys": missing,
            "unexpected_keys": unexpected,
        }
        problems = check_invariants(d, counts, digest, readback_digest)
        if conflicts:
            problems.append(f"key conflicts (same key, different content): {conflict_samples}")
        return {
            "status": "failed" if problems else ("empty" if distinct_rows == 0 else "complete"),
            "problems": problems,
            "schema_version": d.schema_version,
            "description": d.description,
            "semantics": d.semantics,
            "source": {"table": table.table, "columns": list(table.columns)},
            "fanout": d.fanout,
            "time_column": d.time_column,
            "partitioning": "date (UTC day of time_column; 'unknown' when null)"
            if d.time_column
            else "none (current-state dimension, not windowed)",
            "key": list(d.key),
            "sort": list(d.sort),
            "columns": [list(c) for c in d.columns],
            "rows": distinct_rows,
            "digest": digest,
            "counts": counts,
            "rejected_samples": counters["rejected_samples"],
            "files": file_docs,
        }

    @staticmethod
    def _one(con: duckdb.DuckDBPyConnection, sql: str) -> int:
        return scalar_int(con, sql)

    # --- publish --------------------------------------------------------------------

    def _publish(
        self,
        run_id: str,
        run_dir: Path,
        data_dir: Path,
        scope: ExportScope,
        manifest: dict[str, Any],
    ) -> ExportResult:
        export_dir = self.root / "exports" / scope.key
        export_dir.mkdir(parents=True, exist_ok=True)
        with publish_lock(export_dir, run_id):
            current = repair_current(export_dir)
            if current is not None:
                existing = read_manifest(export_dir / revision_name(current))
                if existing["content_id"] == manifest["content_id"]:
                    finished = self._clock()
                    self._finish(run_dir, "unchanged", current, finished)
                    self._record_run(
                        run_id, scope.key, "unchanged", current, manifest["content_id"], {}
                    )
                    return ExportResult(
                        run_id,
                        scope.key,
                        "unchanged",
                        current,
                        export_dir / revision_name(current),
                        existing,
                    )
            revision = max(list_revisions(export_dir), default=0) + 1
            finished = self._clock()
            manifest |= {
                "revision": revision,
                "previous_revision": current,
                "finished_at": _iso(finished),
            }
            _write_json(data_dir / MANIFEST, manifest)
            target = export_dir / revision_name(revision)
            os.rename(data_dir, target)
            _write_json(
                export_dir / CURRENT, {"revision": revision, "content_id": manifest["content_id"]}
            )
        self._finish(run_dir, "published", revision, finished)
        self._record_run(run_id, scope.key, "published", revision, manifest["content_id"], {})
        return ExportResult(run_id, scope.key, "published", revision, target, manifest)

    def _finish(self, run_dir: Path, outcome: str, revision: int, at: datetime) -> None:
        """The staged rows are no longer needed: only the run record stays."""
        state = json.loads((run_dir / "run.json").read_text())
        state |= {"status": outcome, "revision": revision, "finished_at": _iso(at)}
        self._record_state(state)
        shutil.rmtree(run_dir)

    def _record_state(self, state: dict[str, Any]) -> None:
        runs = self.root / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        _write_json(runs / f"{state['run_id']}.json", state)

    def _record_run(
        self,
        run_id: str,
        key: str,
        outcome: str,
        revision: int | None,
        content_id: str | None,
        state: dict[str, Any],
    ) -> None:
        path = self.root / "runs" / f"{run_id}.json"
        doc = json.loads(path.read_text()) if path.exists() else dict(state)
        doc |= {
            "run_id": run_id,
            "export_key": key,
            "status": outcome,
            "revision": revision,
            "content_id": content_id,
        }
        if state.get("error"):
            doc["error"] = state["error"]
        self._record_state(doc)


def content_digest(
    con: duckdb.DuckDBPyConnection, relation: str, d: Dataset | Sequence[str]
) -> str:
    """Order-independent digest over the dataset's columns (rows are distinct here).

    XOR of per-row MD5s of DuckDB's JSON rendering: detects any changed, added or
    removed row between two exports made by the same DuckDB version; not a
    cryptographic commitment (file SHA-256s are).
    """
    columns = d.column_names if isinstance(d, Dataset) else tuple(d)
    names = ", ".join(quote(n) for n in columns)
    row = con.execute(
        "SELECT CAST(coalesce(bit_xor(md5_number(CAST(to_json(_r) AS VARCHAR))), 0) AS VARCHAR), "
        f"count(*) FROM (SELECT {names} FROM {relation}) _r"
    ).fetchone()
    if row is None:
        raise ExportError(f"no digest row for {relation}")
    return f"xor-md5:{int(row[0]):032x}:{int(row[1])}"


def scalar_int(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = con.execute(sql).fetchone()
    if row is None:
        raise ExportError(f"query returned no row: {sql[:80]}")
    return int(row[0])


def parquet_relation(paths: Sequence[Path]) -> str:
    """Exactly these files; the ``date=`` directories are not re-interpreted (the column
    is stored in the files)."""
    files = ", ".join(sql_str(str(p)) for p in paths)
    return f"read_parquet([{files}], hive_partitioning = false)"


def check_invariants(d: Dataset, c: dict[str, int], digest: str, readback_digest: str) -> list[str]:
    """The reconciliation rules of design §6; an empty list = reconciled."""
    problems = []
    if not d.fanout and c["candidates"] + c["rejected"] != c["source_rows"]:
        problems.append("candidates + rejected != source_rows")
    if c["candidates"] - c["out_of_window"] != c["extracted"]:
        problems.append("candidates - out_of_window != extracted")
    if c["staged"] != c["extracted"]:
        problems.append(f"staged {c['staged']} != extracted {c['extracted']}")
    if c["written"] + c["duplicates_collapsed"] != c["staged"]:
        problems.append("written + duplicates_collapsed != staged")
    if c["null_keys"]:
        problems.append(f"{c['null_keys']} rows with a null key")
    if c["readback"] != c["written"] or c["readback_distinct_keys"] != c["written"]:
        problems.append("Parquet read-back rows/keys != written")
    if c["missing_keys"] or c["unexpected_keys"]:
        problems.append("missing or unexpected keys in the Parquet read-back")
    if digest != readback_digest:
        problems.append("Parquet content digest differs from the staged rows")
    return problems


# --- published layout ---------------------------------------------------------------


def revision_name(revision: int) -> str:
    return f"rev-{revision:04d}"


def list_revisions(export_dir: Path) -> list[int]:
    found = []
    for p in export_dir.glob("rev-*"):
        with contextlib.suppress(ValueError):
            found.append(int(p.name.removeprefix("rev-")))
    return sorted(found)


def read_manifest(revision_dir: Path) -> dict[str, Any]:
    path = revision_dir / MANIFEST
    if not path.exists():
        raise ExportError(f"{revision_dir} has no {MANIFEST}: not a published revision")
    doc: dict[str, Any] = json.loads(path.read_text())
    if doc.get("format") != MANIFEST_FORMAT or doc.get("status") != "complete":
        raise ExportError(f"{path} is not a complete {MANIFEST_FORMAT} manifest")
    return doc


def current_revision(export_dir: Path) -> int | None:
    path = export_dir / CURRENT
    if not path.exists():
        return None
    return int(json.loads(path.read_text())["revision"])


def repair_current(export_dir: Path) -> int | None:
    """Point ``CURRENT`` at the newest manifested revision if a run stopped between the
    revision rename and the pointer update (the revision is complete by construction)."""
    current = current_revision(export_dir)
    newest = max(
        (
            r
            for r in list_revisions(export_dir)
            if (export_dir / revision_name(r) / MANIFEST).exists()
        ),
        default=None,
    )
    if newest is not None and (current is None or newest > current):
        manifest = read_manifest(export_dir / revision_name(newest))
        _write_json(
            export_dir / CURRENT, {"revision": newest, "content_id": manifest["content_id"]}
        )
        return newest
    return current


@contextlib.contextmanager
def publish_lock(export_dir: Path, run_id: str) -> Iterator[None]:
    path = export_dir / LOCK
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        holder = path.read_text(errors="replace").strip()
        raise PublishLockedError(
            f"{path} is held ({holder}); if no export of this key is running, remove it"
        ) from exc
    try:
        os.write(fd, f"{run_id} {socket.gethostname()} pid={os.getpid()}\n".encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def resolve_export(path: Path, revision: int | None = None) -> Path:
    """An export key directory (follows ``CURRENT``) or a revision directory."""
    if (path / MANIFEST).exists():
        return path
    rev = revision if revision is not None else current_revision(path)
    if rev is None:
        raise ExportError(f"{path} has no published revision (no {CURRENT})")
    return path / revision_name(rev)
