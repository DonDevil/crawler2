"""The compiled, immutable filter engine (design §6, §7).

``FilterEngine.compile(rules, policy)`` indexes the enabled rules; ``decide``
collects every matching rule, applies the precedence of design §6.2 and
maps the winner to an action (§6.3). Decisions are a pure function of the
input and the ruleset: candidate order is a total key ending in the rule
id, never a dict/set iteration order.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from crawler2.filtering.inputs import PSL_VERSION
from crawler2.filtering.model import (
    BLOCK_CANDIDATE_CLASSES,
    CLASS_RANK,
    ENGINE_SEMANTICS,
    KIND_RANK,
    SOURCE_RANK,
    Action,
    Classification,
    Decision,
    FilterInput,
    Party,
    Policy,
    Rule,
    RuleKind,
    RuleSource,
)
from crawler2.filtering.patterns import TOKEN_RE, compile_pattern

NO_RULESET: Final = "none"

# URL tokens that occur in most URLs make poor index keys.
_COMMON_TOKENS: Final = frozenset(
    {"http", "https", "www", "com", "net", "org", "html", "htm", "php", "index", "js", "css"}
)


class RulesetError(ValueError):
    """A ruleset cannot be compiled (invalid rule, digest or count mismatch)."""


@dataclass(frozen=True, slots=True)
class _Compiled:
    rule: Rule
    key: tuple[int, int, int, int, int, str]
    """Precedence within the classification tier: kind, constrained, -length, class, source, id."""
    regex: re.Pattern[str] | None = None
    prefix: str = ""


@dataclass(frozen=True, slots=True)
class _Bucket:
    """URL patterns sharing an index token, behind one combined prefilter regex.

    Most URLs match none of a bucket's patterns; the prefilter answers that
    with one search instead of one per pattern (Gate D profile, benchmarks.md).
    """

    prefilter: re.Pattern[str]
    entries: tuple[_Compiled, ...]

    @classmethod
    def of(cls, entries: list[_Compiled]) -> _Bucket:
        ordered = sorted(entries, key=_by_key)
        combined = "|".join(f"(?:{e.regex.pattern})" for e in ordered if e.regex is not None)
        return cls(re.compile(combined), tuple(ordered))


def ruleset_digest(rules: Iterable[Rule], policy: Policy) -> str:
    """Content identity of a ruleset: rules (sorted), policy, engine semantics, PSL."""
    h = hashlib.sha256()
    header = {"semantics": ENGINE_SEMANTICS, "psl": PSL_VERSION, "policy": policy.to_doc()}
    h.update(json.dumps(header, sort_keys=True).encode())
    for doc in sorted((r.to_doc() for r in rules), key=lambda d: d["rule_id"]):
        h.update(b"\n")
        h.update(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode())
    return "rs-" + h.hexdigest()[:32]


def _length(rule: Rule, literal: int) -> int:
    """Specificity length: the matched host/label/path text, or a pattern's literal length."""
    return literal if rule.kind is RuleKind.URL_PATTERN else len(rule.pattern)


