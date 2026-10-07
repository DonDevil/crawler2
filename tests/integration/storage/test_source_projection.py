"""P7 increment 2 against the real stack: failure injection (ADR-023, design §9).

The crawl's event path (outbox → relay → Redis streams → main consumer groups)
must keep running with the P7 reader stopped. The reader must then catch up,
or rebuild after a gap, to the pure fold of the same facts. Real Scylla
(throwaway keyspace) and Redis (private stream prefix per test).
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis
from antipiracy_contracts.events import new_event
from antipiracy_contracts.events.web import FetchCompleted, PageObserved
from antipiracy_contracts.models.web import FetchAttempt, FetchCapability, FetchOutcome, UrlRef
from antipiracy_contracts.ownership import Component

from crawler2.core.configuration import (
    DiscoverySettings,
    EventSettings,
    ExecutionQueue,
    FrontierSettings,
    Settings,
)
from crawler2.discovery.admission import Admitter, Candidate, Origin, Outcome
from crawler2.frontier.redis import RedisFrontier, connect_redis
from crawler2.intelligence.advice import SnapshotAdvisor, load_snapshot
from crawler2.intelligence.projection import ReaderConfig, SourceProjector
from crawler2.intelligence.sources import DEFAULT_POLICY, SourceProfile, fact_from_attempt, fold
from crawler2.intelligence.store import ScyllaSourceProfileStore
from crawler2.storage.events.consumer import RedisStreamReader
from crawler2.storage.events.publisher import RedisStreamPublisher, stream_name
from crawler2.storage.events.relay import OutboxRelay
from crawler2.storage.scylla import ScyllaStorage
from tests.fixtures.contracts import page_observation, producer
from tests.unit.discovery.test_admission import _holder
from tests.unit.intelligence.fakes import attempt

pytestmark = pytest.mark.integration

BUDGET = 64 * 1024
WORKER = producer(Component.CRAWLER_WORKER)


def _raw(client: redis.Redis) -> Any:
    return client


def _record(storage: ScyllaStorage, attempts: list[FetchAttempt]) -> None:
    for a in attempts:
        storage.fetch_attempts.record(
            a,
            event=new_event(FetchCompleted(attempt=a), producer=WORKER, occurred_at=a.finished_at),
        )


def _observe(storage: ScyllaStorage, host: str, n: int, at: datetime) -> None:
    """The other half of the crawl's event path: page.observed for the main group."""
    for i in range(n):
        obs = page_observation(UrlRef.of(f"https://{host}/page-{i}"), at=at)
        storage.pages.record(
            obs, event=new_event(PageObserved(observation=obs), producer=WORKER, occurred_at=at)
        )


def _expected(attempts: list[FetchAttempt]) -> dict[str, SourceProfile]:
    out: dict[str, SourceProfile] = {}
    for a in attempts:
        f = fact_from_attempt(a)
        out[f.domain_id] = fold(out.get(f.domain_id), f)
    return out


def _same(p: SourceProfile, q: SourceProfile) -> bool:
    for f in fields(SourceProfile):
        a, b = getattr(p, f.name), getattr(q, f.name)
        for x, y in zip(a, b, strict=True) if isinstance(a, tuple) else [(a, b)]:
            if not (math.isclose(x, y, rel_tol=1e-12) if isinstance(x, float) else x == y):
                return False
    return True


def _projected(
    store: ScyllaSourceProfileStore, projection: str, domains: set[str]
) -> dict[str, SourceProfile]:
    state = store.state(projection)
    assert state is not None
    rows = store.profiles(domains)
    return {
        d: r.profile
        for d, r in rows.items()
        if r.generation == state.generation and r.policy == state.policy
    }


def _catch_up(projector: SourceProjector) -> int:
    cycles = 0
    while True:
        cycles += 1
        report = projector.run_once()
        assert not report.conflict
        if report.caught_up:
            return cycles


def _wait_for_next_bucket() -> None:
    """Let the current outbox minute bucket close."""
    now = datetime.now(UTC)
    time.sleep(60 - now.second - now.microsecond / 1e6 + 0.5)


def _drain(reader: RedisStreamReader, run: str) -> int:
    """Read and acknowledge everything; count this test's entries (the keyspace is
    shared with other tests, whose unpublished rows the relay may also deliver)."""
    n = 0
    while entries := reader.read(count=500):
        reader.ack(entries)
        n += sum(run.encode() in e.envelope for e in entries)
    return n


