"""Repository interfaces: domain operations over P1 contracts, one per concern.

Rules (docs/phases/p02-storage/repositories.md):

- Inputs and outputs are contract models or the small read-model records
  below; no driver type, CQL or bucket arithmetic leaks to callers.
- Every write is an idempotent upsert keyed by contract IDs: replaying it
  (at-least-once delivery) converges to the same state. The only
  compare-and-set operations are E1/E2 (``EvidenceRepository``).
- ``event=`` on a write method appends that envelope to the outbox with the
  authoritative rows (ADR-013). The payload must describe the same write.
- Reads marked "EC" may lag concurrent writes (P1 catalog "EC ok").
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events import EventEnvelope
from antipiracy_contracts.events.evidence import EvidenceCandidateCreated, EvidenceFinalized
from antipiracy_contracts.events.fingerprinting import EncodeFailed, EncodeRequested
from antipiracy_contracts.events.media import MediaDiscovered, MediaObserved
from antipiracy_contracts.events.targets import TargetRetired
from antipiracy_contracts.events.web import (
    FetchCompleted,
    PageChanged,
    PageObserved,
    UrlsDiscovered,
)
from antipiracy_contracts.ids import (
    ContentId,
    DomainId,
    EventId,
    EvidenceId,
    FetchAttemptId,
    MatchId,
    MediaId,
    ObservationId,
    PageRevisionId,
    PageVersionId,
    TargetId,
    UrlId,
)
from antipiracy_contracts.models.evidence import EvidenceCandidate, EvidenceSeal
from antipiracy_contracts.models.matching import MatchResult, MatchVerdict
from antipiracy_contracts.models.media import Media, MediaKind, MediaObservation, ProbeStatus
from antipiracy_contracts.models.representations import Representation
from antipiracy_contracts.models.targets import Target
from antipiracy_contracts.models.web import (
    DiscoveredLink,
    Domain,
    FetchAttempt,
    FetchCapability,
    FetchOutcome,
    HttpValidators,
    PageObservation,
    UrlRef,
)

# --- read models ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchAttemptSummary:
    """W2/W3 row: enough for fetch-profile learning without the full attempt."""

    fetch_attempt_id: FetchAttemptId
    url_id: UrlId
    finished_at: datetime
    capability: FetchCapability
    outcome: FetchOutcome
    http_status: int | None
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class LatestObservation:
    """W5: what a conditional GET and the scheduler need about the newest sighting."""

    url_id: UrlId
    observation_id: ObservationId
    observed_at: datetime
    http_status: int
    page_version_id: PageVersionId
    content_type: str | None
    validators: HttpValidators | None
    snapshot_uri: str | None


@dataclass(frozen=True, slots=True)
class PageVersionSighting:
    """W8: one content state of a URL and when it was seen (merged across months)."""

    page_version_id: PageVersionId
    first_seen: datetime
    first_observation_id: ObservationId
    last_seen: datetime
    body_digest: ContentDigest
    body_size: int
    content_type: str | None


@dataclass(frozen=True, slots=True)
class ObservationSummary:
    """W14 row."""

    observation_id: ObservationId
    url_id: UrlId
    observed_at: datetime
    http_status: int
    page_version_id: PageVersionId
    content_type: str | None
    body_size: int


@dataclass(frozen=True, slots=True)
class Inlink:
    """W10 row: one linking URL (latest linking version) of a target URL."""

    source_url_id: UrlId
    source_url: str
    first_seen: datetime
    last_seen: datetime
    last_page_version_id: PageVersionId


@dataclass(frozen=True, slots=True)
class KnownUrl:
    """W11/W12: a URL the system knows, and its minimal crawl state."""

    url_id: UrlId
    url: str
    domain_id: DomainId
    first_seen: datetime
    last_observed_at: datetime | None
    last_status: int | None
    last_page_version_id: PageVersionId | None = None


@dataclass(frozen=True, slots=True)
class DomainRecord:
    domain: Domain
    first_seen: datetime


@dataclass(frozen=True, slots=True)
class MediaRecord:
    media: Media
    first_seen: datetime
    last_seen: datetime


@dataclass(frozen=True, slots=True)
class MediaSighting:
    """M3 row: one page observation on which a media entity was seen."""

    page_observation_id: ObservationId
    observed_at: datetime
    page_url_id: UrlId
    page_url: str
    page_version_id: PageVersionId
    probe_status: ProbeStatus
    content_id: ContentId | None


@dataclass(frozen=True, slots=True)
class MediaOnPage:
    """M4 row."""

    media_id: MediaId
    kind: MediaKind
    locator_url: str
    content_id: ContentId | None


@dataclass(frozen=True, slots=True)
class MediaWithContent:
    """M5 row."""

    media_id: MediaId
    locator_url: str
    first_seen: datetime


@dataclass(frozen=True, slots=True)
class ContentVersion:
    """M6: one content identity seen at a media locator (merged across months)."""

    content_id: ContentId
    scheme: str
    digest: ContentDigest
    first_seen: datetime
    last_seen: datetime


class RepresentationState(StrEnum):
    REQUESTED = "requested"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RepresentationStatus:
    """P1: the projection for one (content, spec). ``ready`` supersedes any failure."""

    content_id: ContentId
    spec_key: str
    state: RepresentationState
    requested_at: datetime | None
    request_event_id: EventId | None
    representation: Representation | None
    failure: str | None
    retryable: bool | None
    failed_attempt: int | None
    failed_at: datetime | None


@dataclass(frozen=True, slots=True)
class TargetState:
    """P2: the highest known version of a target and whether it is retired."""

    target: Target
    retired_at: datetime | None
    retire_reason: str | None

    @property
    def active(self) -> bool:
        return self.retired_at is None


@dataclass(frozen=True, slots=True)
class MatchSummary:
    """P4/P5 row."""

    match_id: MatchId
    target_id: TargetId
    target_version: int
    content_id: ContentId
    verdict: MatchVerdict
    confidence: float
    decided_at: datetime
    media_ids: frozenset[MediaId] = frozenset()


class EvidenceState(StrEnum):
    COLLECTING = "collecting"
    SEALED = "sealed"


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """E2."""

    candidate: EvidenceCandidate
    state: EvidenceState
    seal: EvidenceSeal | None


@dataclass(frozen=True, slots=True)
class EvidenceSummary:
    """E4 row."""

    evidence_id: EvidenceId
    match_id: MatchId
    collected_at: datetime
    finalized_at: datetime | None

    @property
    def state(self) -> EvidenceState:
        return EvidenceState.COLLECTING if self.finalized_at is None else EvidenceState.SEALED


@dataclass(frozen=True, slots=True)
class EvidenceProvenance:
    """E3: an evidence item and the page observations (with snapshot refs) it rests on."""

    record: EvidenceRecord
    observations: tuple[PageObservation, ...]
    missing_observations: tuple[ObservationId, ...]


@dataclass(frozen=True, slots=True)
class ExtractRecord:
    """P5: the extraction facts of one raw page version; ``doc`` is opaque to storage."""

    page_version_id: PageVersionId
    url_id: UrlId
    extractor: str
    normalization: str
    revision_id: PageRevisionId
    normalized_digest: ContentDigest
    observed_at: datetime
    """Time of the observation that was extracted first (not a wall-clock time)."""
    doc: str


@dataclass(frozen=True, slots=True)
class RevisionSighting:
    """P5 write: one observation of a page revision (ADR-020)."""

    url_id: UrlId
    revision_id: PageRevisionId
    normalization: str
    normalized_digest: ContentDigest
    observation_id: ObservationId
    page_version_id: PageVersionId
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class PageRevision:
    """P5 read model: a revision of a URL with first/last sighting."""

    revision_id: PageRevisionId
    normalization: str
    normalized_digest: ContentDigest
    first_seen: datetime
    first_observation_id: ObservationId
    first_page_version_id: PageVersionId
    last_seen: datetime
    last_observation_id: ObservationId
    last_page_version_id: PageVersionId


class SnapshotDecision(StrEnum):
    RETAIN = "retain"
    SAMPLED_OUT = "sampled_out"


@dataclass(frozen=True, slots=True)
class RetentionDecision:
    """P5 archival decision for one snapshot blob as seen by one observation."""

    digest: ContentDigest
    observation_id: ObservationId
    decision: SnapshotDecision
    reason: str
    decided_at: datetime


# --- repositories -----------------------------------------------------------


class FetchAttemptRepository(Protocol):
    """W1-W3."""

    def record(
        self, attempt: FetchAttempt, *, event: EventEnvelope[FetchCompleted] | None = None
    ) -> None: ...

    def get(self, fetch_attempt_id: FetchAttemptId) -> FetchAttempt | None: ...

    def recent_for_url(self, url_id: UrlId, *, limit: int = 20) -> list[FetchAttemptSummary]:
        """EC. Newest first; bounded by the 30-day TTL."""
        ...

    def for_domain_day(self, domain_id: DomainId, day: datetime) -> list[FetchAttemptSummary]:
        """EC. All attempts of one UTC day, newest first."""
        ...


class PageObservationRepository(Protocol):
    """W4-W8, W14; maintains W5/W8/W12/W11/W13/W14 as derived rows."""

    def record(
        self, observation: PageObservation, *, event: EventEnvelope[PageObserved] | None = None
    ) -> None: ...

    def get(self, observation_id: ObservationId) -> PageObservation | None:
        """LOCAL_QUORUM (W7)."""
        ...

    def latest(self, url_id: UrlId) -> LatestObservation | None:
        """EC. Newest by observed_at, never by arrival order (W5)."""
        ...

    def history(
        self, url_id: UrlId, *, since: datetime, until: datetime, limit: int = 100
    ) -> list[PageObservation]:
        """EC. Newest first, within [since, until] months (W6)."""
        ...

    def versions(
        self, url_id: UrlId, *, since: datetime, until: datetime
    ) -> list[PageVersionSighting]:
        """EC. Newest first_seen first (W8)."""
        ...

    def recent_for_domain_day(self, domain_id: DomainId, day: datetime) -> list[ObservationSummary]:
        """EC. Newest first (W14)."""
        ...


class LinkRepository(Protocol):
    """W9, W10."""

    def record(
        self,
        discovered: UrlsDiscovered,
        *,
        observed_at: datetime,
        event: EventEnvelope[UrlsDiscovered] | None = None,
    ) -> None:
        """``observed_at`` is the page observation's time (the payload carries none)."""
        ...

    def links_of(self, page_version_id: PageVersionId) -> list[DiscoveredLink]: ...

    def inlinks(self, url_id: UrlId) -> Iterator[Inlink]:
        """EC, cold. Streams every shard."""
        ...


