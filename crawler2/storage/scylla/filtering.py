"""P6 filter rules and discovery state on Scylla (V003, design §13).

Rule revisions and rulesets are immutable rows; the active pointer (F4) is
the only compare-and-set. Discovery rows are derived state and use the
derived consistency level; scope sites keep their first origin through
earliest-wins cell timestamps.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any, Final

from antipiracy_contracts.ids import ObservationId, UrlId
from antipiracy_contracts.models.web import UrlRef

from crawler2.storage.layout import day_bucket, earliest_wins
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
from crawler2.storage.scylla.session import Consistency, ScyllaSession, utc

AUTH = Consistency.AUTHORITATIVE_WRITE
DERIVED = Consistency.DERIVED_WRITE
EC = Consistency.EVENTUAL_READ
STRONG = Consistency.STRONG_READ
SERIAL = Consistency.SERIAL

RULE_BUCKETS: Final = 16
SCOPE: Final = "default"

_SOURCE_COLUMNS = (
    "source, revision, revision_at, origin, input_sha256, importer, license, title, "
    "list_version, rule_count, enabled_count, report, created_by"
)
_SOURCE_INSERT = (
    f"INSERT INTO {{ks}}.filter_sources ({_SOURCE_COLUMNS}) VALUES ({', '.join('?' * 13)})"
)
_SOURCE_GET = (
    f"SELECT {_SOURCE_COLUMNS} FROM {{ks}}.filter_sources WHERE source = ? AND revision = ?"
)
_SOURCES_GET = f"SELECT {_SOURCE_COLUMNS} FROM {{ks}}.filter_sources WHERE source = ?"
_RULE_INSERT = (
    "INSERT INTO {ks}.filter_rules (source, revision, bucket, rule_id, enabled, doc) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
_RULES_GET = (
    "SELECT rule_id, enabled, doc FROM {ks}.filter_rules "
    "WHERE source = ? AND revision = ? AND bucket = ?"
)
_RULESET_COLUMNS = (
    "ruleset_id, sources, rule_count, policy, semantics, psl, created_at, created_by, note"
)
_RULESET_INSERT = (
    f"INSERT INTO {{ks}}.filter_rulesets ({_RULESET_COLUMNS}) VALUES ({', '.join('?' * 9)})"
)
_RULESET_GET = f"SELECT {_RULESET_COLUMNS} FROM {{ks}}.filter_rulesets WHERE ruleset_id = ?"
_ACTIVE_GET = (
    "SELECT name, ruleset_id, previous_ruleset_id, activated_at, activated_by "
    "FROM {ks}.filter_active WHERE name = ?"
)
_ACTIVE_CREATE = (
    "INSERT INTO {ks}.filter_active (name, ruleset_id, previous_ruleset_id, activated_at, "
    "activated_by) VALUES (?, ?, ?, ?, ?) IF NOT EXISTS"
)
_ACTIVE_SWAP = (
    "UPDATE {ks}.filter_active SET ruleset_id = ?, previous_ruleset_id = ?, activated_at = ?, "
    "activated_by = ? WHERE name = ? IF ruleset_id = ?"
)
_ADMISSION_GET = (
    "SELECT url_id, last_admitted_at, last_origin, last_action, last_rule_id, last_ruleset "
    "FROM {ks}.url_admission WHERE url_id = ?"
)
_ADMITTED = (
    "UPDATE {ks}.url_admission SET url = ?, last_admitted_at = ?, last_origin = ?, "
    "last_priority = ?, last_queue = ? WHERE url_id = ?"
)
_LAST_DECISION = (
    "UPDATE {ks}.url_admission SET url = ?, last_action = ?, last_rule_id = ?, last_ruleset = ?, "
    "last_decided_at = ? WHERE url_id = ?"
)
_DECISION_INSERT = (
    "INSERT INTO {ks}.filter_decisions_by_url (url_id, decided_at, context, url, outcome, action, "
    "rule_id, ruleset, decision, source_observation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "USING TTL ?"
)
_DECISIONS_GET = (
    "SELECT url_id, decided_at, context, url, outcome, action, rule_id, ruleset, decision, "
    "source_observation_id FROM {ks}.filter_decisions_by_url WHERE url_id = ?"
)
_REDIRECT_INSERT = (
    "INSERT INTO {ks}.redirect_decisions_by_observation (observation_id, action, decisions, "
    "ruleset, decided_at) VALUES (?, ?, ?, ?, ?)"
)
_REDIRECT_GET = (
    "SELECT action, decisions, ruleset, decided_at FROM {ks}.redirect_decisions_by_observation "
    "WHERE observation_id = ?"
)
_INTERCEPT_INSERT = (
    "INSERT INTO {ks}.interceptions_by_observation (observation_id, counts, blocked, ruleset, "
    "recorded_at) VALUES (?, ?, ?, ?, ?)"
)
_INTERCEPT_GET = (
    "SELECT counts, blocked, ruleset, recorded_at FROM {ks}.interceptions_by_observation "
    "WHERE observation_id = ?"
)
_SEED_INSERT = (
    "INSERT INTO {ks}.seeds_by_source (seed_source, url_id, url, original, file_path, "
    "file_sha256, line, imported_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_SEEDS_GET = (
    "SELECT url_id, url, original, file_path, file_sha256, line, imported_at, metadata "
    "FROM {ks}.seeds_by_source WHERE seed_source = ?"
)
_SEARCH_INSERT = (
    "INSERT INTO {ks}.search_results_by_query_day (query_digest, day, retrieved_at, engine, rank, "
    "query, adapter_version, returned_url, url, title, snippet, outcome) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_SEARCH_GET = (
    "SELECT retrieved_at, engine, rank, query, adapter_version, returned_url, url, title, "
    "snippet, outcome FROM {ks}.search_results_by_query_day WHERE query_digest = ? AND day = ?"
)
_SCOPE_INSERT = (
    "UPDATE {ks}.scope_sites USING TIMESTAMP ? SET origin = ?, added_at = ?, detail = ? "
    "WHERE scope = ? AND domain = ?"
)
_SCOPE_GET = "SELECT domain, origin, added_at, detail FROM {ks}.scope_sites WHERE scope = ?"


def rule_bucket(rule_id: str) -> int:
    return int(hashlib.sha256(rule_id.encode()).hexdigest()[:8], 16) % RULE_BUCKETS


def query_digest(query: str) -> str:
    return hashlib.sha256(query.encode()).hexdigest()[:32]


def _source(row: Any) -> FilterSourceRevision:
    return FilterSourceRevision(
        source=row.source,
        revision=row.revision,
        revision_at=utc(row.revision_at),
        origin=row.origin,
        input_sha256=row.input_sha256,
        importer=row.importer,
        rule_count=row.rule_count,
        enabled_count=row.enabled_count,
        report=row.report,
        created_by=row.created_by,
        license=row.license,
        title=row.title,
        list_version=row.list_version,
    )


class ScyllaFilterRuleRepository:
    def __init__(self, session: ScyllaSession) -> None:
        self._s = session

    def put_source(self, revision: FilterSourceRevision, rules: Sequence[StoredRule]) -> None:
        r = revision
        self._s.execute_all(
            self._s.bind(
                _RULE_INSERT,
                AUTH,
                (
                    r.source,
                    r.revision,
                    rule_bucket(rule.rule_id),
                    rule.rule_id,
                    rule.enabled,
                    rule.doc,
                ),
            )
            for rule in rules
        )
        self._s.execute(
            self._s.bind(
                _SOURCE_INSERT,
                AUTH,
                (
                    r.source,
                    r.revision,
                    r.revision_at,
                    r.origin,
                    r.input_sha256,
                    r.importer,
                    r.license,
                    r.title,
                    r.list_version,
                    r.rule_count,
                    r.enabled_count,
                    r.report,
                    r.created_by,
                ),
            )
        )

    def source(self, source: str, revision: str) -> FilterSourceRevision | None:
        rows = self._s.execute(self._s.bind(_SOURCE_GET, STRONG, (source, revision)))
        return _source(rows[0]) if rows else None

    def sources(self, source: str) -> list[FilterSourceRevision]:
        rows = self._s.execute(self._s.bind(_SOURCES_GET, STRONG, (source,)))
        return sorted(map(_source, rows), key=lambda s: (s.revision_at, s.revision), reverse=True)

    def rules(self, source: str, revision: str) -> list[StoredRule]:
        results = self._s.execute_many(
            self._s.bind(_RULES_GET, STRONG, (source, revision, bucket))
            for bucket in range(RULE_BUCKETS)
        )
        rules = [StoredRule(r.rule_id, bool(r.enabled), r.doc) for rows in results for r in rows]
        return sorted(rules, key=lambda r: r.rule_id)

    def put_ruleset(self, record: FilterRulesetRecord) -> None:
        self._s.execute(
            self._s.bind(
                _RULESET_INSERT,
                AUTH,
                (
                    record.ruleset_id,
                    json.dumps([list(pair) for pair in record.sources]),
                    record.rule_count,
                    record.policy,
                    record.semantics,
                    record.psl,
                    record.created_at,
                    record.created_by,
                    record.note,
                ),
            )
        )

    def ruleset(self, ruleset_id: str) -> FilterRulesetRecord | None:
        rows = self._s.execute(self._s.bind(_RULESET_GET, STRONG, (ruleset_id,)))
        if not rows:
            return None
        r = rows[0]
        return FilterRulesetRecord(
            ruleset_id=r.ruleset_id,
            sources=tuple((s, v) for s, v in json.loads(r.sources)),
            rule_count=r.rule_count,
            policy=r.policy,
            semantics=r.semantics,
            psl=r.psl,
            created_at=utc(r.created_at),
            created_by=r.created_by,
            note=r.note or "",
        )

    def active(self, name: str = "default") -> ActiveRulesetPointer | None:
        rows = self._s.execute(self._s.bind(_ACTIVE_GET, SERIAL, (name,)))
        if not rows or rows[0].ruleset_id is None:
            return None
        r = rows[0]
        return ActiveRulesetPointer(
            name=r.name,
            ruleset_id=r.ruleset_id,
            previous_ruleset_id=r.previous_ruleset_id,
            activated_at=utc(r.activated_at),
            activated_by=r.activated_by,
        )

    def activate(
        self, ruleset_id: str, *, expected: str | None, by: str, at: datetime, name: str = "default"
    ) -> bool:
        if expected is None:
            statement = self._s.bind(_ACTIVE_CREATE, SERIAL, (name, ruleset_id, None, at, by))
        else:
            statement = self._s.bind(
                _ACTIVE_SWAP, SERIAL, (ruleset_id, expected, at, by, name, expected)
            )
        rows = self._s.execute(statement)
        return bool(rows and rows[0].applied)


class ScyllaDiscoveryRepository:
    def __init__(self, session: ScyllaSession) -> None:
        self._s = session

    def admission_states(self, url_ids: Collection[UrlId]) -> dict[UrlId, UrlAdmissionState]:
        ids = list(dict.fromkeys(url_ids))
        results = self._s.execute_many(self._s.bind(_ADMISSION_GET, EC, (u.uuid,)) for u in ids)
        states: dict[UrlId, UrlAdmissionState] = {}
        for url_id, rows in zip(ids, results, strict=True):
            if not rows:
                continue
            r = rows[0]
            states[url_id] = UrlAdmissionState(
                url_id=url_id,
                last_admitted_at=utc(r.last_admitted_at) if r.last_admitted_at else None,
                last_origin=r.last_origin,
                last_action=r.last_action,
                last_rule_id=r.last_rule_id,
                last_ruleset=r.last_ruleset,
            )
        return states

    def mark_admitted(self, rows: Sequence[AdmittedUrl]) -> None:
        self._s.execute_all(
            self._s.bind(
                _ADMITTED,
                DERIVED,
                (a.url.url, a.at, a.origin, a.priority, a.queue, a.url.url_id.uuid),
            )
            for a in rows
        )

    def record_decisions(self, rows: Sequence[FilterDecisionRow], *, ttl_s: int) -> None:
        statements = []
        for d in rows:
            observation = d.source_observation_id.uuid if d.source_observation_id else None
            statements.append(
                self._s.bind(
                    _DECISION_INSERT,
                    DERIVED,
                    (
                        d.url_id.uuid,
                        d.decided_at,
                        d.context,
                        d.url,
                        d.outcome,
                        d.action,
                        d.rule_id,
                        d.ruleset,
                        d.decision,
                        observation,
                        ttl_s,
                    ),
                )
            )
            statements.append(
                self._s.bind(
                    _LAST_DECISION,
                    DERIVED,
                    (d.url, d.action, d.rule_id, d.ruleset, d.decided_at, d.url_id.uuid),
                )
            )
        self._s.execute_all(statements)

    def decisions(self, url_id: UrlId) -> list[FilterDecisionRow]:
        rows = self._s.execute(self._s.bind(_DECISIONS_GET, EC, (url_id.uuid,)))
        return [
            FilterDecisionRow(
                url_id=url_id,
                url=r.url,
                decided_at=utc(r.decided_at),
                context=r.context,
                outcome=r.outcome,
                decision=r.decision,
                action=r.action,
                rule_id=r.rule_id,
                ruleset=r.ruleset,
                source_observation_id=ObservationId.from_uuid(r.source_observation_id)
                if r.source_observation_id
                else None,
            )
            for r in rows
        ]

    def record_redirect(self, row: RedirectDecisionRow) -> None:
        self._s.execute(
            self._s.bind(
                _REDIRECT_INSERT,
                DERIVED,
                (row.observation_id.uuid, row.action, row.decisions, row.ruleset, row.decided_at),
            )
        )

    def redirect(self, observation_id: ObservationId) -> RedirectDecisionRow | None:
        rows = self._s.execute(self._s.bind(_REDIRECT_GET, EC, (observation_id.uuid,)))
        if not rows:
            return None
        r = rows[0]
        return RedirectDecisionRow(
            observation_id, r.action, r.decisions, r.ruleset, utc(r.decided_at)
        )

    def record_interceptions(self, row: InterceptionSummary) -> None:
        self._s.execute(
            self._s.bind(
                _INTERCEPT_INSERT,
                DERIVED,
                (row.observation_id.uuid, row.counts, row.blocked, row.ruleset, row.recorded_at),
            )
        )

    def interceptions(self, observation_id: ObservationId) -> InterceptionSummary | None:
        rows = self._s.execute(self._s.bind(_INTERCEPT_GET, EC, (observation_id.uuid,)))
        if not rows:
            return None
        r = rows[0]
        return InterceptionSummary(
            observation_id, dict(r.counts or {}), r.blocked, r.ruleset, utc(r.recorded_at)
        )

    def record_seeds(self, rows: Sequence[SeedRecord]) -> None:
        self._s.execute_all(
            self._s.bind(
                _SEED_INSERT,
                AUTH,
                (
                    s.seed_source,
                    s.url.url_id.uuid,
                    s.url.url,
                    s.original,
                    s.file_path,
                    s.file_sha256,
                    s.line,
                    s.imported_at,
                    s.metadata,
                ),
            )
            for s in rows
        )

    def seeds(self, seed_source: str) -> list[SeedRecord]:
        rows = self._s.execute(self._s.bind(_SEEDS_GET, STRONG, (seed_source,)))
        return [
            SeedRecord(
                seed_source=seed_source,
                url=UrlRef.of(r.url),
                original=r.original,
                file_path=r.file_path,
                file_sha256=r.file_sha256,
                line=r.line,
                imported_at=utc(r.imported_at),
                metadata=dict(r.metadata or {}),
            )
            for r in rows
        ]

    def record_search_results(self, rows: Sequence[SearchResultRecord]) -> None:
        self._s.execute_all(
            self._s.bind(
                _SEARCH_INSERT,
                AUTH,
                (
                    query_digest(s.query),
                    day_bucket(s.retrieved_at),
                    s.retrieved_at,
                    s.engine,
                    s.rank,
                    s.query,
                    s.adapter_version,
                    s.returned_url,
                    s.url,
                    s.title,
                    s.snippet,
                    s.outcome,
                ),
            )
            for s in rows
        )

    def search_results(self, query: str, day: datetime) -> list[SearchResultRecord]:
        rows = self._s.execute(
            self._s.bind(_SEARCH_GET, STRONG, (query_digest(query), day_bucket(day)))
        )
        return [
            SearchResultRecord(
                query=r.query,
                engine=r.engine,
                adapter_version=r.adapter_version,
                rank=r.rank,
                returned_url=r.returned_url,
                url=r.url,
                title=r.title,
                snippet=r.snippet,
                retrieved_at=utc(r.retrieved_at),
                outcome=r.outcome,
            )
            for r in rows
        ]

    def add_scope_sites(self, sites: Sequence[ScopeSite]) -> None:
        self._s.execute_all(
            self._s.bind(
                _SCOPE_INSERT,
                AUTH,
                (earliest_wins(s.added_at), s.origin, s.added_at, s.detail, SCOPE, s.domain),
            )
            for s in sites
        )

    def scope_sites(self) -> list[ScopeSite]:
        rows = self._s.execute(self._s.bind(_SCOPE_GET, STRONG, (SCOPE,)))
        return [ScopeSite(r.domain, r.origin, utc(r.added_at), r.detail or "") for r in rows]
