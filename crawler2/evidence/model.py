"""P12 evidence model (design §3): the working candidate, its gaps, and validation results.

The *candidate* (``EvidenceDossier``) is crawler2-internal and may change while
provenance resolves; it is never exported as such. Finalization turns it into
an immutable manifest (``manifest.py``). The P1 contracts
(``EvidenceCandidate``/``EvidenceSeal``) stay the cross-component identity and
seal; this model carries the detail they deliberately leave open (ADR-005).

Every field states where its value comes from:

- *observed*: written by a crawler fetch (W4 page observation, W1 fetch attempt);
- *derived*: computed by crawler2 from stored records (attribution, target info);
- *claimed*: asserted by the fingerprinter's matcher (``MatchResult``);
- review: conclusions no automated component makes (listed in the manifest).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.models.blobs import BlobRef
from antipiracy_contracts.models.matching import MatchResult, MatchVerdict
from antipiracy_contracts.models.web import UrlRef
from pydantic import BaseModel, ConfigDict, Field

CANDIDATE_SCHEMA: Literal["crawler2.evidence.candidate/v1"] = "crawler2.evidence.candidate/v1"
POLICY = "evidence-finalization/v1"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CandidateStatus(StrEnum):
    COMPLETE = "complete"
    """Every required fact is present (artifact bytes are verified at validation)."""
    INCOMPLETE = "incomplete"
    """A required fact is missing; the gaps say which."""
    AMBIGUOUS = "ambiguous"
    """Pages carried the media locator, but none was probed with the matched content."""
    UNATTRIBUTED = "unattributed"
    """No page in the registry is known to carry the matched content."""
    REJECTED = "rejected"
    """The last validation found an integrity failure; see the stored validation."""
    FINALIZED = "finalized"
    """A finalized version exists; the collector no longer changes the candidate."""


class SourceBasis(StrEnum):
    """How a page is linked to the matched content (derived, M3/M5)."""

    CONTENT_VERIFIED = "content_verified"
    """The registry probed the locator on this page and got the matched content id."""
    LOCATOR_ONLY = "locator_only"
    """The locator was seen on this page, but its bytes were not probed there."""


class GapCode(StrEnum):
    UNATTRIBUTED = "unattributed"
    AMBIGUOUS_ATTRIBUTION = "ambiguous_attribution"
    TARGET_UNKNOWN = "target_unknown"
    VERDICT_NOT_CONFIRMED = "verdict_not_confirmed"
    PAGE_OBSERVATION_MISSING = "page_observation_missing"
    SNAPSHOT_NOT_ARCHIVED = "snapshot_not_archived"
    FETCH_ATTEMPT_MISSING = "fetch_attempt_missing"
    SOURCES_TRUNCATED = "sources_truncated"
    SCREENSHOT_NOT_COLLECTED = "screenshot_not_collected"
    MEDIA_SAMPLE_NOT_RETAINED = "media_sample_not_retained"
    RESPONSE_HEADERS_NOT_RETAINED = "response_headers_not_retained"


REQUIRED_GAPS = frozenset(
    {
        GapCode.UNATTRIBUTED,
        GapCode.AMBIGUOUS_ATTRIBUTION,
        GapCode.TARGET_UNKNOWN,
        GapCode.VERDICT_NOT_CONFIRMED,
        GapCode.PAGE_OBSERVATION_MISSING,
        GapCode.SNAPSHOT_NOT_ARCHIVED,
    }
)
"""Gaps that keep a candidate from being *complete* (policy ``evidence-finalization/v1``)."""


class Gap(_Model):
    code: GapCode
    subject: str | None = None
    """The record the gap is about (e.g. a page observation id); None for the item."""
    detail: str

    @property
    def required(self) -> bool:
        return self.code in REQUIRED_GAPS


class TargetInfo(_Model):
    """Derived: the P2 target projection (fingerprinter's ``target.registered``)."""

    target_id: str
    version: int
    title: str
    kind: str
    retired: bool


class Redirect(_Model):
    url: str
    status: int


class MediaInfo(_Model):
    """Derived: an M1/M5 media entity carrying the matched content."""

    media_id: str
    locator: str
    kind: str | None


class SourceRecord(_Model):
    """One page that carried the media: the sighting (derived) plus W4/W1 (observed)."""

    page_observation_id: str
    media_id: str
    media_locator: str
    basis: SourceBasis
    sighted_at: datetime
    page_url: str
    page_version_id: str
    observation_present: bool
    # W4 page observation (observed), when present
    requested_url: str | None = None
    final_url: str | None = None
    redirects: tuple[Redirect, ...] = ()
    http_status: int | None = None
    content_type: str | None = None
    capability: str | None = None
    observed_at: datetime | None = None
    body_digest: ContentDigest | None = None
    body_size: int | None = None
    snapshot: BlobRef | None = None
    # W1 fetch attempt (observed), when present
    fetch_attempt_id: str | None = None
    worker: str | None = None
    fetch_started_at: datetime | None = None
    fetch_finished_at: datetime | None = None
    # M3 media observation (derived by the registry's probe)
    probe_status: str | None = None
    content_key_scheme: str | None = None
    content_key_digest: ContentDigest | None = None

    @property
    def domain(self) -> str:
        return UrlRef.of(self.page_url).url.host

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.page_observation_id, self.media_id)


