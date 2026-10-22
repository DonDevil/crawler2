"""P11 runtime: match.found → evidence, and evidence → admission priority (design §4, §6).

``MatchFeedback`` is the ``IdempotentConsumer`` handler for ``match.found``:
it attributes the matched content to the pages that carried it through
crawler2's own registry (M5 content → media, M3 media → page observations,
W4 observation → capability) and writes ``MatchEvidence`` rows. It also
records the match in the existing P3-P5 projections. ``TargetFeedback``
keeps the P2 target projection current from ``target.registered`` /
``target.retired``, which gives the relevance of each target.

``FeedbackPrioritizer`` is what admission calls: in memory, never raising,
never doing I/O. A background thread swaps in a fresh snapshot; when the
snapshot is missing or too old, or the feature is disabled, the decision is
the base priority with a ``fallback`` reason (B.5 #1).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from antipiracy_contracts.events import ContractError, EventEnvelope
from antipiracy_contracts.events.fingerprinting import MatchFound
from antipiracy_contracts.events.targets import TargetRegistered, TargetRetired
from antipiracy_contracts.ids import ContentId, DomainId, MediaId, ObservationId, TargetId
from antipiracy_contracts.models.matching import MatchResult
from antipiracy_contracts.models.targets import Target
from antipiracy_contracts.models.web import PageObservation, UrlRef
from pydantic import ValidationError

from crawler2.core.configuration import FeedbackSettings, Settings
from crawler2.core.observability import Metrics, get_logger
from crawler2.intelligence.feedback import (
    DEFAULT_FEEDBACK_POLICY,
    FeedbackPolicy,
    MatchEvidence,
    PriorityDecision,
    SourceFeedback,
    TargetRelevance,
    assess_source,
    decide,
    page_shape,
)
from crawler2.intelligence.feedback_store import FeedbackStore
from crawler2.storage.errors import StorageError
from crawler2.storage.events.consumer import IdempotentConsumer, RedisStreamReader, StreamEntry
from crawler2.storage.repositories import MatchSummary, MediaSighting, MediaWithContent, TargetState

_log = get_logger("intelligence.feedback")

MAX_MEDIA_PER_CONTENT = 50
MAX_SIGHTINGS_PER_MEDIA = 50
SIGHTING_WINDOW = timedelta(days=90)
"""Pages observed up to this long before the decision count as carriers."""


class FeedbackMetrics:
    def __init__(self, metrics: Metrics | None) -> None:
        self._c: Any = None
        if metrics is not None:
            self._c = metrics.counter(
                "feedback_matches_total",
                "match.found events handled by P11 feedback, by result",
                ["result"],
            )
            self._p = metrics.counter(
                "feedback_priority_total",
                "Admission priority decisions by P11 feedback: boosted, neutral or fallback reason",
                ["result"],
            )
            self._e = metrics.counter(
                "feedback_evidence_rows_total", "Evidence rows written (upserts, replays included)"
            )

    def match(self, result: str) -> None:
        if self._c is not None:
            self._c.labels(result).inc()

    def priority(self, result: str) -> None:
        if self._c is not None:
            self._p.labels(result).inc()

    def evidence(self, n: int) -> None:
        if self._c is not None and n:
            self._e.inc(n)


# --- match.found → evidence ---------------------------------------------------------


class MediaLookup(Protocol):
    """The parts of ``MediaRepository`` (M3, M5) attribution reads."""

    def by_content(self, content_id: ContentId) -> list[MediaWithContent]: ...

    def sightings(
        self, media_id: MediaId, *, since: datetime, until: datetime, limit: int = 100
    ) -> list[MediaSighting]: ...


class PageLookup(Protocol):
    def get(self, observation_id: ObservationId) -> PageObservation | None: ...


class MatchProjections(Protocol):
    """The parts of ``ProjectionRepository`` (P2-P5) P11 writes and reads."""

    def record_match(
        self,
        match: MatchResult,
        *,
        media_ids: Collection[MediaId],
        source_domains: Collection[DomainId],
    ) -> None: ...

    def apply_target_registered(self, target: Target) -> None: ...

    def apply_target_retired(self, retired: TargetRetired) -> None: ...

    def targets(self) -> list[TargetState]: ...

    def matches_for_target(self, target_id: TargetId, month: datetime) -> list[MatchSummary]: ...

    def matches_for_content(self, content_id: ContentId) -> list[MatchResult]: ...


class MatchFeedback:
    def __init__(
        self,
        *,
        media: MediaLookup,
        pages: PageLookup,
        projections: MatchProjections,
        store: FeedbackStore,
        metrics: FeedbackMetrics | None = None,
    ) -> None:
        self._media = media
        self._pages = pages
        self._projections = projections
        self._store = store
        self._m = metrics or FeedbackMetrics(None)

    def handle(self, envelope: EventEnvelope[MatchFound]) -> None:
        """Storage errors propagate (the event stays pending and is redelivered)."""
        result = self.apply(envelope.payload.match)
        self._m.match(result)

    def attribute(self, match: MatchResult) -> tuple[list[MediaId], list[MatchEvidence]]:
        """(media ids, evidence rows) from crawler2's registry; never from the event's guesses."""
        media = self._media.by_content(match.content_id)[:MAX_MEDIA_PER_CONTENT]
        rows: dict[tuple[str, str], MatchEvidence] = {}
        for item in media:
            sightings = self._media.sightings(
                item.media_id,
                since=match.decided_at - SIGHTING_WINDOW,
                until=match.decided_at + timedelta(days=1),
                limit=MAX_SIGHTINGS_PER_MEDIA,
            )
            for s in sightings:
                if s.content_id is not None and s.content_id != match.content_id:
                    continue  # the locator served other bytes on that page
                page = UrlRef.of(s.page_url)
                observation = self._pages.get(s.page_observation_id)
                template, kind = page_shape(page.url)
                e = MatchEvidence(
                    domain_id=str(page.domain_id),
                    match_id=str(match.match_id),
                    page_observation_id=str(s.page_observation_id),
                    target_id=str(match.target.target_id),
                    target_version=match.target.version,
                    decided_at=match.decided_at,
                    confidence=match.confidence,
                    verdict=match.verdict.value,
                    page_url=page.url,
                    template=template,
                    page_kind=kind,
                    capability=observation.capability.value if observation else None,
                )
                rows[(e.domain_id, e.page_observation_id)] = e
        return [m.media_id for m in media], sorted(
            rows.values(), key=lambda e: (e.domain_id, e.page_observation_id)
        )

    def apply(self, match: MatchResult) -> str:
        media_ids, evidence = self.attribute(match)
        domains = sorted({e.domain_id for e in evidence})
        self._projections.record_match(
            match, media_ids=media_ids, source_domains=[DomainId(d) for d in domains]
        )
        if not evidence:
            _log.warning("feedback_unattributed", match_id=str(match.match_id))
            return "unattributed"
        self._store.put_evidence(evidence)
        self._m.evidence(len(evidence))
        return "applied"


class TargetFeedback:
    """P2 projection from the target manager's events (relevance input)."""

    def __init__(self, projections: MatchProjections) -> None:
        self._p = projections

    def registered(self, envelope: EventEnvelope[TargetRegistered]) -> None:
        self._p.apply_target_registered(envelope.payload.target)

    def retired(self, envelope: EventEnvelope[TargetRetired]) -> None:
        self._p.apply_target_retired(envelope.payload)


