"""Durable rules → compiled engine, and controlled hot reload (design §13, §14).

Scylla holds the authority: immutable source revisions (F1/F2), immutable
rulesets (F3) and one active pointer (F4). A process keeps a compiled
``FilterEngine`` as derived state and swaps it only for a ruleset that
loads completely, verifies and compiles; otherwise it keeps the engine it
has. Nothing here is on a fetch's critical path: with no ruleset the engine
is allow-all (B.5 #1).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from crawler2.core.observability import Metrics, get_logger
from crawler2.filtering.engine import FilterEngine, RulesetError
from crawler2.filtering.inputs import PSL_VERSION
from crawler2.filtering.model import ENGINE_SEMANTICS, Policy, Rule, RuleSource
from crawler2.storage.errors import StorageError
from crawler2.storage.repositories import (
    FilterRuleRepository,
    FilterRulesetRecord,
    FilterSourceRevision,
    StoredRule,
)

_log = get_logger("filtering.store")

RETRY_FAILED_AFTER_S: Final = 300.0
"""A ruleset that failed to load is not retried before this (unless the pointer moves)."""


def rules_revision(rules: Iterable[Rule]) -> str:
    """Content digest of a rule set, independent of any stored revision label."""
    h = hashlib.sha256()
    for doc in sorted((r.to_doc() for r in rules), key=lambda d: d["rule_id"]):
        doc.pop("revision")
        h.update(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode())
        h.update(b"\n")
    return h.hexdigest()[:16]


def to_stored(rule: Rule) -> StoredRule:
    return StoredRule(rule.rule_id, rule.enabled, json.dumps(rule.to_doc(), sort_keys=True))


def from_stored(stored: StoredRule) -> Rule:
    rule = Rule.from_doc(json.loads(stored.doc))
    if rule.rule_id != stored.rule_id or rule.enabled != stored.enabled:
        raise RulesetError(f"stored rule {stored.rule_id} does not match its document")
    return rule


@dataclass(frozen=True, slots=True)
class SourceImport:
    """What an importer produced, ready to be stored as one source revision."""

    source: RuleSource
    rules: Sequence[Rule]
    origin: str
    input_sha256: str
    importer: str
    report: dict[str, Any]
    license: str | None = None
    title: str | None = None
    list_version: str | None = None


def store_source(
    repo: FilterRuleRepository, item: SourceImport, *, by: str, at: datetime
) -> FilterSourceRevision:
    """Write one source revision; re-importing identical rules is a no-op."""
    revision = rules_revision(item.rules)
    existing = repo.source(item.source.value, revision)
    if existing is not None:
        return existing
    rules = [r.with_revision(revision) for r in item.rules]
    record = FilterSourceRevision(
        source=item.source.value,
        revision=revision,
        revision_at=at,
        origin=item.origin,
        input_sha256=item.input_sha256,
        importer=item.importer,
        rule_count=len(rules),
        enabled_count=sum(r.enabled for r in rules),
        report=json.dumps(item.report, sort_keys=True),
        created_by=by,
        license=item.license,
        title=item.title,
        list_version=item.list_version,
    )
    repo.put_source(record, [to_stored(r) for r in rules])
    return record


def load_rules(repo: FilterRuleRepository, source: str, revision: str) -> list[Rule]:
    """Every rule of a revision; raises if the revision is missing or incomplete."""
    record = repo.source(source, revision)
    if record is None:
        raise RulesetError(f"source revision {source}@{revision} does not exist")
    stored = repo.rules(source, revision)
    if len(stored) != record.rule_count:
        raise RulesetError(
            f"{source}@{revision}: {len(stored)} rules stored, {record.rule_count} recorded"
        )
    return [from_stored(s) for s in stored]


def publish(
    repo: FilterRuleRepository,
    sources: Sequence[tuple[str, str]],
    policy: Policy,
    *,
    by: str,
    at: datetime,
    note: str = "",
) -> tuple[FilterRulesetRecord, FilterEngine]:
    """Compose source revisions into an immutable ruleset (not yet active)."""
    if len({s for s, _ in sources}) != len(sources):
        raise RulesetError("a ruleset takes at most one revision per source")
    rules = [rule for s, r in sorted(sources) for rule in load_rules(repo, s, r)]
    engine = FilterEngine.compile(rules, policy)
    record = FilterRulesetRecord(
        ruleset_id=engine.ruleset_id,
        sources=tuple(sorted(sources)),
        rule_count=engine.rule_count,
        policy=json.dumps(policy.to_doc(), sort_keys=True),
        semantics=ENGINE_SEMANTICS,
        psl=PSL_VERSION,
        created_at=at,
        created_by=by,
        note=note,
    )
    repo.put_ruleset(record)
    return record, engine


def load_engine(repo: FilterRuleRepository, ruleset_id: str) -> FilterEngine:
    """Load, verify and compile a stored ruleset; raises ``RulesetError`` if anything is off."""
    record = repo.ruleset(ruleset_id)
    if record is None:
        raise RulesetError(f"ruleset {ruleset_id} does not exist")
    if record.semantics != ENGINE_SEMANTICS or record.psl != PSL_VERSION:
        raise RulesetError(
            f"ruleset {ruleset_id} was built for {record.semantics}/psl {record.psl}; "
            f"this process runs {ENGINE_SEMANTICS}/psl {PSL_VERSION}"
        )
    rules = [rule for s, r in record.sources for rule in load_rules(repo, s, r)]
    policy = Policy.from_doc(json.loads(record.policy))
    engine = FilterEngine.compile(rules, policy, expected_id=ruleset_id)
    if engine.rule_count != record.rule_count:
        raise RulesetError(f"{ruleset_id}: {engine.rule_count} rules, {record.rule_count} recorded")
    return engine


class ReloadOutcome(StrEnum):
    UNCHANGED = "unchanged"
    SWAPPED = "swapped"
    FAILED = "failed"
    NO_ACTIVE = "no_active"
    SKIPPED = "skipped"


class RulesetHolder:
    """The process's current engine; swapped atomically by ``refresh``.

    Readers take ``holder.engine`` once per evaluation and keep that object,
    so an evaluation in progress never sees a half-swapped engine.
    """

    def __init__(
        self,
        repo: FilterRuleRepository | None,
        *,
        name: str = "default",
        interval_s: float = 15.0,
        metrics: Metrics | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repo = repo
        self._name = name
        self._interval = interval_s
        self._clock = clock
        self._engine = FilterEngine.empty()
        self._next_poll = 0.0
        self._failed: tuple[str, float] | None = None
        self._reloads = None
        self._state = None
        self._rules = None
        if metrics is not None:
            self._reloads = metrics.counter(
                "filter_reload_total", "Ruleset reload attempts by result", ["result"]
            )
            self._state = metrics.gauge(
                "filter_ruleset_state", "1 when a stored ruleset is active in this process"
            )
            self._rules = metrics.gauge("filter_ruleset_rules", "Compiled rules in the engine")

    @property
    def engine(self) -> FilterEngine:
        return self._engine

    def refresh(self) -> ReloadOutcome:
        outcome = self._refresh()
        if self._reloads is not None:
            self._reloads.labels(outcome.value).inc()
        if self._state is not None and self._rules is not None:
            self._state.set(0 if self._engine.ruleset_id == "none" else 1)
            self._rules.set(self._engine.rule_count)
        return outcome

    def maybe_refresh(self) -> ReloadOutcome:
        """``refresh`` at most once per interval (call it from any loop)."""
        now = self._clock()
        if now < self._next_poll:
            return ReloadOutcome.SKIPPED
        self._next_poll = now + self._interval
        return self.refresh()

    def _refresh(self) -> ReloadOutcome:
        if self._repo is None:
            return ReloadOutcome.NO_ACTIVE
        try:
            pointer = self._repo.active(self._name)
        except StorageError:
            _log.warning("filter_pointer_unavailable", ruleset=self._engine.ruleset_id)
            return ReloadOutcome.FAILED
        if pointer is None:
            return ReloadOutcome.NO_ACTIVE
        target = pointer.ruleset_id
        if target == self._engine.ruleset_id:
            return ReloadOutcome.UNCHANGED
        if self._failed and self._failed[0] == target and self._clock() < self._failed[1]:
            return ReloadOutcome.FAILED
        started = time.perf_counter()
        try:
            engine = load_engine(self._repo, target)
        except (RulesetError, StorageError, ValueError, KeyError) as exc:
            self._failed = (target, self._clock() + RETRY_FAILED_AFTER_S)
            _log.error(
                "filter_reload_failed",
                target=target,
                keeping=self._engine.ruleset_id,
                error=str(exc)[:500],
            )
            return ReloadOutcome.FAILED
        previous = self._engine.ruleset_id
        self._engine = engine
        self._failed = None
        _log.info(
            "filter_ruleset_swapped",
            previous=previous,
            ruleset=engine.ruleset_id,
            rules=engine.rule_count,
            seconds=round(time.perf_counter() - started, 3),
        )
        return ReloadOutcome.SWAPPED
