"""P11 match feedback: attribution, bounds, decay, stability, fallback, admission."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from antipiracy_contracts.events import encode_event
from antipiracy_contracts.events.fingerprinting import MatchFound
from antipiracy_contracts.events.targets import TargetRegistered, TargetRetired
from antipiracy_contracts.models.matching import MatchResult
from antipiracy_contracts.models.web import FetchCapability, UrlRef
from antipiracy_contracts.ownership import Component

from crawler2.core.configuration import DiscoverySettings, ExecutionQueue, FeedbackSettings
from crawler2.discovery.admission import Admitter, Candidate, Origin
from crawler2.intelligence.feedback import (
    DEFAULT_FEEDBACK_POLICY,
    MatchEvidence,
    PriorityDecision,
    TargetRelevance,
    assess_source,
    decide,
    page_shape,
    preferred_capability,
)
from crawler2.intelligence.feedback_service import (
    FeedbackLoop,
    FeedbackPrioritizer,
    FeedbackSnapshot,
    MatchFeedback,
    TargetFeedback,
    feedback_policy,
    load_feedback_snapshot,
    rebuild,
)
from crawler2.storage.errors import StorageError
from crawler2.storage.events.consumer import ConsumeOutcome, IdempotentConsumer, StreamEntry
from crawler2.storage.repositories import ScopeSite
from tests.fixtures.contracts import event_of, match, target
from tests.unit.discovery.test_admission import FakeFrontier, _holder
from tests.unit.filtering.fakes import MemoryDiscovery
from tests.unit.intelligence.feedback_fakes import (
    MemoryFeedbackStore,
    MemoryProcessed,
    Projections,
    Registry,
)

T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)
P = DEFAULT_FEEDBACK_POLICY


def dom(host: str) -> str:
    return str(UrlRef.of(f"https://{host}/").domain_id)


def ev(
    host: str,
    target_id: str,
    *,
    at: datetime = T0,
    page: str = "p1",
    path: str | None = None,
    conf: float = 1.0,
    verdict: str = "confirmed",
    cap: str | None = "http",
    match_id: str | None = None,
) -> MatchEvidence:
    url = f"https://{host}/{path or 'movie/2024/' + page}"
    template, kind = page_shape(url)
    return MatchEvidence(
        domain_id=dom(host),
        match_id=match_id or f"m-{host}-{target_id}-{page}",
        page_observation_id=f"o-{host}-{page}",
        target_id=target_id,
        target_version=1,
        decided_at=at,
        confidence=conf,
        verdict=verdict,
        page_url=url,
        template=template,
        page_kind=kind,
        capability=cap,
    )


ACTIVE = {f"t{i}": TargetRelevance.ACTIVE for i in range(2000)}


def assess(rows: list[MatchEvidence], now: datetime = T0, targets=ACTIVE):  # type: ignore[no-untyped-def]
    return assess_source(rows[0].domain_id, rows, targets, now)


# --- scoring: bounds, distinct targets, decay, relevance ------------------------------


def test_one_target_many_observations_counts_once() -> None:
    one = assess([ev("a.test", "t0")])
    many = assess([ev("a.test", "t0", page=f"p{i}") for i in range(200)])
    assert one.value == pytest.approx(many.value) == pytest.approx(1.0)
    assert one.boost == many.boost == 6  # round(15 * (1 - e^-0.5))


def test_boost_is_bounded_whatever_the_evidence() -> None:
    rows = [ev("a.test", f"t{i}", page=f"p{i % 7}") for i in range(2000)]
    s = assess(rows)
    assert s.boost == P.max_source_boost
    for base in (0, 50, 95, 100):
        d = decide(base=base, url="https://a.test/movie/2024/new", url_id="u", source=s)
        assert d.priority - base <= P.max_total_boost
        assert 0 <= d.priority <= 100
        assert d.source_boost + d.pattern_boost == d.priority - base or d.priority == 100


def test_nan_and_out_of_range_confidence_add_nothing_beyond_bounds() -> None:
    s = assess([ev("a.test", "t0", conf=math.nan), ev("a.test", "t1", conf=7.0)])
    assert math.isfinite(s.value)
    assert s.value == pytest.approx(1.0)


def test_decay_moves_an_old_productive_source_to_neutral() -> None:
    rows = [ev("a.test", f"t{i}") for i in range(3)]
    boosts = [assess(rows, T0 + timedelta(days=d)).boost for d in (0, 14, 28, 60, 91)]
    assert boosts == sorted(boosts, reverse=True)
    assert boosts[0] == 12  # past max_evidence_age: neutral
    assert boosts[-1] == 0


def test_target_relevance_weights_within_bounds() -> None:
    rows = [ev("a.test", "tx")]
    active = assess(rows, targets={"tx": TargetRelevance.ACTIVE})
    unknown = assess(rows, targets={})
    retired = assess(rows, targets={"tx": TargetRelevance.RETIRED})
    assert active.boost > unknown.boost > retired.boost >= 0
    assert active.boost <= P.max_source_boost


def test_probable_matches_count_less_than_confirmed() -> None:
    assert assess([ev("a.test", "t0", verdict="probable")]).value == pytest.approx(0.5)


# --- patterns and capability --------------------------------------------------------


def test_pattern_needs_support_and_applies_to_its_template_only() -> None:
    single = assess([ev("a.test", "t0", page="x")])
    assert single.patterns == {}  # one page is noise
    s = assess([ev("a.test", "t0", page="x"), ev("a.test", "t1", page="y")])
    assert s.patterns == {"/{slug}/{year}/{slug}": 3}  # WHAT: "movie" is not a literal
    detail = decide(base=50, url="https://a.test/movie/2025/other", url_id="u", source=s)
    listing = decide(base=50, url="https://a.test/category/action/", url_id="u", source=s)
    assert detail.pattern_boost == 3
    assert listing.pattern_boost == 0
    assert detail.priority == listing.priority + 3


def test_browser_preferred_only_when_only_browser_reached_media() -> None:
    browser_only = assess([ev("a.test", f"t{i}", page=f"p{i}", cap="browser") for i in range(3)])
    picks = {preferred_capability(browser_only, f"u{i}")[0] for i in range(200)}
    assert picks == {"browser", None}  # mostly browser, a share kept on http (exploration)
    share = sum(preferred_capability(browser_only, f"u{i}")[0] is None for i in range(1000))
    assert 100 <= share <= 300  # ~20 %
    mixed = assess(
        [ev("a.test", "t0", page="p0", cap="browser"), ev("a.test", "t1", page="p1", cap="http")]
    )
    assert preferred_capability(mixed, "u1")[0] is None  # cheap capability works: keep it
    one = assess([ev("a.test", "t0", page="p0", cap="browser", conf=0.5)])
    assert preferred_capability(one, "u1")[0] is None  # below the evidence floor


def test_browser_preference_fades_with_decay() -> None:
    rows = [ev("a.test", f"t{i}", page=f"p{i}", cap="browser") for i in range(2)]
    assert assess(rows).capability_weight["browser"] == pytest.approx(2.0)
    late = assess(rows, T0 + timedelta(days=14))
    assert preferred_capability(late, "zz")[0] is None  # 2 e^-1 < 1: retest cheaper path


# --- stability ----------------------------------------------------------------------


def test_rankings_do_not_change_without_new_evidence() -> None:
    sources = {
        "a.test": [ev("a.test", "t0", at=T0 - timedelta(days=2)), ev("a.test", "t1")],
        "b.test": [ev("b.test", "t0", at=T0 - timedelta(days=9), conf=0.9)],
        "c.test": [ev("c.test", f"t{i}", at=T0 - timedelta(days=20)) for i in range(4)],
        "d.test": [ev("d.test", "t3", verdict="probable")],
    }

    def ranking(now: datetime) -> list[tuple[str, float, int]]:
        scored = [(h, assess(rows, now)) for h, rows in sources.items()]
        ordered = sorted(scored, key=lambda x: (-x[1].value, x[0]))
        return [(h, s.value, s.boost) for h, s in ordered]

    first = [h for h, _, _ in ranking(T0)]
    previous = {h: (v, b) for h, v, b in ranking(T0)}
    expired: set[str] = set()
    for hours in range(0, 24 * 120, 6):  # evaluate every 6 h for 120 days
        now = T0 + timedelta(hours=hours)
        r = ranking(now)
        live = [h for h, v, _ in r if v > 0]
        assert live == [h for h in first if h in live]  # order among live hosts never flips
        for h, v, b in r:
            assert v <= previous[h][0]  # nothing grows without evidence
            assert b <= previous[h][1]
            if h in expired:
                assert (v, b) == (0.0, 0)  # past max age: neutral, and stays neutral
            if v == 0:
                expired.add(h)
        previous = {h: (v, b) for h, v, b in r}
        assert ranking(now) == r  # re-evaluation is a pure function
    assert expired == set(sources)


def test_cold_sources_keep_base_and_feedback_never_lowers() -> None:
    d = decide(base=50, url="https://cold.test/x", url_id="u", source=None)
    assert (d.priority, d.fallback) == (50, None)
    weak = assess([ev("a.test", "t0", at=T0 - timedelta(days=80))])
    assert decide(base=50, url="https://a.test/x", url_id="u", source=weak).priority >= 50


# --- attribution & consumption --------------------------------------------------------


def _world() -> tuple[Registry, Projections, MemoryFeedbackStore, MatchFeedback]:
    registry, projections, store = Registry(), Projections(), MemoryFeedbackStore()
    fb = MatchFeedback(media=registry, pages=registry, projections=projections, store=store)
    return registry, projections, store, fb


def _match(content, tgt, at: datetime = T0, **update: object) -> MatchResult:  # type: ignore[no-untyped-def]
    return match(content, tgt.ref, at=at).model_copy(update=update)


def test_match_is_attributed_to_the_pages_that_carried_the_media() -> None:
    registry, projections, store, fb = _world()
    t = target("alpha")
    page, _, content = registry.add(
        "https://pirate.test/movie/2024/alpha-hd",
        "v/alpha.mp4",
        b"alpha",
        at=T0 - timedelta(days=1),
        capability=FetchCapability.BROWSER,
        cdn="cdn.other",
    )
    registry.add("https://unrelated.test/movie/2024/x", "v/x.mp4", b"other", at=T0)
    m = _match(content, t)
    assert fb.apply(m) == "applied"
    (row,) = store.rows.values()
    assert row.domain_id == dom("pirate.test")  # the page host, not the CDN host
    assert row.page_observation_id == str(page.observation_id)
    assert (row.template, row.page_kind, row.capability) == (
        "/{slug}/{year}/{slug}",
        "detail",
        "browser",
    )
    assert (row.target_id, row.confidence, row.verdict) == (
        str(t.ref.target_id),
        m.confidence,
        "confirmed",
    )
    assert projections.matches[str(m.match_id)][1] == {dom("pirate.test")}


def test_unattributable_match_gives_no_feedback() -> None:
    registry, projections, store, fb = _world()
    _, _, content = registry.add("https://a.test/x", "v/a.mp4", b"a", at=T0)
    registry.observations[0] = registry.observations[0].model_copy(
        update={"content": None}
    )  # media seen but never hashed
    assert fb.apply(_match(content, target("t"))) == "unattributed"
    assert store.rows == {}
    assert len(projections.matches) == 1  # the match itself is still recorded


def test_redelivered_match_is_counted_once_and_changes_nothing() -> None:
    registry, _, store, fb = _world()
    _, _, content = registry.add("https://a.test/movie/2024/a", "v/a.mp4", b"a", at=T0)
    m = _match(content, target("t"))
    processed = MemoryProcessed()
    consumer = IdempotentConsumer("crawl-feedback", MatchFound, fb.handle, processed)
    first = event_of(MatchFound(match=m), Component.MATCHER)
    retry = event_of(MatchFound(match=m), Component.MATCHER)  # producer retry: new event id
    assert consumer.handle(encode_event(first)) is ConsumeOutcome.APPLIED
    snapshot_1 = dict(store.rows)
    assert consumer.handle(encode_event(first)) is ConsumeOutcome.DUPLICATE
    assert consumer.handle(encode_event(retry)) is ConsumeOutcome.DUPLICATE
    assert store.rows == snapshot_1
    assert store.writes == 1
    # Even without the marker (crash before mark), re-applying writes identical rows.
    fb.apply(m)
    assert store.rows == snapshot_1
    s1 = assess(list(snapshot_1.values()))
    assert s1 == assess(list(store.rows.values()))


def test_storage_outage_is_never_a_successful_update() -> None:
    registry, _, store, fb = _world()
    _, _, content = registry.add("https://a.test/x", "v/a.mp4", b"a", at=T0)
    processed = MemoryProcessed()
    consumer = IdempotentConsumer("crawl-feedback", MatchFound, fb.handle, processed)
    wire = encode_event(event_of(MatchFound(match=_match(content, target("t"))), Component.MATCHER))
    store.fail = True
    with pytest.raises(StorageError):
        consumer.handle(wire)
    assert processed.keys == set()  # not marked: it will be redelivered
    registry.fail, store.fail = True, False
    with pytest.raises(StorageError):
        consumer.handle(wire)
    registry.fail = False
    assert consumer.handle(wire) is ConsumeOutcome.APPLIED


class FakeReader:
    def __init__(self, entries: list[StreamEntry]) -> None:
        self.entries = entries
        self.acked: list[str] = []

    def claim_stale(self, *, min_idle_ms: int, count: int = 100) -> list[StreamEntry]:
        return []

    def read(self, *, count: int = 100, block_ms: int | None = None) -> list[StreamEntry]:
        out, self.entries = self.entries[:count], self.entries[count:]
        return out

    def ack(self, entries: list[StreamEntry]) -> None:
        self.acked += [e.entry_id for e in entries]


def test_loop_acks_malformed_events_and_leaves_storage_failures_pending() -> None:
    registry, _, store, fb = _world()
    _, _, content = registry.add("https://a.test/x", "v/a.mp4", b"a", at=T0)
    good = encode_event(event_of(MatchFound(match=_match(content, target("t"))), Component.MATCHER))
    entries = [
        StreamEntry("1-0", b"{not json"),
        StreamEntry("2-0", good.encode() if isinstance(good, str) else good),
        StreamEntry("3-0", b'{"event_type": "match.found"}'),
    ]
    reader = FakeReader(entries)
    consumer = IdempotentConsumer("crawl-feedback", MatchFound, fb.handle, MemoryProcessed())
    loop = FeedbackLoop([(reader, consumer)], batch_size=10, block_ms=1, claim_idle_ms=1)  # type: ignore[list-item]
    store.fail = True
    assert loop.run_once() == 1
    assert reader.acked == ["1-0"]  # stopped at the outage: 2-0 and later stay pending
    store.fail = False
    reader.entries = entries[1:]
    assert loop.run_once() == 2
    assert reader.acked == ["1-0", "2-0", "3-0"]
    assert len(store.rows) == 1


def test_target_events_drive_relevance_in_the_snapshot() -> None:
    _, projections, store, _ = _world()
    targets = TargetFeedback(projections)
    a, b = target("a"), target("b")
    targets.registered(event_of(TargetRegistered(target=a), Component.TARGET_MANAGER))
    targets.registered(event_of(TargetRegistered(target=b), Component.TARGET_MANAGER))
    targets.retired(
        event_of(TargetRetired(target_id=b.ref.target_id, retired_at=T0), Component.TARGET_MANAGER)
    )
    snap = load_feedback_snapshot(store, projections, now=T0)
    assert snap.targets == {
        str(a.ref.target_id): TargetRelevance.ACTIVE,
        str(b.ref.target_id): TargetRelevance.RETIRED,
    }


def test_rebuild_reproduces_evidence_from_match_projections() -> None:
    registry, projections, store, fb = _world()
    t = target("r")
    projections.apply_target_registered(t)
    for i in range(3):
        _, _, c = registry.add(f"https://a.test/movie/2024/m{i}", f"v/{i}.mp4", b"%d" % i, at=T0)
        fb.apply(_match(c, t))
    before = dict(store.rows)
    report = rebuild(fb, store, projections, now=T0 + timedelta(days=1))
    assert report == {"dropped_sources": 1, "applied": 3, "unattributed": 0}
    assert store.rows == before


# --- admission integration (the real Admitter, a recording frontier) -------------------


def _snapshot(rows: list[MatchEvidence], at: datetime = T0) -> FeedbackSnapshot:
    by: dict[str, tuple[MatchEvidence, ...]] = {}
    for e in rows:
        by[e.domain_id] = (*by.get(e.domain_id, ()), e)
    return FeedbackSnapshot(by, ACTIVE, at)


def _admitter(prioritizer: object | None, frontier: FakeFrontier) -> Admitter:
    repo = MemoryDiscovery()
    for host in ("a.test", "b.test", "c.test"):
        repo.add_scope_sites([ScopeSite(host, "seed", T0)])
    return Admitter(
        frontier=frontier,
        discovery=repo,
        holder=_holder([]),
        settings=DiscoverySettings(),
        decision_ttl_s=3600,
        clock=lambda: T0,
        prioritizer=prioritizer,  # type: ignore[arg-type]
    )


def _prioritizer(load) -> FeedbackPrioritizer:  # type: ignore[no-untyped-def]
    return FeedbackPrioritizer(load, max_snapshot_age=timedelta(minutes=15), refresh_s=60)


def _links(*urls: str) -> list[Candidate]:
    return [Candidate(UrlRef.of(u), Origin.LINK, source_url="https://a.test/") for u in urls]


def test_admission_uses_the_effective_priority_and_capability() -> None:
    rows = [ev("a.test", f"t{i}", page=f"p{i}", cap="browser") for i in range(3)]
    p = _prioritizer(lambda: _snapshot(rows))
    assert p.refresh()
    frontier = FakeFrontier()
    urls = [f"https://a.test/movie/2025/n{i}" for i in range(20)] + ["https://b.test/movie/2025/x"]
    _admitter(p, frontier).admit(_links(*urls))
    got = {str(a.url.url): a for a in frontier.admitted}
    assert got["https://b.test/movie/2025/x"].priority == 50  # cold host: static priority
    boosted = [got[u] for u in urls[:-1]]
    assert {a.priority for a in boosted} == {50 + 12 + 4}  # source 12 + pattern 4
    queues = [a.queue for a in boosted]
    assert ExecutionQueue.BROWSER in queues  # exploration
    assert ExecutionQueue.HTTP in queues


def test_admission_falls_back_to_static_priorities() -> None:
    frontier = FakeFrontier()
    rows = [ev("a.test", "t0")]

    def broken() -> FeedbackSnapshot:
        raise StorageError("down")

    cases: list[object | None] = [None, _prioritizer(broken)]  # disabled; never loaded
    stale = _prioritizer(lambda: _snapshot(rows, at=T0 - timedelta(hours=2)))
    stale.refresh()
    cases.append(stale)

    class Raising:
        def prioritize(self, url: UrlRef, base: int, now: datetime) -> PriorityDecision:
            raise RuntimeError("bug")

    class Invalid:
        def prioritize(self, url: UrlRef, base: int, now: datetime) -> PriorityDecision:
            return PriorityDecision(250, base)

    cases += [Raising(), Invalid()]
    for i, case in enumerate(cases):
        if isinstance(case, FeedbackPrioritizer):
            case.refresh()
        url = f"https://a.test/movie/2025/f{i}"
        _admitter(case, frontier).admit(_links(url))
        assert frontier.admitted[-1].priority == 50, case
        assert frontier.admitted[-1].queue is ExecutionQueue.HTTP


def test_operator_bounds_come_from_settings() -> None:
    policy = feedback_policy(FeedbackSettings(max_source_boost=5, max_total_boost=8))
    assert (policy.max_source_boost, policy.max_total_boost) == (5, 8)
    assert policy.version == "feedback/v1+custom"
    assert feedback_policy(FeedbackSettings()) == replace(DEFAULT_FEEDBACK_POLICY)
    with pytest.raises(ValueError, match="boost bounds"):
        feedback_policy(FeedbackSettings(max_source_boost=30, max_total_boost=20))


def test_feedback_is_off_by_default() -> None:
    assert FeedbackSettings().enabled is False
