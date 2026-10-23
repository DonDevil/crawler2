"""P11 / M4 replay against the real stack (P11 validation §4).

Real: Redis streams and consumer groups, ``IdempotentConsumer`` with Scylla
processed-event markers, the media registry (M1-M6), page observations (W4),
the P2/P3-P5 projections, the P11 tables, the snapshot loader, the P6
``Admitter`` and the P3 ``RedisFrontier`` (claim order). Simulated, and
labelled so in the docs: the network fetch (a fixture site oracle decides
what a page carries for a capability) and the fingerprinter (a fixture oracle
publishes a contract-valid ``match.found`` for matching content).

Population (fixed seed, fixed timestamps): two productive hosts and six
comparable unproductive ones. ``alpha`` serves its player only to a browser
(an http fetch answers 200 with no media); ``beta`` carries media on http.
Cycle 1 crawls a few pages of every host and produces the evidence. Cycle 2
offers the same new pages of every host to the same frontier budget with
feedback off and on, and counts the matching media the claimed pages carry.
"""

from __future__ import annotations

import json
import random
import uuid
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis
from antipiracy_contracts.events import encode_event
from antipiracy_contracts.events.fingerprinting import MatchFound
from antipiracy_contracts.events.targets import TargetRegistered
from antipiracy_contracts.models.targets import Target
from antipiracy_contracts.models.web import FetchCapability, UrlRef
from antipiracy_contracts.ownership import Component

from crawler2.core.configuration import (
    DiscoverySettings,
    EventSettings,
    ExecutionQueue,
    FrontierSettings,
    Settings,
)
from crawler2.discovery.admission import Admitter, Candidate, Origin
from crawler2.frontier.model import Admission
from crawler2.frontier.redis import RedisFrontier, connect_redis
from crawler2.intelligence.feedback_service import (
    FeedbackLoop,
    FeedbackPrioritizer,
    MatchFeedback,
    TargetFeedback,
    load_feedback_snapshot,
    rebuild,
)
from crawler2.intelligence.feedback_store import ScyllaFeedbackStore
from crawler2.storage.events.consumer import (
    IdempotentConsumer,
    RedisStreamReader,
    connect_stream_client,
)
from crawler2.storage.events.publisher import OutboundEvent, RedisStreamPublisher, stream_name
from crawler2.storage.scylla import ScyllaStorage
from tests.fixtures.contracts import (
    content_key,
    event_of,
    match,
    media,
    media_observation,
    page_observation,
    target,
)
from tests.unit.discovery.test_admission import _holder

pytestmark = pytest.mark.integration

T1 = datetime(2026, 10, 20, 9, tzinfo=UTC)
"""Cycle 1 (bootstrap crawl and matching)."""
T2 = T1 + timedelta(hours=6)
"""Cycle 2 (the next crawl cycle)."""
SEED = 1101
PRODUCTIVE = {"alpha": "A", "beta": "B"}
HOSTS = ["alpha", "beta", *(f"dud{i}" for i in range(1, 7))]
JS_ONLY = {"alpha"}
WORKERS, TICKS = 2, 8
"""Cycle 2 budget: 2 claims per tick for 8 ticks = 16 of 64 offered pages. Workers
are capability-flexible: slot 0 prefers the browser queue, slot 1 the http queue,
each falling back to the other, so both arms spend the same budget."""
_SLOTS = (
    (ExecutionQueue.BROWSER, ExecutionQueue.HTTP),
    (ExecutionQueue.HTTP, ExecutionQueue.BROWSER),
)


def page_url(host: str, n: int, run: str) -> str:
    """URLs are private to a test run: admission state is shared in the keyspace."""
    return f"https://{host}.test/movie/2026/{host}-title-{run}-{n}/"


def carries(host: str, capability: FetchCapability, run: str, n: int) -> tuple[bytes, str | None]:
    """The fixture site: (media bytes, matched target key or None) a fetch finds."""
    if host in JS_ONLY and capability is not FetchCapability.BROWSER:
        return b"", None  # 200 OK, player not rendered: no media
    if host in PRODUCTIVE:
        return f"{run}:{host}:{n}:copy-of-{PRODUCTIVE[host]}".encode(), PRODUCTIVE[host]
    return f"{run}:{host}:{n}:unrelated".encode(), None


