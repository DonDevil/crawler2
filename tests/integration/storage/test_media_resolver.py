"""P8 increment 1 on the real Scylla: the resolver's concurrency and failure suite
(design §10.1, C-1…C-10; C-11 runs on the two-host profile) plus gap, provenance,
duplicates and the crawl loop running with the registry stopped.

Two resolver instances (A, B) run in one process with an injected Redis-TIME clock
and named pause hooks. No sleep decides an outcome. Every scenario ends with the
invariant of ``tests.fixtures.media.Bench.invariant``.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis
from antipiracy_contracts.events import decode_event_as, new_event
from antipiracy_contracts.events.media import MediaDiscovered, MediaObserved
from antipiracy_contracts.events.web import PageObserved
from antipiracy_contracts.ids import EventId, MediaId, ObservationId
from antipiracy_contracts.models.media import Media, MediaKind, MediaObservation, ProbeStatus
from antipiracy_contracts.models.web import UrlRef
from antipiracy_contracts.ownership import Component

from crawler2.core.configuration import EventSettings, Settings
from crawler2.core.observability import Metrics
from crawler2.media import cli
from crawler2.media.model import MediaLatest, ProbeRecord, ReaderMode, ReaderState
from crawler2.media.resolver import Level, Resolver, ResolverConfig
from crawler2.media.store import ScyllaRegistryStore
from crawler2.storage.errors import StorageUnavailableError
from crawler2.storage.events.consumer import RedisStreamReader
from crawler2.storage.events.publisher import RedisStreamPublisher, stream_name
from crawler2.storage.events.relay import OutboxRelay
from crawler2.storage.layout import OUTBOX_SHARDS, outbox_bucket, shard_of
from crawler2.storage.scylla import ScyllaStorage
from crawler2.storage.scylla.session import Consistency
from tests.fixtures.contracts import T0, page_observation, producer
from tests.fixtures.media import (
    Background,
    Bench,
    Crash,
    Pause,
    discovered,
    reference,
    registry,
)

pytestmark = pytest.mark.integration


def _one(bench: Bench, name: str = "p", n: int = 1) -> tuple[ObservationId, list[MediaId]]:
    page = bench.page(name)
    refs = [reference(f"https://cdn.example/{bench.reader}/{name}-{i}.mp4") for i in range(n)]
    bench.put(discovered(page, refs))
    return page.observation_id, [MediaId.of_locator(r.locator.url) for r in refs]


class SpyStore(ScyllaRegistryStore):
    """Records the envelopes each reader *tried* to claim."""

    def __init__(self, store: ScyllaRegistryStore) -> None:
        super().__init__(store._s)
        self.attempted: list[str] = []

    def claim_observation(self, *args: Any, **kwargs: Any) -> Any:
        self.attempted.append(str(kwargs["event_id"]))
        return super().claim_observation(*args, **kwargs)


class Views:
    """A fixed probe view per reader (C-4: readers build different envelopes)."""

    def __init__(self, status: ProbeStatus, etag: str) -> None:
        self._status, self._etag = status, etag

    def latest(self, media_id: MediaId) -> MediaLatest:
        probe = ProbeRecord(
            probe_id=str(uuid.uuid4()),
            media_id=str(media_id),
            locator_url="https://cdn.example/x.mp4",
            kind=MediaKind.VIDEO_FILE,
            probed_at=T0,
            probe_status=self._status,
            worker="it",
            fetch_outcome="media",
            etag=self._etag,
            content_type="video/mp4",
        )
        return MediaLatest(media_id=media_id, probe=probe)


# --- C-1 … C-4: two readers, one observation ------------------------------------------


def test_c1_c2_new_owner_commits_the_stale_owners_claim(storage: ScyllaStorage) -> None:
    bench = Bench.create(storage)
    obs1, (m1,) = _one(bench, "one")
    obs2, (m2,) = _one(bench, "two")
    pause = Pause("after_claim")
    a = bench.resolver("A", hook=pause)
    run_a = Background(a.run_once)
    assert pause.reached.wait(30)
    claimed = pause.context["reference"]
    first = obs1 if claimed == m1 else obs2

    bench.expire_leases()  # A is paused past lease_until
    b = bench.resolver("B")
    report_b = b.run_once()
    assert report_b.acquired
    assert report_b.buckets == 1
    assert bench.state().next_bucket == bench.b0 + 1
    assert bench.state().epoch == 3  # created 1, A 2, B 3
    stored = bench.store.claim(first, claimed)
    assert stored is not None
    assert str(stored.claimed_by).endswith(":A")

    # C-2: A resumes; its writes are identical no-ops; its advance/lease check fails.
    pause.resume.set()
    report_a = run_a.join()
    assert report_a.lease_lost or report_a.conflict
    assert bench.state().next_bucket == bench.b0 + 1
    assert bench.state().epoch == 3
    for obs, media in ((obs1, m1), (obs2, m2)):
        bench.invariant(obs, media)


def test_c3_stale_owner_built_but_never_writes_its_envelope(storage: ScyllaStorage) -> None:
    bench = Bench.create(storage)
    obs, (media,) = _one(bench)
    spy = SpyStore(bench.store)
    pause = Pause("after_build")
    a = bench.resolver("A", hook=pause, store=spy)
    run_a = Background(a.run_once)
    assert pause.reached.wait(30)
    bench.expire_leases()
    assert bench.resolver("B").run_once().buckets == 1
    pause.resume.set()
    report_a = run_a.join()
    assert report_a.lost == 1  # A's claim lost to B's
    claim = bench.store.claim(obs, media)
    assert claim is not None
    assert str(claim.claimed_by).endswith(":B")
    assert spy.attempted
    assert spy.attempted[0] != claim.event_id
    rows = bench.observed_rows(bench.key(obs, media))
    assert spy.attempted[0] not in {r.event_id for r in rows}
    bench.invariant(obs, media)


def test_c4_claim_race_with_different_envelopes_has_one_winner(storage: ScyllaStorage) -> None:
    bench = Bench.create(storage)
    obs, (media,) = _one(bench)
    pa, pb = Pause("after_build"), Pause("after_build")
    a = bench.resolver("A", hook=pa, views=Views(ProbeStatus.PROBED, '"a"'))
    run_a = Background(a.run_once)
    assert pa.reached.wait(30)
    bench.expire_leases()
    b = bench.resolver("B", hook=pb, views=Views(ProbeStatus.FORBIDDEN, '"b"'))
    run_b = Background(b.run_once)
    assert pb.reached.wait(30)
    pa.resume.set()
    pb.resume.set()  # both claim now
    run_a.join()
    report_b = run_b.join()
    assert report_b.buckets == 1
    claim = bench.store.claim(obs, media)
    assert claim is not None
    decided = decode_event_as(claim.envelope, MediaObserved).payload.observation
    a_won = str(claim.claimed_by).endswith(":A")
    winner_status = ProbeStatus.PROBED if a_won else ProbeStatus.FORBIDDEN
    assert decided.probe_status is winner_status
    bench.invariant(obs, media)


# --- C-5 … C-8: crashes and storage failure ---------------------------------------------


def test_c5_crash_after_claim_before_commit(storage: ScyllaStorage) -> None:
    bench = Bench.create(storage)
    obs, (media,) = _one(bench)
    with pytest.raises(Crash):
        bench.resolver("A", hook=Pause("after_claim", crash=True)).run_once()
    assert bench.invariant(obs, media, committed=False) == 0
    bench.expire_leases()
    assert bench.resolver("A2").run_once().buckets == 1
    assert bench.invariant(obs, media) == 1


@pytest.mark.parametrize("k", [1, 3, 5])
def test_c6_repeated_crash_after_record_before_mark(
    storage: ScyllaStorage, k: int, record_property: Any
) -> None:
    bench = Bench.create(storage)
    obs, (media,) = _one(bench)
    physical = []
    for i in range(k):
        with pytest.raises(Crash):
            bench.resolver(f"A{i}", hook=Pause("before_mark", crash=True)).run_once()
        physical.append(bench.invariant(obs, media, committed=False))
        claim = bench.store.claim(obs, media)
        assert claim is not None
        assert not claim.committed
        assert bench.state().next_bucket == bench.b0
        bench.expire_leases()
    report = bench.resolver("final").run_once()
    assert report.buckets == 1
    rows = bench.invariant(obs, media)
    physical.append(rows)
    assert bench.state().next_bucket == bench.b0 + 1
    # Re-commits in one minute bucket upsert the same outbox row (same shard, bucket and
    # event_id); each re-commit in a later bucket adds a copy. Recorded, never bounded.
    record_property("physical_outbox_rows", physical)
    print(f"C-6 k={k}: physical outbox rows after each cycle {physical}")
    assert physical[-1] >= 1


def test_c6_copies_in_later_buckets_are_byte_identical(storage: ScyllaStorage) -> None:
    """Re-commits in different minute buckets add physical rows: same event_id, same bytes."""
    bench = Bench.create(storage)
    obs, (media,) = _one(bench)
    for i in range(2):
        with pytest.raises(Crash):
            bench.resolver(f"A{i}", hook=Pause("before_mark", crash=True)).run_once()
        bench.expire_leases()
        now = datetime.now(UTC)
        time.sleep(60 - now.second - now.microsecond / 1e6 + 0.5)  # next real outbox bucket
    assert bench.resolver("final").run_once().buckets == 1
    rows = bench.invariant(obs, media)
    print(f"C-6 across buckets: {rows} physical rows, one event_id")
    assert rows == 3
    assert len({r.bucket for r in bench.observed_rows(bench.key(obs, media))}) == 3


def test_c7_crash_after_commit_before_advance(storage: ScyllaStorage) -> None:
    bench = Bench.create(storage)
    obs, medias = _one(bench, n=3)
    with pytest.raises(Crash):
        bench.resolver("A", hook=Pause("before_advance", crash=True)).run_once()
    before = {m: bench.invariant(obs, m) for m in medias}
    assert bench.state().next_bucket == bench.b0
    bench.expire_leases()
    report = bench.resolver("A2").run_once()
    assert (report.buckets, report.claimed, report.committed, report.replayed) == (1, 0, 0, 3)
    assert {m: bench.invariant(obs, m) for m in medias} == before  # no new outbox rows
    assert bench.state().next_bucket == bench.b0 + 1


class FailOnce:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.failed = False

    def record_observation(self, observation: MediaObservation, **kwargs: Any) -> None:
        if not self.failed:
            self.failed = True
            raise StorageUnavailableError("injected")
        self._inner.record_observation(observation, **kwargs)


def test_c8_storage_error_never_advances_then_retry_commits(storage: ScyllaStorage) -> None:
    bench = Bench.create(storage)
    obs, (media,) = _one(bench)
    resolver = bench.resolver("A", observations=FailOnce(storage.media))
    with pytest.raises(StorageUnavailableError):
        resolver.run_once()
    assert bench.state().next_bucket == bench.b0
    bench.invariant(obs, media, committed=False)
    report = resolver.run_once()  # same process, still the owner
    assert report.buckets == 1
    assert report.committed == 1
    bench.invariant(obs, media)


# --- C-9, C-10: checkpoint fencing and replay -----------------------------------------------


def test_c9_stale_owner_cannot_advance(storage: ScyllaStorage) -> None:
    bench = Bench.create(storage)
    obs, (media,) = _one(bench)
    pa, pb = Pause("before_advance"), Pause("before_advance")
    run_a = Background(bench.resolver("A", hook=pa).run_once)
    assert pa.reached.wait(30)
    bench.expire_leases()
    run_b = Background(bench.resolver("B", hook=pb).run_once)
    assert pb.reached.wait(30)  # B is at b, not yet advanced
    pa.resume.set()
    report_a = run_a.join()
    assert report_a.conflict
    assert report_a.buckets == 0
    assert bench.state().next_bucket == bench.b0
    pb.resume.set()
    assert run_b.join().buckets == 1
    assert bench.state().next_bucket == bench.b0 + 1
    bench.invariant(obs, media)


def test_c10_replay_of_an_advanced_bucket_emits_nothing(storage: ScyllaStorage) -> None:
    bench = Bench.create(storage)
    obs, medias = _one(bench, n=2)
    assert bench.resolver("A").run_once().buckets == 1
    before = {m: bench.invariant(obs, m) for m in medias}
    storage.session.execute_raw(
        f"UPDATE {storage.session.keyspace}.media_reader_state SET next_bucket = {bench.b0} "
        f"WHERE reader = '{bench.reader}'"
    )
    bench.expire_leases()
    report = bench.resolver("B").run_once()
    assert (report.buckets, report.claimed, report.committed, report.replayed) == (1, 0, 0, 2)
    assert {m: bench.invariant(obs, m) for m in medias} == before


# --- duplicates, provenance, identity -------------------------------------------------------


def test_duplicate_delivery_one_observation_and_provenance(storage: ScyllaStorage) -> None:
    bench = Bench.create(storage)
    page = bench.page("dup")
    shared = f"https://cdn.example/{bench.reader}/shared.mp4"
    refs = [
        reference(shared),
        reference(shared),  # the same locator twice on one page: one observation
        reference(f"https://cdn.example/{bench.reader}/v.mp4?token=a"),
        reference(f"https://cdn.example/{bench.reader}/v.mp4?token=b"),
    ]
    first = discovered(page, refs)
    retry = discovered(page, refs)  # a P5 producer retry: new event_id, same key
    bench.put(first)
    bench.put(retry, bucket=bench.b0 + 1)
    other = page_observation(
        UrlRef.of(f"https://mirror.example/{bench.reader}"), at=T0, seed=f"{bench.reader}:m"
    )
    bench.put(discovered(other, [reference(shared)]))
    bench.clock.advance(60)
    report = bench.resolver("A", buckets=2).run_once()
    assert report.buckets == 2
    assert report.committed == 4
    assert report.replayed == 3
    medias = {MediaId.of_locator(r.locator.url) for r in refs}
    assert len(medias) == 3  # token URLs are distinct level-1 identities (P8-D2)
    for media in medias:
        bench.invariant(page.observation_id, media)
    observation = storage.media.observation(
        page.observation_id, MediaId.of_locator(UrlRef.of(shared).url)
    )
    assert observation is not None
    assert observation.page == page.requested
    assert observation.page_version_id == page.page_version_id
    assert observation.observed_at == first.occurred_at
    assert observation.probe_status is ProbeStatus.NOT_PROBED
    assert observation.content is None
    claim = bench.store.claim(page.observation_id, MediaId.of_locator(UrlRef.of(shared).url))
    assert claim is not None
    envelope = decode_event_as(claim.envelope, MediaObserved)
    assert envelope.causation_id == first.event_id
    assert envelope.producer.component is Component.MEDIA_REGISTRY
    sightings = storage.media.sightings(
        MediaId.of_locator(UrlRef.of(shared).url),
        since=T0 - timedelta(days=1),
        until=T0 + timedelta(days=1),
    )
    pages = {s.page_observation_id for s in sightings}
    assert {page.observation_id, other.observation_id} <= pages  # one locator, two domains


def test_invalid_rows_are_skipped_and_counted(storage: ScyllaStorage, settings: Settings) -> None:
    bench = Bench.create(storage)
    obs, (media,) = _one(bench)
    bad = discovered(bench.page("bad"), [reference("https://cdn.example/bad.mp4")])
    shard, bucket = shard_of(bad.event_id, OUTBOX_SHARDS), bench.b0
    storage.session.execute(
        storage.session.bind(
            "INSERT INTO {ks}.outbox (shard, bucket, event_id, event_type, schema_version, "
            "idempotency_key, envelope, enqueued_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            Consistency.AUTHORITATIVE_WRITE,
            (shard, bucket, bad.event_id.uuid, "media.discovered", "1.0", "x", "{}", bench.base),
        )
    )
    metrics = Metrics(settings)
    report = bench.resolver("A", metrics=metrics).run_once()
    assert report.invalid == 1
    assert report.committed == 1
    assert report.buckets == 1
    assert b'crawler2_media_observations_total{result="invalid"} 1.0' in metrics.render()
    bench.invariant(obs, media)


# --- gap: halt, alarm, non-zero status, accept-gap ------------------------------------------


def test_gap_halts_alarms_and_needs_accept_gap(
    storage: ScyllaStorage, settings: Settings, keyspace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = Bench.create(storage)
    _one(bench)
    metrics = Metrics(settings)
    resolver = bench.resolver("A", metrics=metrics, buckets=2)
    lost_from = resolver.retained_from(bench.clock()) - 500
    storage.session.execute_raw(
        f"UPDATE {keyspace}.media_reader_state SET next_bucket = {lost_from} "
        f"WHERE reader = '{bench.reader}'"
    )
    report = resolver.run_once()
    assert report.gap_detected
    assert report.halted
    assert report.buckets == 0
    state = bench.state()
    assert state.state is ReaderMode.GAP
    assert state.gap_from == lost_from
    assert state.next_bucket == lost_from  # never skipped silently
    rendered = metrics.render()
    assert b"crawler2_media_reader_gap_total 1.0" in rendered
    assert b'crawler2_media_reader_state{state="gap"} 1.0' in rendered
    assert resolver.status().level is Level.GAP
    assert resolver.status().exit_code == 2

    # Stays halted on every later cycle, whoever runs it.
    bench.expire_leases()
    assert bench.resolver("B").run_once().halted
    assert bench.state().next_bucket == lost_from

    # The CLI status exits non-zero while halted.
    monkeypatch.setenv("CRAWLER2_SCYLLA__KEYSPACE", keyspace)
    monkeypatch.setenv("CRAWLER2_MEDIA__READER", bench.reader)
    assert cli.main(["resolve", "status"]) == 2

    # Operator acceptance: recorded first, then the checkpoint moves (epoch + 1).
    accepted = resolver.accept_gap(by="it-operator", note="test gap")
    assert accepted is not None
    assert accepted.state is ReaderMode.RUNNING
    assert accepted.next_bucket == resolver.retained_from(bench.clock())
    assert accepted.epoch == state.epoch + 1
    (gap,) = bench.store.gaps(bench.reader)
    assert gap.gap_from == lost_from
    assert gap.accepted_by == "it-operator"
    assert gap.detected_at is not None
    assert gap.note == "test gap"
    with pytest.raises(ValueError, match="not halted"):
        resolver.accept_gap(by="x", note="again")
    resumed = resolver.run_once()
    assert resumed.acquired
    assert resumed.buckets == 2
    # Resumed at the oldest retained bucket (the test keyspace's outbox is ~1 h old).
    assert resolver.status().level is not Level.GAP
    assert cli.main(["resolve", "status"]) == 0


# --- the crawl loop with the registry stopped (B.5 #1, ADR-024) ---------------------------


def test_a_stopped_registry_never_holds_back_the_crawl_event_path(
    storage: ScyllaStorage, redis_client: redis.Redis, event_settings: EventSettings
) -> None:
    run = uuid.uuid4().hex[:10]
    bounded = event_settings.model_copy(update={"stream_max_bytes": 64 * 1024, "stream_maxlen": 50})
    relay = OutboxRelay(
        storage.outbox, RedisStreamPublisher(redis_client, bounded), relay_id=run, settle_s=600
    )
    main = RedisStreamReader(
        redis_client, stream_name(bounded, PageObserved.EVENT_TYPE, 1), "extraction", "c1"
    )
    main.ensure_group()
    worker = producer(Component.CRAWLER_WORKER)
    start = datetime.now(UTC)
    store = ScyllaRegistryStore(storage.session)
    reader = f"it-loop-{run}"
    assert store.create_state(
        ReaderState(
            reader=reader,
            epoch=1,
            owner=None,
            lease_until=start - timedelta(hours=1),
            next_bucket=outbox_bucket(start),
        ),
        at=start,
        by="it",
    )
    pages = []
    for i in range(40):  # more media.discovered than the stream's byte budget holds
        page = page_observation(UrlRef.of(f"https://loop-{run}.example/p{i}"), at=T0)
        if i < 5:  # the main consumer's stream stays within its budget
            storage.pages.record(
                page,
                event=new_event(PageObserved(observation=page), producer=worker, occurred_at=T0),
            )
        refs = [reference(f"https://cdn-{run}.example/{i}-{j}.mp4") for j in range(20)]
        storage.outbox.enqueue(discovered(page, refs))
        pages.append(page)
    report = relay.run_once()
    assert report.backpressure is None  # never held back by the registry
    assert redis_client.xinfo_groups(stream_name(bounded, MediaDiscovered.EVENT_TYPE, 1)) == []
    n = 0
    while entries := main.read(count=500):
        main.ack(entries)
        n += sum(run.encode() in e.envelope for e in entries)
    assert n == 5  # the main consumer kept up while the registry was stopped
    stopped = store.state(reader)
    assert stopped is not None
    assert stopped.next_bucket == outbox_bucket(start)  # the registry did nothing

    # The registry starts later and resolves everything from the outbox.
    now = datetime.now(UTC)
    time.sleep(60 - now.second - now.microsecond / 1e6 + 0.5)  # let the bucket close
    resolver = Resolver(
        storage.outbox,
        store,
        storage.media,
        producer=registry("loop"),
        config=ResolverConfig(reader=reader, settle=timedelta(0)),
    )
    while not (r := resolver.run_once()).caught_up:
        pass
    for page in pages:
        assert len(storage.media.observations_on(page.observation_id)) == 20
    assert r.lag_s < 180


def test_media_identity_is_level_one_only() -> None:
    a = Media.of(UrlRef.of("https://a.example/v.mp4"), MediaKind.VIDEO_FILE)
    b = Media.of(UrlRef.of("https://b.example/v.mp4"), MediaKind.VIDEO_FILE)
    assert a.media_id != b.media_id
    assert EventId.new() != EventId.new()
