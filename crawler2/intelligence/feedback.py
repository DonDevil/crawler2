"""P11 match feedback: confirmed matches → bounded crawl priority (P11 design §4-§6).

Pure and deterministic: no I/O, no clock, no frontier. Inputs are
``MatchEvidence`` rows (one per (source host, match, page observation) that
carried the matching media) and the known target states. Outputs are an
integer priority boost per URL and, where evidence supports it, a preferred
starting capability.

Bounds and stability (design §5):

- Value of a host = sum over distinct targets of
  ``relevance(target) * max over its evidence of strength * exp(-age / tau)``.
  Many observations of one target cannot add up (max per target); many
  targets saturate through ``1 - exp(-value / saturation)``.
- Boost = ``round(max_source_boost * saturation)`` plus a pattern boost
  for URLs whose template already yielded matches on that host (at least
  ``min_pattern_support`` distinct pages), together never more than
  ``max_total_boost``. P11 never lowers a priority: cold and unproductive
  hosts keep the base priority (exploration is the default, not a mode).
- Every term decays by the same factor, so without new evidence the
  ordering of hosts by value never changes (no oscillation); boosts only
  shrink toward zero, and below ``neutral_value`` a host is neutral.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from crawler2.intelligence.what import tokenize_url


class TargetRelevance(StrEnum):
    ACTIVE = "active"
    UNKNOWN = "unknown"
    """No ``target.registered`` seen yet (events may arrive in any order)."""
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class FeedbackPolicy:
    """Every value is justified in design §5.2; changing one changes ``version``."""

    version: str = "feedback/v1"
    tau: timedelta = timedelta(days=14)
    saturation: float = 2.0
    max_source_boost: int = 15
    max_pattern_boost: int = 5
    max_total_boost: int = 20
    neutral_value: float = 0.05
    min_pattern_support: int = 2
    probable_weight: float = 0.5
    relevance_weight: tuple[tuple[TargetRelevance, float], ...] = (
        (TargetRelevance.ACTIVE, 1.0),
        (TargetRelevance.UNKNOWN, 0.5),
        (TargetRelevance.RETIRED, 0.1),
    )
    min_capability_evidence: float = 1.0
    explore_percent: int = 20
    """Share of URLs (by url_id hash) kept on http when browser is preferred."""
    max_evidence_age: timedelta = timedelta(days=90)

    def __post_init__(self) -> None:
        if not 0 <= self.max_source_boost <= self.max_total_boost <= 50:
            raise ValueError("boost bounds must satisfy 0 <= source <= total <= 50")
        if not 0 <= self.max_pattern_boost <= self.max_total_boost:
            raise ValueError("pattern boost must be within the total boost")
        if self.saturation <= 0 or self.tau <= timedelta(0):
            raise ValueError("saturation and tau must be positive")
        if not 0 <= self.explore_percent <= 100:
            raise ValueError("explore_percent must be 0..100")

    def relevance(self, state: TargetRelevance) -> float:
        return dict(self.relevance_weight)[state]


DEFAULT_FEEDBACK_POLICY = FeedbackPolicy()


@dataclass(frozen=True, slots=True)
class MatchEvidence:
    """One page observation that carried media matching a target (design §4.2)."""

    domain_id: str
    match_id: str
    page_observation_id: str
    target_id: str
    target_version: int
    decided_at: datetime
    confidence: float
    verdict: str
    page_url: str
    template: str | None
    page_kind: str
    """``listing`` when the URL alone is listing-like (WHAT ``UrlShape.structural``),
    else ``detail``; ``unknown`` for an unparsable URL."""
    capability: str | None
    """Fetch capability of the page observation (P1 ``FetchCapability``)."""


def page_shape(url: str) -> tuple[str | None, str]:
    """(template, page kind) of a page URL, from the P7 WHAT tokenizer."""
    shape = tokenize_url(url)
    if shape is None:
        return None, "unknown"
    return shape.template, "listing" if shape.structural else "detail"


def _strength(e: MatchEvidence, now: datetime, policy: FeedbackPolicy) -> float:
    age = now - e.decided_at
    if age > policy.max_evidence_age:
        return 0.0
    verdict = 1.0 if e.verdict == "confirmed" else policy.probable_weight
    confidence = min(max(e.confidence, 0.0), 1.0) if math.isfinite(e.confidence) else 0.0
    # Evidence from the future (clock skew) counts as fresh, never as more than fresh.
    return confidence * verdict * math.exp(-max(age, timedelta(0)) / policy.tau)


def value_of(
    evidence: Iterable[MatchEvidence],
    targets: Mapping[str, TargetRelevance],
    now: datetime,
    policy: FeedbackPolicy = DEFAULT_FEEDBACK_POLICY,
) -> float:
    """Distinct-target, relevance-weighted, decayed value (module docstring)."""
    best: dict[str, float] = {}
    for e in evidence:
        s = _strength(e, now, policy)
        if s > best.get(e.target_id, 0.0):
            best[e.target_id] = s
    return sum(
        policy.relevance(targets.get(t, TargetRelevance.UNKNOWN)) * s for t, s in best.items()
    )


def _saturated(value: float, policy: FeedbackPolicy) -> float:
    if not math.isfinite(value) or value < policy.neutral_value:
        return 0.0
    return 1.0 - math.exp(-value / policy.saturation)


@dataclass(frozen=True, slots=True)
class SourceFeedback:
    """Everything known about one host's productivity, evaluated at ``at``."""

    domain_id: str
    value: float
    boost: int
    patterns: Mapping[str, int]
    """Template → pattern boost, only templates with enough distinct pages."""
    capability_weight: Mapping[str, float]
    targets: int
    matches: int
    last_match_at: datetime | None


