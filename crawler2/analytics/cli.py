"""``crawler2-analytics``: P13 offline exports, reports, graph and clustering.

``export`` scans the crawler2 keyspace (read-only, token ranges) into a
validated Parquet revision under ``<root>/exports/<export_key>/``; re-running
the same scope is ``unchanged`` unless the source changed, which publishes the
next revision. ``validate`` re-checks a published revision. ``report``,
``sql``, ``graph`` and ``cluster`` read a revision only — never a live store.

EXPORT arguments accept an export key, an export-key directory (follows
CURRENT) or a revision directory.

Exit codes: 0 ok · 1 not found / usage · 2 validation failed · 3 dataset
unavailable / report incomplete with ``--require-complete`` · 4 export failed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from crawler2.core.configuration import Settings
from crawler2.core.observability import configure_logging

EXIT_NOT_FOUND, EXIT_INVALID, EXIT_UNAVAILABLE, EXIT_FAILED = 1, 2, 3, 4


def _instant(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _root(args: argparse.Namespace, settings: Settings) -> Path:
    return Path(args.root) if args.root else settings.analytics.root


def _revision(args: argparse.Namespace, settings: Settings) -> Path:
    from crawler2.analytics.export import resolve_export

    path = Path(args.export)
    if not path.exists():
        path = _root(args, settings) / "exports" / args.export
    return resolve_export(path, args.revision)


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    from crawler2.analytics.export import Exporter, ExportError, ExportOptions
    from crawler2.analytics.source import ScyllaRowSource
    from crawler2.storage.scylla import ScyllaSession

    cfg = settings.analytics
    keyspace = args.keyspace or settings.scylla.keyspace
    options = ExportOptions(
        segments=args.segments or cfg.segments,
        max_retries=cfg.max_retries,
        retry_backoff_s=cfg.retry_backoff_s,
        duckdb_memory_limit=cfg.duckdb_memory_limit,
        duckdb_threads=cfg.duckdb_threads,
        verify_counts=args.verify_counts,
    )
    with ScyllaSession.connect(settings.scylla, keyspace=keyspace) as session:
        exporter = Exporter(
            ScyllaRowSource(session, keyspace=keyspace),
            _root(args, settings),
            options=options,
            code_version=_version(),
        )
        try:
            result = exporter.export(
                until=_instant(args.until) if args.until else None,
                since=_instant(args.since) if args.since else None,
                datasets=args.dataset,
                resume=args.resume,
            )
        except ExportError as exc:
            print(f"export failed (nothing published): {exc}", file=sys.stderr)
            return EXIT_FAILED
    datasets = result.manifest.get("datasets", {})
    _print(
        {
            "outcome": result.outcome,
            "run_id": result.run_id,
            "export_key": result.export_key,
            "revision": result.revision,
            "path": str(result.revision_dir),
            "content_id": result.manifest.get("content_id"),
            "datasets": {
                n: {
                    "status": d["status"],
                    "rows": d.get("rows"),
                    "rejected": d.get("counts", {}).get("rejected"),
                }
                for n, d in sorted(datasets.items())
            },
        }
    )
    return 0


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("crawler2")
    except PackageNotFoundError:
        return "unknown"


def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    from crawler2.analytics.export import current_revision, list_revisions, read_manifest

    root = _root(args, settings)
    exports = []
    for d in sorted((root / "exports").glob("*")):
        cur = current_revision(d)
        scope = read_manifest(d / f"rev-{cur:04d}")["scope"] if cur else None
        exports.append(
            {
                "export_key": d.name,
                "current": cur,
                "revisions": list_revisions(d),
                "scope": scope,
                "locked": (d / ".publish.lock").exists(),
            }
        )
    staging = []
    for d in sorted((root / "_staging").glob("*")):
        state_path = d / "run.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        staging.append(
            {
                "run_id": d.name,
                "status": state.get("status", "unknown"),
                "error": state.get("error"),
                "export_key": state.get("export_key"),
            }
        )
    _print({"root": str(root), "exports": exports, "unpublished_runs": staging})
    return 0


def cmd_validate(args: argparse.Namespace, settings: Settings) -> int:
    from crawler2.analytics.query import validate_revision

    result = validate_revision(_revision(args, settings), checksums=not args.quick)
    _print(result)
    return 0 if result["ok"] else EXIT_INVALID


def cmd_datasets(args: argparse.Namespace, settings: Settings) -> int:
    from crawler2.analytics.datasets import EXCLUDED, SOURCES

    _print(
        {
            "datasets": {
                d.name: {
                    "source_table": s.table,
                    "schema_version": d.schema_version,
                    "key": list(d.key),
                    "time_column": d.time_column,
                    "description": d.description,
                }
                for s in SOURCES
                for d in s.datasets
            },
            "excluded": EXCLUDED,
        }
    )
    return 0


def cmd_sql(args: argparse.Namespace, settings: Settings) -> int:
    from crawler2.analytics.query import ExportView

    query = Path(args.file).read_text() if args.file else args.query
    if not query:
        print("give a query or --file", file=sys.stderr)
        return EXIT_NOT_FOUND
    import duckdb

    with ExportView(_revision(args, settings)) as view:
        try:
            rows = view.rows(query)
        except duckdb.Error as exc:
            print(f"query failed: {exc}", file=sys.stderr)
            return EXIT_INVALID
    if args.format == "json":
        _print(rows)
    else:
        if rows:
            cols = list(rows[0])
            print("\t".join(cols))
            for r in rows:
                print("\t".join("" if r[c] is None else str(r[c]) for c in cols))
    return 0


def cmd_report(args: argparse.Namespace, settings: Settings) -> int:
    from crawler2.analytics.query import DatasetUnavailableError, ExportView
    from crawler2.analytics.reports import campaign_report, render_markdown, source_report

    with ExportView(_revision(args, settings), verify_checksums=args.verify) as view:
        try:
            if args.kind == "campaign":
                report = campaign_report(view, args.campaign)
            else:
                report = source_report(view, site=args.site)
        except DatasetUnavailableError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_UNAVAILABLE
        except LookupError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_NOT_FOUND
    text = (
        render_markdown(report)
        if args.format == "markdown"
        else json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"{args.out}: complete={report['complete']} digest={report['report_digest']}")
    else:
        sys.stdout.write(text)
    if args.require_complete and not report["complete"]:
        return EXIT_UNAVAILABLE
    return 0


def cmd_graph(args: argparse.Namespace, settings: Settings) -> int:
    from crawler2.analytics.export import ExportError
    from crawler2.analytics.graph import build_graph
    from crawler2.analytics.query import ExportView

    with ExportView(_revision(args, settings)) as view:
        try:
            result = build_graph(view, _root(args, settings), replace=args.replace)
        except ExportError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_INVALID
    _print({k: result[k] for k in ("outcome", "path", "stats", "checks", "skipped_edge_types")})
    return 0


def cmd_cluster(args: argparse.Namespace, settings: Settings) -> int:
    from crawler2.analytics.cluster import ClusterParams, run_clustering
    from crawler2.analytics.export import ExportError
    from crawler2.analytics.query import DatasetUnavailableError, ExportView

    params = ClusterParams(
        min_pages=args.min_pages,
        min_similarity=args.min_similarity,
        min_samples=args.min_samples,
        max_sites=args.max_sites,
    )
    with ExportView(_revision(args, settings)) as view:
        try:
            result = run_clustering(view, _root(args, settings), params)
        except DatasetUnavailableError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_UNAVAILABLE
        except ExportError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_INVALID
    _print({k: result[k] for k in ("outcome", "path", "counts", "params", "bootstrap_status")})
    return 0


def cmd_gc(args: argparse.Namespace, settings: Settings) -> int:
    """Remove unpublished run directories (failed or abandoned); never a published revision."""
    root = _root(args, settings)
    removed, kept = [], []
    horizon = time.time() - args.older_than_hours * 3600
    for d in sorted((root / "_staging").glob("*")):
        state_path = d / "run.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        status = state.get("status", "unknown")
        old = d.stat().st_mtime < horizon
        if (status == "failed" or (status in ("running", "unknown") and args.force)) and old:
            if not args.dry_run:
                shutil.rmtree(d)
            removed.append({"run_id": d.name, "status": status})
        else:
            kept.append({"run_id": d.name, "status": status})
    _print({"removed": removed, "kept": kept, "dry_run": args.dry_run})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crawler2-analytics", description=__doc__)
    parser.add_argument("--root", default=None, help="analytics root (CRAWLER2_ANALYTICS__ROOT)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("export", help="durable store -> validated Parquet revision")
    p.add_argument("--keyspace", default=None, help="default: CRAWLER2_SCYLLA__KEYSPACE")
    p.add_argument("--since", default=None, help="inclusive ISO-8601 instant (UTC if naive)")
    p.add_argument("--until", default=None, help="exclusive ISO-8601 boundary (default: now)")
    p.add_argument("--dataset", action="append", default=None, help="repeatable; default all")
    p.add_argument("--segments", type=int, default=None)
    p.add_argument("--verify-counts", action="store_true", help="re-count each segment after")
    p.add_argument("--resume", default=None, metavar="RUN_ID", help="continue a failed run")
    p.set_defaults(func=cmd_export)

    sub.add_parser("status", help="exports, revisions and unpublished runs").set_defaults(
        func=cmd_status
    )
    sub.add_parser("datasets", help="the dataset catalog and excluded tables").set_defaults(
        func=cmd_datasets
    )

    def export_arg(q: argparse.ArgumentParser) -> None:
        q.add_argument("export", help="export key, export directory or revision directory")
        q.add_argument("--revision", type=int, default=None, help="default: CURRENT")

    p = sub.add_parser("validate", help="re-check a published revision against its manifest")
    export_arg(p)
    p.add_argument("--quick", action="store_true", help="skip file SHA-256")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("sql", help="run DuckDB SQL over a revision's datasets (views)")
    export_arg(p)
    p.add_argument("query", nargs="?", default=None)
    p.add_argument("--file", default=None)
    p.add_argument("--format", choices=("tsv", "json"), default="tsv")
    p.set_defaults(func=cmd_sql)

    report = sub.add_parser("report", help="standard reports from Parquet only").add_subparsers(
        dest="kind", required=True
    )
    for kind in ("campaign", "source"):
        p = report.add_parser(kind)
        export_arg(p)
        if kind == "campaign":
            p.add_argument("--campaign", required=True)
        else:
            p.add_argument("--site", default=None, help="one registrable domain")
        p.add_argument("--format", choices=("json", "markdown"), default="json")
        p.add_argument("--out", default=None)
        p.add_argument("--verify", action="store_true", help="check file SHA-256 first")
        p.add_argument("--require-complete", action="store_true", help="exit 3 if incomplete")
        p.set_defaults(func=cmd_report)

    graph = sub.add_parser("graph", help="offline derived graph").add_subparsers(
        dest="action", required=True
    )
    p = graph.add_parser("build")
    export_arg(p)
    p.add_argument("--replace", action="store_true", help="rebuild if the content differs")
    p.set_defaults(func=cmd_graph)

    cluster = sub.add_parser("cluster", help="site/template clustering prototype").add_subparsers(
        dest="action", required=True
    )
    p = cluster.add_parser("run")
    export_arg(p)
    p.add_argument("--min-pages", type=int, default=3)
    p.add_argument("--min-similarity", type=float, default=0.6)
    p.add_argument("--min-samples", type=int, default=3)
    p.add_argument("--max-sites", type=int, default=2000)
    p.set_defaults(func=cmd_cluster)

    p = sub.add_parser("gc", help="remove failed/abandoned unpublished runs (never revisions)")
    p.add_argument("--older-than-hours", type=float, default=24.0)
    p.add_argument("--force", action="store_true", help="also remove runs still marked running")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_gc)

    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings, stream=sys.stderr)  # stdout carries data (JSON, TSV)
    from crawler2.analytics.export import ExportError

    try:
        result: int = args.func(args, settings)
    except (FileNotFoundError, ExportError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NOT_FOUND
    return result


if __name__ == "__main__":
    sys.exit(main())
