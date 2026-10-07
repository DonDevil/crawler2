"""Export the M1 history (keyspace ``crawler2_m1``) to a compact, local dataset.

The export is the P7 audit and replay input. It reads the crawler2 fact
tables that carry the same records as the events P7 consumes:

=====================  ================================  ======================
file                   source table                      event equivalent
=====================  ================================  ======================
attempts.jsonl.gz      ``fetch_attempts`` (doc)          ``fetch.completed``
observations.jsonl.gz  ``page_observations`` (doc)       ``page.observed``
extracts.jsonl.gz      ``page_extracts`` (doc)           ``urls.discovered`` /
                                                         ``media.discovered``
revisions.jsonl.gz     ``page_revisions_by_url``         ``page.changed``
admissions.jsonl.gz    ``url_admission`` (P6 F5)         —
search.jsonl.gz        ``search_results_by_query_day``   —
seeds.jsonl.gz /       ``seeds_by_source`` /             —
scope.jsonl.gz         ``scope_sites``
=====================  ================================  ======================

Read-only. Output goes to ``var/p7/m1-export`` (git-ignored); it contains
URLs and titles but no page bodies. Usage::

    env/bin/python benchmarks/p7-intelligence/export_m1.py --host <scylla-ip>
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from cassandra.cluster import Cluster, Session
from cassandra.query import SimpleStatement

OUT = Path(os.environ.get("P7_EXPORT", "var/p7/m1-export"))


def _rows(session: Session, cql: str, fetch_size: int = 1000) -> Iterator[Any]:
    stmt = SimpleStatement(cql, fetch_size=fetch_size)
    yield from session.execute(stmt, timeout=300)


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat() + "Z"


def _export(name: str, rows: Iterator[dict[str, Any]]) -> int:
    path = OUT / f"{name}.jsonl.gz"
    tmp = path.with_suffix(".tmp")
    n = 0
    t0 = time.monotonic()
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":"), sort_keys=True))
            fh.write("\n")
            n += 1
            if n % 50000 == 0:
                print(f"  {name}: {n} rows, {time.monotonic() - t0:.0f} s", flush=True)
    tmp.rename(path)
    print(f"{name}: {n} rows in {time.monotonic() - t0:.0f} s", flush=True)
    return n


def attempts(s: Session) -> Iterator[dict[str, Any]]:
    for r in _rows(s, "SELECT doc FROM fetch_attempts"):
        d = json.loads(r.doc)
        yield {
            "id": d["fetch_attempt_id"],
            "url": d["requested"]["url"],
            "url_id": d["requested"]["url_id"],
            "domain_id": d["requested"]["domain_id"],
            "capability": d["capability"],
            "worker": d["worker"],
            "started_at": d["started_at"],
            "finished_at": d["finished_at"],
            "outcome": d["outcome"],
            "detail": d.get("detail"),
            "http_status": d.get("http_status"),
            "final_url": (d.get("final") or {}).get("url"),
            "redirects": len(d.get("redirects") or []),
            "bytes": d.get("bytes_received"),
        }


def observations(s: Session) -> Iterator[dict[str, Any]]:
    for r in _rows(s, "SELECT doc FROM page_observations"):
        d = json.loads(r.doc)
        yield {
            "id": d["observation_id"],
            "attempt_id": d.get("fetch_attempt_id"),
            "url": d["requested"]["url"],
            "url_id": d["requested"]["url_id"],
            "final_url": d["final"]["url"],
            "final_url_id": d["final"]["url_id"],
            "capability": d["capability"],
            "observed_at": d["observed_at"],
            "http_status": d.get("http_status"),
            "content_type": d.get("content_type"),
            "body_size": d.get("body_size"),
            "page_version_id": d.get("page_version_id"),
            "redirects": len(d.get("redirects") or []),
            "validators": d.get("validators"),
        }


def extracts(s: Session) -> Iterator[dict[str, Any]]:
    for r in _rows(s, "SELECT doc, observed_at FROM page_extracts", fetch_size=200):
        d = json.loads(r.doc)
        yield {
            "page_version_id": d["page_version_id"],
            "url": d["page"]["url"],
            "url_id": d["page"]["url_id"],
            "observed_at": _iso(r.observed_at),
            "revision_id": d.get("revision_id"),
            "normalization": d.get("normalization"),
            "hashes": d.get("hashes"),
            "metadata": d.get("metadata"),
            "stats": d.get("stats"),
            "links": [
                [lk["target"]["url"], lk["relation"], lk.get("anchor_text")]
                for lk in d.get("links") or []
            ],
            "media": [
                [
                    m.get("locator", {}).get("url")
                    if isinstance(m.get("locator"), dict)
                    else m.get("locator"),
                    m.get("kind"),
                    m.get("method"),
                ]
                for m in d.get("media") or []
            ],
        }


def revisions(s: Session) -> Iterator[dict[str, Any]]:
    cql = (
        "SELECT url_id, revision_id, normalization, first_seen, first_observation_id, "
        "last_seen, last_observation_id FROM page_revisions_by_url"
    )
    for r in _rows(s, cql, fetch_size=5000):
        yield {
            "url_id": "url_" + r.url_id.hex,
            "revision_id": "pgr_" + r.revision_id.hex,
            "normalization": r.normalization,
            "first_seen": _iso(r.first_seen),
            "first_observation_id": None
            if r.first_observation_id is None
            else "obs_" + r.first_observation_id.hex,
            "last_seen": _iso(r.last_seen),
            "last_observation_id": None
            if r.last_observation_id is None
            else "obs_" + r.last_observation_id.hex,
        }


def admissions(s: Session) -> Iterator[dict[str, Any]]:
    cql = (
        "SELECT url_id, url, last_admitted_at, last_origin, last_priority, last_queue, "
        "last_action, last_decided_at FROM url_admission"
    )
    for r in _rows(s, cql, fetch_size=5000):
        yield {
            "url_id": "url_" + r.url_id.hex,
            "url": r.url,
            "admitted_at": _iso(r.last_admitted_at),
            "origin": r.last_origin,
            "priority": r.last_priority,
            "queue": r.last_queue,
            "action": r.last_action,
            "decided_at": _iso(r.last_decided_at),
        }


def plain(s: Session, table: str) -> Iterator[dict[str, Any]]:
    cql = f"SELECT * FROM {table}"  # noqa: S608 - fixed table names from main()
    for r in _rows(s, cql, fetch_size=1000):
        yield {
            k: (
                _iso(v)
                if hasattr(v, "isoformat")
                else (
                    str(v)
                    if v is not None and not isinstance(v, (int, float, str, bool, list, dict))
                    else v
                )
            )
            for k, v in r._asdict().items()
        }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True, help="Scylla contact point (container IP)")
    ap.add_argument("--keyspace", default="crawler2_m1")
    ap.add_argument("--only", nargs="*", help="export only these files")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    session = Cluster([args.host]).connect(args.keyspace)
    jobs = {
        "attempts": lambda: attempts(session),
        "observations": lambda: observations(session),
        "revisions": lambda: revisions(session),
        "admissions": lambda: admissions(session),
        "search": lambda: plain(session, "search_results_by_query_day"),
        "seeds": lambda: plain(session, "seeds_by_source"),
        "scope": lambda: plain(session, "scope_sites"),
        "extracts": lambda: extracts(session),
    }
    counts = {}
    for name, job in jobs.items():
        if args.only and name not in args.only:
            continue
        counts[name] = _export(name, job())
    (OUT / "export.json").write_text(
        json.dumps(
            {
                "keyspace": args.keyspace,
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "rows": counts,
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
