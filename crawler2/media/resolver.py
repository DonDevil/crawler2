"""The resolver: ``media.discovered`` → ``MediaObservation`` + ``media.observed`` (design §4).

Reads the outbox through P2's read-only ``OutboxStore`` with its own
checkpoint, never a consumer group (ADR-024), so a stopped or slow registry
cannot hold back the relay or the crawl (B.5 #1).

Per media reference ``r`` of an outbox bucket ``b``:

1. build the observation (latest probe/hash view) and a new envelope;
2. claim it (write-once LWT in the observation's partition). The first claim
   wins; a loser continues with the **stored** envelope;
3. if the claim is not committed: P2 ``record_observation`` with the stored
   envelope (M2 + M3 + outbox row, one logged batch), then ``mark_committed``;
4. want a probe for the media (per ``MediaId``, never per sighting).

The checkpoint advances ``b → b + 1`` (LWT on the epoch) only after every
reference of ``b`` passed step 3. Guarantees and limits: ADR-024 (one
decision and one ``event_id`` per observation; at-least-once delivery with
unbounded physical copies under repeated crashes; no strict lease fencing of
single fact writes).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from antipiracy_contracts.events import (
    EventEnvelope,
    decode_event_as,
    encode_event,
    new_event,
)
from antipiracy_contracts.events.media import MediaDiscovered, MediaObserved
from antipiracy_contracts.ids import MediaId, ObservationId
from antipiracy_contracts.models.media import (
    Media,
    MediaObservation,
    MediaReference,
    ProbeStatus,
)
from antipiracy_contracts.ownership import Producer

from crawler2.core.observability import Metrics, get_logger
from crawler2.media.model import ClaimResult, MediaLatest, ReaderMode, ReaderState, StoredClaim
from crawler2.storage.errors import IntegrityError, StorageError
from crawler2.storage.events.relay import OutboxRecord
from crawler2.storage.layout import OUTBOX_SHARDS, outbox_bucket

_log = get_logger("media.resolver")

POINTS = ("after_build", "after_claim", "after_record", "before_mark", "before_advance")
"""Named pause points between the steps above (tests inject a hook, design §10.1)."""


# --- dependencies ----------------------------------------------------------------------


class OutboxReader(Protocol):
    """The read-only part of P2's ``OutboxStore`` (ADR-023/024)."""

    def rows(self, shard: int, bucket: int) -> list[OutboxRecord]: ...

    def earliest_bucket(self) -> int: ...


class RegistryStore(Protocol):
    def state(self, reader: str) -> ReaderState | None: ...

    def create_state(self, state: ReaderState, *, at: datetime, by: str) -> bool: ...

    def acquire(
        self, reader: str, *, expected_epoch: int, owner: str, lease_until: datetime, now: datetime
    ) -> bool: ...

    def renew(
        self, reader: str, *, epoch: int, owner: str, lease_until: datetime, now: datetime
    ) -> bool: ...

    def advance(
        self,
        reader: str,
        *,
        epoch: int,
        from_bucket: int,
        lease_until: datetime,
        now: datetime,
        by: str,
    ) -> bool: ...

    def enter_gap(
        self, state: ReaderState, *, epoch: int, gap_to: int, now: datetime, by: str
    ) -> bool: ...

    def accept_gap(
        self, state: ReaderState, *, resume_at: int, now: datetime, by: str, note: str
    ) -> bool: ...

    def claim_observation(
        self,
        page_observation_id: ObservationId,
        media_id: MediaId,
        *,
        event_id: Any,
        envelope: str,
        at: datetime,
        epoch: int,
        by: str,
    ) -> ClaimResult: ...

    def claim(
        self, page_observation_id: ObservationId, media_id: MediaId
    ) -> StoredClaim | None: ...

    def mark_committed(
        self, page_observation_id: ObservationId, media_id: MediaId, *, at: datetime
    ) -> None: ...


class ObservationWriter(Protocol):
    """P2 ``ScyllaMediaRepository.record_observation`` (unchanged)."""

    def record_observation(
        self, observation: MediaObservation, *, event: EventEnvelope[MediaObserved] | None = None
    ) -> None: ...


class LatestViews(Protocol):
    """What the registry knows about a media at resolution time (``media_probe_latest``)."""

    def latest(self, media_id: MediaId) -> MediaLatest | None: ...


