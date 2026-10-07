"""P7 increment 2: source advice for P6 admission (design §9.4).

Every way P7 can be absent or wrong must give the fallback (admit, http)."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import pytest
from antipiracy_contracts.models.web import FetchCapability, FetchOutcome

from crawler2.intelligence.advice import (
    ProfileSnapshot,
    SnapshotAdvisor,
    SourceAdvice,
    load_snapshot,
)
from crawler2.intelligence.projection import ProjectionState, StoredProfile
from crawler2.intelligence.sources import DEFAULT_POLICY, SourceProfile, fact_from_attempt, fold
from crawler2.storage.errors import StorageUnavailableError
from crawler2.storage.layout import outbox_bucket
from tests.unit.intelligence.fakes import T0, MemoryProfileStore, attempt

NOW = T0 + timedelta(hours=3)
FALLBACK = (True, "http", True)  # admit, http, and marked as a fallback


def profile(host: str, *attempts: object) -> SourceProfile:
    p = None
    for a in attempts:
        p = fold(p, fact_from_attempt(a))  # type: ignore[arg-type]
    assert p is not None
    return p


def dead(host: str = "dead.example") -> SourceProfile:
    return profile(
        host,
        *[
            attempt(host, m, path=f"d{m}", outcome=FetchOutcome.TIMEOUT, status=None)
            for m in (0, 20, 40, 60)
        ],
    )


def spa(host: str = "spa.example") -> SourceProfile:
    facts = []
    for m in (0, 10, 20, 30):
        facts.append(attempt(host, m, path=f"a{m}", detail="outcome=needs_js"))
        facts.append(attempt(host, m + 1, path=f"a{m}", capability=FetchCapability.BROWSER))
    return profile(host, *facts)


def state(
    lag: timedelta = timedelta(minutes=11), policy: str = DEFAULT_POLICY.version
) -> ProjectionState:
    return ProjectionState("sources", 1, policy, outbox_bucket(NOW - lag))


def advisor(snapshot: ProfileSnapshot | None, **kw: object) -> SnapshotAdvisor:
    a = SnapshotAdvisor(
        lambda: snapshot,  # type: ignore[arg-type,return-value]
        max_lag=timedelta(hours=1),
        max_snapshot_age=timedelta(minutes=30),
        refresh_s=300,
        **kw,  # type: ignore[arg-type]
    )
    if snapshot is not None:
        a.refresh()
    return a


def snap(
    *profiles: SourceProfile, st: ProjectionState | None = None, at: datetime = NOW
) -> ProfileSnapshot:
    return ProfileSnapshot(st or state(), {p.domain_id: p for p in profiles}, at)


def key(a: SourceAdvice) -> tuple[bool, str, bool]:
    return a.admit, a.capability, a.fallback is not None


# --- fallback ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("snapshot", "why"),
    [
        (None, "unavailable"),  # never loaded (P7 down / start-up)
        (ProfileSnapshot(None, {}, NOW), "unavailable"),  # projection never ran
        (snap(dead(), at=NOW - timedelta(hours=1)), "snapshot_stale"),  # refresh failing
        (snap(dead(), st=state(lag=timedelta(hours=2))), "projection_stale"),  # reader down
        (snap(dead(), st=state(policy="source-health/v0")), "policy_mismatch"),
    ],
)
def test_untrusted_projection_gives_the_fallback(
    snapshot: ProfileSnapshot | None, why: str
) -> None:
    d = dead().domain_id
    advice = advisor(snapshot).advise(d, NOW)
    assert key(advice) == FALLBACK
    assert advice.fallback == why


def test_rebuilding_projection_lags_and_gives_the_fallback() -> None:
    rebuilding = state(lag=timedelta(days=13))  # generation restarted from retained facts
    assert advisor(snap(dead(), st=rebuilding)).advise(dead().domain_id, NOW).fallback == (
        "projection_stale"
    )


def test_empty_projection_admits_every_host_on_http() -> None:
    advice = advisor(snap()).advise(dead().domain_id, NOW)
    assert (advice.admit, advice.capability, advice.state, advice.fallback) == (
        True,
        "http",
        "unknown",
        None,
    )


def test_failed_refresh_keeps_the_old_snapshot_until_it_ages_out() -> None:
    clock_now = [NOW]
    snapshots: list[ProfileSnapshot | Exception] = [snap(dead())]

    def load() -> ProfileSnapshot:
        item = snapshots[-1]
        if isinstance(item, Exception):
            raise item
        return item

    a = SnapshotAdvisor(
        load,
        max_lag=timedelta(hours=10),
        max_snapshot_age=timedelta(minutes=30),
        refresh_s=300,
        clock=lambda: clock_now[0],
    )
    assert a.refresh()
    snapshots.append(StorageUnavailableError("scylla down"))
    assert not a.refresh()
    assert a.advise(dead().domain_id, NOW + timedelta(minutes=10)).fallback is None
    assert a.advise(dead().domain_id, NOW + timedelta(minutes=31)).fallback == "snapshot_stale"


def test_internal_error_is_a_fallback_not_an_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    a = advisor(snap(dead()))

    def boom(*_: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr("crawler2.intelligence.advice.advise", boom)
    assert a.advise(dead().domain_id, NOW).fallback == "error"


def test_a_hanging_projection_store_never_blocks_advice() -> None:
    release = threading.Event()

    def load() -> ProfileSnapshot:
        release.wait(5)  # Scylla timing out
        raise StorageUnavailableError("timeout")

    a = SnapshotAdvisor(
        load,
        max_lag=timedelta(hours=1),
        max_snapshot_age=timedelta(minutes=30),
        refresh_s=300,
    )
    a.start()
    started = time.perf_counter()
    advice = a.advise(dead().domain_id, NOW)
    elapsed = time.perf_counter() - started
    release.set()
    a.close()
    assert advice.fallback == "unavailable"
    assert elapsed < 0.05


# --- informed advice -----------------------------------------------------------------------


def test_dead_host_is_deferred_until_its_probe_is_due() -> None:
    d = dead()
    a = advisor(snap(d))
    advice = a.advise(d.domain_id, NOW)
    assert (advice.admit, advice.state, advice.fallback) == (False, "unreachable", None)
    assert "probe at" in advice.reason


def test_one_probe_per_host_per_probe_interval() -> None:
    d = dead()
    later = NOW + timedelta(hours=7)  # the 6 h probe interval has passed
    s = ProfileSnapshot(state(), {d.domain_id: d}, later)
    s = ProfileSnapshot(
        ProjectionState("sources", 1, DEFAULT_POLICY.version, outbox_bucket(later)),
        s.profiles,
        later,
    )
    a = advisor(s)
    first = a.advise(d.domain_id, later)
    second = a.advise(d.domain_id, later + timedelta(minutes=1))
    assert (first.admit, first.probe) == (True, True)
    assert (second.admit, second.probe) == (False, False)
    assert "already granted" in second.reason


def test_browser_first_host_starts_on_the_browser() -> None:
    p = spa()
    assert advisor(snap(p)).advise(p.domain_id, NOW).capability == "browser"


def test_live_host_is_admitted_on_http() -> None:
    p = profile("live.example", attempt("live.example", 170))
    advice = advisor(snap(p)).advise(p.domain_id, NOW)
    assert (advice.admit, advice.capability, advice.state) == (True, "http", "active")


def test_snapshot_reads_only_the_current_generation_and_policy() -> None:
    store = MemoryProfileStore()
    store.states["sources"] = ProjectionState("sources", 2, DEFAULT_POLICY.version, 1)
    cur, old, other = dead("a.example"), dead("b.example"), dead("c.example")
    store.put_profiles(
        [
            StoredProfile(cur, 2, DEFAULT_POLICY.version, 1),
            StoredProfile(old, 1, DEFAULT_POLICY.version, 1),
            StoredProfile(other, 2, "source-health/v0", 1),
        ]
    )
    s = load_snapshot(store, "sources", now=NOW, policy=DEFAULT_POLICY)
    assert set(s.profiles) == {cur.domain_id}
