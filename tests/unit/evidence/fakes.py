"""In-memory stores and a small fixture world for the P12 unit tests.

The registry and projections are the P11 fakes; the object store keeps real
bytes and real SHA-256 digests, so artifact verification is exercised, not mocked.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events import EventEnvelope
from antipiracy_contracts.events.evidence import EvidenceFinalized
from antipiracy_contracts.ids import (
    ContentId,
    DomainId,
    EvidenceId,
    FetchAttemptId,
    MatchId,
    MediaId,
    ObservationId,
    TargetId,
)
from antipiracy_contracts.models.blobs import BlobRef
from antipiracy_contracts.models.evidence import EvidenceCandidate, EvidenceSeal
from antipiracy_contracts.models.matching import MatchResult, MatchVerdict
from antipiracy_contracts.models.media import MediaObservation
from antipiracy_contracts.models.targets import Target
from antipiracy_contracts.models.web import (
    FetchAttempt,
    FetchCapability,
    FetchOutcome,
    PageObservation,
    RedirectHop,
    UrlRef,
)

from crawler2.core.configuration import EvidenceSettings
from crawler2.evidence.collector import EvidenceCollector
from crawler2.evidence.finalize import EvidenceFinalizer
from crawler2.evidence.model import CandidateStatus, EvidenceDossier, ValidationReport
from crawler2.evidence.store import (
    AuditEntry,
    Campaign,
    FinalizedVersion,
    StoredCandidate,
)
from crawler2.storage.errors import ConflictError, IntegrityError, StorageUnavailableError
from crawler2.storage.layout import month_bucket
from crawler2.storage.objectstore.base import ObjectInfo, Payload
from crawler2.storage.objectstore.layout import raw_key, uri_of
from crawler2.storage.repositories import (
    EvidenceRecord,
    EvidenceState,
    EvidenceSummary,
    MatchSummary,
    MediaRecord,
    MediaSighting,
    MediaWithContent,
)
from tests.fixtures.contracts import (
    T0,
    WORKER,
    content_key,
    match,
    media,
    media_observation,
    page_observation,
    seeded_uuid7,
    target,
)
from tests.unit.intelligence.feedback_fakes import Projections as _P11Projections
from tests.unit.intelligence.feedback_fakes import Registry

BUCKET = "crawler2-raw"


@dataclass
class Projections(_P11Projections):
    """P11's projection fake plus the media ids of each match (P4 ``media_ids``)."""

    media: dict[str, frozenset[MediaId]] = field(default_factory=dict)

    def record_match(
        self,
        match: MatchResult,
        *,
        media_ids: Collection[MediaId],
        source_domains: Collection[DomainId],
    ) -> None:
        super().record_match(match, media_ids=media_ids, source_domains=source_domains)
        self.media[str(match.match_id)] = frozenset(media_ids)

    def matches_for_target(self, target_id: TargetId, month: datetime) -> list[MatchSummary]:
        return [
            replace(s, media_ids=self.media.get(str(s.match_id), frozenset()))
            for s in super().matches_for_target(target_id, month)
        ]


@dataclass
class MemoryObjects:
    blobs: dict[str, bytes] = field(default_factory=dict)
    puts: int = 0
    bucket: str = BUCKET
    fail_puts: int = 0

    def put(
        self,
        data: Payload,
        *,
        media_type: str | None = None,
        expected_digest: ContentDigest | None = None,
    ) -> BlobRef:
        if self.fail_puts:
            self.fail_puts -= 1
            raise StorageUnavailableError("object store down")
        assert isinstance(data, bytes)
        digest = ContentDigest.of_bytes(data)
        if expected_digest is not None and expected_digest != digest:
            raise IntegrityError("digest mismatch")
        self.puts += 1
        self.blobs.setdefault(digest.hex, data)
        return BlobRef(
            uri=uri_of(self.bucket, raw_key(digest)),
            digest=digest,
            size_bytes=len(data),
            media_type=media_type,
        )

    def get(self, ref: BlobRef) -> bytes:
        data = self.blobs.get(ref.digest.hex)
        if data is None:
            raise StorageUnavailableError("404")
        if hashlib.sha256(data).hexdigest() != ref.digest.hex or len(data) != ref.size_bytes:
            raise IntegrityError(f"{ref.uri}: stored bytes do not match")
        return data

    def stat(self, digest: ContentDigest) -> ObjectInfo | None:
        data = self.blobs.get(digest.hex)
        if data is None:
            return None
        # Like S3ObjectStore: the digest comes from the object's metadata, not a re-hash.
        return ObjectInfo(uri_of(self.bucket, raw_key(digest)), digest, len(data), None)

    def corrupt(self, digest: ContentDigest, data: bytes) -> None:
        """Replace stored bytes behind a digest (same length: only a re-hash notices)."""
        self.blobs[digest.hex] = data

    def delete(self, digest: ContentDigest) -> None:
        del self.blobs[digest.hex]