class ObservedSink(Protocol):
    """Probe/hash scheduling after an observation is committed (design §5.1)."""

    def observed(self, observation: MediaObservation, latest: MediaLatest | None) -> None: ...


class Hook(Protocol):
    def __call__(self, point: str, **context: Any) -> None: ...


def _no_hook(point: str, **context: Any) -> None:
    return None


class NoViews:
    def latest(self, media_id: MediaId) -> MediaLatest | None:
        return None


class LeaseLostError(Exception):
    """The reader's epoch changed or its lease could not be renewed: stop folding."""


# --- configuration and reports -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolverConfig:
    reader: str = "resolver"
    lease_ttl: timedelta = timedelta(seconds=60)
    settle: timedelta = timedelta(minutes=10)
    retention: timedelta = timedelta(days=14)
    retention_margin: timedelta = timedelta(hours=1)
    max_buckets_per_cycle: int = 60
    lag_warn: timedelta = timedelta(days=3)
    lag_critical: timedelta = timedelta(days=7)
    shards: tuple[int, ...] = field(default_factory=lambda: tuple(range(OUTBOX_SHARDS)))


class Level(StrEnum):
    ABSENT = "absent"
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"
    GAP = "gap"


@dataclass(frozen=True, slots=True)
class ReaderStatus:
    level: Level
    state: ReaderState | None
    lag_s: float | None
    lease_valid: bool

    @property
    def exit_code(self) -> int:
        """``resolve status``: 0 ok/warning/absent, 1 critical lag, 2 gap (halted)."""
        return {Level.CRITICAL: 1, Level.GAP: 2}.get(self.level, 0)


@dataclass(slots=True)
class CycleReport:
    buckets: int = 0
    events: int = 0
    references: int = 0
    claimed: int = 0
    lost: int = 0
    committed: int = 0
    replayed: int = 0
    invalid: int = 0
    bootstrapped: bool = False
    acquired: bool = False
    standby: bool = False
    halted: bool = False
    gap_detected: bool = False
    conflict: bool = False
    lease_lost: bool = False
    caught_up: bool = False
    lag_s: float = 0.0


# --- the resolver ------------------------------------------------------------------------


