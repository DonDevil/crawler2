"""The exportable datasets (design §3): source table → typed analytical rows.

A ``SourceTable`` is scanned once per export; each of its ``Dataset``s maps
every scanned row to zero or more output rows (``page_extracts`` also yields
``page_links`` and ``page_media_refs``, so the largest table is read once).

Rules every mapper follows:

- columns, types and keys are declared here and nowhere else; a mapper returns
  exactly the declared columns (missing optional facts are ``None``);
- identifiers are the contract wire form (``url_<hex>``, ADR-008) so rows trace
  back to the durable record and to events;
- a row whose required facts are missing or malformed raises ``Malformed``: it
  is counted as rejected, never repaired or guessed;
- ``host`` is the URL's lower-cased host; ``site`` its registrable domain
  (eTLD+1, IPs are their own site); both are ``None`` when no URL is known;
- page bodies, visible text, response headers, operator names and free-text
  reasons are never exported (design §11).

Changing a dataset's columns or semantics requires a new ``schema_version``
(design §5); additive columns are a new version too, so a reader never sees
two shapes under one version.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from antipiracy_contracts.models.web import UrlRef

from crawler2.filtering.inputs import registrable_domain

Row = dict[str, Any]
SourceRow = Mapping[str, Any]

VARCHAR, INTEGER, BIGINT, DOUBLE, BOOLEAN = "VARCHAR", "INTEGER", "BIGINT", "DOUBLE", "BOOLEAN"
TIMESTAMP = "TIMESTAMPTZ"
VARCHAR_LIST = "VARCHAR[]"


class Malformed(ValueError):  # noqa: N818 -- a classification, not a crash
    """A source row cannot be mapped; it is counted as rejected (with this reason)."""


@dataclass(frozen=True, slots=True)
class Dataset:
    name: str
    schema_version: int
    columns: tuple[tuple[str, str], ...]
    key: tuple[str, ...]
    """Columns that identify a record; unique within one export."""
    time_column: str | None
    """Controls the ``date`` partition and the export window; ``None`` = a current-state
    dimension, exported whole and unpartitioned."""
    sort: tuple[str, ...]
    """Row order inside each file (site first where known: row-group pruning by domain)."""
    mapper: Callable[[SourceRow], Iterable[Row]]
    description: str
    semantics: str = ""
    fanout: bool = False
    """One source row yields 0..n rows (child datasets of a document)."""

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.columns)


@dataclass(frozen=True, slots=True)
class SourceTable:
    table: str
    partition_key: tuple[str, ...]
    """The table's partition key: token ranges over it segment the scan."""
    columns: tuple[str, ...]
    datasets: tuple[Dataset, ...]
    fetch_size: int = 1000
    note: str = ""


# --- helpers --------------------------------------------------------------------------


def wire(prefix: str, value: Any) -> str | None:
    """Storage uuid (implied prefix) → contract wire id; already-wire strings pass."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return f"{prefix}_{value.hex}"
    text = str(value)
    if text.startswith(f"{prefix}_"):
        return text
    try:
        return f"{prefix}_{uuid.UUID(text).hex}"
    except ValueError as exc:
        raise Malformed(f"not a {prefix} id: {text[:80]}") from exc


def ts(value: Any) -> str | None:
    """Driver datetime (naive UTC) or ISO string → ISO-8601 UTC string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return moment.isoformat()
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise Malformed(f"not a timestamp: {str(value)[:80]}") from exc
    return ts(moment)


def soft_ts(value: Any) -> str | None:
    """Like ``ts`` for page-declared dates: unparsable values become ``None``."""
    try:
        return ts(value)
    except Malformed:
        return None