@dataclass
class MemoryEvidenceRepo:
    """E1, E2 and E4 with the Scylla repository's semantics (LWT outcomes)."""

    opened_rows: dict[str, tuple[EvidenceId, datetime]] = field(default_factory=dict)
    records: dict[str, EvidenceRecord] = field(default_factory=dict)
    index: dict[tuple[str, int], set[tuple[str, str, datetime]]] = field(default_factory=dict)
    events: list[EventEnvelope[EvidenceFinalized]] = field(default_factory=list)
    fail_seal: int = 0

    def open_candidate(
        self, match_id: MatchId, proposed: EvidenceId, *, at: datetime
    ) -> EvidenceId:
        self.opened_rows.setdefault(str(match_id), (proposed, at))
        return self.opened_rows[str(match_id)][0]

    def opened(self, match_id: MatchId) -> tuple[EvidenceId, datetime] | None:
        return self.opened_rows.get(str(match_id))

    def create_candidate(
        self, candidate: EvidenceCandidate, *, event: object | None = None
    ) -> EvidenceRecord:
        key = str(candidate.evidence_id)
        existing = self.records.get(key)
        if existing is not None and existing.candidate != candidate:
            raise ConflictError("different candidate")
        if existing is None:
            self.records[key] = EvidenceRecord(candidate, EvidenceState.COLLECTING, None)
        return self.records[key]

    def seal(
        self, seal: EvidenceSeal, *, event: EventEnvelope[EvidenceFinalized] | None = None
    ) -> EvidenceRecord:
        if self.fail_seal:
            self.fail_seal -= 1
            raise StorageUnavailableError("scylla timeout")
        record = self.records[str(seal.evidence_id)]
        if record.seal is not None and record.seal != seal:
            raise ConflictError("sealed with another manifest")
        self.records[str(seal.evidence_id)] = EvidenceRecord(
            record.candidate, EvidenceState.SEALED, seal
        )
        if event is not None:
            self.events.append(event)
        return self.records[str(seal.evidence_id)]

    def get(self, evidence_id: EvidenceId) -> EvidenceRecord | None:
        return self.records.get(str(evidence_id))

    def index_candidate(self, dossier: EvidenceDossier) -> None:
        key = (str(dossier.match.target.target_id), month_bucket(dossier.collected_at))
        self.index.setdefault(key, set()).add(
            (dossier.evidence_id, str(dossier.match.match_id), dossier.collected_at)
        )

    def for_target(self, target_id: TargetId, month: datetime) -> list[EvidenceSummary]:
        rows = self.index.get((str(target_id), month_bucket(month)), set())
        return [EvidenceSummary(EvidenceId(e), MatchId(m), at, None) for e, m, at in sorted(rows)]


