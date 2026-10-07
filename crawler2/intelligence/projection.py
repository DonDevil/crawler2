"""Source-profile projection: outbox → bounded profiles (P7 design §9.2, ADR-023).

The reader folds every ``fetch.completed`` fact into its host's profile
(``sources.fold``). It reads the outbox (the 14-day system of record) through
P2's read-only ``OutboxStore`` interface, never a Redis consumer group: a dead
or slow reader therefore cannot hold back the relay (ADR-022) or any other
consumer. Nothing in the crawl depends on it (B.5 #1).

- **Checkpoint:** whole minute buckets in order, only once settled (the relay's
  ``settle`` rule). The state row is advanced by LWT once per bucket.
- **Exactly once per (row, bucket):** a profile row records the last bucket
  folded into it; a bucket re-read after a crash skips rows already past it.
- **Duplicates:** facts with an idempotency key already seen in the last
  ``dedupe_window`` facts are dropped (producer retries create a second outbox
  row with a new event id, ADR-013).
- **Gap:** a checkpoint older than the outbox retains starts a new generation
  from the oldest retained bucket. So do a policy change and an operator
  rebuild. Readers ignore rows of other generations and policies.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from antipiracy_contracts.events import decode_event_as
from antipiracy_contracts.events.web import FetchCompleted

from crawler2.core.observability import Metrics, get_logger
from crawler2.intelligence.sources import (
    DEFAULT_POLICY,
    FetchFact,
    SourcePolicy,
    SourceProfile,
    fact_from_attempt,
    fold,
)
from crawler2.storage.errors import StorageError
from crawler2.storage.events.relay import OutboxRecord
from crawler2.storage.layout import OUTBOX_SHARDS, bucket_start, outbox_bucket

_log = get_logger("intelligence.projection")


# --- stored forms -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProjectionState:
    """The reader's checkpoint and generation (table ``intel_projection_state``)."""

    projection: str
    generation: int
    policy: str
    next_bucket: int
    gaps: int = 0
    rebuilds: int = 0
    last_gap_from: int | None = None
    last_gap_to: int | None = None
    generation_started_at: datetime | None = None

    def lag(self, now: datetime) -> timedelta:
        """How far behind ``now`` the projection is complete."""
        return now - bucket_start(self.next_bucket)


@dataclass(frozen=True, slots=True)
class StoredProfile:
    """One row of ``intel_source_profiles``."""

    profile: SourceProfile
    generation: int
    policy: str
    through_bucket: int


