"""Deterministic builders of contract-shaped values for storage tests and benchmarks.

Everything goes through the real P1 constructors and validators, so rows
written by tests and by the partition benchmark have production shapes.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events import EventEnvelope, EventPayload, new_event
from antipiracy_contracts.ids import (
    ContentId,
    EventId,
    EvidenceId,
    FetchAttemptId,
    ObservationId,
    PageVersionId,
    RepresentationId,
    TargetId,
)
from antipiracy_contracts.models.blobs import BlobRef
from antipiracy_contracts.models.evidence import EvidenceCandidate
from antipiracy_contracts.models.matching import MatchResult, MatchVerdict, Technique
from antipiracy_contracts.models.media import (
    ContentKey,
    Media,
    MediaKind,
    MediaObservation,
    ProbeStatus,
)
from antipiracy_contracts.models.representations import Representation, RepresentationSpec
from antipiracy_contracts.models.targets import Target, TargetKind, TargetRef
from antipiracy_contracts.models.web import (
    DiscoveredLink,
    FetchAttempt,
    FetchCapability,
    FetchOutcome,
    HttpValidators,
    LinkRelation,
    PageObservation,
    UrlRef,
)
from antipiracy_contracts.ownership import Component, Producer, ServiceName

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
"""Fixed past instant: test facts must not lie in the future (Scylla refuses > 3 days)."""
WORKER = "dev-1:http:1:" + "0" * 32
SPEC = RepresentationSpec(
    name="dinov2-vits14", version="1", config_digest=ContentDigest.of_bytes(b"config")
)


def seeded_uuid7(seed: str, at: datetime) -> uuid.UUID:
    """A UUIDv7 whose random bits come from ``seed``: deterministic allocated IDs."""
    millis = int(at.timestamp() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(hashlib.sha256(seed.encode()).digest()[:10], "big")
    value = millis << 80 | rand
    value = (value & ~(0xF << 76)) | (7 << 76)
    value = (value & ~(0x3 << 62)) | (0x2 << 62)
    return uuid.UUID(int=value)


def producer(component: Component, instance: str = WORKER) -> Producer:
    return Producer(service=component.service, component=component, instance=instance)


def event_of[P: EventPayload](
    payload: P, component: Component, *, at: datetime = T0, event_id: EventId | None = None
) -> EventEnvelope[P]:
    return new_event(payload, producer=producer(component), occurred_at=at, event_id=event_id)


def url(path: str, host: str = "site.example") -> UrlRef:
    return UrlRef.of(f"https://{host}/{path.lstrip('/')}")


def fetch_attempt(
    target: UrlRef,
    *,
    at: datetime = T0,
    seed: str | None = None,
    outcome: FetchOutcome = FetchOutcome.RESPONSE,
) -> FetchAttempt:
    ident = FetchAttemptId.from_uuid(seeded_uuid7(seed or f"fat:{target.url}:{at}", at))
    response = outcome is FetchOutcome.RESPONSE
    return FetchAttempt(
        fetch_attempt_id=ident,
        requested=target,
        capability=FetchCapability.HTTP,
        worker=WORKER,
        started_at=at,
        finished_at=at + timedelta(milliseconds=850),
        outcome=outcome,
        http_status=200 if response else None,
        final=target if response else None,
        bytes_received=48_213 if response else None,
    )


def page_observation(
    target: UrlRef,
    *,
    at: datetime = T0,
    body: bytes = b"<html>v1</html>",
    seed: str | None = None,
    snapshot: BlobRef | None = None,
    status: int = 200,
) -> PageObservation:
    key = seed or f"obs:{target.url}:{at.isoformat()}"
    digest = ContentDigest.of_bytes(body)
    return PageObservation(
        observation_id=ObservationId.from_uuid(seeded_uuid7(key, at)),
        fetch_attempt_id=FetchAttemptId.from_uuid(seeded_uuid7("fat:" + key, at)),
        requested=target,
        final=target,
        capability=FetchCapability.HTTP,
        observed_at=at,
        http_status=status,
        content_type="text/html; charset=utf-8",
        body_digest=digest,
        body_size=len(body),
        page_version_id=PageVersionId.of(target.url_id, digest),
        validators=HttpValidators(etag=f'"{digest.hex[:16]}"', last_modified=None),
        snapshot=snapshot,
    )


def links(
    count: int, *, host: str = "site.example", prefix: str = "p"
) -> tuple[DiscoveredLink, ...]:
    return tuple(
        DiscoveredLink(
            target=url(f"{prefix}/{i}", host),
            relation=LinkRelation.ANCHOR,
            anchor_text=f"link {i}",
        )
        for i in range(count)
    )


def media(path: str, host: str = "cdn.example") -> Media:
    return Media.of(UrlRef.of(f"https://{host}/{path.lstrip('/')}"), MediaKind.VIDEO_FILE)


def content_key(data: bytes) -> ContentKey:
    return ContentKey.of("sampled-bytes/v1", ContentDigest.of_bytes(data))


def media_observation(
    item: Media,
    page: PageObservation,
    *,
    content: ContentKey | None = None,
    at: datetime | None = None,
) -> MediaObservation:
    return MediaObservation(
        media=item,
        page_observation_id=page.observation_id,
        page=page.requested,
        page_version_id=page.page_version_id,
        observed_at=at or page.observed_at,
        probe_status=ProbeStatus.PROBED if content else ProbeStatus.NOT_PROBED,
        content=content,
    )


def target(seed: str, *, version: int = 1, at: datetime = T0) -> Target:
    return Target(
        ref=TargetRef(
            target_id=TargetId.from_uuid(seeded_uuid7("tgt:" + seed, T0)), version=version
        ),
        title=f"Protected work {seed} v{version}",
        kind=TargetKind.MOVIE,
        registered_at=at,
    )


def representation(content_id: ContentId, *, at: datetime = T0) -> Representation:
    return Representation(
        representation_id=Representation.expected_id(content_id, SPEC),
        content_id=content_id,
        spec=SPEC,
        created_at=at,
        segment_count=12,
    )


def match(content_id: ContentId, ref: TargetRef, *, at: datetime = T0) -> MatchResult:
    rep = Representation.expected_id(content_id, SPEC)
    target_rep = RepresentationId.for_target(
        ref.target_id, ref.version, SPEC.name, SPEC.version, SPEC.config_digest
    )
    technique = Technique(name="temporal-align", version="1")
    return MatchResult(
        match_id=MatchResult.expected_id(rep, target_rep, technique),
        content_id=content_id,
        representation_id=rep,
        target=ref,
        target_representation_id=target_rep,
        technique=technique,
        verdict=MatchVerdict.CONFIRMED,
        confidence=0.97,
        decided_at=at,
    )


def evidence_candidate(
    result: MatchResult,
    evidence_id: EvidenceId,
    item: Media,
    page: PageObservation,
    *,
    at: datetime = T0,
) -> EvidenceCandidate:
    return EvidenceCandidate(
        evidence_id=evidence_id,
        match_id=result.match_id,
        target=result.target,
        content_id=result.content_id,
        media_ids=(item.media_id,),
        page_observation_ids=(page.observation_id,),
        collected_at=at,
    )


__all__ = [
    "SPEC",
    "T0",
    "ServiceName",
    "content_key",
    "event_of",
    "evidence_candidate",
    "fetch_attempt",
    "links",
    "match",
    "media",
    "media_observation",
    "page_observation",
    "producer",
    "representation",
    "seeded_uuid7",
    "target",
    "url",
]
