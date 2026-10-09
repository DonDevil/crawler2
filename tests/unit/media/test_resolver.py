"""Resolver state machine with in-memory stores (``fakes`` mirrors the CAS semantics).

The real-Scylla concurrency suite is tests/integration/storage/test_media_resolver.py.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from antipiracy_contracts.events import encode_event, new_event
from antipiracy_contracts.events.media import MediaDiscovered
from antipiracy_contracts.ids import MediaId
from antipiracy_contracts.models.media import (
    DiscoveryMethod,
    MediaKind,
    MediaObservation,
    MediaReference,
    ProbeStatus,
)
from antipiracy_contracts.models.web import UrlRef
from antipiracy_contracts.ownership import Component

from crawler2.core.configuration import Settings
from crawler2.core.observability import Metrics
from crawler2.media.model import (
    HashOutcome,
    HashRecord,
    MediaLatest,
    ProbeRecord,
    ReaderMode,
    ReaderState,
    StoredClaim,
    Validators,
)
from crawler2.media.resolver import Level, Resolver, ResolverConfig
from crawler2.storage.errors import IntegrityError, StorageUnavailableError
from crawler2.storage.events.idempotency import idempotency_key
from crawler2.storage.events.relay import OutboxRecord
from crawler2.storage.layout import bucket_start, outbox_bucket, shard_of
from tests.fixtures.contracts import T0, page_observation, producer
from tests.unit.media.fakes import FakeObservations, FakeOutbox, FakeStore

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
B0 = outbox_bucket(NOW - timedelta(minutes=20))


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _record(bucket: int, n_refs: int = 1, *, page: str = "p") -> OutboxRecord:
    obs = page_observation(UrlRef.of(f"https://site.example/{page}"), at=T0)
    env = new_event(
        MediaDiscovered(
            page_observation_id=obs.observation_id,
            page=obs.requested,
            page_version_id=obs.page_version_id,
            references=tuple(
                MediaReference(
                    locator=UrlRef.of(f"https://cdn.example/{page}-{i}.mp4"),
                    kind=MediaKind.VIDEO_FILE,
                    method=DiscoveryMethod.LINK,
                )
                for i in range(n_refs)
            ),
        ),
        producer=producer(Component.EXTRACTION),
        occurred_at=T0,
    )
    return OutboxRecord(
        shard=shard_of(env.event_id, 32),
        bucket=bucket,
        event_id=str(env.event_id),
        event_type=env.event_type,
        schema_version=env.schema_version,
        idempotency_key=idempotency_key(env),
        envelope=encode_event(env),
        enqueued_at=bucket_start(bucket),
        published_at=None,
    )


class Sink:
    def __init__(self) -> None:
        self.seen: list[tuple[MediaObservation, MediaLatest | None]] = []

    def observed(self, observation: MediaObservation, latest: MediaLatest | None) -> None:
        self.seen.append((observation, latest))


def _resolver(
    store: FakeStore,
    outbox: FakeOutbox,
    clock: Clock,
    name: str = "A",
    **kwargs: Any,
) -> Resolver:
    return Resolver(
        outbox,
        store,
        kwargs.pop("observations", FakeObservations()),
        producer=producer(Component.MEDIA_REGISTRY, instance=f"u:media_probe:1:{name}"),
        config=kwargs.pop("config", ResolverConfig(reader="r")),
        clock=clock,
        views=store,
        **kwargs,
    )


def _state(**kw: Any) -> ReaderState:
    base: dict[str, Any] = {
        "reader": "r",
        "epoch": 1,
        "owner": None,
        "lease_until": NOW - timedelta(hours=1),
        "next_bucket": B0,
    }
    base.update(kw)
    return ReaderState(**base)


def test_bootstrap_starts_at_the_oldest_retained_bucket() -> None:
    store, outbox, clock = FakeStore(), FakeOutbox(earliest=B0 - 5), Clock()
    report = _resolver(store, outbox, clock).run_once()
    assert report.bootstrapped
    state = store.states["r"]
    assert state.started_bucket == B0 - 5
    assert state.next_bucket == outbox_bucket(NOW - timedelta(minutes=10))  # caught up


def test_bootstrap_conflict_and_standby() -> None:
    store, outbox, clock = FakeStore(), FakeOutbox(), Clock()
    store.refuse_create = True
    assert _resolver(store, outbox, clock).run_once().conflict
    store.refuse_create = False
    store.states["r"] = _state(owner="other", lease_until=NOW + timedelta(seconds=30))
    report = _resolver(store, outbox, clock).run_once()
    assert report.standby
    assert store.states["r"].owner == "other"


def test_owner_keeps_renewing_and_folds_with_sink_and_views() -> None:
    store, outbox, clock = FakeStore(), FakeOutbox(), Clock()
    store.states["r"] = _state()
    record = _record(B0, 2)
    outbox.add(record)
    media = MediaId.of_locator(UrlRef.of("https://cdn.example/p-0.mp4").url)
    probe = ProbeRecord(
        probe_id="p1",
        media_id=str(media),
        locator_url="https://cdn.example/p-0.mp4",
        kind=MediaKind.VIDEO_FILE,
        probed_at=T0,
        probe_status=ProbeStatus.PROBED,
        worker="w",
        fetch_outcome="media",
        etag='"e"',
        total_length=10,
        content_type="video/mp4",
    )
    ok = HashRecord(
        hash_id="h1",
        media_id=str(media),
        locator_url=probe.locator_url,
        hashed_at=T0,
        outcome=HashOutcome.OK,
        worker="w",
        probe_id="p1",
        validators=Validators(10, '"e"', None),
        digest="sha256:" + "a" * 64,
        byte_count=10,
    )
    store.latest_rows[str(media)] = MediaLatest(media, probe=probe, hash=ok, ok_hash=ok)
    sink = Sink()
    observations = FakeObservations()
    resolver = _resolver(
        store,
        outbox,
        clock,
        sink=sink,
        observations=observations,
        config=ResolverConfig(reader="r", max_buckets_per_cycle=1),
    )
    report = resolver.run_once()
    assert report.acquired
    assert report.committed == 2
    assert report.buckets == 1
    with_content = [o for o, _ in observations.written if o.media.media_id == media]
    assert with_content[0].content == ok.content
    assert with_content[0].probe_status is ProbeStatus.PROBED
    assert with_content[0].hints is not None
    assert with_content[0].hints.content_length == 10
    assert len(sink.seen) == 2
    clock.now += timedelta(seconds=40)  # past half the lease: renewed on the next cycle
    again = resolver.run_once()
    assert not again.acquired
    assert again.buckets == 1
    assert store.states["r"].lease_until == clock.now + timedelta(seconds=60)


def test_renew_failure_and_lease_lost_mid_bucket() -> None:
    store, outbox, clock = FakeStore(), FakeOutbox(), Clock()
    store.states["r"] = _state()
    outbox.add(_record(B0, page="a"))
    outbox.add(_record(B0, page="b"))
    resolver = _resolver(store, outbox, clock, config=ResolverConfig(reader="r", shards=(0,)))
    assert resolver.run_once().acquired
    store.refuse_renew = True
    assert resolver.run_once().lease_lost
    # a foreign epoch bump is seen at the per-row lease check
    store.refuse_renew = False
    store.states["r"] = _state(next_bucket=B0)
    outbox.by_bucket = {}
    for rec in (_record(B0, page="c"), _record(B0, page="d")):
        outbox.add(rec)
    calls = {"n": 0}

    def hook(point: str, **context: Any) -> None:
        if point == "before_mark":
            calls["n"] += 1
            if calls["n"] == 1:
                s = store.states["r"]
                store.states["r"] = replace(s, epoch=s.epoch + 5)

    clock.now += timedelta(seconds=120)
    full = ResolverConfig(reader="r")
    report = _resolver(store, outbox, clock, name="B", hook=hook, config=full).run_once()
    assert report.lease_lost
    assert store.states["r"].next_bucket == B0


def test_status_levels_and_exit_codes() -> None:
    store, outbox, clock = FakeStore(), FakeOutbox(), Clock()
    resolver = _resolver(store, outbox, clock)
    assert resolver.status().level is Level.ABSENT
    assert resolver.status().exit_code == 0
    store.states["r"] = _state(next_bucket=outbox_bucket(NOW - timedelta(hours=1)))
    assert resolver.status().level is Level.OK
    store.states["r"] = _state(next_bucket=outbox_bucket(NOW - timedelta(days=4)))
    assert resolver.status().level is Level.WARNING
    store.states["r"] = _state(next_bucket=outbox_bucket(NOW - timedelta(days=8)))
    assert resolver.status().level is Level.CRITICAL
    assert resolver.status().exit_code == 1
    store.states["r"] = _state(state=ReaderMode.GAP, gap_from=1, gap_to=2)
    assert resolver.status().exit_code == 2


def test_gap_halts_and_accept_gap_resumes() -> None:
    store, outbox, clock = FakeStore(), FakeOutbox(earliest=B0 - 2), Clock()
    store.states["r"] = _state(next_bucket=B0 - 100)
    metrics = Metrics(Settings())
    resolver = _resolver(store, outbox, clock, metrics=metrics)
    with pytest.raises(ValueError, match="not halted"):
        resolver.accept_gap(by="op", note="none")
    report = resolver.run_once()
    assert report.gap_detected
    assert store.states["r"].state is ReaderMode.GAP
    assert resolver.run_once().halted
    assert b'crawler2_media_reader_state{state="gap"} 1.0' in metrics.render()
    resumed = resolver.accept_gap(by="op", note="accepted")
    assert resumed is not None
    assert resumed.next_bucket == B0 - 2
    assert resolver.run_once().acquired


def test_round_trip_violation_is_an_integrity_error() -> None:
    store, outbox, clock = FakeStore(), FakeOutbox(), Clock()
    store.states["r"] = _state()
    record = _record(B0)
    outbox.add(record)
    resolver = _resolver(store, outbox, clock)
    resolver.run_once()
    ((key, claim),) = store.claims.items()
    store.claims[key] = StoredClaim(
        claim.event_id, claim.envelope.replace("{", "{ ", 1), 1, "x", committed=False
    )
    store.states["r"] = _state(next_bucket=B0, epoch=9, lease_until=NOW - timedelta(days=1))
    with pytest.raises(IntegrityError):
        _resolver(store, outbox, clock, name="B").run_once()


def test_unsupported_major_is_invalid_not_fatal() -> None:
    store, outbox, clock = FakeStore(), FakeOutbox(), Clock()
    store.states["r"] = _state()
    record = _record(B0)
    outbox.add(replace(record, schema_version="2.0"))
    report = _resolver(store, outbox, clock, config=ResolverConfig(reader="r")).run_once()
    assert report.invalid == 1


def test_run_forever_retries_storage_errors_and_stops() -> None:
    store, outbox, clock = FakeStore(), FakeOutbox(), Clock()
    store.states["r"] = _state()
    resolver = _resolver(store, outbox, clock)
    calls = {"n": 0}
    real = resolver.run_once

    def flaky() -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise StorageUnavailableError("down")
        return real()

    resolver.run_once = flaky  # type: ignore[method-assign]
    sleeps: list[float] = []
    resolver.run_forever(
        poll_interval_s=1.0,
        retry_max_s=4.0,
        stop=lambda: calls["n"] >= 3,
        sleep=sleeps.append,
    )
    assert sleeps[0] == 1.0
    assert calls["n"] == 3
