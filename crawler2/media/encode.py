"""Encode requester and the representation projection (design §7).

**One logical request per (ContentId, spec).** After an ``ok`` hash:

1. if ``representation_status`` already shows the default spec ``ready`` or
   ``requested``, nothing is emitted;
2. the event ID is allocated first, the request envelope built, the sweeper's
   index row written, then ``encode_claims`` is claimed (single-partition LWT,
   ``IF NOT EXISTS``, storing the complete envelope). A loser emits nothing;
3. only the winner appends the **stored** envelope to the outbox (pattern B,
   after the LWT), marks the projection ``requested`` and sets ``emitted_at``
   (``IF emitted_at = null``).

A crash after the claim and before ``emitted_at`` is completed by the
**sweeper**, which re-emits the stored envelope (same ``event_id``) for claims
older than ``media.claim_emit_grace_s``. A re-emission is a redelivery of one
event, never a second logical task. ``required_spec`` is absent (the
fingerprinter's default, spec key ``default``). After ``encode.failed`` nothing
is re-requested (P8-D7: the re-request conflict belongs to P9).

**Representation projection.** ``representation.ready`` and ``encode.failed``
are produced by the fingerprinter, never written to crawler2's outbox, so
crawler2's relay never carries them. They are read from their streams by the
consumer group ``media.consumer_group`` and applied with P2's idempotent
``ProjectionRepository`` methods.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from antipiracy_contracts.events import (
    ContractError,
    EventEnvelope,
    decode_event_as,
    encode_event,
    new_event,
)
from antipiracy_contracts.events.fingerprinting import (
    EncodeFailed,
    EncodeRequested,
    RepresentationReady,
)
from antipiracy_contracts.ids import ContentId, EventId, MediaId
from antipiracy_contracts.models.media import ContentKey, Media
from antipiracy_contracts.models.web import UrlRef
from antipiracy_contracts.ownership import Producer
from pydantic import ValidationError

from crawler2.core.observability import Metrics, get_logger
from crawler2.media.model import (
    EncodeClaim,
    EncodeClaimResult,
    HashRecord,
    MediaLatest,
    ProbeRecord,
)
from crawler2.storage.errors import IntegrityError, StorageError
from crawler2.storage.events.consumer import IdempotentConsumer, RedisStreamReader, StreamEntry
from crawler2.storage.repositories import RepresentationState, RepresentationStatus
from crawler2.storage.scylla.projections import DEFAULT_SPEC_KEY

_log = get_logger("media.encode")


class EncodeStore(Protocol):
    def index_encode_claim(self, content_id: ContentId, spec_key: str, *, at: datetime) -> None: ...

    def claim_encode(
        self,
        content_id: ContentId,
        spec_key: str,
        *,
        event_id: EventId,
        envelope: str,
        at: datetime,
        by: str,
        source_media_id: MediaId,
        referer: str | None,
    ) -> EncodeClaimResult: ...

    def encode_claim(self, content_id: ContentId, spec_key: str) -> EncodeClaim | None: ...

    def mark_emitted(self, content_id: ContentId, spec_key: str, *, at: datetime) -> None: ...

    def encode_claims_on(self, day: datetime) -> list[tuple[ContentId, str]]: ...


class Projections(Protocol):
    """P2 ``ScyllaProjectionRepository`` (unchanged)."""

    def representation_status(self, content_id: ContentId) -> list[RepresentationStatus]: ...

    def mark_encode_requested(self, event: EventEnvelope[EncodeRequested]) -> None: ...


class Outbox(Protocol):
    def enqueue(self, envelope: EventEnvelope[EncodeRequested]) -> None: ...


class Hook(Protocol):
    def __call__(self, point: str, **context: Any) -> None: ...


def _no_hook(point: str, **context: Any) -> None:
    return None


class RequestResult(StrEnum):
    EMITTED = "emitted"
    SUPPRESSED = "suppressed"
    """``representation_status`` already shows ready/requested."""
    CLAIMED_ELSEWHERE = "claimed_elsewhere"
    """Another request (another media or host) holds the claim."""


@dataclass(slots=True)
class SweepReport:
    scanned: int = 0
    reemitted: int = 0
    pending: int = 0
    """Unemitted claims younger than the grace period (left for a later sweep)."""


class EncodeRequester:
    def __init__(
        self,
        store: EncodeStore,
        projections: Projections,
        outbox: Outbox,
        *,
        producer: Producer,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        grace: timedelta = timedelta(seconds=300),
        lookback_days: int = 2,
        hook: Hook = _no_hook,
        metrics: Metrics | None = None,
    ) -> None:
        self._store = store
        self._projections = projections
        self._outbox = outbox
        self._producer = producer
        self._clock = clock
        self._grace = grace
        self._lookback_days = lookback_days
        self._hook = hook
        self._claims = (
            metrics.counter("media_encode_claims_total", "Encode requests by result", ["result"])
            if metrics is not None
            else None
        )

    # --- request -------------------------------------------------------------------

    def request(self, record: HashRecord, probe: ProbeRecord, referer: str | None) -> RequestResult:
        content = record.content
        if content is None:
            raise ValueError("only an ok hash has a content key")
        source = Media.of(UrlRef.of(probe.locator_url), probe.kind)
        return self.request_content(content, source, referer)

    def request_content(
        self, content: ContentKey, source: Media, referer: str | None
    ) -> RequestResult:
        if self._suppressed(content.content_id):
            return self._result(RequestResult.SUPPRESSED, content)
        now = self._clock()
        envelope = new_event(
            EncodeRequested(
                content=content,
                source=source,
                referer=UrlRef.of(referer) if referer else None,
            ),
            producer=self._producer,
            occurred_at=now,
            event_id=EventId.new(),  # allocated before the claim
        )
        self._store.index_encode_claim(content.content_id, DEFAULT_SPEC_KEY, at=now)
        self._hook("after_index", content_id=content.content_id)
        result = self._store.claim_encode(
            content.content_id,
            DEFAULT_SPEC_KEY,
            event_id=envelope.event_id,
            envelope=encode_event(envelope).decode("utf-8"),
            at=now,
            by=self._producer.instance,
            source_media_id=source.media_id,
            referer=referer,
        )
        self._hook("after_claim", content_id=content.content_id, won=result.won)
        if not result.won:
            return self._result(RequestResult.CLAIMED_ELSEWHERE, content)
        self._emit(result.claim)
        return self._result(RequestResult.EMITTED, content)

    def ensure(self, latest: MediaLatest, source: Media, referer: str | None) -> None:
        """Recovery for a crash between an ok hash and its claim: a later sighting of a
        media with a current content key requests it if nothing did yet."""
        content = latest.current_content()
        if content is None or self._suppressed(content.content_id):
            return  # cheap EC read first; the SERIAL claim read only when needed
        if self._store.encode_claim(content.content_id, DEFAULT_SPEC_KEY) is None:
            self.request_content(content, source, referer)

    def _suppressed(self, content_id: ContentId) -> bool:
        """The default spec is requested/ready, or any representation is ready.

        ``representation.ready`` carries a concrete spec, so it lands on that spec's
        row, never on ``default`` (the row ``encode.requested`` marks). Which concrete
        spec is the default is P9's to define (design §12); until then any ready
        representation suppresses. The claim still guarantees one logical request.
        """
        return any(
            s.state is RepresentationState.READY
            or (s.spec_key == DEFAULT_SPEC_KEY and s.state is RepresentationState.REQUESTED)
            for s in self._projections.representation_status(content_id)
        )

    def _emit(self, claim: EncodeClaim) -> None:
        """Outbox row with the **stored** envelope, then projection, then emitted_at."""
        envelope = decode_event_as(claim.envelope, EncodeRequested)
        if encode_event(envelope).decode("utf-8") != claim.envelope:
            raise IntegrityError(f"stored encode.requested {claim.event_id} does not round-trip")
        self._outbox.enqueue(envelope)
        self._hook("after_outbox", content_id=claim.content_id)
        self._projections.mark_encode_requested(envelope)
        self._hook("before_emitted", content_id=claim.content_id)
        self._store.mark_emitted(ContentId(claim.content_id), claim.spec_key, at=self._clock())

    def _result(self, result: RequestResult, content: ContentKey) -> RequestResult:
        if self._claims is not None:
            self._claims.labels(result=result.value).inc()
        _log.info("media_encode_request", result=result.value, content_id=str(content.content_id))
        return result

    # --- sweeper ---------------------------------------------------------------------

    def sweep(self) -> SweepReport:
        """Re-emit claims without ``emitted_at`` older than the grace period."""
        report = SweepReport()
        now = self._clock()
        for back in range(self._lookback_days):
            for content_id, spec_key in self._store.encode_claims_on(now - timedelta(days=back)):
                report.scanned += 1
                claim = self._store.encode_claim(content_id, spec_key)
                if claim is None or claim.emitted:
                    continue
                if now - claim.claimed_at < self._grace:
                    report.pending += 1
                    continue
                self._emit(claim)
                report.reemitted += 1
                if self._claims is not None:
                    self._claims.labels(result="reemitted").inc()
                _log.warning(
                    "media_encode_reemitted", content_id=claim.content_id, event_id=claim.event_id
                )
        return report

    def sweep_forever(
        self,
        *,
        interval_s: float,
        stop: Callable[[], bool],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        while not stop():
            try:
                self.sweep()
            except StorageError as exc:
                _log.warning("media_encode_sweep_retry", error=str(exc)[:300])
            sleep(interval_s)


# --- representation projection (design §7.3) ---------------------------------------------


class RepresentationProjection:
    """Applies the fingerprinter's events to P2's ``representation_status`` (idempotent)."""

    def __init__(self, projections: Any) -> None:
        self._p = projections

    def ready(self, envelope: EventEnvelope[RepresentationReady]) -> None:
        self._p.apply_representation_ready(envelope.payload.representation)

    def failed(self, envelope: EventEnvelope[EncodeFailed]) -> None:
        self._p.apply_encode_failed(envelope)  # no re-request (P8-D7)


class RepresentationLoop:
    """One consumer-group reader per fingerprinter stream (``representation.ready.v1``,
    ``encode.failed.v1``); stale entries of a dead consumer are reclaimed."""

    def __init__(
        self,
        pairs: list[tuple[RedisStreamReader, IdempotentConsumer[Any]]],
        *,
        batch_size: int,
        block_ms: int,
        claim_idle_ms: int,
    ) -> None:
        self._pairs = pairs
        self._batch = batch_size
        self._block_ms = block_ms
        self._idle_ms = claim_idle_ms
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    def run_once(self) -> int:
        done_total = 0
        for reader, consumer in self._pairs:
            entries = reader.claim_stale(min_idle_ms=self._idle_ms, count=self._batch)
            if not entries:
                entries = reader.read(count=self._batch, block_ms=self._block_ms)
            done: list[StreamEntry] = []
            for entry in entries:
                try:
                    consumer.handle(entry.envelope)
                except (ContractError, ValidationError):
                    _log.exception("representation_event_invalid", entry_id=entry.entry_id)
                except StorageError:
                    _log.exception("representation_storage_unavailable", entry_id=entry.entry_id)
                    break  # left pending; reclaimed once idle
                done.append(entry)
            reader.ack(done)
            done_total += len(done)
        return done_total

    def run(self, *, max_idle_polls: int | None = None) -> None:
        idle = 0
        while not self._stopping:
            try:
                handled = self.run_once()
            except StorageError:
                _log.exception("representation_poll_failed")
                time.sleep(1.0)
                continue
            idle = 0 if handled else idle + 1
            if max_idle_polls is not None and idle >= max_idle_polls:
                return
