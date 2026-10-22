"""Validation of a candidate before finalization (design §6, policy ``evidence-finalization/v1``).

Required checks (all must pass for ``complete``):

- ``match.identity``      the match id is derived from (representations, technique);
- ``target.known``        the target is in crawler2's P2 projection;
- ``match.confirmed``     the matcher's verdict is ``confirmed`` (``probable`` needs review);
- ``attribution.content_verified``  at least one source page was probed with the matched
                          content (not just the locator);
- ``provenance.observations``  every recorded source's W4 observation exists and still
                          says what the candidate recorded (integrity check);
- ``artifacts.page_snapshots``  every source's raw snapshot exists and its bytes hash
                          to the recorded SHA-256 (integrity check).

``rejected``: any integrity failure, or no source with an observation at all
(nothing observed to finalize). Otherwise a failed required check gives
``partial``, which finalization accepts only when explicitly allowed.
Transient storage errors propagate: an unknown is never recorded as a result.
"""

from __future__ import annotations

from typing import Protocol

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.ids import ObservationId
from antipiracy_contracts.models.blobs import BlobRef
from antipiracy_contracts.models.matching import MatchResult

from crawler2.evidence.collector import EvidenceMetrics, PageLookup
from crawler2.evidence.model import (
    ArtifactCheck,
    ArtifactRole,
    ArtifactState,
    Check,
    CheckOutcome,
    EvidenceDossier,
    GapCode,
    SourceBasis,
    SourceRecord,
    ValidationOutcome,
    ValidationReport,
)
from crawler2.storage.errors import IntegrityError
from crawler2.storage.objectstore.base import ObjectInfo
from crawler2.storage.objectstore.layout import parse_uri, raw_key


class ArtifactReader(Protocol):
    """The parts of ``ObjectStore`` verification uses."""

    def stat(self, digest: ContentDigest) -> ObjectInfo | None: ...

    def get(self, ref: BlobRef) -> bytes: ...


def verify_blob(
    objects: ArtifactReader, ref: BlobRef, *, bucket: str | None, max_bytes: int
) -> tuple[ArtifactState, str]:
    """Existence, size and SHA-256 of one stored object, bounded by ``max_bytes``."""
    try:
        ref_bucket, key = parse_uri(ref.uri)
    except ValueError:
        return ArtifactState.CORRUPTED, "reference is not a content-addressed object URI"
    if key != raw_key(ref.digest):
        return ArtifactState.CORRUPTED, "object key does not match the recorded digest"
    if bucket is not None and ref_bucket != bucket:
        return (
            ArtifactState.UNVERIFIABLE,
            f"stored in bucket {ref_bucket!r}, not the configured one",
        )
    info = objects.stat(ref.digest)
    if info is None:
        return ArtifactState.MISSING, "no object under the recorded digest"
    if info.size_bytes != ref.size_bytes or info.digest != ref.digest:
        return ArtifactState.CORRUPTED, (
            f"stored object is {info.size_bytes} B / {info.digest}, reference pins "
            f"{ref.size_bytes} B / {ref.digest}"
        )
    if ref.size_bytes > max_bytes:
        return ArtifactState.OVERSIZED, f"{ref.size_bytes} B exceeds the {max_bytes} B bound"
    try:
        objects.get(ref)
    except IntegrityError as exc:
        return ArtifactState.CORRUPTED, str(exc)[:300]
    return ArtifactState.VERIFIED, "size and sha256 match the stored bytes"


def _snapshot_check(
    s: SourceRecord, objects: ArtifactReader, bucket: str | None, max_bytes: int
) -> ArtifactCheck:
    if s.snapshot is None:
        state, detail = (
            ArtifactState.MISSING,
            ("page observation missing" if not s.observation_present else "no snapshot archived"),
        )
        return ArtifactCheck(
            role=ArtifactRole.PAGE_SNAPSHOT,
            subject=s.page_observation_id,
            digest=s.body_digest,
            size_bytes=s.body_size,
            state=state,
            detail=detail,
        )
    state, detail = verify_blob(objects, s.snapshot, bucket=bucket, max_bytes=max_bytes)
    return ArtifactCheck(
        role=ArtifactRole.PAGE_SNAPSHOT,
        subject=s.page_observation_id,
        uri=s.snapshot.uri,
        digest=s.snapshot.digest,
        size_bytes=s.snapshot.size_bytes,
        media_type=s.snapshot.media_type,
        state=state,
        detail=detail,
    )


def _item_artifacts(dossier: EvidenceDossier) -> list[ArtifactCheck]:
    found = []
    roles = {
        GapCode.SCREENSHOT_NOT_COLLECTED: ArtifactRole.SCREENSHOT,
        GapCode.MEDIA_SAMPLE_NOT_RETAINED: ArtifactRole.MEDIA_SAMPLE,
    }
    for gap in dossier.gaps:
        if gap.code in roles:
            found.append(
                ArtifactCheck(
                    role=roles[gap.code],
                    subject=None,
                    state=ArtifactState.NOT_COLLECTED,
                    detail=gap.detail,
                )
            )
    return found