class UrlRepository(Protocol):
    """W11-W13."""

    def record_discovered(self, urls: Sequence[UrlRef], *, seen_at: datetime) -> None: ...

    def get(self, url_id: UrlId) -> KnownUrl | None: ...

    def urls_of_domain(self, domain_id: DomainId) -> Iterator[KnownUrl]:
        """EC, cold. Streams every shard."""
        ...

    def domain(self, domain_id: DomainId) -> DomainRecord | None: ...


class PageIntelligenceRepository(Protocol):
    """P5: extracts per raw page version, revisions per URL, archival decisions."""

    def record_extract(self, record: ExtractRecord) -> None:
        """Idempotent upsert keyed by ``page_version_id``."""
        ...

    def extract(self, page_version_id: PageVersionId) -> ExtractRecord | None: ...

    def record_sighting(
        self,
        sighting: RevisionSighting,
        *,
        changed: EventEnvelope[PageChanged] | None = None,
        media: EventEnvelope[MediaDiscovered] | None = None,
    ) -> None:
        """first_* earliest-wins, last_* latest-wins; the events go to the outbox in the
        same logged batch and must describe this sighting."""
        ...

    def revision(self, url_id: UrlId, revision_id: PageRevisionId) -> PageRevision | None:
        """EC. A stale miss only causes a duplicate ``page.changed`` (same key)."""
        ...

    def revisions(self, url_id: UrlId) -> list[PageRevision]:
        """EC. Newest first_seen first."""
        ...

    def record_retention(self, decision: RetentionDecision) -> None: ...

    def retention(self, digest: ContentDigest) -> list[RetentionDecision]: ...


