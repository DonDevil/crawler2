"""P8 test builders: media.discovered facts in chosen outbox buckets, a settable clock,
pause hooks and the resolver invariant (design §10.1). Used by the real-Scylla tests
and the two-host driver."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from antipiracy_contracts.events import EventEnvelope, decode_event_as, new_event
from antipiracy_contracts.events.media import MediaDiscovered, MediaObserved
from antipiracy_contracts.ids import EventId, MediaId, ObservationId
from antipiracy_contracts.models.media import DiscoveryMethod, MediaKind, MediaReference
from antipiracy_contracts.models.web import PageObservation, UrlRef
from antipiracy_contracts.ownership import Component

from crawler2.media.model import ReaderState
from crawler2.media.resolver import Resolver, ResolverConfig
from crawler2.media.store import ScyllaRegistryStore
from crawler2.storage.events.idempotency import idempotency_key
from crawler2.storage.events.relay import OutboxRecord
from crawler2.storage.layout import OUTBOX_SHARDS, bucket_start, outbox_bucket
from crawler2.storage.scylla import ScyllaStorage
from crawler2.storage.scylla.session import Consistency
from tests.fixtures.contracts import T0, page_observation, producer

_ROWS = (
    "SELECT shard, bucket, event_id, event_type, schema_version, idempotency_key, envelope, "
    "enqueued_at, published_at FROM {ks}.outbox WHERE shard = ? AND bucket = ?"
)

EXTRACTION = producer(Component.EXTRACTION)


def registry(name: str) -> Any:
    return producer(Component.MEDIA_REGISTRY, instance=f"it:media_probe:1:{name}")


class Clock:
    """The injected Redis-TIME clock: tests move it explicitly."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += timedelta(seconds=seconds)


class Crash(BaseException):
    """Raised by a hook: the resolver instance is discarded mid-step."""


class Pause:
    """Blocks (or crashes) the first time ``point`` is reached; tests release it."""

    def __init__(self, point: str, *, crash: bool = False, times: int = 1) -> None:
        self.point = point
        self.crash = crash
        self.left = times
        self.reached = threading.Event()
        self.resume = threading.Event()
        self.context: dict[str, Any] = {}

    def __call__(self, point: str, **context: Any) -> None:
        if point != self.point or self.left <= 0:
            return
        self.left -= 1
        self.context = context
        if self.crash:
            raise Crash(point)
        self.reached.set()
        if not self.resume.wait(60):
            raise TimeoutError(f"pause at {point} never released")


class Background:
    """Run ``fn`` in a thread and keep its result or exception."""

    def __init__(self, fn: Any) -> None:
        self.result: Any = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self._thread.start()

    def _run(self, fn: Any) -> None:
        try:
            self.result = fn()
        except BaseException as exc:  # noqa: BLE001 -- surfaced to the test
            self.error = exc

    def join(self) -> Any:
        self._thread.join(60)
        assert not self._thread.is_alive()
        if self.error is not None:
            raise self.error
        return self.result


def reference(url: str, kind: MediaKind = MediaKind.VIDEO_FILE) -> MediaReference:
    return MediaReference(locator=UrlRef.of(url), kind=kind, method=DiscoveryMethod.SOURCE_ELEMENT)


def discovered(
    page: PageObservation, refs: list[MediaReference], *, at: datetime | None = None
) -> EventEnvelope[MediaDiscovered]:
    return new_event(
        MediaDiscovered(
            page_observation_id=page.observation_id,
            page=page.requested,
            page_version_id=page.page_version_id,
            references=tuple(refs),
        ),
        producer=EXTRACTION,
        occurred_at=at or page.observed_at,
    )


_slot = 0
_slot_lock = threading.Lock()


