"""Offline derived graph (design §9), built from one export revision only.

Two views share one node/edge table pair:

- **site ↔ site** (registrable domains): ``site_links_to`` (cross-site links in
  extracts), ``site_redirects_to`` (requested → final URL of an observation),
  ``site_embeds_media_from`` (a page references media hosted on another site),
  ``site_shares_content`` (undirected: both sites' pages carried the same
  identified content). These are observed co-occurrences; none implies common
  ownership or coordination.
- **page → media → content ← match → target**, plus ``evidence`` nodes:
  ``page_has_media`` and ``media_has_content`` (P8 media observations),
  ``match_of_content`` and ``match_for_target`` (the matcher's claim, with its
  verdict and confidence unchanged), ``evidence_of_match`` and
  ``evidence_attributes_page`` (P12 sources, ``status`` = attribution basis).
  A match whose content was never observed on a page has no page edge; it is
  counted, never bridged.

Every edge carries ``weight`` (supporting records), ``source_dataset`` and up to
``SAMPLE_KEYS`` sorted source record keys from which it can be traced; node and
edge ids are content-derived, so rebuilding from the same revision yields the
same ids and the same content digest.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from crawler2.analytics.export import (
    MANIFEST,
    ExportError,
    _write_json,
    content_digest,
    revision_name,
    sha256_file,
    sql_str,
)
from crawler2.analytics.query import ExportView

GRAPH_VERSION = "graph/v1"
GRAPH_FORMAT = "crawler2.analytics.graph/v1"
SAMPLE_KEYS = 5

NODE_COLUMNS = ("node_id", "node_type", "natural_id", "label", "site", "known")
EDGE_COLUMNS = (
    "edge_id",
    "edge_type",
    "src",
    "dst",
    "directed",
    "weight",
    "distinct_pages",
    "status",
    "confidence",
    "first_at",
    "last_at",
    "source_dataset",
    "source_keys",
)


def _edge(
    edge_type: str,
    dataset: str,
    src: str,
    dst: str,
    key: str,
    where: str,
    *,
    at: str | None,
    pages: str | None = None,
    status: str = "NULL",
    confidence: str = "NULL",
    directed: bool = True,
    discriminator: str = "''",
) -> str:
    """One aggregated edge type as SQL over an export view."""
    first, last = (f"min({at})", f"max({at})") if at else ("NULL", "NULL")
    pages_col = f"count(DISTINCT {pages})" if pages else "NULL"
    return f"""
SELECT substr(sha256('{edge_type}|' || src || '|' || dst || '|' || disc), 1, 32) AS edge_id,
       '{edge_type}' AS edge_type, src, dst, {str(directed).lower()} AS directed,
       weight, distinct_pages, status, confidence, first_at, last_at,
       '{dataset}' AS source_dataset, source_keys
FROM (
  SELECT {src} AS src, {dst} AS dst, {discriminator} AS disc,
         count(*) AS weight, {pages_col} AS distinct_pages,
         {status} AS status, {confidence} AS confidence,
         {first} AS first_at, {last} AS last_at,
         list_sort(list_distinct(list({key})))[1:{SAMPLE_KEYS}] AS source_keys
  FROM "{dataset}" WHERE {where}
  GROUP BY 1, 2, 3
)"""


EDGE_SPECS: dict[str, tuple[tuple[str, ...], str]] = {
    "site_links_to": (
        ("page_links",),
        _edge(
            "site_links_to",
            "page_links",
            "'site:' || site",
            "'site:' || target_site",
            "page_version_id || '#' || ordinal",
            "cross_site",
            at="observed_at",
            pages="url_id",
        ),
    ),
    "site_redirects_to": (
        ("page_observations",),
        _edge(
            "site_redirects_to",
            "page_observations",
            "'site:' || site",
            "'site:' || final_site",
            "observation_id",
            "site IS NOT NULL AND final_site IS NOT NULL AND site <> final_site",
            at="observed_at",
            pages="url_id",
        ),
    ),
    "site_embeds_media_from": (
        ("page_media_refs",),
        _edge(
            "site_embeds_media_from",
            "page_media_refs",
            "'site:' || site",
            "'site:' || locator_site",
            "page_version_id || '#' || ordinal",
            "site IS NOT NULL AND locator_site IS NOT NULL AND site <> locator_site",
            at="observed_at",
            pages="url_id",
        ),
    ),
    "page_has_media": (
        ("media_observations",),
        _edge(
            "page_has_media",
            "media_observations",
            "'page:' || page_url_id",
            "'media:' || media_id",
            "page_observation_id",
            "true",
            at="observed_at",
            status="string_agg(DISTINCT probe_status, ',' ORDER BY probe_status)",
        ),
    ),
    "media_has_content": (
        ("media_observations",),
        _edge(
            "media_has_content",
            "media_observations",
            "'media:' || media_id",
            "'content:' || content_id",
            "page_observation_id",
            "content_id IS NOT NULL",
            at="observed_at",
            pages="page_url_id",
        ),
    ),
    "match_of_content": (
        ("matches",),
        _edge(
            "match_of_content",
            "matches",
            "'match:' || match_id",
            "'content:' || content_id",
            "match_id",
            "true",
            at="decided_at",
            status="any_value(verdict)",
            confidence="any_value(confidence)",
        ),
    ),
    "match_for_target": (
        ("matches",),
        _edge(
            "match_for_target",
            "matches",
            "'match:' || match_id",
            "'target:' || target_id",
            "match_id",
            "true",
            at="decided_at",
            status="any_value(verdict)",
            confidence="any_value(confidence)",
        ),
    ),
    "evidence_of_match": (
        ("evidence_candidates",),
        _edge(
            "evidence_of_match",
            "evidence_candidates",
            "'evidence:' || evidence_id",
            "'match:' || match_id",
            "evidence_id",
            "true",
            at="collected_at",
            status="any_value(status)",
        ),
    ),
    "evidence_attributes_page": (
        ("evidence_sources",),
        _edge(
            "evidence_attributes_page",
            "evidence_sources",
            "'evidence:' || evidence_id",
            "'page:' || page_url_id",
            "evidence_id || '#' || ordinal",
            "true",
            at="sighted_at",
            status="string_agg(DISTINCT basis, ',' ORDER BY basis)",
        ),
    ),
}

SHARED_CONTENT_SQL = f"""
WITH s AS (SELECT DISTINCT page_site AS site, content_id FROM media_observations
           WHERE content_id IS NOT NULL AND page_site IS NOT NULL)