def assess_source(
    domain_id: str,
    evidence: Iterable[MatchEvidence],
    targets: Mapping[str, TargetRelevance],
    now: datetime,
    policy: FeedbackPolicy = DEFAULT_FEEDBACK_POLICY,
) -> SourceFeedback:
    rows = [e for e in evidence if e.domain_id == domain_id]
    value = value_of(rows, targets, now, policy)
    boost = round(policy.max_source_boost * _saturated(value, policy))
    by_template: dict[str, list[MatchEvidence]] = {}
    for e in rows:
        if e.template is not None:
            by_template.setdefault(e.template, []).append(e)
    patterns: dict[str, int] = {}
    for template, group in by_template.items():
        if len({e.page_observation_id for e in group}) < policy.min_pattern_support:
            continue
        p = round(
            policy.max_pattern_boost * _saturated(value_of(group, targets, now, policy), policy)
        )
        if p > 0:
            patterns[template] = p
    # Capability: decayed strength per distinct page observation (not per match).
    per_page: dict[tuple[str, str], float] = {}
    for e in rows:
        if e.capability is None:
            continue
        key = (e.capability, e.page_observation_id)
        per_page[key] = max(per_page.get(key, 0.0), _strength(e, now, policy))
    capability: dict[str, float] = {}
    for (cap, _), s in per_page.items():
        capability[cap] = capability.get(cap, 0.0) + s
    return SourceFeedback(
        domain_id=domain_id,
        value=value,
        boost=boost,
        patterns=patterns,
        capability_weight=capability,
        targets=len({e.target_id for e in rows}),
        matches=len({e.match_id for e in rows}),
        last_match_at=max((e.decided_at for e in rows), default=None),
    )


@dataclass(frozen=True, slots=True)
class PriorityDecision:
    """The effective admission priority of one URL and why (design §5.4)."""

    priority: int
    base: int
    source_boost: int = 0
    pattern_boost: int = 0
    capability: str | None = None
    """``browser`` when matches were reached only by browser; None = no opinion."""
    reason: str = ""
    fallback: str | None = None
    """Why no feedback was applied; None when the snapshot was used."""


def _explore(url_id: str, percent: int) -> bool:
    digest = hashlib.blake2b(url_id.encode(), digest_size=2).digest()
    return int.from_bytes(digest) % 100 < percent


def preferred_capability(
    source: SourceFeedback, url_id: str, policy: FeedbackPolicy = DEFAULT_FEEDBACK_POLICY
) -> tuple[str | None, str]:
    """``browser`` only when browser pages carried matches and http pages did not.

    A fixed share of URLs (``explore_percent``, by url_id) stays on http so a
    site that stops needing the browser is noticed; http evidence of matches
    always wins (it is the cheaper capability that demonstrably works).
    """
    browser = source.capability_weight.get("browser", 0.0)
    http = source.capability_weight.get("http", 0.0)
    if browser < policy.min_capability_evidence or http >= 0.25 * browser:
        return None, f"media via http {http:.2f}, browser {browser:.2f}"
    if _explore(url_id, policy.explore_percent):
        return None, "browser preferred; http re-test share"
    return "browser", f"media reached via browser {browser:.2f}, http {http:.2f}"


def decide(
    *,
    base: int,
    url: str,
    url_id: str,
    source: SourceFeedback | None,
    policy: FeedbackPolicy = DEFAULT_FEEDBACK_POLICY,
) -> PriorityDecision:
    """base + bounded boost, clamped to the P1 priority range 0..100."""
    if source is None or (source.boost == 0 and not source.patterns):
        cap, why = (
            (None, "no match evidence")
            if source is None
            else preferred_capability(source, url_id, policy)
        )
        return PriorityDecision(base, base, capability=cap, reason=why)
    template, _ = page_shape(url)
    pattern = source.patterns.get(template, 0) if template is not None else 0
    total = min(source.boost + pattern, policy.max_total_boost)
    pattern = total - source.boost  # source boost <= max_source_boost <= max_total_boost
    cap, why = preferred_capability(source, url_id, policy)
    priority = min(max(base + total, 0), 100)
    return PriorityDecision(
        priority,
        base,
        source_boost=source.boost,
        pattern_boost=pattern,
        capability=cap,
        reason=(
            f"value {source.value:.2f} over {source.targets} target(s)"
            f"{f'; pattern {template}' if pattern else ''}; {why}"
        ),
    )
