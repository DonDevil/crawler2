"""Site/template clustering prototype (plan feature 9; design §10).

Offline and review-only: the output is a set of *unreviewed* bootstrap
candidates for P7 source profiles. Nothing here reads or writes a P7 profile,
crawl priority, admission decision or feedback state.

1. **Pages** — the latest extract per URL of each site (``page_extracts``),
   with its anchor-link and media-reference aggregates. Only persisted P5 facts
   are used: URL, ``og_type``, ``lang``, text/link/media counts, the structural
   hash. No body or visible text.
2. **Template groups** (within a site) — pages sharing the P7 URL template
   (``intelligence.what.tokenize_url``) with their structural-hash agreement.
3. **Site features** (``site-features/v1``) — a set of tokens per site: frequent
   URL templates, dominant ``og_type`` / ``lang``, media kinds and discovery
   methods, and quantized medians/shares. A site with fewer than ``min_pages``
   pages is ``insufficient_data`` and never clustered.
4. **Clustering** (``dbscan-jaccard/v1``) — DBSCAN over Jaccard similarity of
   the token sets, deterministic (sites in sorted order, no randomness). A site
   that is neither core nor within ``min_similarity`` of a core site is
   ``noise``: nothing is forced into a cluster.

Cluster ids are content-derived (``c-`` + SHA-256 of the sorted members), so the
same membership always gets the same id. Shared features describe *template
similarity*, never common ownership.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import statistics
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from crawler2.analytics.export import (
    MANIFEST,
    ExportError,
    _write_json,
    canonical,
    revision_name,
    sha256_file,
    sql_str,
)
from crawler2.analytics.query import ExportView
from crawler2.intelligence.what import tokenize_url

FEATURE_SET = "site-features/v1"
ALGORITHM = "dbscan-jaccard/v1"
CLUSTER_FORMAT = "crawler2.analytics.cluster/v1"


@dataclass(frozen=True, slots=True)
class ClusterParams:
    min_pages: int = 3
    """Pages (distinct URLs with an extract) a site needs to be clustered."""
    min_similarity: float = 0.6
    """Jaccard similarity at which two sites are neighbours (DBSCAN eps = 1 - this)."""
    min_samples: int = 3
    """Sites (itself included) within ``min_similarity`` for a site to be a core point."""
    template_min_share: float = 0.1
    """A URL template becomes a feature when it covers this share (and >= 2) of a site's pages."""
    max_sites: int = 2000
    """Upper bound on clustered sites (O(n^2) similarity); more is refused, not sampled."""

    def __post_init__(self) -> None:
        if self.min_pages < 1 or self.min_samples < 2:
            raise ValueError("min_pages must be >= 1 and min_samples >= 2")
        if not 0 < self.min_similarity <= 1 or not 0 < self.template_min_share <= 1:
            raise ValueError("min_similarity and template_min_share must be in (0, 1]")

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical(asdict(self))).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class PageFeatures:
    site: str
    url: str
    og_type: str | None
    lang: str | None
    text_chars: int | None
    anchors: int
    same_site_anchors: int
    media_kinds: tuple[str, ...]
    media_methods: tuple[str, ...]
    structural: str | None


@dataclass(slots=True)
class SiteFeatures:
    site: str
    pages: int
    tokens: frozenset[str] = frozenset()
    templates: dict[str, int] = field(default_factory=dict)
    insufficient: bool = False


PAGES_SQL = """
WITH latest AS (
  SELECT * FROM (
    SELECT *, row_number() OVER (PARTITION BY url_id ORDER BY observed_at DESC NULLS LAST,
                                 page_version_id) AS rn
    FROM page_extracts WHERE site IS NOT NULL) WHERE rn = 1
), links AS (
  SELECT page_version_id, count(*) AS anchors,
         count(*) FILTER (WHERE target_site = site) AS same_site
  FROM page_links WHERE relation = 'anchor' GROUP BY 1
), media AS (
  SELECT page_version_id, list_sort(list_distinct(list(kind))) AS kinds,
         list_sort(list_distinct(list(method))) AS methods
  FROM page_media_refs WHERE kind IS NOT NULL GROUP BY 1
)
SELECT l.site, l.url, l.og_type, l.lang, l.text_chars, coalesce(k.anchors, 0) AS anchors,
       coalesce(k.same_site, 0) AS same_site, coalesce(m.kinds, []) AS kinds,
       coalesce(m.methods, []) AS methods, l.hash_structural
FROM latest l LEFT JOIN links k USING (page_version_id) LEFT JOIN media m USING (page_version_id)
ORDER BY l.site, l.url
"""


