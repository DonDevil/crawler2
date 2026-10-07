"""P7 increment 1: source health and fetch profiles (design §4)."""

from __future__ import annotations

import ast
import random
from datetime import timedelta
from pathlib import Path

import pytest
from antipiracy_contracts.models.web import FetchOutcome, RedirectHop, UrlRef
from hypothesis import given, settings
from hypothesis import strategies as st

from crawler2.intelligence.sources import (
    DEFAULT_POLICY,
    FetchFact,
    OutcomeClass,
    SourceProfile,
    SourceState,
    advise,
    assess,
    capability,
    classify,
    fact_from_attempt,
    fact_from_export,
    fold,
)
from tests.fixtures.contracts import T0, fetch_attempt

H = timedelta(hours=1)
D = timedelta(days=1)
REQ = "https://www.site.example/film/"


def fact(at_h: float, outcome: OutcomeClass, cap: str = "http", n: int = 0) -> FetchFact:
    return FetchFact(f"f{at_h}-{n}-{outcome}", "dom", T0 + at_h * H, cap, outcome)


def build(*facts: FetchFact) -> SourceProfile | None:
    profile = None
    for f in facts:
        profile = fold(profile, f)
    return profile


# --- classification --------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "status", "detail", "landed", "expected"),
    [
        ("response", 200, "outcome=ok", REQ, OutcomeClass.OK),
        ("response", 304, "outcome=not_modified", REQ, OutcomeClass.OK),
        ("response", 200, "outcome=ok", "https://site.example/x", OutcomeClass.OK),  # same site
        ("response", 200, "outcome=needs_js", REQ, OutcomeClass.NEEDS_JS),
        ("response", 404, "outcome=http_error", REQ, OutcomeClass.HTTP_ERROR),
        ("response", 525, "outcome=http_error", REQ, OutcomeClass.HTTP_ERROR),
        ("blocked", 429, "outcome=blocked;signals=status:429", None, OutcomeClass.BLOCKED),
        ("timeout", None, "outcome=timeout", None, OutcomeClass.UNREACHABLE),
        ("dns_failure", None, "outcome=dns_error", None, OutcomeClass.UNREACHABLE),
        ("connection_failure", None, "outcome=redirect_error", None, OutcomeClass.OTHER),
        ("too_large", None, "outcome=too_large", None, OutcomeClass.OTHER),
        ("response", 200, "outcome=ok", "https://mirror.example/", OutcomeClass.MOVED),
        ("response", 404, None, "https://parked.example/", OutcomeClass.MOVED),
    ],
)
def test_classify(
    outcome: str, status: int | None, detail: str | None, landed: str | None, expected: OutcomeClass
) -> None:
    cls, moved_to = classify(
        outcome=outcome, http_status=status, detail=detail, requested_url=REQ, landed_url=landed
    )
    assert cls is expected
    assert (moved_to is not None) == (expected is OutcomeClass.MOVED)


def test_live_adapter_sees_a_move_through_redirect_hops() -> None:
    target = UrlRef.of("https://old-mirror.example/")
    attempt = fetch_attempt(target, outcome=FetchOutcome.CONNECTION_FAILURE).model_copy(
        update={
            "redirects": (
                RedirectHop(location=UrlRef.of("https://new-mirror.example/"), status=301),
            )
        }
    )
    f = fact_from_attempt(attempt)
    assert f.outcome is OutcomeClass.MOVED
    assert f.moved_to == "new-mirror.example"
    assert f.domain_id == str(target.domain_id)
    ok = fact_from_attempt(fetch_attempt(target))
    assert ok.outcome is OutcomeClass.OK


def test_export_adapter() -> None:
    row = {
        "id": "fat_1",
        "domain_id": "dom_1",
        "url": REQ,
        "final_url": None,
        "finished_at": "2026-10-04T10:00:00.5Z",
        "capability": "http",
        "outcome": "timeout",
        "detail": "outcome=timeout;error=TimeoutError",
        "http_status": None,
    }
    f = fact_from_export(row)
    assert f.outcome is OutcomeClass.UNREACHABLE
    assert f.at.tzinfo is not None


# --- folding: bounded, order- and delivery-independent -----------------------------

_facts = st.lists(
    st.builds(
        fact,
        st.floats(min_value=0, max_value=24 * 30),
        st.sampled_from(list(OutcomeClass)),
        st.sampled_from(["http", "browser"]),
        st.integers(0, 3),
    ),
    min_size=1,
    max_size=40,
    unique_by=lambda f: f.fact_id,
)