def test_a_stopped_p7_never_holds_back_the_event_path_and_catches_up(
    storage: ScyllaStorage, redis_client: redis.Redis, event_settings: EventSettings
) -> None:
    run = uuid.uuid4().hex[:10]
    bounded = event_settings.model_copy(update={"stream_max_bytes": BUDGET, "stream_maxlen": 50})
    relay = OutboxRelay(
        storage.outbox, RedisStreamPublisher(redis_client, bounded), relay_id=run, settle_s=600
    )
    main = RedisStreamReader(
        redis_client, stream_name(bounded, PageObserved.EVENT_TYPE, 1), "extraction", "c1"
    )
    main.ensure_group()
    fetch_stream = stream_name(bounded, FetchCompleted.EVENT_TYPE, 1)
    store = ScyllaSourceProfileStore(storage.session)
    projection = f"it-{run}"

    # Real clock, settle 0: a minute bucket is read once it has closed (the test waits
    # for that instead of the production 10-minute settle).
    def reader() -> SourceProjector:
        return SourceProjector(
            storage.outbox,
            store,
            config=ReaderConfig(
                projection=projection, settle=timedelta(0), max_buckets_per_cycle=500
            ),
        )

    base = datetime.now(UTC) - timedelta(hours=2)
    live, dead, spa = (f"{h}-{run}.p7.example" for h in ("live", "dead", "spa"))

    # 1. P7 consumes normally.
    phase1 = [attempt(live, m, path=f"a{m}", base=base) for m in range(0, 30, 3)] + [
        attempt(dead, m, path=f"d{m}", outcome=FetchOutcome.TIMEOUT, status=None, base=base)
        for m in range(0, 30, 5)
    ]
    _record(storage, phase1)
    _observe(storage, live, 5, base)
    relay.run_once()
    assert _drain(main, run) == 5
    _wait_for_next_bucket()
    _catch_up(reader())
    domains = {fact_from_attempt(a).domain_id for a in phase1}
    got = _projected(store, projection, domains)
    want = _expected(phase1)
    assert set(got) == set(want)
    assert all(_same(got[d], want[d]) for d in want)
    checkpoint = store.state(projection)
    assert checkpoint is not None

    # 2-4. P7 is stopped; the crawl goes on and produces more fetch.completed than the
    #      stream's whole byte budget, plus page.observed for the main group.
    phase2 = [
        attempt(dead, 30 + m, path=f"e{m}", outcome=FetchOutcome.TIMEOUT, status=None, base=base)
        for m in range(60)
    ]
    for m in range(0, 40, 4):
        phase2.append(attempt(spa, m, path=f"s{m}", detail="outcome=needs_js", base=base))
        phase2.append(
            attempt(spa, m + 1, path=f"s{m}", capability=FetchCapability.BROWSER, base=base)
        )
    _record(storage, phase2)
    _observe(storage, dead, 7, base)
    report = relay.run_once()
    assert report.backpressure is None  # the relay was never held back
    assert report.published >= len(phase2) + 7
    assert _raw(redis_client).xinfo_groups(fetch_stream) == []  # P7 holds no group
    assert _drain(main, run) == 7  # the main consumer kept up
    assert _raw(redis_client).xpending(main.stream, "extraction")["pending"] == 0
    # The ungrouped stream was trimmed (ADR-022 rule 3): P7 could not have read it back.
    assert _raw(redis_client).xlen(fetch_stream) < len(phase1) + len(phase2)
    # No crawl data lost: every attempt is in the authoritative table.
    assert all(storage.fetch_attempts.get(a.fetch_attempt_id) for a in phase1 + phase2)
    assert store.state(projection) == checkpoint  # P7 did nothing meanwhile

    # 5-8. P7 restarts and catches up from the outbox to the pure projection.
    _wait_for_next_bucket()
    _catch_up(reader())
    every = phase1 + phase2
    domains = {fact_from_attempt(a).domain_id for a in every}
    got = _projected(store, projection, domains)
    want = _expected(every)
    assert set(got) == set(want)
    assert all(_same(got[d], want[d]) for d in want)

    # Gap: the checkpoint is older than the outbox retains (a 14-day outage).
    state = store.state(projection)
    assert state is not None
    lost = replace(state, next_bucket=reader().retained_from(datetime.now(UTC)) - 10_000)
    assert store.replace_state(state, lost, by="it")
    p = reader()
    first = p.run_once()
    assert first.new_generation == "gap"
    _catch_up(p)
    rebuilt = store.state(projection)
    assert rebuilt is not None
    assert (rebuilt.generation, rebuilt.gaps) == (state.generation + 1, 1)
    got = _projected(store, projection, domains)
    assert set(got) == set(want)
    assert all(_same(got[d], want[d]) for d in want)


