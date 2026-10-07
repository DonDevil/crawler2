"""In-memory outbox and P7 store with failure injection (P7 increment 2 tests)."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Sequence
from dataclasses import replace
from datetime import datetime, timedelta

from antipiracy_contracts.events import encode_event
from antipiracy_contracts.events.web import FetchCompleted
from antipiracy_contracts.models.web import FetchAttempt, FetchCapability, FetchOutcome, UrlRef
from antipiracy_contracts.ownership import Component

from crawler2.intelligence.projection import ProjectionState, StoredProfile
from crawler2.storage.errors import StorageUnavailableError
from crawler2.storage.events.idempotency import idempotency_key
from crawler2.storage.events.relay import OutboxRecord
from crawler2.storage.layout import bucket_start, outbox_bucket
from tests.fixtures.contracts import event_of, fetch_attempt

T0 = datetime.fromisoformat("2026-06-01T12:00:00+00:00")
B0 = outbox_bucket(T0)


def attempt(
    host: str,
    minute: float,
    *,
    path: str = "x",
    outcome: FetchOutcome = FetchOutcome.RESPONSE,
    status: int | None = 200,
    detail: str | None = None,
    capability: FetchCapability = FetchCapability.HTTP,
    final: str | None = None,
    base: datetime = T0,
) -> FetchAttempt:
    at = base + timedelta(minutes=minute)
    made = fetch_attempt(UrlRef.of(f"https://{host}/{path}"), at=at, outcome=outcome)
    update: dict[str, object] = {"http_status": status, "detail": detail, "capability": capability}
    if final is not None:
        update["final"] = UrlRef.of(final)
    return made.model_copy(update=update)


def record(a: FetchAttempt, bucket: int | None = None, shard: int = 0) -> OutboxRecord:
    """The outbox row a recorder writes for ``a`` (a fresh event id every call)."""
    env = event_of(FetchCompleted(attempt=a), Component.CRAWLER_WORKER, at=a.finished_at)
    b = outbox_bucket(a.finished_at) if bucket is None else bucket
    return OutboxRecord(
        shard=shard,
        bucket=b,
        event_id=str(env.event_id),
        event_type=env.event_type,
        schema_version=env.schema_version,
        idempotency_key=idempotency_key(env),
        envelope=encode_event(env),
        enqueued_at=bucket_start(b),
        published_at=None,
    )


class MemoryOutbox:
    def __init__(self, earliest: int = B0 - 60) -> None:
        self.parts: dict[tuple[int, int], list[OutboxRecord]] = {}
        self.earliest = earliest
        self.fail = False
        self.reads = 0

    def add(self, *records: OutboxRecord) -> None:
        for r in records:
            self.parts.setdefault((r.shard, r.bucket), []).append(r)

    def rows(self, shard: int, bucket: int) -> list[OutboxRecord]:
        if self.fail:
            raise StorageUnavailableError("outbox read timed out")
        self.reads += 1
        return list(self.parts.get((shard, bucket), []))

    def earliest_bucket(self) -> int:
        return self.earliest


class MemoryProfileStore:
    """The ``SourceProfileStore`` contract, with switches for failure injection."""

    def __init__(self) -> None:
        self.states: dict[str, ProjectionState] = {}
        self.rows: dict[str, StoredProfile] = {}
        self.fail_reads = False
        self.fail_puts = 0
        """Fail the next N ``put_profiles`` calls."""
        self.fail_cas_after_put = 0
        """Raise on the next N checkpoint CAS that follow a profile write (a crash
        between the profile rows and the checkpoint)."""
        self.on_put: Callable[[], None] | None = None
        """Runs after a profile write: an external writer acting mid-cycle."""
        self.writes = 0
        self._dirty = False

    def _check(self) -> None:
        if self.fail_reads:
            raise StorageUnavailableError("scylla unavailable")

    def state(self, projection: str) -> ProjectionState | None:
        self._check()
        return self.states.get(projection)

    def create_state(self, state: ProjectionState, *, by: str) -> bool:
        self._check()
        if state.projection in self.states:
            return False
        self.states[state.projection] = state
        return True

    def replace_state(self, expected: ProjectionState, new: ProjectionState, *, by: str) -> bool:
        self._check()
        dirty, self._dirty = self._dirty, False
        if self.fail_cas_after_put and dirty:
            self.fail_cas_after_put -= 1
            raise StorageUnavailableError("cas timed out")
        current = self.states.get(expected.projection)
        if current is None or (current.generation, current.next_bucket) != (
            expected.generation,
            expected.next_bucket,
        ):
            return False
        self.states[new.projection] = new
        return True

    def profiles(self, domain_ids: Collection[str]) -> dict[str, StoredProfile]:
        self._check()
        return {d: self.rows[d] for d in domain_ids if d in self.rows}

    def put_profiles(self, rows: Sequence[StoredProfile]) -> None:
        self._check()
        if self.fail_puts:
            self.fail_puts -= 1
            raise StorageUnavailableError("write timed out")
        for row in rows:
            self.rows[row.profile.domain_id] = row
            self.writes += 1
        self._dirty = True
        if self.on_put is not None:
            self.on_put()

    def scan(self) -> Iterator[StoredProfile]:
        self._check()
        yield from list(self.rows.values())

    def bump(self, projection: str, **changes: object) -> None:
        """An external writer (operator rebuild, a second reader) changes the state."""
        self.states[projection] = replace(self.states[projection], **changes)  # type: ignore[arg-type]