@settings(max_examples=200, deadline=None)
@given(_facts, st.randoms(use_true_random=False))
def test_fold_is_order_independent(facts: list[FetchFact], rnd: random.Random) -> None:
    shuffled = facts[:]
    rnd.shuffle(shuffled)
    a, b = build(*facts), build(*shuffled)
    assert a is not None
    assert b is not None
    assert a.health == pytest.approx(b.health, rel=1e-9, abs=1e-12)
    assert (a.http_ok, a.http_needs_js, a.browser_ok) == pytest.approx(
        (b.http_ok, b.http_needs_js, b.browser_ok), rel=1e-9, abs=1e-12
    )
    assert (a.ref_at, a.first_seen, a.last_ok, a.last_attempt, a.facts) == (
        b.ref_at,
        b.first_seen,
        b.last_ok,
        b.last_attempt,
        b.facts,
    )
    now = max(f.at for f in facts) + timedelta(hours=7)
    assert advise(a, now) == advise(b, now)


def test_profile_size_is_bounded() -> None:
    p = build(*(fact(h * 0.5, OutcomeClass.UNREACHABLE, n=h) for h in range(5_000)))
    assert p is not None
    assert p.facts == 5_000
    assert len(p.health) == 5  # fixed size whatever the history


def test_fold_rejects_a_fact_of_another_host() -> None:
    p = build(fact(0, OutcomeClass.OK))
    other = FetchFact("x", "other", T0, "http", OutcomeClass.OK)
    with pytest.raises(ValueError, match="other"):
        fold(p, other)


# --- lifecycle, negative knowledge, reactivation -----------------------------------


def test_unknown_and_active() -> None:
    assert assess(None, T0).state is SourceState.UNKNOWN
    advice = advise(None, T0)
    assert advice.admit
    assert advice.capability == "http"
    p = build(fact(0, OutcomeClass.OK))
    assert assess(p, T0 + 23 * H).state is SourceState.ACTIVE
    assert assess(p, T0 + 25 * H).state is SourceState.DEGRADED


def test_three_failures_within_12h_make_a_host_unreachable_two_do_not() -> None:
    two = build(fact(0, OutcomeClass.UNREACHABLE), fact(6, OutcomeClass.UNREACHABLE, n=1))
    assert assess(two, T0 + 6 * H).state is SourceState.DEGRADED
    three = build(*(fact(h, OutcomeClass.UNREACHABLE, n=h) for h in (0, 6, 12)))
    assert assess(three, T0 + 12 * H).state is SourceState.UNREACHABLE
    spread = build(*(fact(h, OutcomeClass.UNREACHABLE, n=h) for h in (0, 24, 48)))
    assert assess(spread, T0 + 48 * H).state is SourceState.DEGRADED  # too spread out


def test_a_recent_success_overrides_failures_and_dominance_needs_80_percent() -> None:
    p = build(
        fact(0, OutcomeClass.OK), *(fact(h, OutcomeClass.UNREACHABLE, n=h) for h in (1, 2, 3))
    )
    assert assess(p, T0 + 3 * H).state is SourceState.ACTIVE
    # 30 h later the success is outside the active window; 1 ok in 4 keeps it out of
    # the negative state only while its weight is > 20 %
    mixed = build(
        fact(0, OutcomeClass.OK),
        fact(1, OutcomeClass.UNREACHABLE, n=1),
        fact(2, OutcomeClass.UNREACHABLE, n=2),
    )
    assert assess(mixed, T0 + 30 * H).state is SourceState.DEGRADED


@pytest.mark.parametrize(
    ("cls", "state"),
    [
        (OutcomeClass.BLOCKED, SourceState.BLOCKED),
        (OutcomeClass.HTTP_ERROR, SourceState.ERROR),
        (OutcomeClass.UNREACHABLE, SourceState.UNREACHABLE),
    ],
)
def test_negative_states(cls: OutcomeClass, state: SourceState) -> None:
    p = build(*(fact(h, cls, n=h) for h in range(4)))
    assert assess(p, T0 + 4 * H).state is state


def test_moved_state_reports_the_latest_target() -> None:
    facts = [
        FetchFact(f"m{i}", "dom", T0 + i * H, "http", OutcomeClass.MOVED, f"mirror{i}.example")
        for i in range(3)
    ]
    p = build(*reversed(facts))
    assert p is not None
    assert p.moved_to == "mirror2.example"
    status = assess(p, T0 + 3 * H)
    assert status.state is SourceState.MOVED
    assert "mirror2.example" in status.reason