@dataclass
class MemoryEvidenceStore:
    rows: dict[str, StoredCandidate] = field(default_factory=dict)
    version_rows: dict[str, list[FinalizedVersion]] = field(default_factory=dict)
    log: dict[str, list[AuditEntry]] = field(default_factory=dict)
    campaign_rows: dict[str, Campaign] = field(default_factory=dict)
    index: MemoryEvidenceRepo | None = None
    fail_claims: int = 0
    candidate_writes: int = 0

    def candidate(self, evidence_id: EvidenceId) -> StoredCandidate | None:
        return self.rows.get(str(evidence_id))

    def put_candidate(
        self,
        dossier: EvidenceDossier,
        *,
        status: CandidateStatus,
        revision: int,
        updated_at: datetime,
    ) -> None:
        self.candidate_writes += 1
        old = self.rows.get(dossier.evidence_id)
        self.rows[dossier.evidence_id] = StoredCandidate(
            dossier,
            status,
            revision,
            updated_at,
            old.validation if old else None,
            old.validated_at if old else None,
        )
        if self.index is not None:
            self.index.index_candidate(dossier)

    def set_status(self, evidence_id: EvidenceId, status: CandidateStatus) -> None:
        self.rows[str(evidence_id)] = replace(self.rows[str(evidence_id)], status=status)

    def record_validation(
        self, evidence_id: EvidenceId, report: ValidationReport, *, at: datetime
    ) -> None:
        self.rows[str(evidence_id)] = replace(
            self.rows[str(evidence_id)], validation=report, validated_at=at
        )

    def versions(self, evidence_id: EvidenceId) -> list[FinalizedVersion]:
        return sorted(self.version_rows.get(str(evidence_id), []), key=lambda v: -v.version)

    def claim_version(self, row: FinalizedVersion) -> tuple[bool, FinalizedVersion]:
        if self.fail_claims:
            self.fail_claims -= 1
            raise StorageUnavailableError("LWT timeout")
        rows = self.version_rows.setdefault(str(row.evidence_id), [])
        for v in rows:
            if v.version == row.version:
                return False, v
        rows.append(row)
        return True, row

    def audit(
        self, evidence_id: EvidenceId, action: str, *, actor: str, detail: str, at: datetime
    ) -> None:
        self.log.setdefault(str(evidence_id), []).append(AuditEntry(at, action, actor, detail))

    def audit_log(self, evidence_id: EvidenceId) -> list[AuditEntry]:
        return list(self.log.get(str(evidence_id), []))

    def actions(self, evidence_id: EvidenceId | str) -> list[str]:
        return [a.action for a in self.log.get(str(evidence_id), [])]

    def create_campaign(self, campaign: Campaign) -> None:
        self.campaign_rows[campaign.campaign_id] = campaign

    def add_campaign_target(self, campaign_id: str, target_id: TargetId, *, at: datetime) -> None:
        c = self.campaign_rows[campaign_id]
        ids = tuple(sorted({*c.target_ids, target_id}, key=str))
        self.campaign_rows[campaign_id] = replace(c, target_ids=ids)

    def campaign(self, campaign_id: str) -> Campaign | None:
        return self.campaign_rows.get(campaign_id)

    def campaigns(self) -> list[str]:
        return sorted(self.campaign_rows)


@dataclass
class MediaView:
    """``MediaLookup`` over the P11 ``Registry`` (whose ``get`` is the page lookup)."""

    registry: Registry

    def by_content(self, content_id: ContentId) -> list[MediaWithContent]:
        return self.registry.by_content(content_id)

    def sightings(
        self, media_id: MediaId, *, since: datetime, until: datetime, limit: int = 100
    ) -> list[MediaSighting]:
        return self.registry.sightings(media_id, since=since, until=until, limit=limit)

    def get(self, media_id: MediaId) -> MediaRecord | None:
        for o in self.registry.observations:
            if o.media.media_id == media_id:
                return MediaRecord(o.media, o.observed_at, o.observed_at)
        return None

    def observation(
        self, page_observation_id: ObservationId, media_id: MediaId
    ) -> MediaObservation | None:
        for o in self.registry.observations:
            if o.page_observation_id == page_observation_id and o.media.media_id == media_id:
                return o
        return None


@dataclass
class Attempts:
    rows: dict[str, FetchAttempt] = field(default_factory=dict)

    def get(self, fetch_attempt_id: FetchAttemptId) -> FetchAttempt | None:
        return self.rows.get(str(fetch_attempt_id))


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        self.at += timedelta(seconds=1)
        return self.at