@dataclass
class Bench:
    """One test's private outbox region (future buckets no other test writes) and reader."""

    storage: ScyllaStorage
    store: ScyllaRegistryStore
    reader: str
    base: datetime
    clock: Clock
    started: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def create(cls, storage: ScyllaStorage) -> Bench:
        global _slot
        with _slot_lock:
            _slot += 1
            slot = _slot
        run = uuid.uuid4()
        minute = datetime.now(UTC).replace(second=0, microsecond=0)
        # Future buckets: outbox enqueued_at is a column value (never a write timestamp).
        base = minute + timedelta(days=1, hours=slot * 3)
        bench = cls(
            storage=storage,
            store=ScyllaRegistryStore(storage.session),
            reader=f"it-{run.hex[:12]}",
            base=base,
            clock=Clock(base + timedelta(minutes=11)),
        )
        assert bench.store.create_state(
            ReaderState(
                reader=bench.reader,
                epoch=1,
                owner=None,
                lease_until=base - timedelta(hours=1),
                next_bucket=bench.b0,
                started_bucket=bench.b0,
            ),
            at=base,
            by="it",
        )
        return bench

    @property
    def b0(self) -> int:
        return outbox_bucket(self.base)

    def put(self, envelope: EventEnvelope[Any], *, bucket: int | None = None) -> None:
        at = bucket_start(self.b0 if bucket is None else bucket) + timedelta(seconds=5)
        self.storage.session.execute(self.storage.outbox.statement(envelope, now=at))

    def page(self, name: str, host: str = "site.example") -> PageObservation:
        return page_observation(
            UrlRef.of(f"https://{host}/{self.reader}/{name}"), at=T0, seed=f"{self.reader}:{name}"
        )

    def resolver(self, name: str, **kwargs: Any) -> Resolver:
        config = kwargs.pop(
            "config",
            ResolverConfig(reader=self.reader, max_buckets_per_cycle=kwargs.pop("buckets", 1)),
        )
        return Resolver(
            self.storage.outbox,
            kwargs.pop("store", self.store),
            kwargs.pop("observations", self.storage.media),
            producer=registry(name),
            config=config,
            clock=self.clock,
            **kwargs,
        )

    def expire_leases(self) -> None:
        self.clock.advance(61)

    def state(self) -> ReaderState:
        state = self.store.state(self.reader)
        assert state is not None
        return state

    # --- the invariant (design §10.1) ------------------------------------------------

    def observed_rows(self, key: str) -> list[OutboxRecord]:
        """Every outbox row of this idempotency key written since the test started."""
        first = outbox_bucket(self.started - timedelta(minutes=2))
        last = outbox_bucket(datetime.now(UTC) + timedelta(minutes=2))
        session = self.storage.session
        results = session.execute_many(
            session.bind(_ROWS, Consistency.STRONG_READ, (shard, bucket))
            for bucket in range(first, last + 1)
            for shard in range(OUTBOX_SHARDS)
        )
        return [
            OutboxRecord(
                shard=r.shard,
                bucket=r.bucket,
                event_id=str(EventId.from_uuid(r.event_id)),
                event_type=r.event_type,
                schema_version=r.schema_version,
                idempotency_key=r.idempotency_key,
                envelope=r.envelope.encode("utf-8"),
                enqueued_at=r.enqueued_at,
                published_at=r.published_at,
            )
            for rows in results
            for r in rows
            if r.idempotency_key == key
        ]

    def key(self, page_obs: ObservationId, media_id: MediaId) -> str:
        claim = self.store.claim(page_obs, media_id)
        assert claim is not None
        return idempotency_key(decode_event_as(claim.envelope, MediaObserved))

    def invariant(
        self, page_obs: ObservationId, media_id: MediaId, *, committed: bool = True
    ) -> int:
        """One decided envelope; M2/M3 equal to it; one event_id; identical copies.
        Returns the number of physical outbox rows (recorded, never bounded)."""
        claim = self.store.claim(page_obs, media_id)
        assert claim is not None
        decided = decode_event_as(claim.envelope, MediaObserved)
        rows = self.observed_rows(idempotency_key(decided))
        assert {r.event_id for r in rows} <= {claim.event_id}
        assert all(r.envelope == claim.envelope.encode("utf-8") for r in rows)
        if committed:
            assert claim.committed
            assert rows
            assert self.storage.media.observation(page_obs, media_id) == decided.payload.observation
            sightings = [
                s
                for s in self.storage.media.sightings(
                    media_id, since=T0 - timedelta(days=1), until=T0 + timedelta(days=1)
                )
                if s.page_observation_id == page_obs
            ]
            assert len(sightings) == 1
        else:
            assert self.state().next_bucket <= self.b0  # never past uncommitted work
        return len(rows)
