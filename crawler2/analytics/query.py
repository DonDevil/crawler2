"""DuckDB over one published export revision (design §7).

``ExportView`` opens an in-memory DuckDB and creates one view per dataset over
exactly the Parquet files its manifest lists (never a directory glob, so a
stray or partial file is never read). Nothing here can reach Scylla, Redis or
MinIO: a report built on a view is a function of the revision alone.

``validate_revision`` re-checks a published revision against its manifest
(files, sizes, SHA-256, row counts, key uniqueness, content digest).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import duckdb

from crawler2.analytics.datasets import DATASETS
from crawler2.analytics.export import (
    ExportError,
    content_digest,
    parquet_relation,
    quote,
    read_manifest,
    scalar_int,
    sha256_file,
    sql_str,
)

READABLE = ("complete", "empty")


class DatasetUnavailableError(ExportError):
    """A required dataset is not in the export (unavailable at the source or not requested)."""


class ExportView:
    def __init__(self, revision_dir: Path, *, verify_checksums: bool = False) -> None:
        self.revision_dir = revision_dir
        self.manifest = read_manifest(revision_dir)
        self.con = duckdb.connect()
        self.con.execute("SET TimeZone = 'UTC'")
        self.available: dict[str, dict[str, Any]] = {}
        self.unavailable: dict[str, str] = {}
        for name, doc in sorted(self.manifest["datasets"].items()):
            if doc["status"] not in READABLE:
                self.unavailable[name] = doc.get("reason") or doc["status"]
                continue
            known = DATASETS.get(name)
            if known is None or known.schema_version != doc["schema_version"]:
                self.unavailable[name] = (
                    f"schema_version {doc['schema_version']} is not readable by this code"
                )
                continue
            paths: list[Path] = []
            for f in doc["files"]:
                path = revision_dir / f["path"]
                if not path.exists() or path.stat().st_size != f["bytes"]:
                    raise ExportError(f"{path} is missing or truncated (manifest {f['bytes']} B)")
                if verify_checksums and sha256_file(path) != f["sha256"]:
                    raise ExportError(f"{path} does not match its manifest SHA-256")
                paths.append(path)
            self.con.execute(
                f"CREATE VIEW {quote(name)} AS SELECT * FROM {parquet_relation(paths)}"
            )
            self.available[name] = doc
        for name in sorted(set(DATASETS) - set(self.manifest["datasets"])):
            self.unavailable[name] = "not requested in this export"

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> ExportView:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def ref(self) -> dict[str, Any]:
        """What a report was computed from (enough to re-open the same revision)."""
        m = self.manifest
        return {
            "export_key": m["export_key"],
            "revision": m["revision"],
            "content_id": m["content_id"],
            "scope": m["scope"],
            "datasets": {
                n: {
                    "schema_version": d["schema_version"],
                    "status": d["status"],
                    "rows": d.get("rows"),
                }
                for n, d in sorted(m["datasets"].items())
            },
        }

    def missing(self, names: Sequence[str]) -> dict[str, str]:
        return {n: self.unavailable[n] for n in names if n not in self.available}

    def require(self, *names: str) -> None:
        gone = self.missing(names)
        if gone:
            raise DatasetUnavailableError(f"export lacks required dataset(s): {gone}")

    def rows(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Query → list of dicts. Timestamp-with-time-zone columns come back as ISO strings
        (the Python driver would need pytz for them, and strings are stable in reports)."""
        rel = self.con.sql(sql, params=params) if params else self.con.sql(sql)
        cols = rel.columns
        types = [str(t) for t in rel.types]
        if any(t == "TIMESTAMP WITH TIME ZONE" for t in types):
            exprs = ", ".join(
                f"strftime({quote(c)}, '%Y-%m-%dT%H:%M:%S.%fZ') AS {quote(c)}"
                if t == "TIMESTAMP WITH TIME ZONE"
                else quote(c)
                for c, t in zip(cols, types, strict=True)
            )
            rel = rel.project(exprs)
        return [dict(zip(cols, r, strict=True)) for r in rel.fetchall()]

    def scalar(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        out = self.rows(sql, params)
        return next(iter(out[0].values())) if out else None


def validate_revision(revision_dir: Path, *, checksums: bool = True) -> dict[str, Any]:
    """Independent re-validation of a published revision (``crawler2-analytics validate``)."""
    manifest = read_manifest(revision_dir)
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    results: dict[str, Any] = {}
    ok = True
    try:
        for name, doc in sorted(manifest["datasets"].items()):
            if doc["status"] not in READABLE:
                results[name] = {"status": doc["status"], "checked": False}
                continue
            problems: list[str] = []
            paths: list[Path] = []
            for f in doc["files"]:
                path = revision_dir / f["path"]
                if not path.exists():
                    problems.append(f"missing file {f['path']}")
                    continue
                if path.stat().st_size != f["bytes"]:
                    problems.append(f"size mismatch {f['path']}")
                if checksums and sha256_file(path) != f["sha256"]:
                    problems.append(f"sha256 mismatch {f['path']}")
                rows = con.execute(f"SELECT count(*) FROM read_parquet({sql_str(str(path))})")
                got = rows.fetchone()
                if got is None or int(got[0]) != f["rows"]:
                    problems.append(f"row count mismatch {f['path']}")
                paths.append(path)
            if not problems:
                rel = parquet_relation(paths)
                keys = ", ".join(quote(k) for k in doc["key"])
                total = scalar_int(con, f"SELECT count(*) FROM {rel}")
                distinct = scalar_int(
                    con, f"SELECT count(*) FROM (SELECT DISTINCT {keys} FROM {rel})"
                )
                if total != doc["rows"]:
                    problems.append(f"rows {total} != manifest {doc['rows']}")
                if distinct != total:
                    problems.append("duplicate keys")
                columns = [c for c, _ in doc["columns"]]
                if content_digest(con, rel, columns) != doc["digest"]:
                    problems.append("content digest differs from the manifest")
            results[name] = {"status": doc["status"], "checked": True, "problems": problems}
            ok = ok and not problems
    finally:
        con.close()
    return {
        "revision_dir": str(revision_dir),
        "export_key": manifest["export_key"],
        "revision": manifest["revision"],
        "content_id": manifest["content_id"],
        "checksums": checksums,
        "ok": ok,
        "datasets": results,
    }