@dataclass
class Stack:
    storage: ScyllaStorage
    redis_bytes: redis.Redis
    events: EventSettings
    run: str
    targets: dict[str, Target]
    frontier_client: redis.Redis
    namespaces: list[str] = field(default_factory=list)

    def frontier(self, clock: list[float]) -> RedisFrontier:
        ns = f"it-p11-{uuid.uuid4().hex[:8]}"
        self.namespaces.append(ns)
        return RedisFrontier(
            self.frontier_client,
            FrontierSettings(default_interval_s=1.0, max_inflight_per_domain=1),
            namespace=ns,
            clock=lambda: clock[0],
        )

    def publish(self, payloads: list[Any], component: Component, at: datetime) -> None:
        envs = [event_of(p, component, at=at) for p in payloads]
        RedisStreamPublisher(self.redis_bytes, self.events).publish(
            [
                OutboundEvent(
                    str(e.event_id),
                    e.event_type,
                    e.schema_major,
                    encode_event(e),
                )
                for e in envs
            ]
        )

    def fetch(self, url: str, capability: FetchCapability, at: datetime) -> list[Any]:
        """Simulated fetch + real registry writes; returns the match.found payloads the
        simulated matcher would publish for what this page carries."""
        host = UrlRef.of(url).url.split("//")[1].split(".")[0]
        n = int(url.rstrip("/").rsplit("-", 1)[1])
        page = page_observation(UrlRef.of(url), at=at, seed=f"{self.run}:{url}:{at}").model_copy(
            update={"capability": capability}
        )
        self.storage.pages.record(page)
        data, matched = carries(host, capability, self.run, n)
        if not data:
            return []
        key = content_key(data)
        item = media(f"{self.run}/{host}/{n}.mp4", host="cdn.p11.example")
        self.storage.media.record_observation(media_observation(item, page, content=key))
        if matched is None:
            return []
        return [MatchFound(match=match(key.content_id, self.targets[matched].ref, at=at))]


@pytest.fixture
def stack(
    storage: ScyllaStorage, settings: Settings, request: pytest.FixtureRequest
) -> Iterator[Stack]:
    # Fixed per test (the keyspace is recreated per session), so the measured
    # numbers are reproducible run to run.
    run = "r" + format(zlib.crc32(request.node.name.encode()), "08x")
    events = EventSettings(stream_prefix=f"it-p11-{run}:")
    rb = connect_stream_client(settings.redis)
    fc = connect_redis(settings.redis.model_copy(update={"db": 9}))
    targets = {k: target(f"p11-{run}-{k}") for k in ("A", "B")}
    s = Stack(storage, rb, events, run, targets, fc)
    ScyllaFeedbackStore(storage.session).clear()
    yield s
    for key in rb.scan_iter(match=f"it-p11-{run}:*"):
        rb.delete(key)
    for ns in s.namespaces:
        for key in fc.scan_iter(match=f"{ns}:*"):
            fc.delete(key)
    rb.close()
    fc.close()


def consume(s: Stack) -> FeedbackLoop:
    group = f"feedback-{s.run}"
    feedback = MatchFeedback(
        media=s.storage.media,
        pages=s.storage.pages,
        projections=s.storage.projections,
        store=ScyllaFeedbackStore(s.storage.session),
    )
    targets = TargetFeedback(s.storage.projections)
    pairs: list[Any] = []
    handlers: tuple[tuple[Any, Any], ...] = (
        (TargetRegistered, targets.registered),
        (MatchFound, feedback.handle),
    )
    for payload, handler in handlers:
        reader = RedisStreamReader(
            s.redis_bytes,
            stream_name(s.events, payload.EVENT_TYPE, payload.SCHEMA_MAJOR),
            group,
            f"it-{s.run}",
        )
        reader.ensure_group()
        pairs.append(
            (
                reader,
                IdempotentConsumer(
                    group, payload, handler, s.storage.processed_events, clock=lambda: T1
                ),
            )
        )
    loop = FeedbackLoop(pairs, batch_size=100, block_ms=10, claim_idle_ms=60_000)
    loop.run(max_idle_polls=2)
    return loop