def load_pages(view: ExportView) -> list[PageFeatures]:
    view.require("page_extracts", "page_links", "page_media_refs")
    return [
        PageFeatures(
            site=r[0],
            url=r[1],
            og_type=r[2],
            lang=r[3],
            text_chars=r[4],
            anchors=int(r[5]),
            same_site_anchors=int(r[6]),
            media_kinds=tuple(r[7]),
            media_methods=tuple(r[8]),
            structural=r[9],
        )
        for r in view.con.execute(PAGES_SQL).fetchall()
    ]


def _bucket(value: float) -> str:
    """Coarse log2 bucket of a non-negative count."""
    return "0" if value <= 0 else f"2^{int(math.log2(value))}"


def _share(part: int, whole: int) -> str:
    """Quarter bucket of a share: 0, q1 (<25 %), q2, q3, q4 (>= 75 %)."""
    if whole == 0 or part == 0:
        return "0"
    return f"q{min(4, int(4 * part / whole) + 1)}"


def site_features(pages: Sequence[PageFeatures], params: ClusterParams) -> SiteFeatures:
    site = pages[0].site
    n = len(pages)
    templates: Counter[str] = Counter()
    for p in pages:
        shape = tokenize_url(p.url)
        templates[shape.template if shape else "{unparsable}"] += 1
    feature = SiteFeatures(site=site, pages=n, templates=dict(sorted(templates.items())))
    if n < params.min_pages:
        feature.insufficient = True
        return feature
    tokens: set[str] = set()
    floor = max(2, math.ceil(params.template_min_share * n))
    tokens |= {f"tpl:{t}" for t, c in templates.items() if c >= floor}
    for name, values in (
        ("og", [p.og_type for p in pages]),
        ("lang", [(p.lang or "").split("-")[0].lower() or None for p in pages]),
    ):
        counts = Counter(v for v in values if v)
        if counts:
            top, c = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            if c * 5 >= n:  # dominant: on >= 20 % of the pages
                tokens.add(f"{name}:{top}")
    kinds = Counter(k for p in pages for k in p.media_kinds)
    methods = Counter(m for p in pages for m in p.media_methods)
    tokens |= {f"mkind:{k}" for k, c in kinds.items() if c * 10 >= n}
    tokens |= {f"mmethod:{m}" for m, c in methods.items() if c * 10 >= n}
    tokens.add(f"text:{_bucket(statistics.median(p.text_chars or 0 for p in pages))}")
    tokens.add(f"anchors:{_bucket(statistics.median(p.anchors for p in pages))}")
    tokens.add(f"media_pages:{_share(sum(1 for p in pages if p.media_kinds), n)}")
    anchors = sum(p.anchors for p in pages)
    tokens.add(f"internal_links:{_share(sum(p.same_site_anchors for p in pages), anchors)}")
    structural = Counter(p.structural for p in pages if p.structural)
    top_struct = max(structural.values(), default=0)
    tokens.add(f"struct_reuse:{_share(top_struct, n)}")
    feature.tokens = frozenset(tokens)
    return feature


def template_groups(pages: Sequence[PageFeatures]) -> list[dict[str, Any]]:
    """Per-site URL template groups with their structural-hash agreement."""
    groups: dict[tuple[str, str], list[PageFeatures]] = defaultdict(list)
    per_site = Counter(p.site for p in pages)
    for p in pages:
        shape = tokenize_url(p.url)
        groups[(p.site, shape.template if shape else "{unparsable}")].append(p)
    out = []
    for (site, template), members in sorted(groups.items()):
        hashes = Counter(m.structural for m in members if m.structural)
        dominant = max(hashes.values(), default=0)
        out.append(
            {
                "site": site,
                "url_template": template,
                "pages": len(members),
                "site_share": round(len(members) / per_site[site], 6),
                "distinct_structural_hashes": len(hashes),
                "dominant_structural_share": round(dominant / len(members), 6),
            }
        )
    return out


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = len(a | b)
    return 0.0 if union == 0 else len(a & b) / union


def dbscan(sites: Sequence[SiteFeatures], params: ClusterParams) -> dict[str, Any]:
    """Deterministic DBSCAN over Jaccard similarity; returns per-site assignments."""
    n = len(sites)
    sim = [[0.0] * n for _ in range(n)]
    neighbours: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        sim[i][i] = 1.0
        for j in range(i + 1, n):
            s = jaccard(sites[i].tokens, sites[j].tokens)
            sim[i][j] = sim[j][i] = s
            if s >= params.min_similarity:
                neighbours[i].append(j)
                neighbours[j].append(i)
    core = [len(neighbours[i]) + 1 >= params.min_samples for i in range(n)]
    label = [-1] * n
    next_label = 0
    for i in range(n):
        if label[i] != -1 or not core[i]:
            continue
        label[i] = next_label
        queue = list(neighbours[i])
        while queue:
            j = queue.pop(0)
            if label[j] == -1:
                label[j] = next_label
                if core[j]:
                    queue.extend(k for k in neighbours[j] if label[k] == -1)
        next_label += 1
    return {"sim": sim, "core": core, "label": label}