def test_probe_only_schedule_reactivation_and_no_permanent_death() -> None:
    dead = build(*(fact(h, OutcomeClass.UNREACHABLE, n=h) for h in (0, 1, 2)))
    assert dead is not None
    early = advise(dead, T0 + 4 * H)
    assert not early.admit  # suppressed until the probe is due
    assert early.next_probe_at == T0 + 2 * H + 6 * H  # minimum interval 6 h
    due = advise(dead, T0 + 8 * H)
    assert due.admit
    assert due.probe
    # the interval grows with the time since the host last answered, capped at 7 days
    later = fold(dead, fact(24 * 40, OutcomeClass.UNREACHABLE, n=99))
    later = fold(later, fact(24 * 40 + 1, OutcomeClass.UNREACHABLE, n=98))
    later = fold(later, fact(24 * 40 + 2, OutcomeClass.UNREACHABLE, n=97))
    advice = advise(later, T0 + (24 * 40 + 3) * H)
    assert advice.state is SourceState.UNREACHABLE
    assert advice.next_probe_at == T0 + (24 * 40 + 2) * H + 7 * D
    # one successful probe reactivates at once
    back = fold(later, fact(24 * 48, OutcomeClass.OK, n=1))
    assert advise(back, T0 + (24 * 48) * H).state is SourceState.ACTIVE


def test_negative_evidence_ages_out() -> None:
    dead = build(*(fact(h, OutcomeClass.BLOCKED, n=h) for h in (0, 1, 2)))
    assert assess(dead, T0 + 3 * H).state is SourceState.BLOCKED
    assert assess(dead, T0 + 4 * D).state is SourceState.DEGRADED  # ~3 tau later


def test_a_query_time_before_the_last_fact_does_not_grow_weights() -> None:
    p = build(*(fact(h, OutcomeClass.UNREACHABLE, n=h) for h in (10, 11, 12)))
    assert assess(p, T0).state is SourceState.UNREACHABLE  # no negative decay


# --- fetch profile (HOW) -----------------------------------------------------------


def test_browser_first_needs_repeated_escalation_and_a_browser_success() -> None:
    js = [fact(h, OutcomeClass.NEEDS_JS, "http", n=h) for h in (0, 1, 2)]
    assert capability(build(*js), T0 + 3 * H)[0] == "http"  # no browser success yet
    p = build(*js, fact(3, OutcomeClass.OK, "browser"))
    assert capability(p, T0 + 4 * H)[0] == "browser"
    assert advise(p, T0 + 4 * H).capability == "browser"
    # http pages that work keep the host on http
    mostly_http = build(*js, *(fact(h, OutcomeClass.OK, "http", n=h) for h in range(4, 9)))
    assert capability(mostly_http, T0 + 9 * H)[0] == "http"


def test_browser_preference_is_retested_on_http_after_seven_days() -> None:
    # strong escalation evidence that is still above threshold after 7 days
    p = build(
        *(fact(h * 0.5, OutcomeClass.NEEDS_JS, "http", n=h) for h in range(10)),
        *(fact(h, OutcomeClass.OK, "browser", n=h) for h in range(6, 24 * 7, 12)),
    )
    assert capability(p, T0 + 6 * D)[0] == "browser"
    cap, why = capability(p, T0 + 7 * D + 6 * H)
    assert cap == "http"
    assert "re-test" in why


def test_weak_escalation_evidence_decays_back_to_http() -> None:
    js = [fact(h, OutcomeClass.NEEDS_JS, "http", n=h) for h in (0, 1, 2)]
    assert capability(build(*js, fact(3, OutcomeClass.OK, "browser")), T0 + 4 * H)[0] == "browser"
    p = build(*js, *(fact(h, OutcomeClass.OK, "browser", n=h) for h in range(3, 24 * 8, 12)))
    cap, why = capability(p, T0 + 8 * D)
    assert cap == "http"
    assert "needs_js" in why  # below the evidence threshold again


# --- boundaries ----------------------------------------------------------------------


def test_intelligence_package_stays_off_the_runtime_and_storage_paths() -> None:
    """B.5 #1 and design §3: no frontier, Redis, Scylla, worker or P6 store imports."""
    # Increment 2 adds the projection, its P7-owned store and the CLI (P2 storage
    # allowed). No P7 module may reach the frontier, workers, discovery or Redis
    # (design §3, ADR-023), and the model itself stays pure.
    everywhere = ("crawler2.frontier", "crawler2.crawlers", "crawler2.discovery", "redis")
    pure = (*everywhere, "crawler2.storage", "crawler2.filtering.store", "cassandra")
    root = Path(__file__).resolve().parents[3] / "crawler2" / "intelligence"
    for path in root.glob("*.py"):
        forbidden = pure if path.name in ("sources.py", "advice.py") else everywhere
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                assert not name.startswith(forbidden), f"{path.name} imports {name}"


def test_policy_constants_match_the_design() -> None:
    assert DEFAULT_POLICY.version == "source-health/v1"
    assert DEFAULT_POLICY.min_evidence == pytest.approx(2.213, abs=1e-3)
    assert DEFAULT_POLICY.min_cap_evidence == pytest.approx(2.862, abs=1e-3)