def cycle_one(s: Stack, *, matches: bool = True) -> int:
    """Register targets, crawl 4 pages per host (alpha: 2 http + 2 browser), match."""
    s.publish(
        [TargetRegistered(target=t) for t in s.targets.values()], Component.TARGET_MANAGER, T1
    )
    clock = [1_000_000.0]
    frontier = s.frontier(clock)
    for host in HOSTS:
        for n in range(4):
            queue = ExecutionQueue.BROWSER if host in JS_ONLY and n >= 2 else ExecutionQueue.HTTP
            frontier.admit(Admission(UrlRef.of(page_url(host, n, s.run)), queue=queue))
    found: list[Any] = []
    while True:
        clock[0] += 1.0
        claims = [
            c for q in (ExecutionQueue.HTTP, ExecutionQueue.BROWSER) if (c := frontier.claim(q))
        ]
        claims += [
            c for q in (ExecutionQueue.HTTP, ExecutionQueue.BROWSER) if (c := frontier.claim(q))
        ]
        if not claims and frontier.stats().active_tasks == 0:
            break
        for c in claims:
            cap = (
                FetchCapability.BROWSER
                if c.queue is ExecutionQueue.BROWSER
                else FetchCapability.HTTP
            )
            found += s.fetch(c.url, cap, T1)
            frontier.complete(c)
    if matches:
        s.publish(found, Component.MATCHER, T1 + timedelta(minutes=30))
        consume(s)
        consume(s)  # a second consumer pass (redelivery-safe): nothing changes
    return len(found)


def cycle_two(s: Stack, prioritizer: FeedbackPrioritizer | None, now: datetime) -> dict[str, Any]:
    """Offer 8 new pages per host through the real Admitter; spend a fixed claim budget."""
    clock = [2_000_000.0]
    frontier = s.frontier(clock)
    admitter = Admitter(
        frontier=frontier,
        discovery=s.storage.discovery,
        holder=_holder([]),
        settings=DiscoverySettings(),
        decision_ttl_s=3600,
        clock=lambda: now,
        prioritizer=prioritizer,
    )
    rng = random.Random(SEED)  # noqa: S311 -- a fixed shuffle, not a secret
    offered = [(h, n) for h in HOSTS for n in range(10, 18)]
    rng.shuffle(offered)
    candidates = [
        Candidate(UrlRef.of(page_url(h, n, s.run)), Origin.LINK, source_url=f"https://{h}.test/")
        for h, n in offered
    ]
    admitter.admit(candidates, revisit_after_s=0)
    claimed: list[tuple[str, str]] = []
    matching = 0
    for _ in range(TICKS):
        clock[0] += 1.0
        for first, second in _SLOTS[:WORKERS]:
            c = frontier.claim(first) or frontier.claim(second)
            if c is None:
                continue
            cap = (
                FetchCapability.BROWSER
                if c.queue is ExecutionQueue.BROWSER
                else FetchCapability.HTTP
            )
            host = c.url.split("//")[1].split(".")[0]
            n = int(c.url.rstrip("/").rsplit("-", 1)[1])
            matching += carries(host, cap, s.run, n)[1] is not None
            claimed.append((host, c.queue.value))
            frontier.complete(c)
    per_host: dict[str, int] = {}
    for host, _ in claimed:
        per_host[host] = per_host.get(host, 0) + 1
    return {
        "claims": len(claimed),
        "matching_media": matching,
        "per_host": per_host,
        "claimed": claimed,
    }


def _prioritizer(s: Stack, now: datetime) -> FeedbackPrioritizer:
    store = ScyllaFeedbackStore(s.storage.session)
    p = FeedbackPrioritizer(
        lambda: load_feedback_snapshot(store, s.storage.projections, now=now),
        max_snapshot_age=timedelta(minutes=15),
        refresh_s=60,
    )
    assert p.refresh()
    return p