SELECT substr(sha256('site_shares_content|' || src || '|' || dst || '|'), 1, 32) AS edge_id,
       'site_shares_content' AS edge_type, src, dst, false AS directed, weight,
       CAST(NULL AS BIGINT) AS distinct_pages, CAST(NULL AS VARCHAR) AS status,
       CAST(NULL AS DOUBLE) AS confidence, CAST(NULL AS TIMESTAMPTZ) AS first_at,
       CAST(NULL AS TIMESTAMPTZ) AS last_at, 'media_observations' AS source_dataset,
       source_keys
FROM (
  SELECT 'site:' || a.site AS src, 'site:' || b.site AS dst, count(*) AS weight,
         list_sort(list(a.content_id))[1:{SAMPLE_KEYS}] AS source_keys
  FROM s a JOIN s b ON a.content_id = b.content_id AND a.site < b.site
  GROUP BY 1, 2
)"""

NODES_SQL = """
WITH ends AS (SELECT src AS node_id FROM _edges UNION SELECT dst FROM _edges),
known AS (
  {known}
)
SELECT e.node_id, split_part(e.node_id, ':', 1) AS node_type,
       substr(e.node_id, length(split_part(e.node_id, ':', 1)) + 2) AS natural_id,
       k.label, k.site, k.node_id IS NOT NULL AS known
FROM ends e LEFT JOIN (SELECT node_id, any_value(label) AS label, any_value(site) AS site
                       FROM known GROUP BY 1) k USING (node_id)
