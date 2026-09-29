"""In-memory P6 repositories with the Scylla repositories' semantics (for unit tests)."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import replace
from datetime import datetime

from antipiracy_contracts.ids import ObservationId, UrlId

from crawler2.storage.errors import StorageUnavailableError
from crawler2.storage.layout import day_bucket
from crawler2.storage.repositories import (
    ActiveRulesetPointer,
    AdmittedUrl,
    FilterDecisionRow,
    FilterRulesetRecord,
    FilterSourceRevision,
    InterceptionSummary,
    RedirectDecisionRow,
    ScopeSite,
    SearchResultRecord,
    SeedRecord,
    StoredRule,
    UrlAdmissionState,
)


class MemoryFilterRules:
    def __init__(self) -> None:
        self.sources_: dict[tuple[str, str], FilterSourceRevision] = {}
        self.rules_: dict[tuple[str, str], dict[str, StoredRule]] = {}
        self.rulesets: dict[str, FilterRulesetRecord] = {}
        self.pointer: dict[str, ActiveRulesetPointer] = {}
        self.fail_reads = False
        self.active_reads = 0

    def put_source(self, revision: FilterSourceRevision, rules: Sequence[StoredRule]) -> None:
        key = (revision.source, revision.revision)
        self.rules_.setdefault(key, {}).update({r.rule_id: r for r in rules})
        self.sources_[key] = revision

    def source(self, source: str, revision: str) -> FilterSourceRevision | None:
        return self.sources_.get((source, revision))

    def sources(self, source: str) -> list[FilterSourceRevision]:
        found = [r for (s, _), r in self.sources_.items() if s == source]
        return sorted(found, key=lambda r: (r.revision_at, r.revision), reverse=True)

    def rules(self, source: str, revision: str) -> list[StoredRule]:
        if self.fail_reads:
            raise StorageUnavailableError("scylla down")
        return sorted(self.rules_.get((source, revision), {}).values(), key=lambda r: r.rule_id)

    def put_ruleset(self, record: FilterRulesetRecord) -> None:
        self.rulesets[record.ruleset_id] = record

    def ruleset(self, ruleset_id: str) -> FilterRulesetRecord | None:
        return self.rulesets.get(ruleset_id)

    def active(self, name: str = "default") -> ActiveRulesetPointer | None:
        self.active_reads += 1
        if self.fail_reads:
            raise StorageUnavailableError("scylla down")
        return self.pointer.get(name)

    def activate(
        self, ruleset_id: str, *, expected: str | None, by: str, at: datetime, name: str = "default"
    ) -> bool:
        current = self.pointer.get(name)
        if (current.ruleset_id if current else None) != expected:
            return False
        self.pointer[name] = ActiveRulesetPointer(name, ruleset_id, expected, at, by)
        return True


class MemoryDiscovery:
    def __init__(self) -> None:
        self.states: dict[UrlId, UrlAdmissionState] = {}
        self.decision_rows: list[FilterDecisionRow] = []
        self.redirects: dict[ObservationId, RedirectDecisionRow] = {}
        self.intercepts: dict[ObservationId, InterceptionSummary] = {}
        self.seed_rows: dict[tuple[str, UrlId], SeedRecord] = {}
        self.search_rows: list[SearchResultRecord] = []
        self.sites: dict[str, ScopeSite] = {}

    def _state(self, url_id: UrlId) -> UrlAdmissionState:
        return self.states.get(url_id) or UrlAdmissionState(url_id, None, None, None, None, None)

    def admission_states(self, url_ids: Collection[UrlId]) -> dict[UrlId, UrlAdmissionState]:
        return {u: self.states[u] for u in url_ids if u in self.states}

    def mark_admitted(self, rows: Sequence[AdmittedUrl]) -> None:
        for a in rows:
            state = self._state(a.url.url_id)
            self.states[a.url.url_id] = replace(state, last_admitted_at=a.at, last_origin=a.origin)

    def record_decisions(self, rows: Sequence[FilterDecisionRow], *, ttl_s: int) -> None:
        assert ttl_s > 0
        for d in rows:
            self.decision_rows.append(d)
            state = self._state(d.url_id)
            self.states[d.url_id] = replace(
                state, last_action=d.action, last_rule_id=d.rule_id, last_ruleset=d.ruleset
            )

    def decisions(self, url_id: UrlId) -> list[FilterDecisionRow]:
        rows = [d for d in self.decision_rows if d.url_id == url_id]
        return sorted(rows, key=lambda d: d.decided_at, reverse=True)

    def record_redirect(self, row: RedirectDecisionRow) -> None:
        self.redirects[row.observation_id] = row

    def redirect(self, observation_id: ObservationId) -> RedirectDecisionRow | None:
        return self.redirects.get(observation_id)

    def record_interceptions(self, row: InterceptionSummary) -> None:
        self.intercepts[row.observation_id] = row

    def interceptions(self, observation_id: ObservationId) -> InterceptionSummary | None:
        return self.intercepts.get(observation_id)

    def record_seeds(self, rows: Sequence[SeedRecord]) -> None:
        for s in rows:
            self.seed_rows[(s.seed_source, s.url.url_id)] = s

    def seeds(self, seed_source: str) -> list[SeedRecord]:
        return [s for (src, _), s in self.seed_rows.items() if src == seed_source]

    def record_search_results(self, rows: Sequence[SearchResultRecord]) -> None:
        self.search_rows.extend(rows)

    def search_results(self, query: str, day: datetime) -> list[SearchResultRecord]:
        return [
            r
            for r in self.search_rows
            if r.query == query and day_bucket(r.retrieved_at) == day_bucket(day)
        ]

    def add_scope_sites(self, sites: Sequence[ScopeSite]) -> None:
        for s in sites:
            current = self.sites.get(s.domain)
            if current is None or s.added_at < current.added_at:
                self.sites[s.domain] = s

    def scope_sites(self) -> list[ScopeSite]:
        return list(self.sites.values())
