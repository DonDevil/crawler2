"""Source health and fetch profiles: P7 increment 1 (design §4).

Pure and deterministic: no I/O, no clock, no frontier. A host's profile is a
bounded fold of its ``fetch.completed`` facts. Its lifecycle state and the
advice (admit or probe-only, starting capability) are derived at query time
from the profile, the time and a versioned ``SourcePolicy``.

Order independence: every weight is an exponentially decayed sum
sum of exp(-(T - t_i) / tau), which is commutative, and every timestamp kept is a min or a
max. Folding the same facts in any order therefore gives the same profile
(up to float rounding). Duplicates must be removed by fact id before
folding (the consumer and the replay do this); ``fold`` itself counts what
it is given.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from antipiracy_contracts.models.web import FetchAttempt, FetchOutcome

from crawler2.filtering.inputs import host_of, registrable_domain


class OutcomeClass(StrEnum):
    """What one attempt says about its host (design §4.1)."""

    OK = "ok"
    NEEDS_JS = "needs_js"
    MOVED = "moved"
    HTTP_ERROR = "http_error"
    BLOCKED = "blocked"
    UNREACHABLE = "unreachable"
    OTHER = "other"


class SourceState(StrEnum):
    UNKNOWN = "unknown"
    ACTIVE = "active"
    MOVED = "moved"
    BLOCKED = "blocked"
    UNREACHABLE = "unreachable"
    ERROR = "error"
    DEGRADED = "degraded"


NEGATIVE_STATES = frozenset(
    {SourceState.MOVED, SourceState.BLOCKED, SourceState.UNREACHABLE, SourceState.ERROR}
)
"""States in which a host is fetched only when a probe is due."""

_HEALTH = (
    OutcomeClass.OK,
    OutcomeClass.MOVED,
    OutcomeClass.HTTP_ERROR,
    OutcomeClass.BLOCKED,
    OutcomeClass.UNREACHABLE,
)
"""Classes with a health weight; ``NEEDS_JS`` counts as ``OK`` (the host answered)."""

_NEGATIVE = {
    OutcomeClass.MOVED: SourceState.MOVED,
    OutcomeClass.BLOCKED: SourceState.BLOCKED,
    OutcomeClass.UNREACHABLE: SourceState.UNREACHABLE,
    OutcomeClass.HTTP_ERROR: SourceState.ERROR,
}


def evidence_weight(n: int, within: timedelta, tau: timedelta) -> float:
    """Smallest decayed weight of ``n`` facts that span at most ``within``."""
    return 1.0 + (n - 1) * math.exp(-within / tau)


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    """Every value is justified in design §4.5; changing one changes ``version``."""

    version: str = "source-health/v1"
    tau: timedelta = timedelta(hours=24)
    tau_cap: timedelta = timedelta(days=7)
    evidence_n: int = 3
    evidence_within: timedelta = timedelta(hours=12)
    dominance: float = 0.8
    active_window: timedelta = timedelta(hours=24)
    probe_min: timedelta = timedelta(hours=6)
    probe_max: timedelta = timedelta(days=7)
    probe_age_fraction: float = 0.25
    http_retest: timedelta = timedelta(days=7)

    @property
    def min_evidence(self) -> float:
        return evidence_weight(self.evidence_n, self.evidence_within, self.tau)

    @property
    def min_cap_evidence(self) -> float:
        return evidence_weight(self.evidence_n, self.evidence_within, self.tau_cap)


DEFAULT_POLICY = SourcePolicy()


@dataclass(frozen=True, slots=True)
class FetchFact:
    """One ``fetch.completed`` reduced to what source health uses."""

    fact_id: str
    domain_id: str
    at: datetime
    capability: str
    outcome: OutcomeClass
    moved_to: str | None = None


def _site(url: str | None) -> str | None:
    host = host_of(url) if url else None
    return registrable_domain(host) if host else None


def _code(detail: str | None) -> str | None:
    for part in (detail or "").split(";"):
        if part.startswith("outcome="):
            return part[len("outcome=") :]
    return None


def classify(
    *,
    outcome: str,
    http_status: int | None,
    detail: str | None,
    requested_url: str,
    landed_url: str | None,
) -> tuple[OutcomeClass, str | None]:
    """Outcome class of one attempt and, for a move, the other site.

    ``landed_url`` is where the attempt ended: the final URL of a response,
    or the last redirect location of a failed attempt.
    """
    site, landed = _site(requested_url), _site(landed_url)
    if landed is not None and site is not None and landed != site:
        return OutcomeClass.MOVED, landed
    if outcome == FetchOutcome.BLOCKED:
        return OutcomeClass.BLOCKED, None
    if outcome in (
        FetchOutcome.TIMEOUT,
        FetchOutcome.DNS_FAILURE,
        FetchOutcome.CONNECTION_FAILURE,
        FetchOutcome.TLS_FAILURE,
    ):
        # redirect_error / invalid_response travel as connection_failure (P4 §9):
        # the host answered, so they are not evidence of unreachability.
        if _code(detail) in ("redirect_error", "invalid_response"):
            return OutcomeClass.OTHER, None
        return OutcomeClass.UNREACHABLE, None
    if outcome == FetchOutcome.RESPONSE and http_status is not None:
        if _code(detail) == "needs_js":
            return OutcomeClass.NEEDS_JS, None
        if 200 <= http_status < 300 or http_status == 304:
            return OutcomeClass.OK, None
        if http_status >= 400:
            return OutcomeClass.HTTP_ERROR, None
    return OutcomeClass.OTHER, None


def fact_from_attempt(attempt: FetchAttempt) -> FetchFact:
    """The live adapter (``fetch.completed`` payload)."""
    landed = attempt.final.url if attempt.final is not None else None
    if landed is None and attempt.redirects:
        landed = attempt.redirects[-1].location.url
    cls, moved_to = classify(
        outcome=attempt.outcome.value,
        http_status=attempt.http_status,
        detail=attempt.detail,
        requested_url=attempt.requested.url,
        landed_url=landed,
    )
    return FetchFact(
        fact_id=str(attempt.fetch_attempt_id),
        domain_id=str(attempt.requested.domain_id),
        at=attempt.finished_at,
        capability=attempt.capability.value,
        outcome=cls,
        moved_to=moved_to,
    )


def fact_from_export(row: Mapping[str, Any]) -> FetchFact:
    """The replay adapter (``benchmarks/p7-intelligence/export_m1.py`` rows).

    The export keeps the final URL but not the redirect hops, so a move that
    ends in a failure is classified from its outcome (design §5.3).
    """
    cls, moved_to = classify(
        outcome=row["outcome"],
        http_status=row["http_status"],
        detail=row["detail"],
        requested_url=row["url"],
        landed_url=row["final_url"],
    )
    return FetchFact(
        fact_id=row["id"],
        domain_id=row["domain_id"],
        at=datetime.fromisoformat(row["finished_at"].replace("Z", "+00:00")),
        capability=row["capability"],
        outcome=cls,
        moved_to=moved_to,
    )


@dataclass(frozen=True, slots=True)
class SourceProfile:
    """Bounded per-host state (design §4.2): fixed size whatever the history."""

    domain_id: str
    ref_at: datetime
    health: tuple[float, ...] = (0.0,) * len(_HEALTH)
    http_ok: float = 0.0
    http_needs_js: float = 0.0
    browser_ok: float = 0.0
    facts: int = 0
    first_seen: datetime | None = None
    last_ok: datetime | None = None
    last_attempt: datetime | None = None
    last_http_attempt: datetime | None = None
    moved_to: str | None = None
    moved_at: datetime | None = None
    policy: str = field(default=DEFAULT_POLICY.version)


def _max(a: datetime | None, b: datetime) -> datetime:
    return b if a is None or b > a else a


def _min(a: datetime | None, b: datetime) -> datetime:
    return b if a is None or b < a else a


def fold(
    profile: SourceProfile | None, fact: FetchFact, policy: SourcePolicy = DEFAULT_POLICY
) -> SourceProfile:
    """Add one fact. Commutative across facts (module docstring)."""
    if profile is None:
        profile = SourceProfile(domain_id=fact.domain_id, ref_at=fact.at, policy=policy.version)
    if fact.domain_id != profile.domain_id:
        raise ValueError(f"fact for {fact.domain_id} folded into {profile.domain_id}")
    if fact.at > profile.ref_at:
        shift, shift_cap = (
            math.exp(-(fact.at - profile.ref_at) / policy.tau),
            math.exp(-(fact.at - profile.ref_at) / policy.tau_cap),
        )
        add, add_cap, ref = 1.0, 1.0, fact.at
    else:
        shift = shift_cap = 1.0
        add = math.exp(-(profile.ref_at - fact.at) / policy.tau)
        add_cap = math.exp(-(profile.ref_at - fact.at) / policy.tau_cap)
        ref = profile.ref_at
    health_cls = OutcomeClass.OK if fact.outcome is OutcomeClass.NEEDS_JS else fact.outcome
    health = tuple(
        w * shift + (add if cls is health_cls else 0.0)
        for cls, w in zip(_HEALTH, profile.health, strict=True)
    )
    is_http = fact.capability == "http"
    answered = fact.outcome in (OutcomeClass.OK, OutcomeClass.NEEDS_JS)
    moved_to, moved_at = profile.moved_to, profile.moved_at
    if fact.moved_to is not None and (
        moved_at is None or (fact.at, fact.moved_to) > (moved_at, moved_to or "")
    ):
        moved_to, moved_at = fact.moved_to, fact.at
    return replace(
        profile,
        ref_at=ref,
        health=health,
        http_ok=profile.http_ok * shift_cap
        + (add_cap if is_http and fact.outcome is OutcomeClass.OK else 0.0),
        http_needs_js=profile.http_needs_js * shift_cap
        + (add_cap if is_http and fact.outcome is OutcomeClass.NEEDS_JS else 0.0),
        browser_ok=profile.browser_ok * shift_cap
        + (add_cap if fact.capability == "browser" and answered else 0.0),
        facts=profile.facts + 1,
        first_seen=_min(profile.first_seen, fact.at),
        last_ok=_max(profile.last_ok, fact.at) if answered else profile.last_ok,
        last_attempt=_max(profile.last_attempt, fact.at),
        last_http_attempt=_max(profile.last_http_attempt, fact.at)
        if is_http
        else profile.last_http_attempt,
        moved_to=moved_to,
        moved_at=moved_at,
    )


def weights_at(
    profile: SourceProfile, now: datetime, policy: SourcePolicy = DEFAULT_POLICY
) -> dict[OutcomeClass, float]:
    """Health weights decayed to ``now`` (never grown for a ``now`` in the past)."""
    factor = math.exp(-max(timedelta(0), now - profile.ref_at) / policy.tau)
    return {cls: w * factor for cls, w in zip(_HEALTH, profile.health, strict=True)}


@dataclass(frozen=True, slots=True)
class Assessment:
    state: SourceState
    reason: str
    weights: Mapping[OutcomeClass, float]


def assess(
    profile: SourceProfile | None, now: datetime, policy: SourcePolicy = DEFAULT_POLICY
) -> Assessment:
    """Lifecycle state, derived (design §4.3); evaluated in table order."""
    if profile is None or profile.last_attempt is None:
        return Assessment(SourceState.UNKNOWN, "no attempts", {})
    weights = weights_at(profile, now, policy)
    if profile.last_ok is not None and now - profile.last_ok <= policy.active_window:
        return Assessment(SourceState.ACTIVE, f"answered at {profile.last_ok:%FT%TZ}", weights)
    total = sum(weights.values())
    for cls, state in _NEGATIVE.items():
        w = weights[cls]
        if w >= policy.min_evidence and w >= policy.dominance * total:
            extra = f" to {profile.moved_to}" if state is SourceState.MOVED else ""
            return Assessment(state, f"{cls.value}{extra}: {w:.2f} of {total:.2f}", weights)
    return Assessment(SourceState.DEGRADED, f"mixed evidence ({total:.2f})", weights)


@dataclass(frozen=True, slots=True)
class Advice:
    """What admission may do with a URL of this host now (design §4.3, §4.4)."""

    admit: bool
    probe: bool
    capability: str
    state: SourceState
    reason: str
    next_probe_at: datetime | None = None
    policy: str = DEFAULT_POLICY.version


def probe_interval(
    profile: SourceProfile, now: datetime, policy: SourcePolicy = DEFAULT_POLICY
) -> timedelta:
    anchor = profile.last_ok or profile.first_seen or now
    interval = (now - anchor) * policy.probe_age_fraction
    return min(max(interval, policy.probe_min), policy.probe_max)


def capability(
    profile: SourceProfile | None, now: datetime, policy: SourcePolicy = DEFAULT_POLICY
) -> tuple[str, str]:
    """Starting capability and why: browser only on repeated escalation evidence."""
    if profile is None:
        return "http", "no evidence"
    factor = math.exp(-max(timedelta(0), now - profile.ref_at) / policy.tau_cap)
    needs_js, http_ok = profile.http_needs_js * factor, profile.http_ok * factor
    browser_ok = profile.browser_ok * factor
    if not (
        needs_js >= policy.min_cap_evidence and needs_js >= http_ok and browser_ok >= 1.0 * factor
    ):
        return "http", f"needs_js {needs_js:.2f} vs http ok {http_ok:.2f}"
    last_http = profile.last_http_attempt
    if last_http is None or now - last_http >= policy.http_retest:
        return "http", "browser preferred, but http is due for a re-test"
    return "browser", f"needs_js {needs_js:.2f} ≥ http ok {http_ok:.2f}, browser ok"


def advise(
    profile: SourceProfile | None, now: datetime, policy: SourcePolicy = DEFAULT_POLICY
) -> Advice:
    """Admission advice for one host: never a permanent refusal (design §4.3)."""
    status = assess(profile, now, policy)
    cap, why = capability(profile, now, policy)
    if profile is None or status.state not in NEGATIVE_STATES:
        return Advice(
            True, False, cap, status.state, f"{status.reason}; {why}", None, policy.version
        )
    last = profile.last_attempt or now  # a negative state always has facts
    due = last + probe_interval(profile, now, policy)
    if now >= due:
        return Advice(
            True, True, cap, status.state, f"probe due; {status.reason}", due, policy.version
        )
    return Advice(
        False,
        False,
        cap,
        status.state,
        f"probe at {due:%FT%TZ}; {status.reason}",
        due,
        policy.version,
    )