def rebuild(
    feedback: MatchFeedback,
    store: FeedbackStore,
    projections: MatchProjections,
    *,
    now: datetime,
    months: int = 3,
) -> dict[str, int]:
    """Re-derive all evidence from the P4/P3 match projections (design §8)."""
    dropped = store.clear()
    seen: set[str] = set()
    applied = unattributed = 0
    for state in projections.targets():
        for back in range(months):
            month = (now.replace(day=1) - timedelta(days=31 * back)).replace(day=1)
            for summary in projections.matches_for_target(state.target.ref.target_id, month):
                for match in projections.matches_for_content(summary.content_id):
                    if str(match.match_id) in seen or match.match_id != summary.match_id:
                        continue
                    seen.add(str(match.match_id))
                    if feedback.apply(match) == "applied":
                        applied += 1
                    else:
                        unattributed += 1
    return {"dropped_sources": dropped, "applied": applied, "unattributed": unattributed}


# --- consumer loop ------------------------------------------------------------------


class FeedbackLoop:
    """One consumer-group reader per stream (match.found, target.registered/retired).

    Invalid events are logged, counted and acknowledged (they can never become
    valid); storage errors leave the entry pending for redelivery.
    """

    def __init__(
        self,
        pairs: list[tuple[RedisStreamReader, IdempotentConsumer[Any]]],
        *,
        batch_size: int,
        block_ms: int,
        claim_idle_ms: int,
        metrics: FeedbackMetrics | None = None,
    ) -> None:
        self._pairs = pairs
        self._batch = batch_size
        self._block_ms = block_ms
        self._idle_ms = claim_idle_ms
        self._m = metrics or FeedbackMetrics(None)
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    def run_once(self) -> int:
        total = 0
        for reader, consumer in self._pairs:
            entries = reader.claim_stale(min_idle_ms=self._idle_ms, count=self._batch)
            if not entries:
                entries = reader.read(count=self._batch, block_ms=self._block_ms)
            done: list[StreamEntry] = []
            for entry in entries:
                try:
                    consumer.handle(entry.envelope)  # duplicates: consumer_events_total
                except (ContractError, ValidationError):
                    _log.exception("feedback_event_invalid", entry_id=entry.entry_id)
                    self._m.match("invalid")
                except StorageError:
                    _log.exception("feedback_storage_unavailable", entry_id=entry.entry_id)
                    self._m.match("storage_error")
                    break  # left pending; reclaimed once idle
                done.append(entry)
            reader.ack(done)
            total += len(done)
        return total

    def run(self, *, max_idle_polls: int | None = None) -> None:
        idle = 0
        while not self._stopping:
            try:
                handled = self.run_once()
            except StorageError:
                _log.exception("feedback_poll_failed")
                time.sleep(1.0)
                continue
            idle = 0 if handled else idle + 1
            if max_idle_polls is not None and idle >= max_idle_polls:
                return