def cluster_sites(
    pages: Sequence[PageFeatures], params: ClusterParams
) -> tuple[list[SiteFeatures], list[dict[str, Any]], list[dict[str, Any]]]:
    by_site: dict[str, list[PageFeatures]] = defaultdict(list)
    for p in pages:
        by_site[p.site].append(p)
    features = [site_features(by_site[s], params) for s in sorted(by_site)]
    eligible = [f for f in features if not f.insufficient]
    if len(eligible) > params.max_sites:
        raise ExportError(
            f"{len(eligible)} sites exceed max_sites={params.max_sites} for the O(n^2) "
            "prototype; raise the limit explicitly (nothing is sampled)"
        )
    result = dbscan(eligible, params)
    members: dict[int, list[int]] = defaultdict(list)
    for i, lab in enumerate(result["label"]):
        if lab >= 0:
            members[lab].append(i)
    cluster_id = {
        lab: "c-"
        + hashlib.sha256("\n".join(eligible[i].site for i in idx).encode()).hexdigest()[:12]
        for lab, idx in members.items()
    }
    assignments = []
    for f in features:
        if f.insufficient:
            assignments.append(
                {
                    "site": f.site,
                    "status": "insufficient_data",
                    "cluster_id": None,
                    "core": False,
                    "pages": f.pages,
                    "feature_count": 0,
                    "nearest_site": None,
                    "nearest_similarity": None,
                }
            )
    for i, f in enumerate(eligible):
        others = [(result["sim"][i][j], eligible[j].site) for j in range(len(eligible)) if j != i]
        # nearest neighbour: highest similarity, ties broken by site name
        best = min(others, key=lambda t: (-t[0], t[1]), default=(None, None))
        lab = result["label"][i]
        assignments.append(
            {
                "site": f.site,
                "status": "assigned" if lab >= 0 else "noise",
                "cluster_id": cluster_id.get(lab),
                "core": bool(result["core"][i]),
                "pages": f.pages,
                "feature_count": len(f.tokens),
                "nearest_site": best[1],
                "nearest_similarity": None if best[0] is None else round(best[0], 6),
            }
        )
    assignments.sort(key=lambda a: str(a["site"]))
    clusters = []
    for lab, idx in sorted(members.items(), key=lambda kv: cluster_id[kv[0]]):
        pair_sims = [result["sim"][a][b] for x, a in enumerate(idx) for b in idx[x + 1 :]]
        token_counts = Counter(t for i in idx for t in eligible[i].tokens)
        clusters.append(
            {
                "cluster_id": cluster_id[lab],
                "size": len(idx),
                "members": [eligible[i].site for i in idx],
                "shared_features": sorted(
                    t for t, c in token_counts.items() if c * 5 >= 4 * len(idx)
                ),
                "mean_similarity": round(statistics.fmean(pair_sims), 6) if pair_sims else None,
                "min_similarity": round(min(pair_sims), 6) if pair_sims else None,
            }
        )
    return features, assignments, clusters


def _write_parquet(
    view: ExportView, path: Path, rows: Iterable[dict[str, Any]], schema: str
) -> None:
    """Small tables via DuckDB (JSON staging keeps the types explicit)."""
    staged = path.with_suffix(".jsonl")
    with staged.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    view.con.execute(
        f"COPY (SELECT * FROM read_json({sql_str(str(staged))}, format = 'newline_delimited', "
        f"columns = {schema})) TO {sql_str(str(path))} (FORMAT PARQUET, COMPRESSION zstd)"
    )
    staged.unlink()


def _schema(cols: Sequence[tuple[str, str]]) -> str:
    return "{" + ", ".join(f"{sql_str(n)}: {sql_str(t)}" for n, t in cols) + "}"


ASSIGNMENT_COLUMNS = (
    ("site", "VARCHAR"),
    ("status", "VARCHAR"),
    ("cluster_id", "VARCHAR"),
    ("core", "BOOLEAN"),
    ("pages", "BIGINT"),
    ("feature_count", "BIGINT"),
    ("nearest_site", "VARCHAR"),
    ("nearest_similarity", "DOUBLE"),
)
CLUSTER_COLUMNS = (
    ("cluster_id", "VARCHAR"),
    ("size", "BIGINT"),
    ("members", "VARCHAR[]"),
    ("shared_features", "VARCHAR[]"),
    ("mean_similarity", "DOUBLE"),
    ("min_similarity", "DOUBLE"),
)
FEATURE_COLUMNS = (
    ("site", "VARCHAR"),
    ("pages", "BIGINT"),
    ("insufficient_data", "BOOLEAN"),
    ("features", "VARCHAR[]"),
)
TEMPLATE_COLUMNS = (
    ("site", "VARCHAR"),
    ("url_template", "VARCHAR"),
    ("pages", "BIGINT"),
    ("site_share", "DOUBLE"),
    ("distinct_structural_hashes", "BIGINT"),
    ("dominant_structural_share", "DOUBLE"),
)