class MediaRepository(Protocol):
    """M1-M6."""

    def record_observation(
        self, observation: MediaObservation, *, event: EventEnvelope[MediaObserved] | None = None
    ) -> None: ...

    def get(self, media_id: MediaId) -> MediaRecord | None: ...

    def observation(
        self, page_observation_id: ObservationId, media_id: MediaId
    ) -> MediaObservation | None: ...

    def observations_on(self, page_observation_id: ObservationId) -> list[MediaObservation]: ...

    def sightings(
        self, media_id: MediaId, *, since: datetime, until: datetime, limit: int = 100
    ) -> list[MediaSighting]:
        """EC. Newest first (M3)."""
        ...

    def on_page_version(self, page_version_id: PageVersionId) -> list[MediaOnPage]: ...

    def by_content(self, content_id: ContentId) -> list[MediaWithContent]: ...

    def content_versions(
        self, media_id: MediaId, *, since: datetime, until: datetime
    ) -> list[ContentVersion]: ...


class ProjectionRepository(Protocol):
    """P1-P5: crawler2's local copies of fingerprinter facts, built from events."""

    def mark_encode_requested(self, event: EventEnvelope[EncodeRequested]) -> None: ...

    def apply_representation_ready(self, representation: Representation) -> None: ...

    def apply_encode_failed(self, event: EventEnvelope[EncodeFailed]) -> None:
        """Takes the envelope: ``occurred_at`` is the failure time."""
        ...

    def representation_status(self, content_id: ContentId) -> list[RepresentationStatus]: ...

    def apply_target_registered(self, target: Target) -> None: ...

    def apply_target_retired(self, retired: TargetRetired) -> None: ...

    def target(self, target_id: TargetId) -> TargetState | None: ...

    def targets(self) -> list[TargetState]:
        """Every known target; filter ``active`` for the P2 listing."""
        ...

    def record_match(
        self,
        match: MatchResult,
        *,
        media_ids: Collection[MediaId],
        source_domains: Collection[DomainId],
    ) -> None:
        """``media_ids``/``source_domains`` are resolved by the caller from M5 (no joins)."""
        ...

    def matches_for_content(self, content_id: ContentId) -> list[MatchResult]: ...

    def matches_for_target(self, target_id: TargetId, month: datetime) -> list[MatchSummary]: ...

    def matches_for_domain(self, domain_id: DomainId, month: datetime) -> list[MatchSummary]: ...


