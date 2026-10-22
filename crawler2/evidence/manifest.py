"""Canonical evidence manifest (design §7): the bytes a finalized version pins.

Canonical form (``crawler2.evidence.manifest/v1``):

- JSON, UTF-8, ``sort_keys``, separators ``(",", ":")``, no NaN/Infinity, no
  trailing newline;
- timestamps UTC as ``YYYY-MM-DDTHH:MM:SS.ffffffZ``;
- every array is sorted by a stated key (sources by (page observation, media),
  artifacts by (role, subject, digest), checks by id, gaps by (code, subject),
  redirects keep their hop order, which is meaningful);
- no generation time, host name, path, credential or random value: the inputs
  are the frozen candidate, the validation result and the version identity.

The manifest digest (``sha256:`` of these bytes) identifies the manifest;
each artifact has its own digest inside it. Identical inputs give identical
bytes; any changed artifact digest changes the manifest digest.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from antipiracy_contracts.digests import ContentDigest

from crawler2.evidence.model import (
    POLICY,
    ArtifactCheck,
    EvidenceDossier,
    SourceRecord,
    ValidationOutcome,
    ValidationReport,
)
from crawler2.evidence.redact import url_doc

MANIFEST_SCHEMA = "crawler2.evidence.manifest/v1"

DISCLAIMER = (
    "Automated output. A match verdict and confidence are claims of the fingerprint "
    "matcher, not a legal determination of infringement. Hashes show that the listed "
    "bytes are unchanged since they were recorded; they do not by themselves prove when, "
    "where or by whom they were collected. Timestamps come from the collecting hosts' "
    "clocks and are not independently trusted. Human and legal review is required."
)
REVIEW_REQUIRED = (
    "whether the matched content is the protected work and whether its use infringes",
    "whether the source pages and collection are adequate for the intended jurisdiction "
    "(ADR-005 open)",
    "whether the matcher's verdict and confidence are reliable for this technique (P10/M3 "
    "matcher gates not passed)",
)
LIMITATIONS = (
    "no trusted timestamp (RFC 3161) or signature is applied; local clocks only",
    "page snapshots are raw response bodies, not WARC records; full response headers are "
    "not retained",
    "no screenshots or media samples are collected by crawler2",
    "object-store immutability is application-level (content-addressed keys, "
    "If-None-Match writes); the bucket has no object lock",
)


def ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_bytes(doc: Any) -> bytes:
    return json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest_of(data: bytes) -> ContentDigest:
    return ContentDigest.of_bytes(data)


def _url(value: str | None) -> dict[str, object] | None:
    return url_doc(value) if value is not None else None


def _source(s: SourceRecord) -> dict[str, Any]:
    return {
        "page_observation_id": s.page_observation_id,
        "page_version_id": s.page_version_id,
        "page": _url(s.page_url),
        "media_id": s.media_id,
        "media_locator": _url(s.media_locator),
        "attribution_basis": s.basis.value,
        "sighted_at": ts(s.sighted_at),
        "observation_present": s.observation_present,
        "fetch": {
            "requested": _url(s.requested_url),
            "final": _url(s.final_url),
            "redirects": [{"status": r.status, **url_doc(r.url)} for r in s.redirects],
            "http_status": s.http_status,
            "content_type": s.content_type,
            "capability": s.capability,
            "observed_at": ts(s.observed_at),
            "fetch_attempt_id": s.fetch_attempt_id,
            "worker": s.worker,
            "started_at": ts(s.fetch_started_at),
            "finished_at": ts(s.fetch_finished_at),
            "body_sha256": s.body_digest,
            "body_size": s.body_size,
        },
        "media_probe": {
            "status": s.probe_status,
            "content_key_scheme": s.content_key_scheme,
            "content_key_digest": s.content_key_digest,
        },
    }


def _artifact(a: ArtifactCheck) -> dict[str, Any]:
    return {
        "role": a.role.value,
        "subject": a.subject,
        "uri": a.uri,
        "sha256": a.digest,
        "size_bytes": a.size_bytes,
        "media_type": a.media_type,
        "state": a.state.value,
        "detail": a.detail,
    }


def build_manifest(
    dossier: EvidenceDossier,
    report: ValidationReport,
    *,
    version: int,
    supersedes: tuple[int, ContentDigest] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    if report.outcome is ValidationOutcome.REJECTED:
        raise ValueError("a rejected candidate has no manifest")
    d, m = dossier, dossier.match
    domains = sorted({s.domain for s in d.sources})
    return {
        "schema": MANIFEST_SCHEMA,
        "policy": POLICY,
        "evidence": {
            "evidence_id": d.evidence_id,
            "version": version,
            "status": report.outcome.value,
            "collected_at": ts(d.collected_at),
            "supersedes": {"version": supersedes[0], "manifest_sha256": supersedes[1]}
            if supersedes
            else None,
            "correction_reason": reason,
        },
        "target": d.target.model_dump(mode="json") if d.target else None,
        "claims": {
            "asserted_by": "fingerprinter matcher (match.found)",
            "match_id": str(m.match_id),
            "target_id": str(m.target.target_id),
            "target_version": m.target.version,
            "content_id": str(m.content_id),
            "representation_id": str(m.representation_id),
            "target_representation_id": str(m.target_representation_id),
            "technique": {"name": m.technique.name, "version": m.technique.version},
            "verdict": m.verdict.value,
            "confidence": m.confidence,
            "decided_at": ts(m.decided_at),
            "segments": [
                seg.model_dump(mode="json")
                for seg in sorted(
                    m.segments, key=lambda x: (x.media_offset_seconds, x.target_offset_seconds)
                )
            ],
        },
        "observed": {
            "recorded_by": "crawler2 fetch workers (W4 page observations, W1 fetch attempts)",
            "sources": [_source(s) for s in sorted(d.sources, key=lambda s: s.sort_key)],
        },
        "derived": {
            "attribution_method": "crawler2 media registry: M5 content -> media, M3 media -> "
            "page observations (P11 rule); a sighting that probed other bytes is excluded",
            "sources_total": d.sources_total,
            "sources_recorded": len(d.sources),
            "source_domains": domains,
            "media": [
                {"media_id": x.media_id, "kind": x.kind, "locator": url_doc(x.locator)}
                for x in sorted(d.media, key=lambda x: x.media_id)
            ],
        },
        "artifacts": [_artifact(a) for a in report.artifacts],
        "validation": {
            "outcome": report.outcome.value,
            "checks": [c.model_dump(mode="json") for c in report.checks],
        },
        "gaps": [
            {"code": g.code.value, "subject": g.subject, "required": g.required, "detail": g.detail}
            for g in d.gaps
        ],
        "review_required": list(REVIEW_REQUIRED),
        "limitations": list(LIMITATIONS),
        "disclaimer": DISCLAIMER,
    }