# --- evidence → admission priority --------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeedbackSnapshot:
    evidence: Mapping[str, tuple[MatchEvidence, ...]]
    targets: Mapping[str, TargetRelevance]
    loaded_at: datetime


def load_feedback_snapshot(
    store: FeedbackStore, projections: MatchProjections, *, now: datetime
) -> FeedbackSnapshot:
    sources = store.sources()
    evidence = store.evidence(sources) if sources else {}
    targets = {
        str(t.target.ref.target_id): TargetRelevance.ACTIVE if t.active else TargetRelevance.RETIRED
        for t in projections.targets()
    }
    return FeedbackSnapshot({d: tuple(rows) for d, rows in evidence.items()}, targets, now)


class FeedbackPrioritizer:
    """Admission-side reader of the snapshot (the ``Admitter``'s ``prioritizer``)."""

    def __init__(
        self,
        load: Callable[[], FeedbackSnapshot],
        *,
        max_snapshot_age: timedelta,
        refresh_s: float,
        policy: FeedbackPolicy = DEFAULT_FEEDBACK_POLICY,
        metrics: Metrics | None = None,
    ) -> None:
        self._load = load
        self._max_age = max_snapshot_age
        self._refresh_s = refresh_s
        self._policy = policy
        self._snapshot: FeedbackSnapshot | None = None
        self._assessed: dict[str, SourceFeedback] = {}
        self._assessed_at: datetime | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._m = FeedbackMetrics(metrics)
        self._failures: Any = (
            metrics.counter(
                "feedback_snapshot_failures_total", "Feedback snapshot refreshes that failed"
            )
            if metrics is not None
            else None
        )

    @property
    def snapshot(self) -> FeedbackSnapshot | None:
        return self._snapshot

    def refresh(self) -> bool:
        """Swap in a new snapshot; on failure keep the old one (it ages into fallback)."""
        try:
            snapshot = self._load()
        except Exception as exc:  # noqa: BLE001 -- feedback storage must never reach admission
            if self._failures is not None:
                self._failures.inc()
            _log.warning("feedback_snapshot_failed", error=str(exc)[:300])
            return False
        with self._lock:
            self._snapshot = snapshot
            self._assessed, self._assessed_at = {}, None
        return True

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                self.refresh()
                self._stop.wait(self._refresh_s)

        threading.Thread(target=loop, name="p11-feedback-snapshot", daemon=True).start()

    def close(self) -> None:
        self._stop.set()

    def source(self, domain_id: str, now: datetime) -> SourceFeedback | None:
        """Assessment of one host, cached per snapshot and minute (deterministic)."""
        snapshot = self._snapshot
        if snapshot is None:
            return None
        minute = now.replace(second=0, microsecond=0)
        with self._lock:
            if self._assessed_at != minute:
                self._assessed, self._assessed_at = {}, minute
            if domain_id not in self._assessed:
                rows = snapshot.evidence.get(domain_id)
                if rows is None:
                    return None
                self._assessed[domain_id] = assess_source(
                    domain_id, rows, snapshot.targets, minute, self._policy
                )
            return self._assessed[domain_id]

    def prioritize(self, url: UrlRef, base: int, now: datetime) -> PriorityDecision:
        try:
            decision = self._prioritize(url, base, now)
        except Exception as exc:  # noqa: BLE001 -- a bug here must not block admission
            _log.warning("feedback_priority_failed", error=str(exc)[:300])
            decision = PriorityDecision(base, base, reason="error", fallback="error")
        if decision.fallback:
            self._m.priority(decision.fallback)
        else:
            self._m.priority("boosted" if decision.priority > base else "neutral")
        return decision

    def _prioritize(self, url: UrlRef, base: int, now: datetime) -> PriorityDecision:
        snapshot = self._snapshot
        if snapshot is None:
            return PriorityDecision(base, base, reason="no snapshot", fallback="unavailable")
        if now - snapshot.loaded_at > self._max_age:
            return PriorityDecision(base, base, reason="snapshot stale", fallback="snapshot_stale")
        source = self.source(str(url.domain_id), now)
        return decide(
            base=base, url=url.url, url_id=str(url.url_id), source=source, policy=self._policy
        )