def parse_ts(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host.rstrip(".").lower() or None


def site_of(host: str | None) -> str | None:
    return None if host is None else registrable_domain(host)


def _doc(row: SourceRow, column: str = "doc") -> dict[str, Any]:
    raw = row.get(column)
    if raw is None:
        raise Malformed(f"{column} is null")
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise Malformed(f"{column} is not JSON") from exc
    if not isinstance(doc, dict):
        raise Malformed(f"{column} is not an object")
    return doc


def _req(doc: Mapping[str, Any], *path: str) -> Any:
    cur: Any = doc
    for part in path:
        if not isinstance(cur, Mapping) or cur.get(part) is None:
            raise Malformed(f"missing {'.'.join(path)}")
        cur = cur[part]
    return cur


def _opt(doc: Mapping[str, Any] | None, *path: str) -> Any:
    cur: Any = doc
    for part in path:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(part)
    return cur


def _int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise Malformed(f"not an integer: {value!r}"[:80])
    try:
        return int(value)
    except ValueError as exc:
        raise Malformed(f"not an integer: {value!r}"[:80]) from exc


def _url_id(url: str) -> str:
    """The contract id of an already canonical URL (P12 sources carry the URL only)."""
    try:
        return str(UrlRef.of(url).url_id)
    except ValueError as exc:
        raise Malformed(f"not a canonical URL: {url[:80]}") from exc


def _url_cols(prefix: str, url: str | None) -> Row:
    host = host_of(url)
    return {f"{prefix}host": host, f"{prefix}site": site_of(host)}


# --- mappers --------------------------------------------------------------------------


def _fetch_attempt(row: SourceRow) -> Iterable[Row]:
    d = _doc(row)
    url = _req(d, "requested", "url")
    final = d.get("final") or {}
    yield {
        "fetch_attempt_id": _req(d, "fetch_attempt_id"),
        "url_id": _req(d, "requested", "url_id"),
        "url": url,
        "domain_id": _req(d, "requested", "domain_id"),
        **_url_cols("", url),
        "capability": _req(d, "capability"),
        "worker": d.get("worker"),
        "started_at": ts(d.get("started_at")),
        "finished_at": ts(_req(d, "finished_at")),
        "outcome": _req(d, "outcome"),
        "http_status": _int(d.get("http_status")),
        "final_url_id": final.get("url_id"),
        "final_url": final.get("url"),
        "redirect_count": len(d.get("redirects") or ()),
        "bytes_received": _int(d.get("bytes_received")),
        "detail": d.get("detail"),
    }


def _page_observation(row: SourceRow) -> Iterable[Row]:
    d = _doc(row)
    url = _req(d, "requested", "url")
    final_url = _req(d, "final", "url")
    yield {
        "observation_id": _req(d, "observation_id"),
        "fetch_attempt_id": d.get("fetch_attempt_id"),
        "url_id": _req(d, "requested", "url_id"),
        "url": url,
        "domain_id": _req(d, "requested", "domain_id"),
        **_url_cols("", url),
        "final_url_id": _req(d, "final", "url_id"),
        "final_url": final_url,
        **_url_cols("final_", final_url),
        "capability": _req(d, "capability"),
        "observed_at": ts(_req(d, "observed_at")),
        "http_status": _int(_req(d, "http_status")),
        "content_type": d.get("content_type"),
        "body_digest": _req(d, "body_digest"),
        "body_size": _int(d.get("body_size")),
        "page_version_id": _req(d, "page_version_id"),
        "redirect_count": len(d.get("redirects") or ()),
        "redirect_urls": [_req(h, "location", "url") for h in d.get("redirects") or ()],
        "snapshot_uri": _opt(d, "snapshot", "uri"),
    }


def _extract_base(row: SourceRow) -> tuple[dict[str, Any], Row]:
    d = _doc(row)
    url = _req(d, "page", "url")
    base = {
        "page_version_id": _req(d, "page_version_id"),
        "url_id": _req(d, "page", "url_id"),
        **_url_cols("", url),
        "observed_at": ts(row.get("observed_at")),
    }
    return d, base | {"url": url}


def _page_extract(row: SourceRow) -> Iterable[Row]:
    d, base = _extract_base(row)
    hashes = d.get("hashes") or {}
    meta = d.get("metadata") or {}
    stats = d.get("stats") or {}
    yield {
        **base,
        "domain_id": _req(d, "page", "domain_id"),
        "extractor": d.get("extractor"),
        "normalization": d.get("normalization"),
        "revision_id": d.get("revision_id"),
        **{f"hash_{k}": hashes.get(k) for k in _HASHES},
        "title": meta.get("title"),
        "lang": meta.get("lang"),
        "og_type": meta.get("og_type"),
        "published_at": soft_ts(meta.get("published_at")),
        "modified_at": soft_ts(meta.get("modified_at")),
        "text_chars": _int(stats.get("text_chars")),
        "elements": _int(stats.get("elements")),
        "link_count": len(d.get("links") or ()),
        "media_count": len(d.get("media") or ()),
        "truncated": [str(t) for t in stats.get("truncated") or ()],
    }


_HASHES = ("raw", "normalized", "visible_text", "link_set", "media_set", "structural")


def _page_links(row: SourceRow) -> Iterable[Row]:
    d, base = _extract_base(row)
    base.pop("url")
    for i, link in enumerate(d.get("links") or ()):
        target_url = _req(link, "target", "url")
        tgt = _url_cols("target_", target_url)
        yield {
            **base,
            "ordinal": i,
            "target_url": target_url,
            "target_url_id": _req(link, "target", "url_id"),
            "target_domain_id": _opt(link, "target", "domain_id"),
            **tgt,
            "relation": _req(link, "relation"),
            "nofollow": bool(link.get("nofollow", False)),
            "anchor_text": link.get("anchor_text"),
            "cross_site": None
            if base["site"] is None or tgt["target_site"] is None
            else base["site"] != tgt["target_site"],
        }


def _page_media_refs(row: SourceRow) -> Iterable[Row]:
    d, base = _extract_base(row)
    base.pop("url")
    for i, ref in enumerate(d.get("media") or ()):
        locator = _req(ref, "locator", "url")
        yield {
            **base,
            "ordinal": i,
            "locator_url": locator,
            "locator_url_id": _opt(ref, "locator", "url_id"),
            **_url_cols("locator_", locator),
            "kind": ref.get("kind"),
            "method": ref.get("method"),
            "declared_type": ref.get("declared_type"),
        }


def _url_state(row: SourceRow) -> Iterable[Row]:
    url = row.get("url")
    if url is None:
        raise Malformed("url is null")
    yield {
        "url_id": wire("url", row.get("url_id")),
        "url": url,
        "domain_id": wire("dom", row.get("domain_id")),
        **_url_cols("", url),
        "first_seen": ts(row.get("first_seen")),
        "last_observed_at": ts(row.get("last_observed_at")),
        "last_status": _int(row.get("last_status")),
    }


def _domain(row: SourceRow) -> Iterable[Row]:
    host = row.get("host")
    if not host:
        raise Malformed("host is null")
    host = str(host).rstrip(".").lower()
    yield {
        "domain_id": wire("dom", row.get("domain_id")),
        "host": host,
        "site": site_of(host),
        "first_seen": ts(row.get("first_seen")),
    }


def _page_revision(row: SourceRow) -> Iterable[Row]:
    yield {
        "url_id": wire("url", row.get("url_id")),
        "revision_id": wire("pgr", row.get("revision_id")),
        "normalization": row.get("normalization"),
        "normalized_digest": row.get("normalized_digest"),
        "first_seen": ts(row.get("first_seen")),
        "first_observation_id": wire("obs", row.get("first_observation_id")),
        "first_page_version_id": wire("pgv", row.get("first_page_version_id")),
        "last_seen": ts(row.get("last_seen")),
        "last_observation_id": wire("obs", row.get("last_observation_id")),
        "last_page_version_id": wire("pgv", row.get("last_page_version_id")),
    }


def _media(row: SourceRow) -> Iterable[Row]:
    d = _doc(row)
    locator = _req(d, "locator", "url")
    yield {
        "media_id": wire("med", row.get("media_id")),
        "locator_url": locator,
        "locator_url_id": _opt(d, "locator", "url_id"),
        **_url_cols("locator_", locator),
        "kind": row.get("kind") or d.get("kind"),
        "first_seen": ts(row.get("first_seen")),
        "last_seen": ts(row.get("last_seen")),
    }


def _media_observation(row: SourceRow) -> Iterable[Row]:
    d = _doc(row)
    page_url = _req(d, "page", "url")
    locator = _req(d, "media", "locator", "url")
    content = d.get("content") or {}
    hints = d.get("hints") or {}
    duration = hints.get("duration_seconds")
    yield {
        "page_observation_id": _req(d, "page_observation_id"),
        "media_id": _req(d, "media", "media_id"),
        "observed_at": ts(_req(d, "observed_at")),
        "page_url": page_url,
        "page_url_id": _req(d, "page", "url_id"),
        **_url_cols("page_", page_url),
        "page_version_id": _req(d, "page_version_id"),
        "locator_url": locator,
        **_url_cols("locator_", locator),
        "media_kind": _opt(d, "media", "kind"),
        "probe_status": _req(d, "probe_status"),
        "content_id": content.get("content_id"),
        "content_scheme": content.get("scheme"),
        "content_digest": content.get("digest"),
        "hint_media_type": hints.get("media_type"),
        "hint_content_length": _int(hints.get("content_length")),
        "hint_duration_seconds": None if duration is None else float(duration),
    }


def _target(row: SourceRow) -> Iterable[Row]:
    d = _doc(row)
    yield {
        "target_id": _req(d, "ref", "target_id"),
        "version": _int(_req(d, "ref", "version")),
        "title": d.get("title"),
        "kind": d.get("kind"),
        "release_year": _int(d.get("release_year")),
        "registered_at": ts(d.get("registered_at")),
        "retired_at": ts(row.get("retired_at")),
        "retire_reason": row.get("retire_reason"),
    }


def _match(row: SourceRow) -> Iterable[Row]:
    d = _doc(row)
    segments = d.get("segments") or ()
    confidence = _req(d, "confidence")
    yield {
        "content_id": _req(d, "content_id"),
        "match_id": _req(d, "match_id"),
        "target_id": _req(d, "target", "target_id"),
        "target_version": _int(_req(d, "target", "version")),
        "verdict": _req(d, "verdict"),
        "confidence": float(confidence),
        "technique_name": _opt(d, "technique", "name"),
        "technique_version": _opt(d, "technique", "version"),
        "representation_id": d.get("representation_id"),
        "target_representation_id": d.get("target_representation_id"),
        "segment_count": len(segments),
        "aligned_seconds": float(sum(float(s.get("duration_seconds") or 0) for s in segments)),
        "decided_at": ts(_req(d, "decided_at")),
    }


def _candidate_doc(row: SourceRow) -> dict[str, Any]:
    return _doc(row)


def _evidence_candidate(row: SourceRow) -> Iterable[Row]:
    d = _candidate_doc(row)
    validation = row.get("validation")
    outcome = None
    if validation:
        try:
            outcome = json.loads(validation).get("outcome")
        except (TypeError, ValueError, AttributeError) as exc:
            raise Malformed("validation is not a JSON object") from exc
    yield {
        "evidence_id": wire("evd", row.get("evidence_id")),
        "match_id": wire("mat", row.get("match_id")),
        "target_id": wire("tgt", row.get("target_id")),
        "status": row.get("status"),
        "revision": _int(row.get("revision")),
        "collected_at": ts(row.get("collected_at")),
        "updated_at": ts(row.get("updated_at")),
        "validated_at": ts(row.get("validated_at")),
        "validation_outcome": outcome,
        "verdict": _opt(d, "match", "verdict"),
        "confidence": _opt(d, "match", "confidence"),
        "content_id": _opt(d, "match", "content_id"),
        "gap_codes": sorted({str(g.get("code")) for g in d.get("gaps") or ()}),
        "sources_total": _int(d.get("sources_total")),
        "sources_recorded": len(d.get("sources") or ()),
        "media_count": len(d.get("media") or ()),
    }


def _evidence_sources(row: SourceRow) -> Iterable[Row]:
    d = _candidate_doc(row)
    evidence_id = wire("evd", row.get("evidence_id"))
    for i, s in enumerate(d.get("sources") or ()):
        page_url = _req(s, "page_url")
        yield {
            "evidence_id": evidence_id,
            "ordinal": i,
            "match_id": wire("mat", row.get("match_id")),
            "target_id": wire("tgt", row.get("target_id")),
            "collected_at": ts(row.get("collected_at")),
            "page_observation_id": _req(s, "page_observation_id"),
            "media_id": _req(s, "media_id"),
            "page_url": page_url,
            "page_url_id": _url_id(page_url),
            **_url_cols("page_", page_url),
            "page_version_id": s.get("page_version_id"),
            "basis": _req(s, "basis"),
            "observation_present": bool(s.get("observation_present")),
            "sighted_at": ts(s.get("sighted_at")),
            "has_snapshot": s.get("snapshot") is not None,
        }


def _evidence_version(row: SourceRow) -> Iterable[Row]:
    yield {
        "evidence_id": wire("evd", row.get("evidence_id")),
        "version": _int(row.get("version")),
        "status": row.get("status"),
        "manifest_uri": row.get("manifest_uri"),
        "manifest_digest": row.get("manifest_digest"),
        "manifest_size": _int(row.get("manifest_size")),
        "finalized_at": ts(row.get("finalized_at")),
        "supersedes": _int(row.get("supersedes")),
    }


def _campaign(row: SourceRow) -> Iterable[Row]:
    yield {
        "campaign_id": row.get("campaign_id"),
        "target_id": wire("tgt", row.get("target_id")),
        "name": row.get("name"),
        "since": ts(row.get("since")),
        "created_at": ts(row.get("created_at")),
        "added_at": ts(row.get("added_at")),
    }


def _source_profile(row: SourceRow) -> Iterable[Row]:
    profile = row.get("profile")
    if profile is not None:
        try:
            json.loads(profile)
        except (TypeError, ValueError) as exc:
            raise Malformed("profile is not JSON") from exc
    yield {
        "domain_id": wire("dom", row.get("domain_id")),
        "generation": _int(row.get("generation")),
        "policy": row.get("policy"),
        "through_bucket": _int(row.get("through_bucket")),
        "updated_at": ts(row.get("updated_at")),
        "profile_json": profile,
    }


# --- catalog --------------------------------------------------------------------------

_V, _I, _B, _D, _T, _L, _BOOL = VARCHAR, INTEGER, BIGINT, DOUBLE, TIMESTAMP, VARCHAR_LIST, BOOLEAN
_URL = (("url_id", _V), ("url", _V), ("domain_id", _V), ("host", _V), ("site", _V))

FETCH_ATTEMPTS = Dataset(
    "fetch_attempts",
    1,
    (
        ("fetch_attempt_id", _V),
        *_URL,
        ("capability", _V),
        ("worker", _V),
        ("started_at", _T),
        ("finished_at", _T),
        ("outcome", _V),
        ("http_status", _I),
        ("final_url_id", _V),
        ("final_url", _V),
        ("redirect_count", _I),
        ("bytes_received", _B),
        ("detail", _V),
    ),
    key=("fetch_attempt_id",),
    time_column="finished_at",
    sort=("site", "finished_at", "fetch_attempt_id"),
    mapper=_fetch_attempt,
    description="W1 fetch attempts (every fetch, any outcome)",
    semantics="one row per fetch attempt; outcome is the P4 FetchOutcome",
)
PAGE_OBSERVATIONS = Dataset(
    "page_observations",
    1,
    (
        ("observation_id", _V),
        ("fetch_attempt_id", _V),
        *_URL,
        ("final_url_id", _V),
        ("final_url", _V),
        ("final_host", _V),
        ("final_site", _V),
        ("capability", _V),
        ("observed_at", _T),
        ("http_status", _I),
        ("content_type", _V),
        ("body_digest", _V),
        ("body_size", _B),
        ("page_version_id", _V),
        ("redirect_count", _I),
        ("redirect_urls", _L),
        ("snapshot_uri", _V),
    ),
    key=("observation_id",),
    time_column="observed_at",
    sort=("site", "observed_at", "observation_id"),
    mapper=_page_observation,
    description="W7 page observations (an HTTP response was received and recorded)",
    semantics="any status; 'fetched OK' = http_status 200-299 (reports)",
)
_EXTRACT_BASE = (("page_version_id", _V), ("url_id", _V), ("host", _V), ("site", _V))
PAGE_EXTRACTS = Dataset(
    "page_extracts",
    1,
    (
        *_EXTRACT_BASE,
        ("url", _V),
        ("domain_id", _V),
        ("observed_at", _T),
        ("extractor", _V),
        ("normalization", _V),
        ("revision_id", _V),
        *((f"hash_{k}", _V) for k in _HASHES),
        ("title", _V),
        ("lang", _V),
        ("og_type", _V),
        ("published_at", _T),
        ("modified_at", _T),
        ("text_chars", _I),
        ("elements", _I),
        ("link_count", _I),
        ("media_count", _I),
        ("truncated", _L),
    ),
    key=("page_version_id",),
    time_column="observed_at",
    sort=("site", "observed_at", "page_version_id"),
    mapper=_page_extract,
    description="P5 extracts: one per parsed raw page version (no body, no visible text)",
    semantics="observed_at = first observation extracted; published_at/modified_at are "
    "page-declared and None when unparsable",
)
PAGE_LINKS = Dataset(
    "page_links",
    1,
    (
        *_EXTRACT_BASE,
        ("observed_at", _T),
        ("ordinal", _I),
        ("target_url", _V),
        ("target_url_id", _V),
        ("target_domain_id", _V),
        ("target_host", _V),
        ("target_site", _V),
        ("relation", _V),
        ("nofollow", _BOOL),
        ("anchor_text", _V),
        ("cross_site", _BOOL),
    ),
    key=("page_version_id", "ordinal"),
    time_column="observed_at",
    sort=("site", "page_version_id", "ordinal"),
    mapper=_page_links,
    fanout=True,
    description="links of each extract (page_extracts.doc.links, in document order)",
    semantics="ordinal = position in the extract; cross_site None if a site is unknown",
)
PAGE_MEDIA_REFS = Dataset(
    "page_media_refs",
    1,
    (
        *_EXTRACT_BASE,
        ("observed_at", _T),
        ("ordinal", _I),
        ("locator_url", _V),
        ("locator_url_id", _V),
        ("locator_host", _V),
        ("locator_site", _V),
        ("kind", _V),
        ("method", _V),
        ("declared_type", _V),
    ),
    key=("page_version_id", "ordinal"),
    time_column="observed_at",
    sort=("site", "page_version_id", "ordinal"),
    mapper=_page_media_refs,
    fanout=True,
    description="media references discovered by extraction (page_extracts.doc.media)",
    semantics="'media discovered' (before the P8 registry resolved or probed it)",
)
URLS = Dataset(
    "urls",
    1,
    (
        *_URL,
        ("first_seen", _T),
        ("last_observed_at", _T),
        ("last_status", _I),
    ),
    key=("url_id",),
    time_column="first_seen",
    sort=("site", "first_seen", "url_id"),
    mapper=_url_state,
    description="W12 known URLs ('page discovered'); last_* is the state at export time",
    semantics="first_seen = discovery instant; last_status None = never observed",
)
DOMAINS = Dataset(
    "domains",
    1,
    (("domain_id", _V), ("host", _V), ("site", _V), ("first_seen", _T)),
    key=("domain_id",),
    time_column=None,
    sort=("site", "host"),
    mapper=_domain,
    description="W13 domain records (one per host)",
)
PAGE_REVISIONS = Dataset(
    "page_revisions",
    1,
    (
        ("url_id", _V),
        ("revision_id", _V),
        ("normalization", _V),
        ("normalized_digest", _V),
        ("first_seen", _T),
        ("first_observation_id", _V),
        ("first_page_version_id", _V),
        ("last_seen", _T),
        ("last_observation_id", _V),
        ("last_page_version_id", _V),
    ),
    key=("url_id", "revision_id"),
    time_column="first_seen",
    sort=("url_id", "first_seen", "revision_id"),
    mapper=_page_revision,
    description="P5 page revisions (normalized content states, ADR-020)",
    semantics="no host column in the source: join urls on url_id",
)
MEDIA = Dataset(
    "media",
    1,
    (
        ("media_id", _V),
        ("locator_url", _V),
        ("locator_url_id", _V),
        ("locator_host", _V),
        ("locator_site", _V),
        ("kind", _V),
        ("first_seen", _T),
        ("last_seen", _T),
    ),
    key=("media_id",),
    time_column="first_seen",
    sort=("locator_site", "first_seen", "media_id"),
    mapper=_media,
    description="M1 media entities (P8 registry)",
)
MEDIA_OBSERVATIONS = Dataset(
    "media_observations",
    1,
    (
        ("page_observation_id", _V),
        ("media_id", _V),
        ("observed_at", _T),
        ("page_url", _V),
        ("page_url_id", _V),
        ("page_host", _V),
        ("page_site", _V),
        ("page_version_id", _V),
        ("locator_url", _V),
        ("locator_host", _V),
        ("locator_site", _V),
        ("media_kind", _V),
        ("probe_status", _V),
        ("content_id", _V),
        ("content_scheme", _V),
        ("content_digest", _V),
        ("hint_media_type", _V),
        ("hint_content_length", _B),
        ("hint_duration_seconds", _D),
    ),
    key=("page_observation_id", "media_id"),
    time_column="observed_at",
    sort=("page_site", "observed_at", "page_observation_id", "media_id"),
    mapper=_media_observation,
    description="M2 media observations: a media entity seen on a page observation",
    semantics="content_id set only when the probe identified the bytes (P8 levels 2-3)",
)
TARGETS = Dataset(
    "targets",
    1,
    (
        ("target_id", _V),
        ("version", _I),
        ("title", _V),
        ("kind", _V),
        ("release_year", _I),
        ("registered_at", _T),
        ("retired_at", _T),
        ("retire_reason", _V),
    ),
    key=("target_id",),
    time_column=None,
    sort=("target_id",),
    mapper=_target,
    description="P2 target projection (latest version per target, as known to crawler2)",
)
MATCHES = Dataset(
    "matches",
    1,
    (
        ("content_id", _V),
        ("match_id", _V),
        ("target_id", _V),
        ("target_version", _I),
        ("verdict", _V),
        ("confidence", _D),
        ("technique_name", _V),
        ("technique_version", _V),
        ("representation_id", _V),
        ("target_representation_id", _V),
        ("segment_count", _I),
        ("aligned_seconds", _D),
        ("decided_at", _T),
    ),
    key=("content_id", "match_id"),
    time_column="decided_at",
    sort=("target_id", "decided_at", "match_id"),
    mapper=_match,
    description="P3 match projection of match.found (the matcher's claims, unmodified)",
    semantics="verdict/confidence are the matcher's; 'confirmed' is its verdict, not a review",
)
EVIDENCE_CANDIDATES = Dataset(
    "evidence_candidates",
    1,
    (
        ("evidence_id", _V),
        ("match_id", _V),
        ("target_id", _V),
        ("status", _V),
        ("revision", _I),
        ("collected_at", _T),
        ("updated_at", _T),
        ("validated_at", _T),
        ("validation_outcome", _V),
        ("verdict", _V),
        ("confidence", _D),
        ("content_id", _V),
        ("gap_codes", _L),
        ("sources_total", _I),
        ("sources_recorded", _I),
        ("media_count", _I),
    ),
    key=("evidence_id",),
    time_column="collected_at",
    sort=("target_id", "collected_at", "evidence_id"),
    mapper=_evidence_candidate,
    description="P12 E5 evidence candidates (working dossiers; status at export time)",
)
EVIDENCE_SOURCES = Dataset(
    "evidence_sources",
    1,
    (
        ("evidence_id", _V),
        ("ordinal", _I),
        ("match_id", _V),
        ("target_id", _V),
        ("collected_at", _T),
        ("page_observation_id", _V),
        ("media_id", _V),
        ("page_url", _V),
        ("page_url_id", _V),
        ("page_host", _V),
        ("page_site", _V),
        ("page_version_id", _V),
        ("basis", _V),
        ("observation_present", _BOOL),
        ("sighted_at", _T),
        ("has_snapshot", _BOOL),
    ),
    key=("evidence_id", "ordinal"),
    time_column="collected_at",
    sort=("page_site", "evidence_id", "ordinal"),
    mapper=_evidence_sources,
    fanout=True,
    description="sources recorded in each E5 candidate (P12 attribution)",
    semantics="basis = content_verified | locator_only (P12 design §2); bounded by max_sources",
)
EVIDENCE_VERSIONS = Dataset(
    "evidence_versions",
    1,
    (
        ("evidence_id", _V),
        ("version", _I),
        ("status", _V),
        ("manifest_uri", _V),
        ("manifest_digest", _V),
        ("manifest_size", _B),
        ("finalized_at", _T),
        ("supersedes", _I),
    ),
    key=("evidence_id", "version"),
    time_column="finalized_at",
    sort=("evidence_id", "version"),
    mapper=_evidence_version,
    description="P12 E6 finalized evidence versions (append-only); actor and reason excluded",
)
CAMPAIGNS = Dataset(
    "campaigns",
    1,
    (
        ("campaign_id", _V),
        ("target_id", _V),
        ("name", _V),
        ("since", _T),
        ("created_at", _T),
        ("added_at", _T),
    ),
    key=("campaign_id", "target_id"),
    time_column=None,
    sort=("campaign_id", "target_id"),
    mapper=_campaign,
    description="P12 E8 campaign (case) membership",
)
SOURCE_PROFILES = Dataset(
    "source_profiles",
    1,
    (
        ("domain_id", _V),
        ("generation", _I),
        ("policy", _V),
        ("through_bucket", _B),
        ("updated_at", _T),
        ("profile_json", _V),
    ),
    key=("domain_id",),
    time_column=None,
    sort=("domain_id",),
    mapper=_source_profile,
    description="P7 I1 source profiles: derived current state (30-day TTL), not history",
)

SOURCES: tuple[SourceTable, ...] = (
    SourceTable("fetch_attempts", ("fetch_attempt_id",), ("doc",), (FETCH_ATTEMPTS,)),
    SourceTable("page_observations", ("observation_id",), ("doc",), (PAGE_OBSERVATIONS,)),
    SourceTable(
        "page_extracts",
        ("page_version_id",),
        ("observed_at", "doc"),
        (PAGE_EXTRACTS, PAGE_LINKS, PAGE_MEDIA_REFS),
        fetch_size=200,
    ),
    SourceTable(
        "url_state",
        ("url_id",),
        ("url_id", "url", "domain_id", "first_seen", "last_observed_at", "last_status"),
        (URLS,),
    ),
    SourceTable("domains", ("domain_id",), ("domain_id", "host", "first_seen"), (DOMAINS,)),
    SourceTable(
        "page_revisions_by_url",
        ("url_id",),
        (
            "url_id",
            "revision_id",
            "normalization",
            "normalized_digest",
            "first_seen",
            "first_observation_id",
            "first_page_version_id",
            "last_seen",
            "last_observation_id",
            "last_page_version_id",
        ),
        (PAGE_REVISIONS,),
    ),
    SourceTable(
        "media", ("media_id",), ("media_id", "kind", "doc", "first_seen", "last_seen"), (MEDIA,)
    ),
    SourceTable("media_observations", ("page_observation_id",), ("doc",), (MEDIA_OBSERVATIONS,)),
    SourceTable("targets", ("bucket",), ("doc", "retired_at", "retire_reason"), (TARGETS,)),
    SourceTable("matches_by_content", ("content_id",), ("doc",), (MATCHES,)),
    SourceTable(
        "evidence_candidates",
        ("evidence_id",),
        (
            "evidence_id",
            "match_id",
            "target_id",
            "status",
            "revision",
            "collected_at",
            "updated_at",
            "doc",
            "validation",
            "validated_at",
        ),
        (EVIDENCE_CANDIDATES, EVIDENCE_SOURCES),
        fetch_size=200,
        note="V007 (P12); absent from keyspaces not migrated past V006",
    ),
    SourceTable(
        "evidence_versions",
        ("evidence_id",),
        (
            "evidence_id",
            "version",
            "status",
            "manifest_uri",
            "manifest_digest",
            "manifest_size",
            "finalized_at",
            "supersedes",
        ),
        (EVIDENCE_VERSIONS,),
        note="V007 (P12)",
    ),
    SourceTable(
        "evidence_campaigns",
        ("campaign_id",),
        ("campaign_id", "target_id", "name", "since", "created_at", "added_at"),
        (CAMPAIGNS,),
        note="V007 (P12)",
    ),
    SourceTable(
        "intel_source_profiles",
        ("domain_id",),
        ("domain_id", "generation", "policy", "through_bucket", "updated_at", "profile"),
        (SOURCE_PROFILES,),
        note="V004 (P7)",
    ),
)

DATASETS: dict[str, Dataset] = {d.name: d for s in SOURCES for d in s.datasets}
SOURCE_OF: dict[str, SourceTable] = {d.name: s for s in SOURCES for d in s.datasets}

EXCLUDED: dict[str, str] = {
    "outbox, outbox_relay_checkpoints, processed_events": "transient delivery state "
    "(14-day TTL); the facts they carry are exported from the authoritative tables",
    "fetch_attempts_by_domain_day, fetch_attempts_by_url, observations_by_domain_day": "TTL'd "
    "(30 days) derived copies of fetch_attempts / page_observations",
    "latest_observation_by_url, page_versions_by_url, inlinks_by_url, urls_by_domain": "derived "
    "read models of page_observations / page_extracts / url_state",
    "links_by_page_version": "same links as page_extracts.doc (the extract is the commit "
    "marker of W9); exported once, as page_links",
    "media_content_versions, media_by_content, media_by_page_version": "derived from M2",
    "media_probes, media_hashes, media_probe_latest, media_observation_claims, encode_claims, "
    "encode_claims_by_day, media_reader_state, media_reader_gaps": "P8 workflow state; the "
    "probe outcome and content identity are in media_observations",
    "representation_status": "projection of fingerprinter events (encoding workflow)",
    "matches_by_target, matches_by_domain": "derived indexes of matches_by_content",
    "evidence, evidence_by_match, evidence_by_target, evidence_campaign_index": "E2 seal and "
    "indexes; the finalized state is evidence_versions",
    "evidence_audit": "operator identities and free-text details (not needed for reports)",
    "feedback_evidence, feedback_sources": "P11 derived (90-day TTL), rebuildable from "
    "matches + media_observations; P11 is off by default",
    "intel_projection_state": "P7 reader checkpoint",
    "filter_*, url_admission, filter_decisions_by_url, redirect_decisions_by_observation, "
    "interceptions_by_observation": "P6 rule data and admission state (not campaign/source "
    "analytics in v1)",
    "search_results_by_query_day": "third-party titles/snippets; not needed in v1",
    "seeds_by_source, scope_sites, snapshot_retention": "small operational configuration",
    "Redis (frontier, streams)": "transient hot state; never an analytics source (B.5)",
    "fingerprinter keyspace": "owned by the fingerprinter; reached only through events (B.5 #2)",
}