def _observations_check(dossier: EvidenceDossier, pages: PageLookup) -> Check:
    missing, drifted = [], []
    for s in dossier.sources:
        if not s.observation_present:
            missing.append(s.page_observation_id)
            continue
        page = pages.get(ObservationId(s.page_observation_id))
        if page is None:
            missing.append(s.page_observation_id)
        elif (
            page.body_digest != s.body_digest
            or page.final.url != s.final_url
            or page.snapshot != s.snapshot
            or page.http_status != s.http_status
        ):
            drifted.append(s.page_observation_id)
    if drifted:
        return Check(
            check="provenance.observations",
            required=True,
            outcome=CheckOutcome.INTEGRITY_FAILURE,
            detail=f"W4 rows differ from the recorded provenance: {sorted(drifted)}",
        )
    if missing:
        return Check(
            check="provenance.observations",
            required=True,
            outcome=CheckOutcome.FAIL,
            detail=f"page observations not found: {sorted(missing)}",
        )
    return Check(
        check="provenance.observations",
        required=True,
        outcome=CheckOutcome.PASS,
        detail=f"{len(dossier.sources)} source observations present and unchanged",
    )


def _passfail(check: str, ok: bool, yes: str, no: str) -> Check:
    return Check(
        check=check,
        required=True,
        outcome=CheckOutcome.PASS if ok else CheckOutcome.FAIL,
        detail=yes if ok else no,
    )


def validate(
    dossier: EvidenceDossier,
    *,
    objects: ArtifactReader,
    pages: PageLookup,
    bucket: str | None,
    max_artifact_bytes: int,
    metrics: EvidenceMetrics | None = None,
) -> ValidationReport:
    m = dossier.match
    identity_ok = m.match_id == MatchResult.expected_id(
        m.representation_id, m.target_representation_id, m.technique
    )
    checks = [
        Check(
            check="match.identity",
            required=True,
            outcome=CheckOutcome.PASS if identity_ok else CheckOutcome.INTEGRITY_FAILURE,
            detail="match id derives from (representations, technique)"
            if identity_ok
            else "match id is not derived from its inputs",
        ),
        _passfail(
            "target.known",
            dossier.target is not None,
            "target present in the P2 projection",
            "target not registered in crawler2's projection",
        ),
        _passfail(
            "match.confirmed",
            dossier.confirmed,
            "matcher verdict is 'confirmed'",
            f"matcher verdict is {m.verdict.value!r} (requires review)",
        ),
        _passfail(
            "attribution.content_verified",
            any(
                s.basis is SourceBasis.CONTENT_VERIFIED and s.observation_present
                for s in dossier.sources
            ),
            "at least one observed page was probed with the matched content",
            "no observed page was probed with the matched content (unattributed or ambiguous)",
        ),
        _observations_check(dossier, pages),
    ]
    artifacts = [
        _snapshot_check(s, objects, bucket, max_artifact_bytes) for s in dossier.sources
    ] + _item_artifacts(dossier)
    states = [a.state for a in artifacts if a.role is ArtifactRole.PAGE_SNAPSHOT]
    if ArtifactState.CORRUPTED in states:
        snap = CheckOutcome.INTEGRITY_FAILURE
        detail = "a stored snapshot does not match its recorded digest/size"
    elif states and all(s is ArtifactState.VERIFIED for s in states):
        snap, detail = CheckOutcome.PASS, f"{len(states)} snapshots verified (sha256)"
    else:
        snap = CheckOutcome.FAIL
        detail = "snapshots not verified: " + ", ".join(
            sorted({s.value for s in states if s is not ArtifactState.VERIFIED}) or ["none"]
        )
    checks.append(
        Check(check="artifacts.page_snapshots", required=True, outcome=snap, detail=detail)
    )
    if metrics is not None:
        for a in artifacts:
            metrics.artifact(a.state.value)

    if any(c.outcome is CheckOutcome.INTEGRITY_FAILURE for c in checks) or not (
        dossier.usable_sources
    ):
        outcome = ValidationOutcome.REJECTED
    elif all(c.outcome is CheckOutcome.PASS for c in checks if c.required):
        outcome = ValidationOutcome.COMPLETE
    else:
        outcome = ValidationOutcome.PARTIAL
    return ValidationReport(
        outcome=outcome,
        checks=tuple(sorted(checks, key=lambda c: c.check)),
        artifacts=tuple(
            sorted(artifacts, key=lambda a: (a.role.value, a.subject or "", str(a.digest or "")))
        ),
    )