class FilterEngine:
    """Immutable after ``compile``; safe to share between threads and coroutines."""

    def __init__(self, ruleset_id: str, policy: Policy) -> None:
        self.ruleset_id = ruleset_id
        self.policy = policy
        self.rule_count = 0
        self.counts: dict[str, int] = {}
        self.untokenized = 0
        self._hosts: dict[str, list[_Compiled]] = {}
        self._labels: dict[str, list[_Compiled]] = {}
        self._paths: dict[str, list[_Compiled]] = {}
        self._tokens: dict[str, _Bucket] = {}
        self._generic: _Bucket | None = None
        self._token_keys: frozenset[str] = frozenset()
        self._rules: dict[str, Rule] = {}
        self._defaults: dict[str, Decision] = {}
        """One immutable ALLOW/UNKNOWN decision per observed field (most decisions)."""

    # --- construction -----------------------------------------------------------

    @classmethod
    def empty(cls) -> FilterEngine:
        """No rules: every input is ALLOW / UNKNOWN (B.5 #1 fallback)."""
        return cls(NO_RULESET, Policy())

    @classmethod
    def compile(
        cls, rules: Iterable[Rule], policy: Policy, *, expected_id: str | None = None
    ) -> FilterEngine:
        enabled = sorted(
            (r for r in rules if r.enabled and r.kind is not RuleKind.SELECTOR),
            key=lambda r: r.rule_id,
        )
        ruleset_id = ruleset_digest(enabled, policy)
        if expected_id is not None and ruleset_id != expected_id:
            raise RulesetError(f"ruleset digest {ruleset_id} != expected {expected_id}")
        engine = cls(ruleset_id, policy)
        engine._build(enabled)
        return engine

    def _build(self, rules: Sequence[Rule]) -> None:
        patterns: list[tuple[Rule, re.Pattern[str], tuple[str, ...], int]] = []
        token_use: Counter[str] = Counter()
        hosts: defaultdict[str, list[_Compiled]] = defaultdict(list)
        labels: defaultdict[str, list[_Compiled]] = defaultdict(list)
        paths: defaultdict[str, list[_Compiled]] = defaultdict(list)
        for rule in rules:
            if rule.rule_id in self._rules:
                raise RulesetError(f"duplicate rule id {rule.rule_id}")
            self._rules[rule.rule_id] = rule
            if rule.kind is RuleKind.URL_PATTERN:
                try:
                    compiled = compile_pattern(rule.pattern)
                except ValueError as exc:
                    raise RulesetError(f"{rule.rule_id}: {exc}") from exc
                patterns.append((rule, compiled.regex, compiled.tokens, compiled.literal_length))
                token_use.update(compiled.tokens)
            elif rule.kind in (RuleKind.HOST, RuleKind.REDIRECT):
                hosts[rule.pattern].append(self._entry(rule, 0))
            elif rule.kind is RuleKind.HOST_LABEL:
                labels[rule.pattern].append(self._entry(rule, 0))
            elif rule.kind is RuleKind.PATH:
                host, _, prefix = rule.pattern.partition("/")
                paths[host].append(self._entry(rule, 0, prefix="/" + prefix))
        tokens: defaultdict[str, list[_Compiled]] = defaultdict(list)
        generic: list[_Compiled] = []
        for rule, regex, rule_tokens, literal in patterns:
            entry = self._entry(rule, literal, regex=regex)
            if rule_tokens:
                # Prefer long, uncommon, rarely used tokens; short ones are a fallback key.
                best = min(
                    rule_tokens,
                    key=lambda t: (len(t) < 3, t in _COMMON_TOKENS, token_use[t], -len(t), t),
                )
                tokens[best].append(entry)
            else:
                generic.append(entry)
        self._hosts = {k: sorted(v, key=_by_key) for k, v in hosts.items()}
        self._labels = {k: sorted(v, key=_by_key) for k, v in labels.items()}
        self._paths = {k: sorted(v, key=_by_key) for k, v in paths.items()}
        self._tokens = {k: _Bucket.of(v) for k, v in tokens.items()}
        self._token_keys = frozenset(self._tokens)
        self._generic = _Bucket.of(generic) if generic else None
        self.untokenized = len(generic)
        self.rule_count = len(rules)
        self.counts = dict(sorted(Counter(r.kind.value for r in rules).items()))

    @staticmethod
    def _entry(
        rule: Rule, literal: int, *, regex: re.Pattern[str] | None = None, prefix: str = ""
    ) -> _Compiled:
        constrained = 0 if rule.source_domains else 1
        key = (
            KIND_RANK[rule.kind],
            constrained,
            -_length(rule, literal),
            CLASS_RANK[rule.classification],
            SOURCE_RANK[rule.source],
            rule.rule_id,
        )
        return _Compiled(rule, key, regex, prefix)

    # --- evaluation -------------------------------------------------------------

    def rule(self, rule_id: str) -> Rule | None:
        return self._rules.get(rule_id)

    def candidates(self, inp: FilterInput) -> list[Rule]:
        """Every enabled rule matching ``inp``, in precedence-key order."""
        found: list[_Compiled] = []
        host = inp.host
        labels = host.split(".")
        hosts, paths = self._hosts, self._paths
        # Host suffixes: "a.b.c" → "a.b.c", "b.c", "c".
        for i in range(len(labels)):
            suffix = ".".join(labels[i:]) if i else host
            entries = hosts.get(suffix)
            if entries:
                found.extend(e for e in entries if self._applies(e.rule, inp))
            if paths:
                entries = paths.get(suffix)
                if entries:
                    path = _path_of(inp.url)
                    found.extend(
                        e
                        for e in entries
                        if path.startswith(e.prefix) and self._applies(e.rule, inp)
                    )
        if self._labels:
            for label in dict.fromkeys(labels[:-1]):
                entries = self._labels.get(label)
                if entries:
                    found.extend(e for e in entries if self._applies(e.rule, inp))
        if self._tokens or self._generic:
            url = inp.url.lower()
            # Only tokens that index a bucket; the intersection runs in C.
            buckets = [
                self._tokens[t] for t in self._token_keys.intersection(TOKEN_RE.findall(url))
            ]
            if self._generic is not None:
                buckets.append(self._generic)
            for bucket in buckets:
                if bucket.prefilter.search(url):
                    found.extend(
                        e for e in bucket.entries if self._applies(e.rule, inp) and _search(e, url)
                    )
        found.sort(key=_by_key)
        return [e.rule for e in found]

    def decide(self, inp: FilterInput) -> Decision:
        return self._decide(inp, self.candidates(inp))

    def _decide(self, inp: FilterInput, matched: list[Rule]) -> Decision:
        if not matched:
            return self._default(inp)
        # Tier 0: operator rules with an explicit action override everything.
        for rule in matched:
            if rule.source is RuleSource.OPERATOR and rule.action is not None:
                others = [r for r in matched if r is not rule]
                return self._result(inp, rule, len(matched), others[0] if others else None)
        exceptions = [r for r in matched if r.exception]
        remaining = [r for r in matched if not r.exception]
        cancelled: Rule | None = None
        if exceptions:
            # An exception cancels non-important ad/tracker rules (ABP semantics).
            keep = []
            for rule in remaining:
                if rule.classification in BLOCK_CANDIDATE_CLASSES and not rule.important:
                    cancelled = cancelled or rule
                else:
                    keep.append(rule)
            remaining = keep
        important = [r for r in remaining if r.important]
        if important:
            return self._result(inp, important[0], len(matched), None)
        if remaining:
            return self._result(inp, remaining[0], len(matched), cancelled)
        return self._result(inp, exceptions[0], len(matched), cancelled)

    def _result(
        self, inp: FilterInput, rule: Rule, count: int, overridden: Rule | None
    ) -> Decision:
        if rule.exception:
            action = Action.ALLOW
            classification = Classification.UNKNOWN
        else:
            classification = rule.classification
            action = rule.action or self._mapped(inp, rule)
        return Decision(
            classification=classification,
            action=action,
            confidence=rule.confidence,
            rule_id=rule.rule_id,
            rule_source=_provenance(rule),
            matched_field=inp.field,
            matched_pattern=rule.pattern,
            reason=rule.reason,
            override_of=overridden.rule_id if overridden is not None else None,
            ruleset=self.ruleset_id,
            candidates=count,
        )

    def _mapped(self, inp: FilterInput, rule: Rule) -> Action:
        blockable = self.policy.block_classes.get(inp.context, frozenset())
        if rule.classification in blockable and rule.confidence >= self.policy.block_threshold:
            return Action.BLOCK
        return Action.CLASSIFY

    def _default(self, inp: FilterInput) -> Decision:
        decision = self._defaults.get(inp.field)
        if decision is None:
            decision = Decision(
                classification=Classification.UNKNOWN,
                action=Action.ALLOW,
                confidence=1.0,
                rule_id="default",
                rule_source="default",
                matched_field=inp.field,
                matched_pattern="",
                reason=None,
                override_of=None,
                ruleset=self.ruleset_id,
                candidates=0,
            )
            self._defaults[inp.field] = decision
        return decision

    @staticmethod
    def _applies(rule: Rule, inp: FilterInput) -> bool:
        if inp.context not in rule.contexts:
            return False
        kind = inp.resource_type
        if rule.resource_types and kind not in rule.resource_types:
            return False
        if kind in rule.excluded_types:
            return False
        if rule.party is not Party.ANY:
            if inp.third_party is None:
                return False
            if inp.third_party != (rule.party is Party.THIRD):
                return False
        if rule.source_domains or rule.excluded_source_domains:
            if inp.source_host is None:
                return not rule.source_domains
            if _in_domains(inp.source_host, rule.excluded_source_domains):
                return False
            if rule.source_domains and not _in_domains(inp.source_host, rule.source_domains):
                return False
        return True


def _provenance(rule: Rule) -> str:
    return f"{rule.source.value}@{rule.revision}" if rule.revision else rule.source.value


def _search(entry: _Compiled, url: str) -> bool:
    return entry.regex is not None and entry.regex.search(url) is not None


def _by_key(entry: _Compiled) -> tuple[int, int, int, int, int, str]:
    return entry.key


def _in_domains(host: str, domains: frozenset[str]) -> bool:
    if host in domains:
        return True
    labels = host.split(".")
    return any(".".join(labels[i:]) in domains for i in range(1, len(labels)))


def _path_of(url: str) -> str:
    start = url.find("://")
    slash = url.find("/", start + 3 if start != -1 else 0)
    return url[slash:] if slash != -1 else "/"


__all__ = ["NO_RULESET", "FilterEngine", "RulesetError", "ruleset_digest"]
