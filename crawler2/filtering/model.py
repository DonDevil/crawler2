"""P6 filter vocabulary: rules, inputs and explainable decisions (design §3, §5).

Everything here is pure data. A ``Rule`` states what a match *means*
(classification, confidence, optional explicit action) and where it came
from (source, revision, origin); the engine only orders and applies rules.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Final

ENGINE_SEMANTICS: Final = "p6-filter/v1"
"""Bumped whenever the same rules could produce a different decision."""


class Classification(StrEnum):
    CONTENT = "content"
    MEDIA = "media"
    PLAYER = "player"
    NAVIGATION = "navigation"
    AD = "ad"
    TRACKER = "tracker"
    UNKNOWN = "unknown"


class Action(StrEnum):
    ALLOW = "allow"
    CLASSIFY = "classify"
    BLOCK = "block"


class Context(StrEnum):
    LINK = "link"
    """A discovered link or a seed/search URL being considered for admission."""
    REQUEST = "request"
    """A browser sub-request (P4 interception)."""
    REDIRECT = "redirect"
    """A hop or the final URL of a recorded redirect chain."""


ALL_CONTEXTS: Final = frozenset(Context)


class RuleKind(StrEnum):
    HOST = "host"
    HOST_LABEL = "host_label"
    PATH = "path"
    URL_PATTERN = "url_pattern"
    REDIRECT = "redirect"
    SELECTOR = "selector"


class RuleSource(StrEnum):
    BUILT_IN = "built_in"
    V1_BLACKLIST = "v1_blacklist"
    EASYLIST = "easylist"
    EASYPRIVACY = "easyprivacy"
    UBLOCK = "ublock"
    OPERATOR = "operator"


class Party(StrEnum):
    ANY = "any"
    FIRST = "first"
    THIRD = "third"


RESOURCE_TYPES: Final = frozenset(
    {
        "document",
        "subdocument",
        "script",
        "image",
        "stylesheet",
        "xmlhttprequest",
        "media",
        "font",
        "ping",
        "websocket",
        "popup",
        "other",
    }
)
"""ABP resource-type vocabulary; Playwright types are mapped onto it (``resource_type_of``)."""

_PLAYWRIGHT_TYPES: Final = {
    "xhr": "xmlhttprequest",
    "fetch": "xmlhttprequest",
    "eventsource": "xmlhttprequest",
    "texttrack": "other",
    "manifest": "other",
}

BLOCK_CANDIDATE_CLASSES: Final = frozenset({Classification.AD, Classification.TRACKER})
"""Classes an ABP-style exception (``@@``) can cancel."""

# Ties on specificity go to the protective class (design §6.2 step 3).
CLASS_RANK: Final = {
    Classification.MEDIA: 0,
    Classification.PLAYER: 1,
    Classification.CONTENT: 2,
    Classification.NAVIGATION: 3,
    Classification.TRACKER: 4,
    Classification.AD: 5,
    Classification.UNKNOWN: 6,
}
SOURCE_RANK: Final = {
    RuleSource.OPERATOR: 0,
    RuleSource.BUILT_IN: 1,
    RuleSource.EASYLIST: 2,
    RuleSource.EASYPRIVACY: 2,
    RuleSource.UBLOCK: 2,
    RuleSource.V1_BLACKLIST: 3,
}
KIND_RANK: Final = {
    RuleKind.REDIRECT: 0,
    RuleKind.PATH: 1,
    RuleKind.HOST: 2,
    RuleKind.URL_PATTERN: 3,
    RuleKind.HOST_LABEL: 4,
}


def resource_type_of(playwright_type: str) -> str:
    """Playwright resource type → ABP vocabulary (unknown types are ``other``)."""
    kind = _PLAYWRIGHT_TYPES.get(playwright_type, playwright_type)
    return kind if kind in RESOURCE_TYPES else "other"


def _sorted(values: frozenset[Any]) -> list[str]:
    return sorted(str(v) for v in values)


@dataclass(frozen=True, slots=True)
class Rule:
    """One filter rule. ``rule_id`` is derived from what the rule *matches*."""

    source: RuleSource
    kind: RuleKind
    pattern: str
    """host (HOST, REDIRECT), label (HOST_LABEL), ``host/prefix`` (PATH),
    ABP pattern body (URL_PATTERN) or CSS selector (SELECTOR)."""
    classification: Classification
    confidence: float
    action: Action | None = None
    """Explicit action; ``None`` = derived from the ruleset policy (design §6.3)."""
    exception: bool = False
    important: bool = False
    resource_types: frozenset[str] = frozenset()
    """Empty = every type (uBO semantics: documents included)."""
    excluded_types: frozenset[str] = frozenset()
    party: Party = Party.ANY
    contexts: frozenset[Context] = ALL_CONTEXTS
    source_domains: frozenset[str] = frozenset()
    excluded_source_domains: frozenset[str] = frozenset()
    reason: str | None = None
    enabled: bool = True
    """False = quarantined: stored and reported, never compiled."""
    revision: str = ""
    """Source revision this rule was imported in (set by the store)."""
    origin: str = ""
    """Original text and position, e.g. ``line 12: ||ads.example^$third-party``."""
    note: str | None = None
    rule_id: str = field(init=False, repr=False, compare=False, default="")
    """Derived from ``match_body()`` in ``__post_init__`` (stable across imports)."""

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be 0..1, got {self.confidence}")
        if not self.pattern:
            raise ValueError("pattern must not be empty")
        unknown = (self.resource_types | self.excluded_types) - RESOURCE_TYPES
        if unknown:
            raise ValueError(f"unknown resource types: {sorted(unknown)}")
        if not self.contexts:
            raise ValueError("a rule must apply to at least one context")
        if self.exception and self.action not in (None, Action.ALLOW):
            raise ValueError("an exception rule can only allow")
        body = json.dumps(self.match_body(), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(body.encode()).hexdigest()[:16]
        object.__setattr__(self, "rule_id", f"{self.source.value}/{self.kind.value}/{digest}")

    def match_body(self) -> dict[str, Any]:
        """What the rule matches; the identity of the rule within its source."""
        return {
            "source": self.source.value,
            "kind": self.kind.value,
            "pattern": self.pattern,
            "exception": self.exception,
            "important": self.important,
            "types": _sorted(self.resource_types),
            "not_types": _sorted(self.excluded_types),
            "party": self.party.value,
            "contexts": _sorted(self.contexts),
            "domains": _sorted(self.source_domains),
            "not_domains": _sorted(self.excluded_source_domains),
        }

    def to_doc(self) -> dict[str, Any]:
        """Canonical, complete serialization (storage and ruleset digests)."""
        return {
            **self.match_body(),
            "rule_id": self.rule_id,
            "classification": self.classification.value,
            "confidence": self.confidence,
            "action": self.action.value if self.action else None,
            "reason": self.reason,
            "enabled": self.enabled,
            "revision": self.revision,
            "origin": self.origin,
            "note": self.note,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> Rule:
        rule = cls(
            source=RuleSource(doc["source"]),
            kind=RuleKind(doc["kind"]),
            pattern=doc["pattern"],
            classification=Classification(doc["classification"]),
            confidence=float(doc["confidence"]),
            action=Action(doc["action"]) if doc.get("action") else None,
            exception=bool(doc["exception"]),
            important=bool(doc["important"]),
            resource_types=frozenset(doc["types"]),
            excluded_types=frozenset(doc["not_types"]),
            party=Party(doc["party"]),
            contexts=frozenset(Context(c) for c in doc["contexts"]),
            source_domains=frozenset(doc["domains"]),
            excluded_source_domains=frozenset(doc["not_domains"]),
            reason=doc.get("reason"),
            enabled=bool(doc["enabled"]),
            revision=doc.get("revision", ""),
            origin=doc.get("origin", ""),
            note=doc.get("note"),
        )
        if "rule_id" in doc and doc["rule_id"] != rule.rule_id:
            raise ValueError(f"rule_id mismatch: stored {doc['rule_id']}, derived {rule.rule_id}")
        return rule

    def with_revision(self, revision: str) -> Rule:
        return replace(self, revision=revision)


@dataclass(frozen=True, slots=True)
class Policy:
    """How a winning rule without an explicit action becomes BLOCK or CLASSIFY (§6.3).

    Part of the ruleset (and its id), so a decision is reproducible from the
    ruleset id alone.
    """

    block_threshold: float = 0.9
    block_classes: dict[Context, frozenset[Classification]] = field(
        default_factory=lambda: {c: BLOCK_CANDIDATE_CLASSES for c in Context}
    )

    def to_doc(self) -> dict[str, Any]:
        return {
            "block_threshold": self.block_threshold,
            "block_classes": {c.value: _sorted(v) for c, v in sorted(self.block_classes.items())},
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> Policy:
        return cls(
            block_threshold=float(doc["block_threshold"]),
            block_classes={
                Context(c): frozenset(Classification(x) for x in v)
                for c, v in doc["block_classes"].items()
            },
        )


@dataclass(frozen=True, slots=True)
class FilterInput:
    """One thing to classify; built by the helpers in ``crawler2.filtering.inputs``."""

    context: Context
    url: str
    host: str
    resource_type: str
    source_host: str | None = None
    """First-party side: the linking page, the requesting frame's page, or the requested URL."""
    third_party: bool | None = None
    """Registrable domains differ; None when there is no first party (seeds, search)."""
    field: str = "url"
    """Name of the observed field, e.g. ``redirect_hop[1]`` (explanations only)."""


@dataclass(frozen=True, slots=True)
class Decision:
    """An explainable filter decision (design §3)."""

    classification: Classification
    action: Action
    confidence: float
    rule_id: str
    rule_source: str
    """``source@revision`` of the winning rule, ``default`` when nothing matched."""
    matched_field: str
    matched_pattern: str
    reason: str | None
    override_of: str | None
    """The best rule this decision overrode (operator override or exception)."""
    ruleset: str
    candidates: int

    def to_doc(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "action": self.action.value,
            "confidence": self.confidence,
            "rule_id": self.rule_id,
            "rule_source": self.rule_source,
            "matched_field": self.matched_field,
            "matched_pattern": self.matched_pattern,
            "reason": self.reason,
            "override_of": self.override_of,
            "ruleset": self.ruleset,
            "candidates": self.candidates,
        }