def test_an_ordinary_group_on_fetch_completed_would_stall_the_relay(
    storage: ScyllaStorage, redis_client: redis.Redis, event_settings: EventSettings
) -> None:
    """Why ADR-023: under ADR-022 a dead group of a P7 reader would hold back the relay,
    and with it every stream (the X-8/X-16 mode). This is the rejected draft design."""
    run = uuid.uuid4().hex[:10]
    bounded = event_settings.model_copy(update={"stream_max_bytes": BUDGET})
    dead_group = RedisStreamReader(
        redis_client, stream_name(bounded, FetchCompleted.EVENT_TYPE, 1), "intel-sources", "x"
    )
    dead_group.ensure_group()  # created, never read
    relay = OutboxRelay(
        storage.outbox, RedisStreamPublisher(redis_client, bounded), relay_id=run, settle_s=600
    )
    base = datetime.now(UTC) - timedelta(hours=1)
    _record(
        storage,
        [attempt(f"h-{run}.p7.example", m / 10, path=f"p{m}", base=base) for m in range(120)],
    )
    report = relay.run_once()
    assert report.backpressure == dead_group.stream


def test_admission_follows_p7_and_falls_back_when_it_is_gone(
    storage: ScyllaStorage, redis_client: redis.Redis, event_settings: EventSettings
) -> None:
    """P7 → P6 → P3 on the real stores: deferral, browser start, fallback."""
    run = uuid.uuid4().hex[:10]
    store = ScyllaSourceProfileStore(storage.session)
    projection = f"it-adm-{run}"
    now = datetime.now(UTC)

    def ahead() -> datetime:
        return now + timedelta(minutes=15)

    base = now - timedelta(hours=3)
    live, dead, spa = (f"{h}-{run}.p7.example" for h in ("live", "dead", "spa"))
    facts = [attempt(live, 170, base=base)] + [
        attempt(dead, m, path=f"d{m}", outcome=FetchOutcome.TIMEOUT, status=None, base=base)
        for m in range(150, 175, 5)
    ]
    for m in range(140, 172, 8):
        facts.append(attempt(spa, m, path=f"s{m}", detail="outcome=needs_js", base=base))
        facts.append(
            attempt(spa, m + 1, path=f"s{m}", capability=FetchCapability.BROWSER, base=base)
        )
    _record(storage, facts)
    _catch_up(
        SourceProjector(
            storage.outbox,
            store,
            config=ReaderConfig(projection=projection, max_buckets_per_cycle=500),
            clock=ahead,
        )
    )
    namespace = f"p7-{run}"
    frontier = RedisFrontier(
        connect_redis(Settings(events=event_settings).redis),
        FrontierSettings(default_interval_s=0.0),
        namespace=namespace,
    )

    def admitter(advisor: SnapshotAdvisor | None) -> Admitter:
        return Admitter(
            frontier=frontier,
            discovery=storage.discovery,
            holder=_holder([]),
            settings=DiscoverySettings(revisit_after_s=0.0),
            decision_ttl_s=3600,
            clock=lambda: now,
            advisor=advisor,
        )

    def advisor(load: Any) -> SnapshotAdvisor:
        a = SnapshotAdvisor(
            load,
            max_lag=timedelta(hours=1),
            max_snapshot_age=timedelta(minutes=30),
            refresh_s=300,
        )
        a.refresh()
        return a

    cands = [Candidate(UrlRef.of(f"https://{h}/new"), Origin.SEED) for h in (live, dead, spa)]
    try:
        informed = advisor(lambda: load_snapshot(store, projection, now=now, policy=DEFAULT_POLICY))
        outcomes = admitter(informed).admit(cands)
        by_host = {c.url.url.split("/")[2]: outcomes[c.url.url_id] for c in cands}
        assert by_host == {
            live: Outcome.ADMITTED,
            dead: Outcome.SOURCE_DEFERRED,
            spa: Outcome.ADMITTED,
        }
        browser = frontier.claim(ExecutionQueue.BROWSER)
        http = frontier.claim(ExecutionQueue.HTTP)
        assert browser is not None
        assert http is not None
        assert browser.url == f"https://{spa}/new"
        assert http.url == f"https://{live}/new"
        assert frontier.claim(ExecutionQueue.HTTP) is None  # the dead host was not admitted

        # P7 gone: its store fails. Nothing is deferred and everything starts on http.
        def broken() -> Any:
            raise ConnectionError("p7 store unreachable")

        fresh = [Candidate(UrlRef.of(f"https://{h}/other"), Origin.SEED) for h in (dead, spa)]
        outcomes = admitter(advisor(broken)).admit(fresh)
        assert set(outcomes.values()) == {Outcome.ADMITTED}
        claimed = [frontier.claim(ExecutionQueue.HTTP), frontier.claim(ExecutionQueue.HTTP)]
        assert sorted(c.url for c in claimed if c is not None) == sorted(c.url.url for c in fresh)
    finally:
        for k in _raw(redis_client).scan_iter(match=f"*{namespace}*"):
            redis_client.delete(k)