@dataclass
class World:
    """A registry with pages, snapshots, fetch attempts, targets and the evidence stores."""

    registry: Registry = field(default_factory=Registry)
    projections: Projections = field(default_factory=Projections)
    objects: MemoryObjects = field(default_factory=MemoryObjects)
    attempts: Attempts = field(default_factory=Attempts)
    repo: MemoryEvidenceRepo = field(default_factory=MemoryEvidenceRepo)
    store: MemoryEvidenceStore = field(default_factory=MemoryEvidenceStore)
    clock: Clock = field(default_factory=lambda: Clock(T0 + timedelta(days=1)))
    settings: EvidenceSettings = field(default_factory=EvidenceSettings)
    allocated: int = 0

    def __post_init__(self) -> None:
        self.store.index = self.repo

    def next_id(self) -> EvidenceId:
        """Deterministic evidence ids: equal worlds allocate equal ids (reproducibility tests)."""
        self.allocated += 1
        return EvidenceId.from_uuid(seeded_uuid7(f"evidence:{self.allocated}", T0))

    def register(self, seed: str) -> Target:
        t = target(seed)
        self.projections.apply_target_registered(t)
        return t

    def page(
        self,
        page_url: str,
        media_path: str,
        data: bytes | None,
        *,
        at: datetime = T0,
        body: bytes | None = None,
        cdn: str = "cdn.example",
        redirect_from: str | None = None,
        snapshot: bool = True,
        observation: bool = True,
        attempt: bool = True,
    ) -> tuple[PageObservation, MediaId]:
        """A page carrying a media locator; ``data`` is the probed content (None: not probed)."""
        ref = UrlRef.of(page_url)
        html = body or f"<html><video src='https://{cdn}/{media_path}'></video></html>".encode()
        blob = self.objects.put(html, media_type="text/html") if snapshot else None
        page = page_observation(ref, at=at, body=html, snapshot=blob)
        if redirect_from is not None:
            page = page.model_copy(
                update={
                    "requested": UrlRef.of(redirect_from),
                    "redirects": (RedirectHop(location=ref, status=301),),
                    "capability": FetchCapability.BROWSER,
                }
            )
        item = media(media_path, host=cdn)
        if observation:
            self.registry.pages[str(page.observation_id)] = page
        self.registry.observations.append(
            media_observation(item, page, content=content_key(data) if data is not None else None)
        )
        if attempt:
            self.attempts.rows[str(page.fetch_attempt_id)] = FetchAttempt(
                fetch_attempt_id=page.fetch_attempt_id,
                requested=page.requested,
                capability=page.capability,
                worker=WORKER,
                started_at=at - timedelta(seconds=2),
                finished_at=at,
                outcome=FetchOutcome.RESPONSE,
                http_status=page.http_status,
                final=page.final,
                redirects=page.redirects,
                bytes_received=len(html),
            )
        return page, item.media_id

    def match(
        self,
        data: bytes,
        t: Target,
        *,
        at: datetime = T0 + timedelta(hours=1),
        verdict: MatchVerdict = MatchVerdict.CONFIRMED,
        confidence: float = 0.97,
    ) -> MatchResult:
        m = match(content_key(data).content_id, t.ref, at=at)
        return m.model_copy(update={"verdict": verdict, "confidence": confidence})

    def collector(self) -> EvidenceCollector:
        return EvidenceCollector(
            evidence=self.repo,
            store=self.store,
            media=MediaView(self.registry),
            pages=self.registry,
            attempts=self.attempts,
            projections=self.projections,
            settings=self.settings,
            clock=self.clock,
            new_id=self.next_id,
        )

    def finalizer(self) -> EvidenceFinalizer:
        return EvidenceFinalizer(
            store=self.store,
            seals=self.repo,
            objects=self.objects,
            pages=self.registry,
            producer_instance=WORKER.replace(":http:", ":finalizer:"),
            bucket=self.objects.bucket,
            max_artifact_bytes=self.settings.max_artifact_bytes,
            clock=self.clock,
        )
