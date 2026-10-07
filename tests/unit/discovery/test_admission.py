"""Discovery admission: filter → scope → revisit gate → frontier (design §9, §10)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import pytest
from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events import EventEnvelope, new_event
from antipiracy_contracts.events.web import UrlsDiscovered
from antipiracy_contracts.ids import FetchAttemptId, ObservationId, PageVersionId
from antipiracy_contracts.models.web import (
    DiscoveredLink,
    FetchCapability,
    LinkRelation,
    PageObservation,
    RedirectHop,
    UrlRef,
)
from antipiracy_contracts.ownership import Component, Producer

from crawler2.core.configuration import DiscoverySettings, ExecutionQueue
from crawler2.discovery.admission import (
    Admitter,
    Candidate,
    LinkAdmissionService,
    Origin,
    Outcome,
    Scope,
)
from crawler2.filtering.model import Action, Classification, Policy, Rule, RuleKind, RuleSource
from crawler2.filtering.store import RulesetHolder, publish, store_source
from crawler2.frontier.model import Admission, AdmitOutcome, AdmitResult
from crawler2.intelligence.advice import SourceAdvice, fallback
from crawler2.storage.repositories import ScopeSite
from tests.unit.filtering.fakes import MemoryDiscovery, MemoryFilterRules
from tests.unit.filtering.test_store import _item

T0 = datetime(2026, 9, 29, 12, tzinfo=UTC)
PRODUCER = Producer(
    service=Component.EXTRACTION.service, component=Component.EXTRACTION, instance="test"
)


class FakeFrontier:
    def __init__(self, full: bool = False) -> None:
        self.admitted: list[Admission] = []
        self.active: set[str] = set()
        self.full = full

    def admit_many(self, admissions: Iterable[Admission]) -> list[AdmitResult]:
        results = []
        for a in admissions:
            if self.full:
                results.append(AdmitResult(AdmitOutcome.REJECTED_FULL))
            elif str(a.url.url_id) in self.active:
                results.append(AdmitResult(AdmitOutcome.DUPLICATE, "ready"))
            else:
                self.active.add(str(a.url.url_id))
                self.admitted.append(a)
                results.append(AdmitResult(AdmitOutcome.READY))
        return results


def _holder(rules: list[Rule]) -> RulesetHolder:
    repo = MemoryFilterRules()
    if rules:
        revision = store_source(repo, _item(rules), by="t", at=T0)
        record, _ = publish(repo, [("operator", revision.revision)], Policy(), by="t", at=T0)
        repo.activate(record.ruleset_id, expected=None, by="t", at=T0)
    holder = RulesetHolder(repo)
    holder.refresh()
    return holder


AD_RULE = Rule(
    source=RuleSource.OPERATOR,
    kind=RuleKind.HOST,
    pattern="ads.test",
    classification=Classification.AD,
    confidence=1.0,
    reason="ad_network",
    note="test",
)


class World:
    def __init__(self, rules: list[Rule] | None = None, *, full: bool = False) -> None:
        self.now = T0
        self.frontier = FakeFrontier(full)
        self.repo = MemoryDiscovery()
        self.holder = _holder([AD_RULE] if rules is None else rules)
        self.admitter = Admitter(
            frontier=self.frontier,
            discovery=self.repo,
            holder=self.holder,
            settings=DiscoverySettings(),
            decision_ttl_s=3600,
            clock=lambda: self.now,
        )
        self.scope = Scope(self.repo, refresh_s=0.0)
        self.repo.add_scope_sites([ScopeSite("site.test", "seed", T0)])


def ref(url: str) -> UrlRef:
    return UrlRef.of(url)


def test_blocked_links_are_recorded_once_and_not_admitted() -> None:
    w = World()
    cands = [
        Candidate(ref("https://ads.test/x"), Origin.LEAF, source_url="https://site.test/"),
        Candidate(ref("https://site.test/a"), Origin.LINK, source_url="https://site.test/"),
    ]
    outcomes = w.admitter.admit(cands)
    assert sorted(o.value for o in outcomes.values()) == ["admitted", "blocked"]
    assert [a.url.url for a in w.frontier.admitted] == ["https://site.test/a"]
    blocked = w.repo.decisions(ref("https://ads.test/x").url_id)
    assert [(d.outcome, d.action) for d in blocked] == [("blocked", "block")]
    w.now += timedelta(hours=1)
    w.admitter.admit(cands[:1])
    assert len(w.repo.decisions(ref("https://ads.test/x").url_id)) == 1  # unchanged: not rewritten


def test_admitted_links_record_why_they_were_allowed() -> None:
    w = World()
    w.admitter.admit(
        [Candidate(ref("https://site.test/a"), Origin.LINK, source_url="https://site.test/")]
    )
    (row,) = w.repo.decisions(ref("https://site.test/a").url_id)
    assert (row.outcome, row.action, row.rule_id, row.context) == (
        "admitted",
        Action.ALLOW.value,
        "default",
        "link",
    )
    assert '"origin": "link"' in row.decision


def test_revisit_gate_is_static_and_uniform() -> None:
    w = World()
    cand = Candidate(ref("https://site.test/a"), Origin.LINK, source_url="https://site.test/")
    assert w.admitter.admit([cand]) == {cand.url.url_id: Outcome.ADMITTED}
    w.frontier.active.clear()  # the task ended; the frontier forgot it (no visited set)
    w.now += timedelta(hours=23)
    assert w.admitter.admit([cand]) == {cand.url.url_id: Outcome.RECENT}
    w.now += timedelta(hours=1)
    assert w.admitter.admit([cand]) == {cand.url.url_id: Outcome.ADMITTED}
    assert len(w.frontier.admitted) == 2


def test_static_priorities_and_queues_by_provenance() -> None:
    w = World()
    w.admitter.admit(
        [
            Candidate(ref("https://seed.test/"), Origin.SEED),
            Candidate(ref("https://found.test/"), Origin.SEARCH),
            Candidate(ref("https://site.test/in"), Origin.LINK, source_url="https://site.test/"),
            Candidate(ref("https://ext.test/out"), Origin.LEAF, source_url="https://site.test/"),
            Candidate(ref("http://abcdefghij234567.onion/"), Origin.SEED),
        ]
    )
    got = {a.url.url: (a.priority, a.queue, a.reason) for a in w.frontier.admitted}
    assert got == {
        "https://seed.test/": (70, ExecutionQueue.HTTP, "seed"),
        "https://found.test/": (60, ExecutionQueue.HTTP, "search"),
        "https://site.test/in": (50, ExecutionQueue.HTTP, "discovered"),
        "https://ext.test/out": (40, ExecutionQueue.HTTP, "discovered"),
        "http://abcdefghij234567.onion/": (70, ExecutionQueue.TOR, "seed"),
    }


def test_full_frontier_is_not_recorded_as_admitted() -> None:
    w = World(full=True)
    cand = Candidate(ref("https://site.test/a"), Origin.LINK, source_url="https://site.test/")
    assert w.admitter.admit([cand]) == {cand.url.url_id: Outcome.REJECTED_FULL}
    assert w.repo.admission_states([cand.url.url_id]) == {}


def test_active_task_is_merged_not_duplicated() -> None:
    w = World()
    cand = Candidate(ref("https://site.test/a"), Origin.SEED)
    w.admitter.admit([cand])
    assert w.admitter.admit([cand], revisit_after_s=0) == {cand.url.url_id: Outcome.MERGED}
    assert len(w.frontier.admitted) == 1


def test_a_leaf_needs_a_source() -> None:
    w = World()
    with pytest.raises(ValueError, match="source page"):
        w.admitter.admit([Candidate(ref("https://x.test/"), Origin.LEAF)])


def _event(
    page: str, links: list[str], observation: ObservationId
) -> EventEnvelope[UrlsDiscovered]:
    payload = UrlsDiscovered(
        page_observation_id=observation,
        page=ref(page),
        page_version_id=PageVersionId.of(ref(page).url_id, ContentDigest.of_bytes(b"x")),
        links=tuple(DiscoveredLink(target=ref(u), relation=LinkRelation.ANCHOR) for u in links),
    )
    return new_event(payload, producer=PRODUCER, occurred_at=T0)


class Pages:
    def __init__(self) -> None:
        self.observations: dict[ObservationId, PageObservation] = {}

    def get(self, observation_id: ObservationId) -> PageObservation | None:
        return self.observations.get(observation_id)


def _service(w: World, pages: Pages) -> LinkAdmissionService:
    return LinkAdmissionService(
        w.admitter,
        w.scope,
        pages,  # type: ignore[arg-type]
        w.repo,
        w.holder,
        clock=lambda: w.now,
    )


def test_rooted_pages_follow_internal_links_and_fetch_external_ones_as_leaves() -> None:
    w, pages = World(), Pages()
    event = _event(
        "https://www.site.test/list",
        [
            "https://site.test/a",
            "https://cdn.site.test/b",
            "https://other.test/c",
            "https://ads.test/d",
        ],
        ObservationId.new(),
    )
    _service(w, pages).handle(event)
    got = {a.url.url: a.priority for a in w.frontier.admitted}
    assert got == {
        "https://site.test/a": 50,
        "https://cdn.site.test/b": 50,
        "https://other.test/c": 40,
    }


def test_leaf_pages_are_not_expanded() -> None:
    w, pages = World(), Pages()
    _service(w, pages).handle(
        _event("https://other.test/c", ["https://other.test/d"], ObservationId.new())
    )
    assert w.frontier.admitted == []


def test_a_blocked_redirect_stops_admission_of_the_pages_links() -> None:
    w, pages = World(), Pages()
    oid = ObservationId.new()
    requested, final = ref("https://site.test/go"), ref("https://ads.test/landing")
    pages.observations[oid] = PageObservation(
        observation_id=oid,
        fetch_attempt_id=FetchAttemptId.new(),
        requested=requested,
        final=final,
        redirects=(RedirectHop(location=final, status=302),),
        capability=FetchCapability.HTTP,
        observed_at=T0,
        http_status=200,
        body_digest=ContentDigest.of_bytes(b"x"),
        body_size=1,
        page_version_id=PageVersionId.of(final.url_id, ContentDigest.of_bytes(b"x")),
    )
    w.repo.add_scope_sites([ScopeSite("ads.test", "seed", T0)])
    _service(w, pages).handle(_event(final.url, ["https://ads.test/more"], oid))
    assert w.frontier.admitted == []
    row = w.repo.redirect(oid)
    assert row is not None
    assert row.action == "block"
    assert '"matched_field": "redirect_hop[0]"' in row.decisions


# --- P7 source advice (P7 design §9.4, increment 2) --------------------------------------


class FakeAdvisor:
    """Scripted P7 advice per host; records which hosts P6 asked about."""

    def __init__(self, advice: dict[str, SourceAdvice] | None = None, *, fail: bool = False):
        self.advice = advice or {}
        self.fail = fail
        self.asked: list[str] = []

    def advise(self, domain_id: str, now: datetime) -> SourceAdvice:
        self.asked.append(domain_id)
        if self.fail:
            raise RuntimeError("p7 bug")
        return self.advice.get(domain_id, fallback("unavailable"))


def _world_with(advisor: object) -> World:
    w = World()
    w.admitter = Admitter(
        frontier=w.frontier,
        discovery=w.repo,
        holder=w.holder,
        settings=DiscoverySettings(),
        decision_ttl_s=3600,
        clock=lambda: w.now,
        advisor=advisor,  # type: ignore[arg-type]
    )
    return w


MIXED = [
    Candidate(ref("https://seed.test/"), Origin.SEED),
    Candidate(ref("https://found.test/"), Origin.SEARCH),
    Candidate(ref("https://site.test/in"), Origin.LINK, source_url="https://site.test/"),
    Candidate(ref("https://ads.test/x"), Origin.LEAF, source_url="https://site.test/"),
    Candidate(ref("http://abcdefghij234567.onion/"), Origin.SEED),
]


def _run(w: World) -> tuple[dict[str, str], list[tuple[str, str, int]], list[str]]:
    outcomes = w.admitter.admit(MIXED)
    return (
        {str(k): v.value for k, v in outcomes.items()},
        [(a.url.url, a.queue.value, a.priority) for a in w.frontier.admitted],
        sorted(str(u) for u, st in w.repo.states.items() if st.last_admitted_at),
    )


def test_p7_fallback_is_exactly_admission_without_p7() -> None:
    baseline = _run(World())
    for advisor in (FakeAdvisor(), FakeAdvisor(fail=True)):
        assert _run(_world_with(advisor)) == baseline


def test_p6_decisions_come_before_p7_and_tor_is_not_asked() -> None:
    advisor = FakeAdvisor()
    w = _world_with(advisor)
    w.admitter.admit(MIXED)
    asked = set(advisor.asked)
    assert str(ref("https://ads.test/x").domain_id) not in asked  # blocked by the P6 filter
    assert str(ref("http://abcdefghij234567.onion/").domain_id) not in asked  # no Tor profiles
    assert str(ref("https://site.test/in").domain_id) in asked
    # Recent (P6 revisit gate): not asked again
    advisor.asked.clear()
    w.frontier.active.clear()
    w.admitter.admit(MIXED[2:3])
    assert advisor.asked == []


def test_p7_defers_a_dead_host_and_sets_the_starting_queue() -> None:
    dead_host, spa_host = ref("https://seed.test/"), ref("https://found.test/")
    advisor = FakeAdvisor(
        {
            str(dead_host.domain_id): SourceAdvice(False, "http", "unreachable", reason="probe at"),
            str(spa_host.domain_id): SourceAdvice(True, "browser", "active"),
        }
    )
    w = _world_with(advisor)
    outcomes = w.admitter.admit(MIXED)
    assert outcomes[dead_host.url_id] is Outcome.SOURCE_DEFERRED
    queues = {str(a.url.url): a.queue for a in w.frontier.admitted}
    assert dead_host.url not in queues
    assert queues[spa_host.url] is ExecutionQueue.BROWSER
    assert queues["https://site.test/in"] is ExecutionQueue.HTTP
    # A deferral is not an admission and not a filter decision: the URL can come back.
    assert dead_host.url_id not in w.repo.states
    assert w.repo.decisions(dead_host.url_id) == []
    # Priorities stay P6's (design §8).
    assert {str(a.url.url): a.priority for a in w.frontier.admitted}[spa_host.url] == 60


def test_p7_cannot_admit_what_p6_rejected() -> None:
    blocked = ref("https://ads.test/x")
    advisor = FakeAdvisor({str(blocked.domain_id): SourceAdvice(True, "browser", "active")})
    w = _world_with(advisor)
    outcomes = w.admitter.admit(MIXED)
    assert outcomes[blocked.url_id] is Outcome.BLOCKED