class Resolver:
    def __init__(
        self,
        outbox: OutboxReader,
        store: RegistryStore,
        observations: ObservationWriter,
        *,
        producer: Producer,
        config: ResolverConfig | None = None,
        views: LatestViews | None = None,
        sink: ObservedSink | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        hook: Hook = _no_hook,
        metrics: Metrics | None = None,
    ) -> None:
        self._outbox = outbox
        self._store = store
        self._observations = observations
        self._producer = producer
        self._me = producer.instance
        self._c = config or ResolverConfig()
        self._views = views or NoViews()
        self._sink = sink
        self._clock = clock
        self._hook = hook
        self._epoch: int | None = None
        self._m: dict[str, Any] = {}
        if metrics is not None:
            self._m = {
                "obs": metrics.counter(
                    "media_observations_total",
                    "Media references resolved, by result",
                    ["result"],
                ),
                "buckets": metrics.counter(
                    "media_reader_buckets_total", "Outbox buckets resolved and checkpointed"
                ),
                "gaps": metrics.counter(
                    "media_reader_gap_total", "Checkpoint fell out of outbox retention (halt)"
                ),
                "conflicts": metrics.counter(
                    "media_reader_conflicts_total",
                    "Reader-state CAS lost or lease lost (another reader owns the epoch)",
                ),
                "failures": metrics.counter(
                    "media_reader_cycle_failures_total", "Cycles aborted by a storage error"
                ),
                "lag": metrics.gauge(
                    "media_reader_lag_seconds", "Age of the oldest bucket not yet resolved"
                ),
                "checkpoint": metrics.gauge(
                    "media_reader_checkpoint_bucket", "Next outbox bucket to resolve"
                ),
                "epoch": metrics.gauge("media_reader_epoch", "Reader-state epoch"),
                "owner": metrics.gauge(
                    "media_reader_owner", "1 while this process owns the reader lease"
                ),
                "state": metrics.gauge(
                    "media_reader_state", "1 for the reader's current state", ["state"]
                ),
            }

    @property
    def config(self) -> ResolverConfig:
        return self._c

    @property
    def epoch(self) -> int | None:
        return self._epoch

    # --- retention and status ----------------------------------------------------

    def retained_from(self, now: datetime) -> int:
        """Oldest bucket whose rows the outbox is trusted to still hold."""
        horizon = now - self._c.retention + self._c.retention_margin
        return max(self._outbox.earliest_bucket(), outbox_bucket(horizon))

    def status(self) -> ReaderStatus:
        now = self._clock()
        state = self._store.state(self._c.reader)
        if state is None:
            return ReaderStatus(Level.ABSENT, None, None, False)
        lag = state.lag(now)
        if state.state is ReaderMode.GAP:
            level = Level.GAP
        elif lag >= self._c.lag_critical:
            level = Level.CRITICAL
        elif lag >= self._c.lag_warn:
            level = Level.WARNING
        else:
            level = Level.OK
        return ReaderStatus(level, state, lag.total_seconds(), state.lease_until > now)

    def accept_gap(self, *, by: str, note: str) -> ReaderState | None:
        """Operator: accept the halted gap as loss and resume at the oldest retained bucket."""
        now = self._clock()
        state = self._store.state(self._c.reader)
        if state is None or state.state is not ReaderMode.GAP:
            raise ValueError("the resolver is not halted in a gap")
        resume_at = max(self.retained_from(now), state.gap_to or 0)
        if not self._store.accept_gap(state, resume_at=resume_at, now=now, by=by, note=note):
            return None
        _log.warning(
            "media_reader_gap_accepted",
            reader=state.reader,
            gap_from=state.gap_from,
            resume_at=resume_at,
            by=by,
        )
        return self._store.state(self._c.reader)

    # --- ownership -----------------------------------------------------------------

    def _lease_until(self, now: datetime) -> datetime:
        return now + self._c.lease_ttl

    def _own(self, report: CycleReport) -> ReaderState | None:
        """Bootstrap, keep or take the lease. None = another reader owns it, or halted."""
        now = self._clock()
        state = self._store.state(self._c.reader)
        if state is None:
            start = self.retained_from(now)
            initial = ReaderState(
                reader=self._c.reader,
                epoch=1,
                owner=self._me,
                lease_until=self._lease_until(now),
                next_bucket=start,
                started_bucket=start,
            )
            if not self._store.create_state(initial, at=now, by=self._me):
                self._count("conflicts")
                report.conflict = True
                return None
            report.bootstrapped = report.acquired = True
            self._epoch = 1
            _log.info("media_reader_bootstrap", reader=self._c.reader, from_bucket=start)
            return initial
        if state.state is ReaderMode.GAP:
            self._epoch = None
            report.halted = True
            self._publish_state(state, now)
            return None
        if self._epoch == state.epoch and state.owner == self._me:
            if not self._store.renew(
                self._c.reader,
                epoch=state.epoch,
                owner=self._me,
                lease_until=self._lease_until(now),
                now=now,
            ):
                self._lost(report)
                return None
            return replace(state, lease_until=self._lease_until(now))
        if state.lease_until > now:
            self._epoch = None
            report.standby = True
            return None
        if not self._store.acquire(
            self._c.reader,
            expected_epoch=state.epoch,
            owner=self._me,
            lease_until=self._lease_until(now),
            now=now,
        ):
            self._epoch = None
            report.standby = True
            return None
        self._epoch = state.epoch + 1
        report.acquired = True
        _log.info("media_reader_acquired", reader=self._c.reader, epoch=self._epoch)
        return replace(state, epoch=self._epoch, owner=self._me, lease_until=self._lease_until(now))

    def _check_lease(self) -> None:
        """Re-read the lease (SERIAL) before each outbox row; renew past half its life."""
        now = self._clock()
        state = self._store.state(self._c.reader)
        if (
            state is None
            or self._epoch is None
            or state.epoch != self._epoch
            or state.owner != self._me
            or state.state is not ReaderMode.RUNNING
        ):
            raise LeaseLostError
        if state.lease_until - now < self._c.lease_ttl / 2 and not self._store.renew(
            self._c.reader,
            epoch=self._epoch,
            owner=self._me,
            lease_until=self._lease_until(now),
            now=now,
        ):
            raise LeaseLostError

    def _lost(self, report: CycleReport) -> None:
        self._epoch = None
        report.lease_lost = True
        self._count("conflicts")
        _log.warning("media_reader_lease_lost", reader=self._c.reader)

    # --- one cycle --------------------------------------------------------------------

    def run_once(self) -> CycleReport:
        report = CycleReport()
        state = self._own(report)
        if state is None:
            return report
        assert self._epoch is not None  # noqa: S101 -- set by _own
        now = self._clock()
        retained = self.retained_from(now)
        if state.next_bucket < retained:
            self._gap(state, retained, now, report)
            return report
        ready_until = outbox_bucket(now - self._c.settle)  # exclusive
        while report.buckets < self._c.max_buckets_per_cycle and state.next_bucket < ready_until:
            bucket = state.next_bucket
            try:
                self._fold_bucket(bucket, report)
            except LeaseLostError:
                self._lost(report)
                break
            self._hook("before_advance", bucket=bucket, epoch=self._epoch)
            now = self._clock()
            if not self._store.advance(
                self._c.reader,
                epoch=self._epoch,
                from_bucket=bucket,
                lease_until=self._lease_until(now),
                now=now,
                by=self._me,
            ):
                report.conflict = True
                self._count("conflicts")
                self._epoch = None
                _log.warning("media_reader_advance_conflict", bucket=bucket)
                break
            state = replace(state, next_bucket=bucket + 1)
            report.buckets += 1
            self._count("buckets")
        report.caught_up = state.next_bucket >= ready_until
        report.lag_s = state.lag(self._clock()).total_seconds()
        self._publish_state(state, self._clock())
        return report

    def _gap(self, state: ReaderState, retained: int, now: datetime, report: CycleReport) -> None:
        assert self._epoch is not None  # noqa: S101
        if self._store.enter_gap(state, epoch=self._epoch, gap_to=retained, now=now, by=self._me):
            report.gap_detected = report.halted = True
            self._count("gaps")
            _log.error(
                "media_reader_gap",
                reader=state.reader,
                gap_from=state.next_bucket,
                gap_to=retained,
                action="halted; run `crawler2-media resolve accept-gap` to record the loss",
            )
        else:
            report.conflict = True
            self._count("conflicts")
        self._epoch = None
        current = self._store.state(self._c.reader)
        if current is not None:
            self._publish_state(current, now)

    def _fold_bucket(self, bucket: int, report: CycleReport) -> None:
        for shard in self._c.shards:
            for record in self._outbox.rows(shard, bucket):
                if record.event_type != MediaDiscovered.EVENT_TYPE:
                    continue
                self._check_lease()
                discovered = self._decode(record, report)
                if discovered is None:
                    continue
                report.events += 1
                seen: set[MediaId] = set()
                for reference in discovered.payload.references:
                    media = Media.of(reference.locator, reference.kind)
                    if media.media_id in seen:
                        continue  # one observation per (page observation, media)
                    seen.add(media.media_id)
                    self._fold_reference(discovered, reference, media, report)

    def _decode(
        self, record: OutboxRecord, report: CycleReport
    ) -> EventEnvelope[MediaDiscovered] | None:
        try:
            if int(record.schema_version.partition(".")[0]) != MediaDiscovered.SCHEMA_MAJOR:
                raise ValueError(f"unsupported schema {record.schema_version}")
            return decode_event_as(record.envelope, MediaDiscovered)
        except Exception as exc:  # noqa: BLE001 -- a malformed row must not halt the reader
            report.invalid += 1
            self._count("obs", result="invalid")
            _log.warning("media_discovered_invalid", event_id=record.event_id, error=str(exc)[:200])
            return None

    def _fold_reference(
        self,
        discovered: EventEnvelope[MediaDiscovered],
        reference: MediaReference,
        media: Media,
        report: CycleReport,
    ) -> None:
        assert self._epoch is not None  # noqa: S101
        report.references += 1
        page_obs = discovered.payload.page_observation_id
        latest = self._views.latest(media.media_id)
        stored = self._store.claim(page_obs, media.media_id)
        if stored is None:
            envelope = self._build(discovered, reference, media, latest)
            self._hook("after_build", reference=media.media_id, epoch=self._epoch)
            result = self._store.claim_observation(
                page_obs,
                media.media_id,
                event_id=envelope.event_id,
                envelope=encode_event(envelope).decode("utf-8"),
                at=self._clock(),
                epoch=self._epoch,
                by=self._me,
            )
            stored = result.claim
            if result.won:
                report.claimed += 1
                self._count("obs", result="claimed")
            else:
                report.lost += 1
                self._count("obs", result="claim_lost")
            self._hook("after_claim", reference=media.media_id, epoch=self._epoch, won=result.won)
        decided = self._decided(stored)
        if stored.committed:
            report.replayed += 1
            self._count("obs", result="replayed")
        else:
            self._observations.record_observation(decided.payload.observation, event=decided)
            self._hook("after_record", reference=media.media_id, epoch=self._epoch)
            self._hook("before_mark", reference=media.media_id, epoch=self._epoch)
            self._store.mark_committed(page_obs, media.media_id, at=self._clock())
            report.committed += 1
            self._count("obs", result="committed")
        if self._sink is not None:
            self._sink.observed(decided.payload.observation, latest)

    def _build(
        self,
        discovered: EventEnvelope[MediaDiscovered],
        reference: MediaReference,
        media: Media,
        latest: MediaLatest | None,
    ) -> EventEnvelope[MediaObserved]:
        payload = discovered.payload
        probe = latest.probe if latest is not None else None
        status = probe.probe_status if probe is not None else ProbeStatus.NOT_PROBED
        observation = MediaObservation(
            media=media,
            page_observation_id=payload.page_observation_id,
            page=payload.page,
            page_version_id=payload.page_version_id,
            observed_at=discovered.occurred_at,
            probe_status=status,
            content=latest.current_content() if latest is not None else None,
            hints=probe.hints() if probe is not None else None,
        )
        return new_event(
            MediaObserved(observation=observation),
            producer=self._producer,
            occurred_at=discovered.occurred_at,
            correlation_id=discovered.correlation_id,
            causation_id=discovered.event_id,
        )

    @staticmethod
    def _decided(stored: StoredClaim) -> EventEnvelope[MediaObserved]:
        """The stored envelope; its re-encoding must be the stored bytes exactly."""
        envelope = decode_event_as(stored.envelope, MediaObserved)
        if encode_event(envelope).decode("utf-8") != stored.envelope:
            raise IntegrityError(f"stored media.observed {stored.event_id} does not round-trip")
        return envelope

    # --- metrics and loop ---------------------------------------------------------------

    def _count(self, name: str, *, n: int = 1, **labels: str) -> None:
        metric = self._m.get(name)
        if metric is not None:
            (metric.labels(**labels) if labels else metric).inc(n)

    def _publish_state(self, state: ReaderState, now: datetime) -> None:
        if not self._m:
            return
        self._m["lag"].set(state.lag(now).total_seconds())
        self._m["checkpoint"].set(state.next_bucket)
        self._m["epoch"].set(state.epoch)
        self._m["owner"].set(1 if self._epoch is not None else 0)
        for mode in ReaderMode:
            self._m["state"].labels(state=mode.value).set(1 if state.state is mode else 0)

    def run_forever(
        self,
        *,
        poll_interval_s: float,
        retry_max_s: float,
        stop: Callable[[], bool],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Resolve until ``stop()``. Storage errors are retried with backoff, never fatal.

        A gap halts resolution: the process stays up (metrics, status) and polls
        the state until an operator ``accept-gap`` resumes it.
        """
        pause = poll_interval_s
        while not stop():
            try:
                report = self.run_once()
            except StorageError as exc:
                # Nothing is skipped: the bucket is folded again on the next cycle.
                self._count("failures")
                _log.warning("media_reader_retry", error=str(exc)[:300], retry_in_s=pause)
                sleep(pause)
                pause = min(pause * 2, retry_max_s)
                continue
            pause = poll_interval_s
            if report.buckets or report.gap_detected or report.acquired:
                _log.info(
                    "media_reader_cycle",
                    buckets=report.buckets,
                    events=report.events,
                    references=report.references,
                    claimed=report.claimed,
                    lost=report.lost,
                    committed=report.committed,
                    replayed=report.replayed,
                    invalid=report.invalid,
                    lag_s=round(report.lag_s),
                    epoch=self._epoch,
                )
            if not (report.buckets and not report.caught_up):
                sleep(poll_interval_s)