def run_clustering(
    view: ExportView, root: Path, params: ClusterParams | None = None
) -> dict[str, Any]:
    params = params or ClusterParams()
    m = view.manifest
    pages = load_pages(view)
    features, assignments, clusters = cluster_sites(pages, params)
    groups = template_groups(pages)
    body = {
        "features": [
            {
                "site": f.site,
                "pages": f.pages,
                "insufficient_data": f.insufficient,
                "features": sorted(f.tokens),
            }
            for f in features
        ],
        "assignments": assignments,
        "clusters": clusters,
        "template_groups": groups,
    }
    digest = hashlib.sha256(canonical(body)).hexdigest()
    name = f"cluster-v1-{params.digest}"
    out = root / "derived" / m["export_key"] / revision_name(m["revision"]) / name
    if (out / MANIFEST).exists():
        existing: dict[str, Any] = json.loads((out / MANIFEST).read_text())
        if existing["content_digest"] == digest:
            return existing | {"outcome": "unchanged", "path": str(out)}
        raise ExportError(f"{out} exists with different content; remove it to rebuild")
    status = Counter(a["status"] for a in assignments)
    manifest: dict[str, Any] = {
        "format": CLUSTER_FORMAT,
        "run_id": f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
        "generated_at": datetime.now(UTC).isoformat(),
        "export": view.ref,
        "feature_set": FEATURE_SET,
        "algorithm": ALGORITHM,
        "params": asdict(params),
        "deterministic": "no randomness; sites processed in sorted order (no seed needed)",
        "content_digest": digest,
        "counts": {
            "pages": len(pages),
            "sites": len(features),
            "by_status": dict(sorted(status.items())),
            "clusters": len(clusters),
            "template_groups": len(groups),
        },
        "feature_provenance": {
            "page_extracts": "latest extract per URL: url (P7 tokenize_url template), og_type, "
            "lang, text_chars, hash_structural",
            "page_links": "anchor relation counts, same-site share",
            "page_media_refs": "media kinds and discovery methods",
        },
        "bootstrap_status": "unreviewed",
        "review_required": "a reviewer must accept or reject each cluster before any P7 "
        "workflow may use it; P13 never imports it",
        "disclaimer": "clusters group sites by similar URL templates and page structure; "
        "membership is not evidence of common ownership, operation or infringement",
    }
    tmp = out.with_name(f".{name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.mkdir(parents=True)
    tables = (
        ("site_features.parquet", body["features"], FEATURE_COLUMNS),
        ("site_clusters.parquet", assignments, ASSIGNMENT_COLUMNS),
        ("clusters.parquet", clusters, CLUSTER_COLUMNS),
        ("template_groups.parquet", groups, TEMPLATE_COLUMNS),
    )
    files = []
    for fname, rows, cols in tables:
        _write_parquet(view, tmp / fname, rows, _schema(cols))
        path = tmp / fname
        files.append({"path": fname, "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    bootstrap = {
        "format": "crawler2.analytics.p7-bootstrap-candidates/v1",
        "status": "unreviewed",
        "export": {k: view.ref[k] for k in ("export_key", "revision", "content_id")},
        "feature_set": FEATURE_SET,
        "algorithm": ALGORITHM,
        "params": asdict(params),
        "clusters": [
            c
            | {
                "templates": {
                    s: [g["url_template"] for g in groups if g["site"] == s and g["pages"] >= 2]
                    for s in c["members"]
                },
                "review": {"decision": None, "reviewer": None, "note": None},
            }
            for c in clusters
        ],
        "disclaimer": manifest["disclaimer"],
    }
    _write_json(tmp / "bootstrap-candidates.json", bootstrap)
    files.append(
        {
            "path": "bootstrap-candidates.json",
            "bytes": (tmp / "bootstrap-candidates.json").stat().st_size,
            "sha256": sha256_file(tmp / "bootstrap-candidates.json"),
        }
    )
    manifest["files"] = files
    _write_json(tmp / MANIFEST, manifest)
    if out.exists():
        shutil.rmtree(out)
    os.rename(tmp, out)
    return manifest | {"outcome": "built", "path": str(out)}
