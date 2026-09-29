"""P6 rule storage, hot reload and discovery state on real Scylla (V003, design §13-§14)."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from antipiracy_contracts.ids import ObservationId
from antipiracy_contracts.models.web import UrlRef

from crawler2.filtering.builtin import built_in_rules
from crawler2.filtering.inputs import link_input
from crawler2.filtering.model import Action, Classification, Policy, Rule, RuleKind, RuleSource
from crawler2.filtering.store import (
    ReloadOutcome,
    RulesetHolder,
    SourceImport,
    load_engine,
    publish,
    store_source,
)
from crawler2.storage.repositories import (
    AdmittedUrl,
    FilterDecisionRow,
    InterceptionSummary,
    RedirectDecisionRow,
    ScopeSite,
    SearchResultRecord,
    SeedRecord,
)
from crawler2.storage.scylla import ScyllaStorage
from crawler2.storage.scylla.filtering import RULE_BUCKETS, rule_bucket

pytestmark = pytest.mark.integration
NOW = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)


def _operator(host: str, klass: Classification = Classification.AD) -> Rule:
    return Rule(
        source=RuleSource.OPERATOR,
        kind=RuleKind.HOST,
        pattern=host,
        classification=klass,
        confidence=1.0,
        reason="it",
        note="integration test",
    )


def test_rules_round_trip_verify_and_hot_reload(storage: ScyllaStorage) -> None:
    repo = storage.filter_rules
    name = f"it-{uuid.uuid4().hex[:8]}"
    builtin = store_source(
        repo,
        SourceImport(RuleSource.BUILT_IN, built_in_rules(), "builtin.py", "sha", "t", {}),
        by="it",
        at=NOW,
    )
    assert (
        store_source(
            repo,
            SourceImport(RuleSource.BUILT_IN, built_in_rules(), "builtin.py", "sha", "t", {}),
            by="it",
            at=NOW,
        )
        == builtin
    )
    stored = repo.rules("built_in", builtin.revision)
    assert len(stored) == builtin.rule_count == len(built_in_rules())
    assert len({rule_bucket(s.rule_id) for s in stored}) > RULE_BUCKETS // 2

    op_a = store_source(
        repo,
        SourceImport(RuleSource.OPERATOR, [_operator("a.test")], "op", "s", "t", {}),
        by="it",
        at=NOW,
    )
    op_b = store_source(
        repo,
        SourceImport(
            RuleSource.OPERATOR, [_operator("a.test", Classification.MEDIA)], "op", "s", "t", {}
        ),
        by="it",
        at=NOW + timedelta(seconds=1),
    )
    assert [s.revision for s in repo.sources("operator")][:2] == [op_b.revision, op_a.revision]
    first, _ = publish(
        repo,
        [("built_in", builtin.revision), ("operator", op_a.revision)],
        Policy(),
        by="it",
        at=NOW,
    )
    second, _ = publish(
        repo,
        [("built_in", builtin.revision), ("operator", op_b.revision)],
        Policy(),
        by="it",
        at=NOW,
    )
    assert repo.ruleset(first.ruleset_id) == first
    assert load_engine(repo, first.ruleset_id).ruleset_id == first.ruleset_id

    holder = RulesetHolder(repo, name=name)
    assert holder.refresh() is ReloadOutcome.NO_ACTIVE
    assert repo.activate(first.ruleset_id, expected=None, by="it", at=NOW, name=name)
    assert not repo.activate(second.ruleset_id, expected=None, by="it", at=NOW, name=name)
    assert holder.refresh() is ReloadOutcome.SWAPPED
    inp = link_input("https://www.a.test/")
    assert inp is not None
    assert holder.engine.decide(inp).classification is Classification.AD
    assert repo.activate(second.ruleset_id, expected=first.ruleset_id, by="it", at=NOW, name=name)
    assert holder.refresh() is ReloadOutcome.SWAPPED
    assert holder.engine.decide(inp).classification is Classification.MEDIA
    pointer = repo.active(name)
    assert pointer is not None
    assert pointer.previous_ruleset_id == first.ruleset_id
    # rollback = re-activate the previous id (compare-and-set against the current one)
    assert repo.activate(first.ruleset_id, expected=second.ruleset_id, by="it", at=NOW, name=name)
    assert holder.refresh() is ReloadOutcome.SWAPPED
    assert holder.engine.ruleset_id == first.ruleset_id


def test_discovery_rows_round_trip(storage: ScyllaStorage) -> None:
    repo = storage.discovery
    ref = UrlRef.of(f"https://it-{uuid.uuid4().hex[:8]}.test/page")
    assert repo.admission_states([ref.url_id]) == {}
    repo.mark_admitted([AdmittedUrl(ref, NOW, "seed", 70, "http")])
    decision = {"action": "allow", "rule_id": "default"}
    repo.record_decisions(
        [
            FilterDecisionRow(
                ref.url_id,
                ref.url,
                NOW,
                "link",
                "admitted",
                json.dumps(decision),
                "allow",
                "default",
                "rs-1",
            ),
            FilterDecisionRow(
                ref.url_id,
                ref.url,
                NOW + timedelta(seconds=1),
                "link",
                "blocked",
                "{}",
                "block",
                "r2",
                "rs-2",
                ObservationId.new(),
            ),
        ],
        ttl_s=3600,
    )
    state = repo.admission_states([ref.url_id])[ref.url_id]
    assert (state.last_admitted_at, state.last_origin) == (NOW, "seed")
    assert (state.last_action, state.last_rule_id, state.last_ruleset) == ("block", "r2", "rs-2")
    rows = repo.decisions(ref.url_id)
    assert [r.outcome for r in rows] == ["blocked", "admitted"]  # newest first
    assert rows[0].source_observation_id is not None
    ttl = storage.session.execute_raw(
        f"SELECT TTL(outcome) FROM {storage.session.keyspace}.filter_decisions_by_url "
        f"WHERE url_id = {ref.url_id.uuid}"
    )
    assert all(0 < r[0] <= 3600 for r in ttl)

    oid = ObservationId.new()
    redirect = RedirectDecisionRow(oid, Action.BLOCK.value, "[]", "rs-1", NOW)
    repo.record_redirect(redirect)
    assert repo.redirect(oid) == redirect
    summary = InterceptionSummary(oid, {"ad:block": 3, "unknown:allow": 9}, "[]", "rs-1", NOW)
    repo.record_interceptions(summary)
    assert repo.interceptions(oid) == summary

    source = f"it-{uuid.uuid4().hex[:6]}"
    seed = SeedRecord(source, ref, ref.url, "/seeds.txt", "sha", 3, NOW, {"operator": "it"})
    repo.record_seeds([seed])
    assert repo.seeds(source) == [seed]
    query = f"query {uuid.uuid4().hex}"
    result = SearchResultRecord(
        query,
        "duckduckgo",
        "p6-search/v1",
        0,
        "https://d.test/l/?x",
        ref.url,
        "T",
        "S",
        NOW,
        "canonical",
    )
    repo.record_search_results([result])
    assert repo.search_results(query, NOW) == [result]

    domain = f"it-{uuid.uuid4().hex[:8]}.test"
    repo.add_scope_sites([ScopeSite(domain, "search", NOW + timedelta(hours=1), "q")])
    repo.add_scope_sites([ScopeSite(domain, "seed", NOW, "file")])  # earlier origin wins
    repo.add_scope_sites([ScopeSite(domain, "search", NOW + timedelta(hours=2), "q2")])
    site = next(s for s in repo.scope_sites() if s.domain == domain)
    assert (site.origin, site.added_at, site.detail) == ("seed", NOW, "file")


def test_hot_reload_picks_up_a_new_ruleset_within_one_interval(storage: ScyllaStorage) -> None:
    repo = storage.filter_rules
    name = f"it-{uuid.uuid4().hex[:8]}"
    op = store_source(
        repo,
        SourceImport(RuleSource.OPERATOR, [_operator("hot.test")], "op", "s", "t", {}),
        by="it",
        at=NOW,
    )
    record, _ = publish(repo, [("operator", op.revision)], Policy(), by="it", at=NOW)
    holders = [RulesetHolder(repo, name=name, interval_s=0.2) for _ in range(3)]
    for h in holders:
        assert h.maybe_refresh() is ReloadOutcome.NO_ACTIVE
    repo.activate(record.ruleset_id, expected=None, by="it", at=NOW, name=name)
    time.sleep(0.25)
    assert [h.maybe_refresh() for h in holders] == [ReloadOutcome.SWAPPED] * 3
    assert {h.engine.ruleset_id for h in holders} == {record.ruleset_id}