def test_m4_replay_feedback_shifts_the_next_cycle_toward_productive_sources(stack: Stack) -> None:
    s = stack
    assert cycle_one(s) == 6  # alpha: its 2 browser pages (http: none); beta: 4 http pages

    p = _prioritizer(s, T2)
    snap = p.snapshot
    assert snap is not None
    alpha, beta = (str(UrlRef.of(page_url(h, 0, s.run)).domain_id) for h in ("alpha", "beta"))
    assert set(snap.evidence) == {alpha, beta}  # no evidence for the duds or for http-on-alpha
    assert {e.capability for e in snap.evidence[alpha]} == {"browser"}
    assert {e.capability for e in snap.evidence[beta]} == {"http"}
    effective = {
        h: p.prioritize(UrlRef.of(page_url(h, 99, s.run)), 50, T2)
        for h in ("alpha", "beta", "dud1")
    }
    assert effective["dud1"].priority == 50
    assert effective["alpha"].priority == effective["beta"].priority == 50 + 6 + 2
    assert effective["alpha"].priority - 50 <= 20

    # Derived state is rebuildable: drop it, replay the match projections, compare.
    feedback = MatchFeedback(
        media=s.storage.media,
        pages=s.storage.pages,
        projections=s.storage.projections,
        store=ScyllaFeedbackStore(s.storage.session),
    )
    report = rebuild(
        feedback, ScyllaFeedbackStore(s.storage.session), s.storage.projections, now=T2
    )
    assert report == {"dropped_sources": 2, "applied": 6, "unattributed": 0}
    rebuilt = _prioritizer(s, T2).snapshot
    assert rebuilt is not None
    assert {d: set(rows) for d, rows in rebuilt.evidence.items()} == {
        d: set(rows) for d, rows in snap.evidence.items()
    }

    off = cycle_two(s, None, T2)
    on = cycle_two(s, p, T2)
    assert off["claims"] == on["claims"] == WORKERS * TICKS  # same budget
    assert on["matching_media"] > off["matching_media"]
    assert set(on["per_host"]) >= {"alpha", "beta"}
    assert sum(v for h, v in on["per_host"].items() if h.startswith("dud")) <= 2

    # Decay: the same evidence, evaluated later, without new matches.
    later = {
        days: _prioritizer(s, T2 + timedelta(days=days))
        .prioritize(UrlRef.of(page_url("alpha", 99, s.run)), 50, T2 + timedelta(days=days))
        .priority
        for days in (0, 14, 30, 60, 91)
    }
    decayed = cycle_two(s, _prioritizer(s, T2 + timedelta(days=91)), T2 + timedelta(days=91))
    assert list(later.values()) == sorted(later.values(), reverse=True)
    assert later[91] == 50
    assert decayed["claimed"] == off["claimed"]  # back to neutral: identical to feedback off

    summary: dict[str, Any] = {
        "cycle1_matches": 6,
        "effective_priority": {h: d.priority for h, d in effective.items()},
        "off": {k: off[k] for k in ("claims", "matching_media", "per_host")},
        "on": {k: on[k] for k in ("claims", "matching_media", "per_host")},
        "on_queues": sorted({q for _, q in on["claimed"]}),
        "alpha_priority_by_age_days": later,
        "decayed_91d": {k: decayed[k] for k in ("claims", "matching_media")},
    }
    print("P11-M4-REPLAY " + json.dumps(summary, sort_keys=True))


def test_no_match_control_gives_no_positive_feedback(stack: Stack) -> None:
    s = stack
    cycle_one(s, matches=False)
    consume(s)  # targets only; no match.found was published
    p = _prioritizer(s, T2)
    assert p.snapshot is not None
    assert dict(p.snapshot.evidence) == {}
    off = cycle_two(s, None, T2)
    on = cycle_two(s, p, T2)
    assert off["claims"] == on["claims"] == WORKERS * TICKS
    assert on["claimed"] == off["claimed"]  # identical allocation: no artificial boost
    print(
        "P11-M4-CONTROL " + json.dumps({"off": off["matching_media"], "on": on["matching_media"]})
    )
