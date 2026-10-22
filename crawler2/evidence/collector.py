"""P12 collector (design §4): ``match.found`` → evidence candidate.

For a match, crawler2's registry gives the pages that carried the content
(P11's attribution rule: M5 content → media, M3 media → sightings within the
window, a sighting that probed *other* bytes is skipped). Each page is
resolved through W4 (the observation, its redirect chain and raw snapshot
reference) and W1 (the fetch attempt and worker). Nothing is re-fetched and
no artifact is created here; what the registry does not have is a ``Gap``.

The evidence id comes from E1 (one per match, LWT), and ``collected_at`` is
E1's ``opened_at``, so replays and concurrent hosts derive identical
candidates. A candidate with a finalized version is never changed by the
collector (``refresh`` + ``supersede`` is the explicit correction path).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from antipiracy_contracts.events import ContractError, EventEnvelope
from antipiracy_contracts.events.fingerprinting import MatchFound
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
from antipiracy_contracts.models.matching import MatchResult
from antipiracy_contracts.models.media import MediaObservation
from antipiracy_contracts.models.web import FetchAttempt, PageObservation, UrlRef
from pydantic import ValidationError

from crawler2.core.configuration import EvidenceSettings
from crawler2.core.observability import Metrics, get_logger
from crawler2.evidence.model import (
    CandidateStatus,
    EvidenceDossier,
    Gap,
    GapCode,
    MediaInfo,
    Redirect,
    SourceBasis,
    SourceRecord,
    TargetInfo,
)
from crawler2.evidence.store import EvidenceStore
from crawler2.storage.errors import StorageError
from crawler2.storage.events.consumer import IdempotentConsumer, RedisStreamReader, StreamEntry
from crawler2.storage.repositories import (
    MediaRecord,
    MediaSighting,
    MediaWithContent,
    TargetState,
)

_log = get_logger("evidence.collector")

MAX_SIGHTINGS_PER_MEDIA = 50
"""Same bound as P11 attribution, so both derive the same carrier set by default."""


class EvidenceMetrics:
    """Counters with bounded labels only (no URLs or ids)."""

    def __init__(self, metrics: Metrics | None) -> None:
        self._on = metrics is not None
        if metrics is not None:
            self._candidates = metrics.counter(
                "evidence_candidates_total",
                "match.found handled by the evidence collector, by result",
                ["result"],
            )
            self._status = metrics.counter(
                "evidence_candidate_status_total",
                "Candidates written, by completeness status",
                ["status"],
            )
            self._final = metrics.counter(
                "evidence_finalizations_total", "Finalization attempts, by result", ["result"]
            )
            self._artifacts = metrics.counter(
                "evidence_artifact_checks_total", "Artifact verifications, by state", ["state"]
            )
            self._exports = metrics.counter(
                "evidence_exports_total", "Case package exports, by result", ["result"]
            )

    def candidate(self, result: str, status: str | None = None) -> None:
        if self._on:
            self._candidates.labels(result).inc()
            if status is not None:
                self._status.labels(status).inc()

    def finalization(self, result: str) -> None:
        if self._on:
            self._final.labels(result).inc()

    def artifact(self, state: str) -> None:
        if self._on:
            self._artifacts.labels(state).inc()

    def export(self, result: str) -> None:
        if self._on:
            self._exports.labels(result).inc()


class MediaLookup(Protocol):
    def by_content(self, content_id: ContentId) -> list[MediaWithContent]: ...

    def sightings(
        self, media_id: MediaId, *, since: datetime, until: datetime, limit: int = 100
    ) -> list[MediaSighting]: ...

    def get(self, media_id: MediaId) -> MediaRecord | None: ...

    def observation(
        self, page_observation_id: ObservationId, media_id: MediaId
    ) -> MediaObservation | None: ...


class PageLookup(Protocol):
    def get(self, observation_id: ObservationId) -> PageObservation | None: ...


class AttemptLookup(Protocol):
    def get(self, fetch_attempt_id: FetchAttemptId) -> FetchAttempt | None: ...


class MatchProjections(Protocol):
    def target(self, target_id: TargetId) -> TargetState | None: ...

    def record_match(
        self,
        match: MatchResult,
        *,
        media_ids: Collection[MediaId],
        source_domains: Collection[DomainId],
    ) -> None: ...


class EvidenceOpener(Protocol):
    """The E1 part of ``EvidenceRepository``."""

    def open_candidate(
        self, match_id: MatchId, proposed: EvidenceId, *, at: datetime
    ) -> EvidenceId: ...

    def opened(self, match_id: MatchId) -> tuple[EvidenceId, datetime] | None: ...


_ITEM_GAPS = (
    Gap(
        code=GapCode.SCREENSHOT_NOT_COLLECTED,
        detail="no screenshot capture exists in crawler2 (ADR-005 Q2 open); none was taken",
    ),
    Gap(
        code=GapCode.MEDIA_SAMPLE_NOT_RETAINED,
        detail="crawler2 never stores media bytes; the content key digest of the probed "
        "sample is recorded instead",
    ),
    Gap(
        code=GapCode.RESPONSE_HEADERS_NOT_RETAINED,
        detail="fetch workers keep the body, status, content type, validators and redirect "
        "chain, not the full response headers (no WARC record)",
    ),
)


class EvidenceCollector:
    def __init__(
        self,
        *,
        evidence: EvidenceOpener,
        store: EvidenceStore,
        media: MediaLookup,
        pages: PageLookup,
        attempts: AttemptLookup,
        projections: MatchProjections,
        settings: EvidenceSettings | None = None,
        actor: str = "collector",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], EvidenceId] = EvidenceId.new,
        metrics: EvidenceMetrics | None = None,
    ) -> None:
        self._new_id = new_id
        self._evidence = evidence
        self._store = store
        self._media = media
        self._pages = pages
        self._attempts = attempts
        self._projections = projections
        self._cfg = settings or EvidenceSettings()
        self._actor = actor
        self._clock = clock
        self._m = metrics or EvidenceMetrics(None)

    def handle(self, envelope: EventEnvelope[MatchFound]) -> None:
        """Storage errors propagate (the event stays pending and is redelivered)."""
        self.collect(envelope.payload.match)

    def collect(self, match: MatchResult, *, refresh: bool = False) -> tuple[str, EvidenceDossier]:
        """(result, candidate); result ∈ created | updated | unchanged | frozen.

        ``refresh`` re-resolves a finalized candidate (before a supersession); it
        never touches a finalized version.
        """
        now = self._clock()
        evidence_id = self._evidence.open_candidate(match.match_id, self._new_id(), at=now)
        opened = self._evidence.opened(match.match_id)
        collected_at = opened[1] if opened is not None else now
        existing = self._store.candidate(evidence_id)
        finalized = existing is not None and (
            existing.status is CandidateStatus.FINALIZED or bool(self._store.versions(evidence_id))
        )
        if finalized and not refresh:
            assert existing is not None  # noqa: S101 -- implied by finalized
            self._m.candidate("frozen")
            return "frozen", existing.dossier
        dossier, media_ids, domains = self.resolve(match, evidence_id, collected_at)
        self._projections.record_match(match, media_ids=media_ids, source_domains=domains)
        if existing is not None and existing.dossier == dossier:
            self._m.candidate("unchanged")
            return "unchanged", dossier
        status = CandidateStatus.FINALIZED if finalized else dossier.status
        revision = existing.revision + 1 if existing is not None else 1
        self._store.put_candidate(dossier, status=status, revision=revision, updated_at=now)
        result = "updated" if existing is not None else "created"
        self._store.audit(
            evidence_id,
            "candidate_" + result,
            actor=self._actor,
            detail=f"revision={revision} status={dossier.status.value} "
            f"sources={len(dossier.sources)}/{dossier.sources_total} "
            f"gaps={','.join(sorted({g.code.value for g in dossier.gaps}))}",
            at=now,
        )
        self._m.candidate(result, dossier.status.value)
        if dossier.status is not CandidateStatus.COMPLETE:
            _log.info(
                "evidence_candidate_incomplete",
                evidence_id=str(evidence_id),
                status=dossier.status.value,
            )
        return result, dossier

    # --- resolution ---------------------------------------------------------------------

    def resolve(
        self, match: MatchResult, evidence_id: EvidenceId, collected_at: datetime
    ) -> tuple[EvidenceDossier, list[MediaId], list[DomainId]]:
        """The candidate, plus the P3-P5 match-projection inputs (same rule as P11)."""
        cfg = self._cfg
        carriers = self._media.by_content(match.content_id)[: cfg.max_media_per_match]
        window = timedelta(days=cfg.sighting_window_days)
        sighted: dict[tuple[str, str], tuple[MediaWithContent, MediaSighting]] = {}
        for item in carriers:
            for s in self._media.sightings(
                item.media_id,
                since=match.decided_at - window,
                until=match.decided_at + timedelta(days=1),
                limit=MAX_SIGHTINGS_PER_MEDIA,
            ):
                if s.content_id is not None and s.content_id != match.content_id:
                    continue  # the locator served other bytes on that page
                sighted[(str(s.page_observation_id), str(item.media_id))] = (item, s)
        domains = sorted({UrlRef.of(s.page_url).domain_id for _, s in sighted.values()}, key=str)

        def rank(pair: tuple[MediaWithContent, MediaSighting]) -> tuple[int, float, str, str]:
            item, s = pair
            verified = s.content_id == match.content_id
            return (
                0 if verified else 1,
                -s.observed_at.timestamp(),
                str(s.page_observation_id),
                str(item.media_id),
            )

        chosen = sorted(sighted.values(), key=rank)[: cfg.max_sources]
        sources = tuple(
            sorted((self._source(match, item, s) for item, s in chosen), key=lambda r: r.sort_key)
        )
        media = tuple(
            sorted(
                (self._media_info(m) for m in {str(i.media_id): i for i, _ in chosen}.values()),
                key=lambda m: m.media_id,
            )
        )
        target = self._target(match)
        gaps = self._gaps(match, sources, len(sighted))
        if target is None:
            gaps = tuple(
                sorted(
                    (
                        *gaps,
                        Gap(
                            code=GapCode.TARGET_UNKNOWN,
                            subject=str(match.target.target_id),
                            detail="target.registered has not reached crawler2's P2 projection",
                        ),
                    ),
                    key=lambda g: (g.code.value, g.subject or ""),
                )
            )
        dossier = EvidenceDossier(
            evidence_id=str(evidence_id),
            collected_at=collected_at,
            match=match,
            target=target,
            media=media,
            sources=sources,
            sources_total=len(sighted),
            gaps=gaps,
        )
        return dossier, [m.media_id for m in carriers], domains

    def _target(self, match: MatchResult) -> TargetInfo | None:
        state = self._projections.target(match.target.target_id)
        if state is None:
            return None
        t = state.target
        return TargetInfo(
            target_id=str(t.ref.target_id),
            version=t.ref.version,
            title=t.title,
            kind=t.kind.value,
            retired=not state.active,
        )

    def _media_info(self, item: MediaWithContent) -> MediaInfo:
        record = self._media.get(item.media_id)
        return MediaInfo(
            media_id=str(item.media_id),
            locator=item.locator_url,
            kind=record.media.kind.value if record is not None else None,
        )

    def _source(self, match: MatchResult, item: MediaWithContent, s: MediaSighting) -> SourceRecord:
        basis = (
            SourceBasis.CONTENT_VERIFIED
            if s.content_id == match.content_id
            else SourceBasis.LOCATOR_ONLY
        )
        fields: dict[str, Any] = {
            "page_observation_id": str(s.page_observation_id),
            "media_id": str(item.media_id),
            "media_locator": item.locator_url,
            "basis": basis,
            "sighted_at": s.observed_at,
            "page_url": s.page_url,
            "page_version_id": str(s.page_version_id),
            "probe_status": s.probe_status.value,
        }
        seen = self._media.observation(s.page_observation_id, item.media_id)
        if seen is not None and seen.content is not None:
            fields |= {
                "content_key_scheme": seen.content.scheme,
                "content_key_digest": seen.content.digest,
            }
        page = self._pages.get(s.page_observation_id)
        fields["observation_present"] = page is not None
        if page is not None:
            fields |= {
                "requested_url": page.requested.url,
                "final_url": page.final.url,
                "redirects": tuple(
                    Redirect(url=h.location.url, status=h.status) for h in page.redirects
                ),
                "http_status": page.http_status,
                "content_type": page.content_type,
                "capability": page.capability.value,
                "observed_at": page.observed_at,
                "body_digest": page.body_digest,
                "body_size": page.body_size,
                "snapshot": page.snapshot,
                "fetch_attempt_id": str(page.fetch_attempt_id),
            }
            attempt = self._attempts.get(page.fetch_attempt_id)
            if attempt is not None:
                fields |= {
                    "worker": attempt.worker,
                    "fetch_started_at": attempt.started_at,
                    "fetch_finished_at": attempt.finished_at,
                }
        return SourceRecord(**fields)

    @staticmethod
    def _gaps(match: MatchResult, sources: tuple[SourceRecord, ...], total: int) -> tuple[Gap, ...]:
        gaps: list[Gap] = list(_ITEM_GAPS)
        if not sources:
            gaps.append(
                Gap(
                    code=GapCode.UNATTRIBUTED,
                    detail="no page in the media registry carried the matched content within "
                    "the sighting window",
                )
            )
        elif not any(s.basis is SourceBasis.CONTENT_VERIFIED for s in sources):
            gaps.append(
                Gap(
                    code=GapCode.AMBIGUOUS_ATTRIBUTION,
                    detail="the media locator was seen on these pages, but none was probed "
                    "with the matched content there; no source is selected",
                )
            )
        if total > len(sources):
            gaps.append(
                Gap(
                    code=GapCode.SOURCES_TRUNCATED,
                    detail=f"{total} sightings found, {len(sources)} recorded (max_sources)",
                )
            )
        if match.verdict.value != "confirmed":
            gaps.append(
                Gap(
                    code=GapCode.VERDICT_NOT_CONFIRMED,
                    detail=f"matcher verdict is {match.verdict.value!r}; the contract requires "
                    "review before evidence",
                )
            )
        for s in sources:
            if not s.observation_present:
                gaps.append(
                    Gap(
                        code=GapCode.PAGE_OBSERVATION_MISSING,
                        subject=s.page_observation_id,
                        detail="the M3 sighting names a page observation that W4 does not hold",
                    )
                )
                continue
            if s.snapshot is None:
                gaps.append(
                    Gap(
                        code=GapCode.SNAPSHOT_NOT_ARCHIVED,
                        subject=s.page_observation_id,
                        detail="the page observation has no raw snapshot reference",
                    )
                )
            if s.worker is None:
                gaps.append(
                    Gap(
                        code=GapCode.FETCH_ATTEMPT_MISSING,
                        subject=s.page_observation_id,
                        detail="the fetch attempt row is gone (W1 rows expire after 30 days)",
                    )
                )
        return tuple(sorted(gaps, key=lambda g: (g.code.value, g.subject or "")))


class EvidenceLoop:
    """Consumer-group reader for ``match.found`` (same semantics as the P11 loop).

    Invalid events are logged, counted and acknowledged (they can never become
    valid); a storage error leaves the entry pending for redelivery
    (``XAUTOCLAIM`` after ``claim_idle_ms``). The processed-event marker is
    written only after the candidate is durably stored.
    """

    def __init__(
        self,
        reader: RedisStreamReader,
        consumer: IdempotentConsumer[Any],
        *,
        settings: EvidenceSettings,
        metrics: EvidenceMetrics | None = None,
    ) -> None:
        self._reader = reader
        self._consumer = consumer
        self._cfg = settings
        self._m = metrics or EvidenceMetrics(None)
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    def run_once(self) -> int:
        cfg = self._cfg
        entries = self._reader.claim_stale(min_idle_ms=cfg.claim_idle_ms, count=cfg.batch_size)
        if not entries:
            entries = self._reader.read(count=cfg.batch_size, block_ms=cfg.block_ms)
        done: list[StreamEntry] = []
        for entry in entries:
            try:
                self._consumer.handle(entry.envelope)  # duplicates: consumer_events_total
            except (ContractError, ValidationError):
                _log.exception("evidence_event_invalid", entry_id=entry.entry_id)
                self._m.candidate("invalid")
            except StorageError:
                _log.exception("evidence_storage_unavailable", entry_id=entry.entry_id)
                self._m.candidate("storage_error")
                break  # left pending; reclaimed once idle
            done.append(entry)
        self._reader.ack(done)
        return len(done)

    def run(self, *, max_idle_polls: int | None = None) -> None:
        idle = 0
        while not self._stopping:
            try:
                handled = self.run_once()
            except StorageError:
                _log.exception("evidence_poll_failed")
                time.sleep(1.0)
                continue
            idle = 0 if handled else idle + 1
            if max_idle_polls is not None and idle >= max_idle_polls:
                return