def _ts(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def profile_to_json(profile: SourceProfile) -> str:
    """Lossless (floats round-trip through ``repr``) and deterministic."""
    return json.dumps(
        {
            "domain_id": profile.domain_id,
            "ref_at": profile.ref_at.isoformat(),
            "health": list(profile.health),
            "http_ok": profile.http_ok,
            "http_needs_js": profile.http_needs_js,
            "browser_ok": profile.browser_ok,
            "facts": profile.facts,
            "first_seen": _ts(profile.first_seen),
            "last_ok": _ts(profile.last_ok),
            "last_attempt": _ts(profile.last_attempt),
            "last_http_attempt": _ts(profile.last_http_attempt),
            "moved_to": profile.moved_to,
            "moved_at": _ts(profile.moved_at),
            "policy": profile.policy,
        },
        sort_keys=True,
    )


def profile_from_json(text: str) -> SourceProfile:
    doc: dict[str, Any] = json.loads(text)
    return SourceProfile(
        domain_id=doc["domain_id"],
        ref_at=datetime.fromisoformat(doc["ref_at"]),
        health=tuple(float(w) for w in doc["health"]),
        http_ok=float(doc["http_ok"]),
        http_needs_js=float(doc["http_needs_js"]),
        browser_ok=float(doc["browser_ok"]),
        facts=int(doc["facts"]),
        first_seen=_dt(doc["first_seen"]),
        last_ok=_dt(doc["last_ok"]),
        last_attempt=_dt(doc["last_attempt"]),
        last_http_attempt=_dt(doc["last_http_attempt"]),
        moved_to=doc["moved_to"],
        moved_at=_dt(doc["moved_at"]),
        policy=doc["policy"],
    )


# --- dependencies -------------------------------------------------------------------


class OutboxReader(Protocol):
    """The read-only part of P2's ``OutboxStore`` (ADR-023)."""

    def rows(self, shard: int, bucket: int) -> list[OutboxRecord]: ...

    def earliest_bucket(self) -> int: ...


class SourceProfileStore(Protocol):
    """P7-owned tables (design §9.3). Errors are ``StorageError``s."""

    def state(self, projection: str) -> ProjectionState | None: ...

    def create_state(self, state: ProjectionState, *, by: str) -> bool:
        """Insert if absent; False when a state already exists."""
        ...

    def replace_state(self, expected: ProjectionState, new: ProjectionState, *, by: str) -> bool:
        """Compare-and-set on (generation, next_bucket); False when it changed."""
        ...

    def profiles(self, domain_ids: Collection[str]) -> dict[str, StoredProfile]: ...

    def put_profiles(self, rows: Sequence[StoredProfile]) -> None: ...

    def scan(self) -> Iterator[StoredProfile]:
        """Every stored row (all generations): the admission snapshot."""
        ...


# --- the reader ---------------------------------------------------------------------


@dataclass(slots=True)
class CycleReport:
    buckets: int = 0
    applied: int = 0
    duplicates: int = 0
    replayed: int = 0
    invalid: int = 0
    profile_writes: int = 0
    new_generation: str | None = None
    """Why a new generation started this cycle (bootstrap, gap, policy, rebuild)."""
    conflict: bool = False
    caught_up: bool = False
    lag_s: float = 0.0


class _RecentKeys:
    """Bounded memory of idempotency keys, oldest evicted first."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._keys: OrderedDict[str, None] = OrderedDict()

    def __contains__(self, key: object) -> bool:
        return key in self._keys

    def add_all(self, keys: Iterable[str]) -> None:
        for key in keys:
            self._keys[key] = None
            self._keys.move_to_end(key)
        while len(self._keys) > self._capacity:
            self._keys.popitem(last=False)

    def clear(self) -> None:
        self._keys.clear()


@dataclass(frozen=True, slots=True)
class ReaderConfig:
    projection: str = "sources"
    settle: timedelta = timedelta(minutes=10)
    retention: timedelta = timedelta(days=14)
    retention_margin: timedelta = timedelta(hours=1)
    max_buckets_per_cycle: int = 60
    dedupe_window: int = 200_000
    shards: tuple[int, ...] = field(default_factory=lambda: tuple(range(OUTBOX_SHARDS)))


class SourceProjector:
    def __init__(
        self,
        outbox: OutboxReader,
        store: SourceProfileStore,
        *,
        config: ReaderConfig | None = None,
        policy: SourcePolicy = DEFAULT_POLICY,
        instance: str = "intel",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        metrics: Metrics | None = None,
    ) -> None:
        self._outbox = outbox
        self._store = store
        self._c = config or ReaderConfig()
        self._policy = policy
        self._instance = instance
        self._clock = clock
        self._seen = _RecentKeys(self._c.dedupe_window)
        self._generation: int | None = None
        self._m: dict[str, Any] = {}
        if metrics is not None:
            self._m = {
                "facts": metrics.counter(
                    "intel_sources_facts_total", "fetch.completed facts by result", ["result"]
                ),
                "writes": metrics.counter(
                    "intel_sources_profile_writes_total", "Source profile rows written"
                ),
                "buckets": metrics.counter(
                    "intel_sources_buckets_total", "Outbox buckets folded into the projection"
                ),
                "gaps": metrics.counter(
                    "intel_sources_gaps_total", "Checkpoint fell out of outbox retention"
                ),
                "rebuilds": metrics.counter(
                    "intel_sources_rebuilds_total", "New projection generations", ["cause"]
                ),
                "conflicts": metrics.counter(
                    "intel_sources_conflicts_total",
                    "Checkpoint compare-and-set lost (a rebuild or a second reader)",
                ),
                "failures": metrics.counter(
                    "intel_sources_cycle_failures_total", "Cycles aborted by a storage error"
                ),
                "lag": metrics.gauge(
                    "intel_sources_lag_seconds", "Age of the oldest bucket not yet folded"
                ),
                "checkpoint": metrics.gauge(
                    "intel_sources_checkpoint_bucket", "Next outbox bucket to fold"
                ),
                "generation": metrics.gauge(
                    "intel_sources_generation", "Current projection generation"
                ),
            }

    # --- state transitions ------------------------------------------------------

    def retained_from(self, now: datetime) -> int:
        """Oldest bucket whose rows the outbox is trusted to still hold."""
        horizon = now - self._c.retention + self._c.retention_margin
        return max(self._outbox.earliest_bucket(), outbox_bucket(horizon))

    def _new_generation(
        self, current: ProjectionState | None, cause: str, now: datetime
    ) -> ProjectionState | None:
        start = self.retained_from(now)
        if current is None:
            state = ProjectionState(
                self._c.projection, 1, self._policy.version, start, generation_started_at=now
            )
            ok = self._store.create_state(state, by=self._instance)
        else:
            gap = cause == "gap"
            state = replace(
                current,
                generation=current.generation + 1,
                policy=self._policy.version,
                next_bucket=start,
                gaps=current.gaps + (1 if gap else 0),
                rebuilds=current.rebuilds + 1,
                last_gap_from=current.next_bucket if gap else current.last_gap_from,
                last_gap_to=start if gap else current.last_gap_to,
                generation_started_at=now,
            )
            ok = self._store.replace_state(current, state, by=self._instance)
        if not ok:
            self._count("conflicts")
            return None
        self._seen.clear()
        self._count("rebuilds", cause=cause)
        if cause == "gap":
            self._count("gaps")
        _log.warning(
            "source_projection_new_generation",
            cause=cause,
            generation=state.generation,
            from_bucket=start,
            lost_from=state.last_gap_from if cause == "gap" else None,
        )
        return state

    def rebuild(self) -> ProjectionState | None:
        """Operator rebuild: a new generation from the oldest retained bucket."""
        now = self._clock()
        return self._new_generation(self._store.state(self._c.projection), "rebuild", now)

    # --- one cycle --------------------------------------------------------------

    def run_once(self) -> CycleReport:
        report = CycleReport()
        now = self._clock()
        state = self._store.state(self._c.projection)
        cause = None
        if state is None:
            cause = "bootstrap"
        elif state.policy != self._policy.version:
            cause = "policy"
        elif state.next_bucket < self.retained_from(now):
            cause = "gap"
        if cause is not None:
            report.new_generation = cause
            state = self._new_generation(state, cause, now)
            if state is None:
                report.conflict = True
                return report
        assert state is not None  # noqa: S101 -- narrowed above
        if self._generation != state.generation:
            self._seen.clear()  # keys of another generation must not suppress facts
            self._generation = state.generation
        ready_until = outbox_bucket(now - self._c.settle)  # exclusive
        while report.buckets < self._c.max_buckets_per_cycle and state.next_bucket < ready_until:
            advanced = self._fold_bucket(state, report)
            if advanced is None:
                report.conflict = True
                self._count("conflicts")
                self._generation = None
                break
            state = advanced
            report.buckets += 1
            self._count("buckets")
        report.caught_up = state.next_bucket >= ready_until
        report.lag_s = state.lag(now).total_seconds()
        if self._m:
            self._m["lag"].set(report.lag_s)
            self._m["checkpoint"].set(state.next_bucket)
            self._m["generation"].set(state.generation)
        return report

    def _fold_bucket(self, state: ProjectionState, report: CycleReport) -> ProjectionState | None:
        bucket = state.next_bucket
        keys: set[str] = set()
        by_domain: dict[str, list[FetchFact]] = {}
        for shard in self._c.shards:
            for record in self._outbox.rows(shard, bucket):
                if record.event_type != FetchCompleted.EVENT_TYPE:
                    continue
                fact = self._fact(record, keys, report)
                if fact is not None:
                    by_domain.setdefault(fact.domain_id, []).append(fact)
        stored = self._store.profiles(list(by_domain)) if by_domain else {}
        rows = []
        for domain_id, facts in by_domain.items():
            current = stored.get(domain_id)
            if current is not None and current.generation != state.generation:
                current = None  # a dead generation's row: start over
            if current is not None and current.policy != state.policy:
                current = None
            if current is not None and current.through_bucket >= bucket:
                report.replayed += len(facts)  # folded before a crash
                self._count("facts", result="replayed", n=len(facts))
                continue
            profile = current.profile if current is not None else None
            for fact in facts:
                profile = fold(profile, fact, self._policy)
            assert profile is not None  # noqa: S101 -- facts is never empty
            rows.append(StoredProfile(profile, state.generation, state.policy, bucket))
            report.applied += len(facts)
            self._count("facts", result="applied", n=len(facts))
        if rows:
            self._store.put_profiles(rows)
            report.profile_writes += len(rows)
            self._count("writes", n=len(rows))
        advanced = replace(state, next_bucket=bucket + 1)
        if not self._store.replace_state(state, advanced, by=self._instance):
            return None
        self._seen.add_all(sorted(keys))
        return advanced

    def _fact(self, record: OutboxRecord, keys: set[str], report: CycleReport) -> FetchFact | None:
        key = record.idempotency_key
        if key in keys or key in self._seen:
            report.duplicates += 1
            self._count("facts", result="duplicate")
            return None
        try:
            if int(record.schema_version.partition(".")[0]) != FetchCompleted.SCHEMA_MAJOR:
                raise ValueError(f"unsupported schema {record.schema_version}")
            envelope = decode_event_as(record.envelope, FetchCompleted)
            fact = fact_from_attempt(envelope.payload.attempt)
        except Exception as exc:  # noqa: BLE001 -- a malformed row must not stop the reader
            report.invalid += 1
            self._count("facts", result="invalid")
            _log.warning("source_fact_invalid", event_id=record.event_id, error=str(exc)[:200])
            return None
        keys.add(key)
        return fact

    def _count(self, name: str, *, n: int = 1, **labels: str) -> None:
        metric = self._m.get(name)
        if metric is not None:
            (metric.labels(**labels) if labels else metric).inc(n)

    # --- loop -------------------------------------------------------------------

    def run_forever(
        self,
        *,
        poll_interval_s: float,
        retry_max_s: float,
        stop: Callable[[], bool],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Fold until ``stop()``. Storage errors are retried in-process, never fatal."""
        pause = poll_interval_s
        while not stop():
            try:
                report = self.run_once()
            except StorageError as exc:
                self._count("failures")
                _log.warning("source_projection_retry", error=str(exc)[:300], retry_in_s=pause)
                sleep(pause)
                pause = min(pause * 2, retry_max_s)
                continue
            pause = poll_interval_s
            if report.buckets or report.new_generation:
                _log.info(
                    "source_projection_cycle",
                    buckets=report.buckets,
                    applied=report.applied,
                    duplicates=report.duplicates,
                    invalid=report.invalid,
                    lag_s=round(report.lag_s),
                    new_generation=report.new_generation,
                )
            if report.caught_up or report.conflict:
                sleep(poll_interval_s)
