"""In-memory registry, projections and feedback store for the P11 unit tests."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from antipiracy_contracts.events.targets import TargetRetired
from antipiracy_contracts.ids import ContentId, DomainId, MediaId, ObservationId, TargetId
from antipiracy_contracts.models.matching import MatchResult
from antipiracy_contracts.models.media import Media, MediaObservation
from antipiracy_contracts.models.targets import Target
from antipiracy_contracts.models.web import FetchCapability, PageObservation, UrlRef

from crawler2.intelligence.feedback import MatchEvidence
from crawler2.storage.errors import StorageUnavailableError
from crawler2.storage.repositories import (
    MatchSummary,
    MediaSighting,
    MediaWithContent,
    TargetState,
)
from tests.fixtures.contracts import content_key, media, media_observation, page_observation


@dataclass
class Registry:
    """M3/M5 and W4 for a handful of pages: what attribution reads."""

    pages: dict[str, PageObservation] = field(default_factory=dict)
    observations: list[MediaObservation] = field(default_factory=list)
    fail: bool = False

    def add(
        self,
        page_url: str,
        media_path: str,
        data: bytes,
        *,
        at: datetime,
        capability: FetchCapability = FetchCapability.HTTP,
        cdn: str = "cdn.example",
    ) -> tuple[PageObservation, Media, ContentId]:
        page = page_observation(UrlRef.of(page_url), at=at).model_copy(
            update={"capability": capability}
        )
        item = media(media_path, host=cdn)
        key = content_key(data)
        self.pages[str(page.observation_id)] = page
        self.observations.append(media_observation(item, page, content=key))
        return page, item, key.content_id

    # MediaRepository (the parts attribution uses)
    def by_content(self, content_id: ContentId) -> list[MediaWithContent]:
        self._check()
        seen: dict[str, MediaWithContent] = {}
        for o in self.observations:
            if o.content is not None and o.content.content_id == content_id:
                seen.setdefault(
                    str(o.media.media_id),
                    MediaWithContent(o.media.media_id, o.media.locator.url, o.observed_at),
                )
        return list(seen.values())

    def sightings(
        self, media_id: MediaId, *, since: datetime, until: datetime, limit: int = 100
    ) -> list[MediaSighting]:
        self._check()
        rows = [
            MediaSighting(
                page_observation_id=o.page_observation_id,
                observed_at=o.observed_at,
                page_url_id=o.page.url_id,
                page_url=o.page.url,
                page_version_id=o.page_version_id,
                probe_status=o.probe_status,
                content_id=o.content.content_id if o.content else None,
            )
            for o in self.observations
            if o.media.media_id == media_id and since <= o.observed_at <= until
        ]
        return sorted(rows, key=lambda s: s.observed_at, reverse=True)[:limit]

    # PageObservationRepository.get
    def get(self, observation_id: ObservationId) -> PageObservation | None:
        self._check()
        return self.pages.get(str(observation_id))

    def _check(self) -> None:
        if self.fail:
            raise StorageUnavailableError("registry down")


@dataclass
class Projections:
    matches: dict[str, tuple[MatchResult, frozenset[str]]] = field(default_factory=dict)
    target_rows: dict[str, TargetState] = field(default_factory=dict)

    def record_match(
        self,
        match: MatchResult,
        *,
        media_ids: Collection[MediaId],
        source_domains: Collection[DomainId],
    ) -> None:
        self.matches[str(match.match_id)] = (match, frozenset(str(d) for d in source_domains))

    def apply_target_registered(self, target: Target) -> None:
        old = self.target_rows.get(str(target.ref.target_id))
        if old is None or old.target.ref.version <= target.ref.version:
            retired = old.retired_at if old else None
            reason = old.retire_reason if old else None
            self.target_rows[str(target.ref.target_id)] = TargetState(target, retired, reason)

    def apply_target_retired(self, retired: TargetRetired) -> None:
        old = self.target_rows.get(str(retired.target_id))
        if old is not None:
            self.target_rows[str(retired.target_id)] = TargetState(
                old.target, retired.retired_at, retired.reason
            )

    def targets(self) -> list[TargetState]:
        return list(self.target_rows.values())

    def matches_for_target(self, target_id: TargetId, month: datetime) -> list[MatchSummary]:
        return [
            MatchSummary(
                m.match_id,
                m.target.target_id,
                m.target.version,
                m.content_id,
                m.verdict,
                m.confidence,
                m.decided_at,
            )
            for m, _ in self.matches.values()
            if m.target.target_id == target_id
        ]

    def matches_for_content(self, content_id: ContentId) -> list[MatchResult]:
        return [m for m, _ in self.matches.values() if m.content_id == content_id]

    def target(self, target_id: TargetId) -> TargetState | None:
        return self.target_rows.get(str(target_id))


@dataclass
class MemoryFeedbackStore:
    rows: dict[tuple[str, str, str], MatchEvidence] = field(default_factory=dict)
    writes: int = 0
    fail: bool = False

    def put_evidence(self, rows: Sequence[MatchEvidence]) -> None:
        if self.fail:
            raise StorageUnavailableError("feedback store down")
        for e in rows:
            self.rows[(e.domain_id, e.match_id, e.page_observation_id)] = e
            self.writes += 1

    def sources(self) -> list[str]:
        if self.fail:
            raise StorageUnavailableError("feedback store down")
        return sorted({k[0] for k in self.rows})

    def evidence(self, domain_ids: Collection[str]) -> dict[str, list[MatchEvidence]]:
        found: dict[str, list[MatchEvidence]] = {}
        for (d, _, _), e in sorted(self.rows.items()):
            if d in domain_ids:
                found.setdefault(d, []).append(e)
        return found

    def clear(self) -> int:
        n = len({k[0] for k in self.rows})
        self.rows.clear()
        return n


class MemoryProcessed:
    def __init__(self) -> None:
        self.keys: set[tuple[str, str]] = set()

    def is_processed(self, consumer: str, key: str) -> bool:
        return (consumer, key) in self.keys

    def mark_processed(
        self, consumer: str, key: str, *, event_id: str, event_type: str, at: datetime
    ) -> None:
        self.keys.add((consumer, key))