class EvidenceDossier(_Model):
    """The working candidate of one evidence item (E5 ``doc``)."""

    schema_: Literal["crawler2.evidence.candidate/v1"] = Field(
        default=CANDIDATE_SCHEMA, alias="schema"
    )
    evidence_id: str
    collected_at: datetime
    """When this evidence item was opened (E1 ``opened_at``; first collection)."""
    match: MatchResult
    target: TargetInfo | None
    media: tuple[MediaInfo, ...]
    sources: tuple[SourceRecord, ...]
    sources_total: int
    """Distinct (page observation, media) sightings found; ``sources`` keeps the first N."""
    gaps: tuple[Gap, ...]

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    @property
    def status(self) -> CandidateStatus:
        codes = {g.code for g in self.gaps}
        if GapCode.UNATTRIBUTED in codes:
            return CandidateStatus.UNATTRIBUTED
        if GapCode.AMBIGUOUS_ATTRIBUTION in codes:
            return CandidateStatus.AMBIGUOUS
        if codes & REQUIRED_GAPS:
            return CandidateStatus.INCOMPLETE
        return CandidateStatus.COMPLETE

    @property
    def usable_sources(self) -> tuple[SourceRecord, ...]:
        """Sources whose W4 page observation exists (the minimum for any finalization)."""
        return tuple(s for s in self.sources if s.observation_present)

    @property
    def confirmed(self) -> bool:
        return self.match.verdict is MatchVerdict.CONFIRMED


# --- validation ------------------------------------------------------------------------


class ArtifactRole(StrEnum):
    PAGE_SNAPSHOT = "page_snapshot"
    """Raw HTTP response body as archived by the fetch worker (not a WARC record)."""
    SCREENSHOT = "screenshot"
    MEDIA_SAMPLE = "media_sample"


class ArtifactState(StrEnum):
    VERIFIED = "verified"
    """Read back from the object store; size and SHA-256 equal the reference."""
    MISSING = "missing"
    CORRUPTED = "corrupted"
    """Stored bytes or reference disagree with the recorded digest/size."""
    OVERSIZED = "oversized"
    """Exists with the recorded size but is over the verification bound; not read."""
    UNVERIFIABLE = "unverifiable"
    """Stored outside the configured bucket; existence and bytes could not be checked."""
    NOT_COLLECTED = "not_collected"
    """No capture of this kind exists; nothing was fabricated."""


class ArtifactCheck(_Model):
    role: ArtifactRole
    subject: str | None
    """Page observation id the artifact belongs to; None for item-level artifacts."""
    uri: str | None = None
    digest: ContentDigest | None = None
    size_bytes: int | None = None
    media_type: str | None = None
    state: ArtifactState
    detail: str


class CheckOutcome(StrEnum):
    PASS = "pass"  # noqa: S105 -- an outcome, not a secret
    FAIL = "fail"
    INTEGRITY_FAILURE = "integrity_failure"


class Check(_Model):
    check: str
    required: bool
    outcome: CheckOutcome
    detail: str


class ValidationOutcome(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    """Finalizable only with an explicit ``--allow-partial``; marked partial everywhere."""
    REJECTED = "rejected"
    """Integrity failure, or not even one observed source: never finalizable."""


class ValidationReport(_Model):
    policy: str = POLICY
    outcome: ValidationOutcome
    checks: tuple[Check, ...]
    artifacts: tuple[ArtifactCheck, ...]

    @property
    def failed(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.outcome is not CheckOutcome.PASS)