class EvidenceRepository(Protocol):
    """E1-E4. The only compare-and-set state in crawler2 (LWT)."""

    def open_candidate(
        self, match_id: MatchId, proposed: EvidenceId, *, at: datetime
    ) -> EvidenceId:
        """E1: returns the one evidence id of this match (``proposed`` if it won)."""
        ...

    def create_candidate(
        self,
        candidate: EvidenceCandidate,
        *,
        event: EventEnvelope[EvidenceCandidateCreated] | None = None,
    ) -> EvidenceRecord:
        """E2 write-once; identical replays succeed, a different candidate raises."""
        ...

    def seal(
        self, seal: EvidenceSeal, *, event: EventEnvelope[EvidenceFinalized] | None = None
    ) -> EvidenceRecord:
        """collecting → sealed exactly once; identical replays succeed."""
        ...

    def get(self, evidence_id: EvidenceId) -> EvidenceRecord | None:
        """LOCAL_SERIAL: observes every completed transition."""
        ...

    def for_target(self, target_id: TargetId, month: datetime) -> list[EvidenceSummary]: ...

    def provenance(self, evidence_id: EvidenceId) -> EvidenceProvenance | None: ...


__all__ = [
    "ContentVersion",
    "DomainRecord",
    "EvidenceProvenance",
    "EvidenceRecord",
    "EvidenceRepository",
    "EvidenceState",
    "EvidenceSummary",
    "ExtractRecord",
    "FetchAttemptRepository",
    "FetchAttemptSummary",
    "Inlink",
    "KnownUrl",
    "LatestObservation",
    "LinkRepository",
    "MatchSummary",
    "MediaOnPage",
    "MediaRecord",
    "MediaRepository",
    "MediaSighting",
    "MediaWithContent",
    "ObservationSummary",
    "PageIntelligenceRepository",
    "PageObservationRepository",
    "PageRevision",
    "PageVersionSighting",
    "ProjectionRepository",
    "RepresentationState",
    "RepresentationStatus",
    "RetentionDecision",
    "RevisionSighting",
    "SnapshotDecision",
    "TargetState",
    "UrlRepository",
]