"""

_KNOWN: tuple[tuple[str, str], ...] = (
    ("urls", "SELECT 'page:' || url_id AS node_id, url AS label, site FROM urls"),
    (
        "page_observations",
        "SELECT 'page:' || final_url_id, final_url, final_site FROM page_observations",
    ),
    ("media", "SELECT 'media:' || media_id, locator_url, locator_site FROM media"),
    ("domains", "SELECT DISTINCT 'site:' || site, site, site FROM domains WHERE site IS NOT NULL"),
    (
        "media",
        "SELECT DISTINCT 'site:' || locator_site, locator_site, locator_site FROM media "
        "WHERE locator_site IS NOT NULL",
    ),
    ("targets", "SELECT 'target:' || target_id, title, NULL FROM targets"),
    ("matches", "SELECT 'match:' || match_id, verdict, NULL FROM matches"),
    (
        "evidence_candidates",
        "SELECT 'evidence:' || evidence_id, status, NULL FROM evidence_candidates",
    ),
    (
        "media_observations",
        "SELECT DISTINCT 'content:' || content_id, content_scheme, NULL "
        "FROM media_observations WHERE content_id IS NOT NULL",
    ),
)


def _relation_digest(view: ExportView, relation: str, columns: tuple[str, ...]) -> str:
    return content_digest(view.con, relation, columns)


def build_graph(view: ExportView, root: Path, *, replace: bool = False) -> dict[str, Any]:
    """Build (or confirm unchanged) the graph of ``view``'s revision under ``root``."""
    m = view.manifest
    out = root / "derived" / m["export_key"] / revision_name(m["revision"]) / "graph-v1"
    con = view.con
    parts, skipped = [], {}
    for edge_type, (needs, sql) in EDGE_SPECS.items():
        gone = view.missing(needs)
        if gone:
            skipped[edge_type] = gone
            continue
        parts.append(sql)
    if "media_observations" in view.available:
        parts.append(SHARED_CONTENT_SQL)
    else:
        skipped["site_shares_content"] = view.missing(["media_observations"])
    if not parts:
        raise ExportError("no edge type can be built: the export lacks every graph input")
    union = " UNION ALL ".join(f"SELECT * FROM ({p})" for p in parts)
    con.execute("CREATE OR REPLACE TEMP TABLE _edges AS " + union)
    known = " UNION ALL ".join(sql for ds, sql in _KNOWN if ds in view.available)
    if not known:
        known = "SELECT NULL::VARCHAR AS node_id, NULL::VARCHAR AS label, NULL::VARCHAR AS site"
    con.execute("CREATE OR REPLACE TEMP TABLE _nodes AS " + NODES_SQL.format(known=known))
    checks = {
        "duplicate_edge_ids": view.scalar("SELECT count(*) - count(DISTINCT edge_id) FROM _edges"),
        "null_endpoints": view.scalar(
            "SELECT count(*) FROM _edges WHERE src IS NULL OR dst IS NULL "
            "OR src LIKE '%:' OR dst LIKE '%:'"
        ),
        "dangling_endpoints": view.scalar(
            "SELECT count(*) FROM _edges WHERE src NOT IN (SELECT node_id FROM _nodes) "
            "OR dst NOT IN (SELECT node_id FROM _nodes)"
        ),
        "duplicate_node_ids": view.scalar("SELECT count(*) - count(DISTINCT node_id) FROM _nodes"),
    }
    bad = {k: v for k, v in checks.items() if v}
    if bad:
        raise ExportError(f"graph validation failed: {bad}")
    stats = {
        "nodes_by_type": {
            r["node_type"]: r["n"]
            for r in view.rows("SELECT node_type, count(*) AS n FROM _nodes GROUP BY 1 ORDER BY 1")
        },
        "edges_by_type": {
            r["edge_type"]: r["n"]
            for r in view.rows("SELECT edge_type, count(*) AS n FROM _edges GROUP BY 1 ORDER BY 1")
        },
        "unknown_nodes": view.scalar("SELECT count(*) FROM _nodes WHERE NOT known"),
    }
    if "matches" in view.available:
        stats["matches_without_observed_content"] = (
            view.scalar(
                "SELECT count(*) FROM matches WHERE content_id NOT IN (SELECT content_id FROM "
                "media_observations WHERE content_id IS NOT NULL)"
            )
            if "media_observations" in view.available
            else None
        )
    digest = {
        "nodes": _relation_digest(view, "_nodes", NODE_COLUMNS),
        "edges": _relation_digest(view, "_edges", EDGE_COLUMNS),
    }
    if (out / MANIFEST).exists():
        existing: dict[str, Any] = json.loads((out / MANIFEST).read_text())
        if existing["content_digest"] == digest:
            return existing | {"outcome": "unchanged", "path": str(out)}
        if not replace:
            raise ExportError(
                f"{out} exists with different content (derivation changed?); use --replace"
            )
    tmp = out.with_name(f".graph-v1.{uuid.uuid4().hex[:8]}.tmp")
    tmp.mkdir(parents=True)
    files = []
    for name, rel, order in (("nodes", "_nodes", "node_id"), ("edges", "_edges", "edge_id")):
        path = tmp / f"{name}.parquet"
        con.execute(
            f"COPY (SELECT * FROM {rel} ORDER BY {order}) TO {sql_str(str(path))} "
            "(FORMAT PARQUET, COMPRESSION zstd)"
        )
        files.append({"path": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    manifest = {
        "format": GRAPH_FORMAT,
        "derivation": GRAPH_VERSION,
        "export": view.ref,
        "generated_at": datetime.now(UTC).isoformat(),
        "content_digest": digest,
        "stats": stats,
        "checks": checks,
        "skipped_edge_types": skipped,
        "files": files,
        "semantics": "observed co-occurrence only; an edge is not evidence of common "
        "ownership, coordination or infringement",
    }
    _write_json(tmp / MANIFEST, manifest)
    if out.exists():
        shutil.rmtree(out)
    os.rename(tmp, out)
    return manifest | {"outcome": "built", "path": str(out)}
