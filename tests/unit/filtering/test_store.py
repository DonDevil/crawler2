"""Rule storage, publishing and hot reload (design §13, §14)."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from crawler2.filtering.builtin import built_in_rules
from crawler2.filtering.engine import NO_RULESET, RulesetError
from crawler2.filtering.inputs import link_input
from crawler2.filtering.model import Action, Classification, Policy, Rule, RuleKind, RuleSource
from crawler2.filtering.store import (
    ReloadOutcome,
    RulesetHolder,
    SourceImport,
    load_engine,
    publish,
    rules_revision,
    store_source,
)
from crawler2.storage.repositories import StoredRule
from tests.unit.filtering.fakes import MemoryFilterRules

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _item(rules: list[Rule], source: RuleSource = RuleSource.OPERATOR) -> SourceImport:
    return SourceImport(source, rules, "test", "sha", "test/v1", {"rules": len(rules)})


def _rule(host: str, klass: Classification = Classification.AD) -> Rule:
    return Rule(
        source=RuleSource.OPERATOR,
        kind=RuleKind.HOST,
        pattern=host,
        classification=klass,
        confidence=1.0,
        reason="test",
        note="unit test",
    )


def _activate(repo: MemoryFilterRules, rules: list[Rule]) -> str:
    revision = store_source(repo, _item(rules), by="t", at=T0)
    record, _ = publish(repo, [("operator", revision.revision)], Policy(), by="t", at=T0)
    current = repo.active()
    assert repo.activate(
        record.ruleset_id, expected=current.ruleset_id if current else None, by="t", at=T0
    )
    return record.ruleset_id


def test_source_revisions_are_content_addressed_and_idempotent() -> None:
    repo = MemoryFilterRules()
    first = store_source(repo, _item([_rule("a.test")]), by="t", at=T0)
    again = store_source(repo, _item([_rule("a.test")]), by="t", at=T0)
    assert first == again
    assert first.revision == rules_revision([_rule("a.test")])
    stored = repo.rules("operator", first.revision)
    assert [json.loads(s.doc)["revision"] for s in stored] == [first.revision]
    other = store_source(repo, _item([_rule("b.test")]), by="t", at=T0)
    assert other.revision != first.revision


def test_published_ruleset_round_trips_with_provenance() -> None:
    repo = MemoryFilterRules()
    builtin = store_source(repo, _item(built_in_rules(), RuleSource.BUILT_IN), by="t", at=T0)
    record, engine = publish(repo, [("built_in", builtin.revision)], Policy(), by="t", at=T0)
    loaded = load_engine(repo, record.ruleset_id)
    assert loaded.ruleset_id == engine.ruleset_id == record.ruleset_id
    inp = link_input("https://ad.doubleclick.net/x")
    assert inp is not None
    decision = loaded.decide(inp)
    assert decision.rule_source == f"built_in@{builtin.revision}"
    assert decision.action is Action.BLOCK


def test_incomplete_or_tampered_rulesets_are_refused() -> None:
    repo = MemoryFilterRules()
    ruleset = _activate(repo, [_rule("a.test"), _rule("b.test")])
    key = next(iter(repo.rules_))
    victim = next(iter(repo.rules_[key]))
    removed = repo.rules_[key].pop(victim)
    with pytest.raises(RulesetError, match="rules stored"):
        load_engine(repo, ruleset)
    doc = json.loads(removed.doc) | {"confidence": 0.1}
    repo.rules_[key][victim] = StoredRule(victim, True, json.dumps(doc))
    with pytest.raises(RulesetError, match="digest"):
        load_engine(repo, ruleset)
    with pytest.raises(RulesetError, match="does not exist"):
        load_engine(repo, "rs-missing")


def test_publish_takes_one_revision_per_source() -> None:
    repo = MemoryFilterRules()
    a = store_source(repo, _item([_rule("a.test")]), by="t", at=T0)
    b = store_source(repo, _item([_rule("b.test")]), by="t", at=T0)
    with pytest.raises(RulesetError):
        publish(repo, [("operator", a.revision), ("operator", b.revision)], Policy(), by="t", at=T0)


def test_holder_starts_allow_all_and_swaps_to_the_active_ruleset() -> None:
    repo = MemoryFilterRules()
    holder = RulesetHolder(repo)
    assert holder.engine.ruleset_id == NO_RULESET
    assert holder.refresh() is ReloadOutcome.NO_ACTIVE
    first = _activate(repo, [_rule("a.test")])
    assert holder.refresh() is ReloadOutcome.SWAPPED
    assert holder.engine.ruleset_id == first
    assert holder.refresh() is ReloadOutcome.UNCHANGED


def test_failed_reload_keeps_the_known_good_ruleset_and_backs_off() -> None:
    repo = MemoryFilterRules()
    clock = [0.0]
    holder = RulesetHolder(repo, clock=lambda: clock[0])
    good = _activate(repo, [_rule("a.test")])
    holder.refresh()
    bad = _activate(repo, [_rule("b.test")])
    repo.rules_[("operator", rules_revision([_rule("b.test")]))].clear()
    assert holder.refresh() is ReloadOutcome.FAILED
    assert holder.engine.ruleset_id == good
    reads = repo.active_reads
    assert holder.refresh() is ReloadOutcome.FAILED  # same bad id: not reloaded before backoff
    assert repo.active_reads == reads + 1
    assert repo.activate(good, expected=bad, by="t", at=T0)  # rollback
    assert holder.refresh() is ReloadOutcome.UNCHANGED
    assert holder.engine.ruleset_id == good


def test_storage_outage_keeps_the_current_engine() -> None:
    repo = MemoryFilterRules()
    holder = RulesetHolder(repo)
    good = _activate(repo, [_rule("a.test")])
    holder.refresh()
    repo.fail_reads = True
    assert holder.refresh() is ReloadOutcome.FAILED
    assert holder.engine.ruleset_id == good


def test_maybe_refresh_polls_at_most_once_per_interval() -> None:
    repo = MemoryFilterRules()
    clock = [100.0]
    holder = RulesetHolder(repo, interval_s=15, clock=lambda: clock[0])
    assert holder.maybe_refresh() is ReloadOutcome.NO_ACTIVE
    assert holder.maybe_refresh() is ReloadOutcome.SKIPPED
    clock[0] += 15
    assert holder.maybe_refresh() is ReloadOutcome.NO_ACTIVE


def test_activate_is_compare_and_set() -> None:
    repo = MemoryFilterRules()
    first = _activate(repo, [_rule("a.test")])
    assert not repo.activate("rs-other", expected=None, by="t", at=T0)
    active = repo.active()
    assert active is not None
    assert active.ruleset_id == first


def test_evaluations_during_a_swap_see_one_whole_engine() -> None:
    """Concurrent readers never observe a partially built engine (design §14)."""
    repo = MemoryFilterRules()
    holder = RulesetHolder(repo)
    rulesets = [
        _activate(repo, [_rule(f"x{i}.test"), _rule("shared.test", klass)])
        for i, klass in enumerate([Classification.AD, Classification.MEDIA] * 3)
    ]
    expected = {
        rs: (Classification.AD if i % 2 == 0 else Classification.MEDIA)
        for i, rs in enumerate(rulesets)
    }
    expected[NO_RULESET] = Classification.UNKNOWN
    inp = link_input("https://shared.test/")
    assert inp is not None
    errors: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            engine = holder.engine
            decision = engine.decide(inp)
            if decision.ruleset != engine.ruleset_id or expected[decision.ruleset] != (
                decision.classification
            ):
                errors.append(decision.ruleset)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for rs in rulesets:
        repo.pointer["default"] = replace(repo.pointer["default"], ruleset_id=rs)
        assert holder.refresh() in (ReloadOutcome.SWAPPED, ReloadOutcome.UNCHANGED)
    stop.set()
    for t in threads:
        t.join()
    assert errors == []
