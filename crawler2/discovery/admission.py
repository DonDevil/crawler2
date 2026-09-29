"""Discovery admission: filter → scope → revisit gate → P3 admission (design §9, §10).

Every URL that reaches the frontier from P6 (a discovered link, a seed, a
search result) goes through ``Admitter.admit``: one filter decision per
URL, the static M1 scope and revisit rules (§22 Q2/Q3), then
``Frontier.admit_many``. Decisions are recorded as data (F5/F6), bounded
by "write on admission or when the decision changes".

Nothing here is intelligence: priorities are static per provenance and the
revisit interval is uniform. P7 replaces both by producing admissions.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Protocol

from antipiracy_contracts.events import EventEnvelope
from antipiracy_contracts.events.web import UrlsDiscovered
from antipiracy_contracts.ids import ObservationId, UrlId
from antipiracy_contracts.models.web import LinkRelation, UrlRef

from crawler2.core.configuration import DiscoverySettings, ExecutionQueue
from crawler2.core.observability import Metrics, get_logger
from crawler2.filtering.inputs import host_of, link_input, redirect_inputs, registrable_domain
from crawler2.filtering.model import Action, Decision
from crawler2.filtering.store import RulesetHolder
from crawler2.frontier.model import Admission, AdmitOutcome, AdmitResult
from crawler2.storage.repositories import (
    AdmittedUrl,
    DiscoveryRepository,
    FilterDecisionRow,
    PageObservationRepository,
    RedirectDecisionRow,
    ScopeSite,
    UrlAdmissionState,
)

_log = get_logger("discovery.admission")

REASON: Final = {"seed": "seed", "search": "search", "link": "discovered", "leaf": "discovered"}
_NEW: Final = frozenset({AdmitOutcome.READY, AdmitOutcome.SCHEDULED})


class Origin(StrEnum):
    SEED = "seed"
    SEARCH = "search"
    LINK = "link"
    """An in-scope link: the target is on a rooted site."""
    LEAF = "leaf"
    """An external link from a rooted site: fetched once, its own links not followed."""


class Outcome(StrEnum):
    ADMITTED = "admitted"
    MERGED = "merged"
    BLOCKED = "blocked"
    OUT_OF_SCOPE = "out_of_scope"
    RECENT = "recent"
    REJECTED_FULL = "rejected_full"
    INVALID = "invalid"
    REDIRECT_BLOCKED = "redirect_blocked"


class AdmissionSink(Protocol):
    """The part of the P3 frontier P6 uses."""

    def admit_many(self, admissions: Iterable[Admission]) -> list[AdmitResult]: ...


@dataclass(frozen=True, slots=True)
class Candidate:
    """One URL offered for admission, with its provenance."""

    url: UrlRef
    origin: Origin
    relation: LinkRelation = LinkRelation.ANCHOR
    source_url: str | None = None
    """The linking page (first party); None for seeds and search results."""


class Scope:
    """Rooted registrable domains (F11), cached and refreshed periodically."""

    def __init__(
        self,
        repo: DiscoveryRepository,
        *,
        refresh_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repo = repo
        self._refresh_s = refresh_s
        self._clock = clock
        self._sites: set[str] = set()
        self._next = 0.0

    def rooted(self, host: str) -> bool:
        now = self._clock()
        if now >= self._next:
            self._sites = {s.domain for s in self._repo.scope_sites()}
            self._next = now + self._refresh_s
        return registrable_domain(host) in self._sites

    def add(self, hosts: Iterable[str], *, origin: str, at: datetime, detail: str = "") -> None:
        new = sorted({registrable_domain(h) for h in hosts} - self._sites)
        if not new:
            return
        self._repo.add_scope_sites([ScopeSite(d, origin, at, detail) for d in new])
        self._sites.update(new)


class Admitter:
    def __init__(
        self,
        *,
        frontier: AdmissionSink,
        discovery: DiscoveryRepository,
        holder: RulesetHolder,
        settings: DiscoverySettings,
        decision_ttl_s: int,
        metrics: Metrics | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._frontier = frontier
        self._repo = discovery
        self._holder = holder
        self._s = settings
        self._ttl = decision_ttl_s
        self._clock = clock
        self._priority = {
            Origin.SEED: settings.priority_seed,
            Origin.SEARCH: settings.priority_search,
            Origin.LINK: settings.priority_link,
            Origin.LEAF: settings.priority_leaf,
        }
        self._outcomes = None
        self._decisions = None
        if metrics is not None:
            self._outcomes = metrics.counter(
                "discovery_admissions_total",
                "URLs offered for admission, by provenance and outcome",
                ["origin", "outcome"],
            )
            self._decisions = metrics.counter(
                "filter_decisions_total",
                "Filter decisions by context, classification, action and rule source",
                ["context", "classification", "action", "source"],
            )

    def admit(
        self,
        candidates: Sequence[Candidate],
        *,
        observation_id: ObservationId | None = None,
        revisit_after_s: float | None = None,
    ) -> dict[UrlId, Outcome]:
        """Decide and admit; the outcome per URL. Deterministic order (by url_id)."""
        now = self._clock()
        revisit = self._s.revisit_after_s if revisit_after_s is None else revisit_after_s
        self._holder.maybe_refresh()
        engine = self._holder.engine
        unique = {c.url.url_id: c for c in sorted(candidates, key=lambda c: str(c.url.url_id))}
        states = self._repo.admission_states(list(unique))
        outcomes: dict[UrlId, Outcome] = {}
        rows: list[FilterDecisionRow] = []
        admissions: list[tuple[Candidate, Decision, Admission]] = []
        for url_id, cand in unique.items():
            if cand.origin is Origin.LEAF and cand.source_url is None:
                raise ValueError("a leaf candidate needs its source page")
            inp = link_input(cand.url.url, source_url=cand.source_url, relation=cand.relation)
            if inp is None:
                self._set(outcomes, cand, Outcome.INVALID)
                continue
            decision = engine.decide(inp)
            self._count_decision("link", decision)
            state = states.get(url_id)
            if decision.action is Action.BLOCK:
                self._set(outcomes, cand, Outcome.BLOCKED)
                if _changed(state, decision):
                    rows.append(self._row(cand, decision, Outcome.BLOCKED, now, observation_id))
                continue
            last = state.last_admitted_at if state else None
            if last is not None and (now - last).total_seconds() < revisit:
                self._set(outcomes, cand, Outcome.RECENT)
                continue
            admissions.append(
                (
                    cand,
                    decision,
                    Admission(
                        url=cand.url,
                        queue=ExecutionQueue.TOR
                        if inp.host.endswith(".onion")
                        else ExecutionQueue.HTTP,
                        priority=self._priority[cand.origin],
                        reason=REASON[cand.origin.value],
                    ),
                )
            )
        results = self._frontier.admit_many(a for _, _, a in admissions) if admissions else []
        admitted: list[AdmittedUrl] = []
        for (cand, decision, admission), result in zip(admissions, results, strict=True):
            if not result.accepted:
                self._set(outcomes, cand, Outcome.REJECTED_FULL)
                continue
            outcome = Outcome.ADMITTED if result.outcome in _NEW else Outcome.MERGED
            self._set(outcomes, cand, outcome)
            admitted.append(
                AdmittedUrl(
                    cand.url, now, cand.origin.value, admission.priority, admission.queue.value
                )
            )
            rows.append(self._row(cand, decision, outcome, now, observation_id))
        if rows:
            self._repo.record_decisions(rows, ttl_s=self._ttl)
        if admitted:
            self._repo.mark_admitted(admitted)
        return outcomes

    def _row(
        self,
        cand: Candidate,
        decision: Decision,
        outcome: Outcome,
        now: datetime,
        observation_id: ObservationId | None,
    ) -> FilterDecisionRow:
        doc = decision.to_doc() | {"origin": cand.origin.value}
        return FilterDecisionRow(
            url_id=cand.url.url_id,
            url=cand.url.url,
            decided_at=now,
            context="link",
            outcome=outcome.value,
            decision=json.dumps(doc, sort_keys=True),
            action=decision.action.value,
            rule_id=decision.rule_id,
            ruleset=decision.ruleset,
            source_observation_id=observation_id,
        )

    def _set(self, outcomes: dict[UrlId, Outcome], cand: Candidate, outcome: Outcome) -> None:
        outcomes[cand.url.url_id] = outcome
        if self._outcomes is not None:
            self._outcomes.labels(cand.origin.value, outcome.value).inc()

    def _count_decision(self, context: str, d: Decision) -> None:
        if self._decisions is not None:
            source = d.rule_source.split("@", 1)[0]
            self._decisions.labels(context, d.classification.value, d.action.value, source).inc()


def _changed(state: UrlAdmissionState | None, decision: Decision) -> bool:
    return state is None or (state.last_action, state.last_rule_id, state.last_ruleset) != (
        decision.action.value,
        decision.rule_id,
        decision.ruleset,
    )


class LinkAdmissionService:
    """``urls.discovered`` handler: redirect check, scope, then ``Admitter`` (design §10)."""

    def __init__(
        self,
        admitter: Admitter,
        scope: Scope,
        pages: PageObservationRepository,
        discovery: DiscoveryRepository,
        holder: RulesetHolder,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        metrics: Metrics | None = None,
    ) -> None:
        self._admitter = admitter
        self._scope = scope
        self._pages = pages
        self._repo = discovery
        self._holder = holder
        self._clock = clock
        self._links = None
        if metrics is not None:
            self._links = metrics.counter(
                "discovery_link_events_total", "urls.discovered events by result", ["result"]
            )

    def handle(self, envelope: EventEnvelope[UrlsDiscovered]) -> None:
        payload = envelope.payload
        if self._redirect_blocked(payload):
            self._event("redirect_blocked")
            return
        source = payload.page
        if not self._scope.rooted(host_of(source.url) or ""):
            # A leaf page: its links stay facts (P5) but are not expanded (§22 Q2).
            self._event("leaf_page")
            return
        candidates = []
        for link in payload.links:
            rooted = self._scope.rooted(host_of(link.target.url) or "")
            origin = Origin.LINK if rooted else Origin.LEAF
            candidates.append(
                Candidate(link.target, origin, relation=link.relation, source_url=source.url)
            )
        self._admitter.admit(candidates, observation_id=payload.page_observation_id)
        self._event("admitted")

    def _redirect_blocked(self, payload: UrlsDiscovered) -> bool:
        observation = self._pages.get(payload.page_observation_id)
        if observation is None or not observation.redirects:
            return False
        engine = self._holder.engine
        inputs = redirect_inputs(
            observation.requested.url,
            [hop.location.url for hop in observation.redirects],
            observation.final.url,
        )
        decisions = [engine.decide(inp) for inp in inputs]
        blocked = any(d.action is Action.BLOCK for d in decisions)
        self._repo.record_redirect(
            RedirectDecisionRow(
                observation_id=payload.page_observation_id,
                action=Action.BLOCK.value if blocked else Action.ALLOW.value,
                decisions=json.dumps([d.to_doc() for d in decisions], sort_keys=True),
                ruleset=engine.ruleset_id,
                decided_at=self._clock(),
            )
        )
        return blocked

    def _event(self, result: str) -> None:
        if self._links is not None:
            self._links.labels(result).inc()