def feedback_policy(settings: FeedbackSettings) -> FeedbackPolicy:
    """The operator's bounds (``CRAWLER2_FEEDBACK__*``) over the v1 defaults."""
    custom = (
        settings.tau_days,
        settings.max_source_boost,
        settings.max_pattern_boost,
        settings.max_total_boost,
        settings.explore_percent,
    )
    d = DEFAULT_FEEDBACK_POLICY
    default = (
        d.tau / timedelta(days=1),
        d.max_source_boost,
        d.max_pattern_boost,
        d.max_total_boost,
        d.explore_percent,
    )
    return replace(
        d,
        version=d.version if custom == default else f"{d.version}+custom",
        tau=timedelta(days=settings.tau_days),
        max_source_boost=settings.max_source_boost,
        max_pattern_boost=settings.max_pattern_boost,
        max_total_boost=settings.max_total_boost,
        explore_percent=settings.explore_percent,
    )


def build_prioritizer(
    settings: Settings, storage: Any, metrics: Metrics | None
) -> FeedbackPrioritizer:
    from crawler2.intelligence.feedback_store import ScyllaFeedbackStore

    store = ScyllaFeedbackStore(storage.session)
    fb = settings.feedback
    return FeedbackPrioritizer(
        lambda: load_feedback_snapshot(store, storage.projections, now=datetime.now(UTC)),
        max_snapshot_age=timedelta(seconds=fb.max_snapshot_age_s),
        refresh_s=fb.snapshot_refresh_s,
        policy=feedback_policy(fb),
        metrics=metrics,
    )
