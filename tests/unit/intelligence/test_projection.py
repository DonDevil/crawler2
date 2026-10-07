"""P7 increment 2: the outbox → source-profile projection (design §9.2, ADR-023)."""

from __future__ import annotations

import math
import random
from dataclasses import fields, replace
from datetime import datetime, timedelta

import pytest
from antipiracy_contracts.models.web import FetchCapability, FetchOutcome

from crawler2.intelligence.projection import (
    CycleReport,
    ReaderConfig,
    SourceProjector,
    profile_from_json,
    profile_to_json,
)
from crawler2.intelligence.sources import (
    DEFAULT_POLICY,
    SourcePolicy,
    SourceProfile,
    SourceState,
    advise,
    fact_from_attempt,
    fold,
)
from crawler2.storage.errors import StorageUnavailableError
from crawler2.storage.layout import outbox_bucket
from tests.unit.intelligence.fakes import (
    B0,
    T0,
    MemoryOutbox,
    MemoryProfileStore,
    attempt,
    record,
)

SHARDS = (0, 1, 2)
H = timedelta(hours=1)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def projector(
    outbox: MemoryOutbox,
    store: MemoryProfileStore,
    clock: Clock,
    *,
    policy: SourcePolicy = DEFAULT_POLICY,
    **config: object,
) -> SourceProjector:
    cfg = ReaderConfig(**({"shards": SHARDS, "max_buckets_per_cycle": 10_000} | config))  # type: ignore[arg-type]
    return SourceProjector(outbox, store, config=cfg, policy=policy, clock=clock)


def expected(*attempts: object) -> dict[str, SourceProfile]:
    out: dict[str, SourceProfile] = {}
    for a in attempts:
        f = fact_from_attempt(a)  # type: ignore[arg-type]
        out[f.domain_id] = fold(out.get(f.domain_id), f)
    return out


def same(p: SourceProfile, q: SourceProfile) -> bool:
    """Equal up to float rounding (the fold is commutative, not bit-identical)."""
    for f in fields(SourceProfile):
        a, b = getattr(p, f.name), getattr(q, f.name)
        pairs = zip(a, b, strict=True) if isinstance(a, tuple) else [(a, b)]
        for x, y in pairs:
            equal = math.isclose(x, y, rel_tol=1e-12) if isinstance(x, float) else x == y
            if not equal:
                return False
    return True


def profiles(store: MemoryProfileStore) -> dict[str, SourceProfile]:
    state = store.states["sources"]
    return {
        d: r.profile
        for d, r in store.rows.items()
        if r.generation == state.generation and r.policy == state.policy
    }


def assert_matches(store: MemoryProfileStore, want: dict[str, SourceProfile]) -> None:
    got = profiles(store)
    assert set(got) == set(want)
    for d, p in want.items():
        assert same(got[d], p), d


@pytest.fixture
def world() -> tuple[MemoryOutbox, MemoryProfileStore, Clock]:
    return MemoryOutbox(), MemoryProfileStore(), Clock(T0 + H)


ATTEMPTS = [
    attempt("live.example", 1),
    attempt("live.example", 2, path="y"),
    attempt("dead.example", 1, outcome=FetchOutcome.TIMEOUT, status=None),
    attempt("dead.example", 3, outcome=FetchOutcome.DNS_FAILURE, status=None),
    attempt("dead.example", 5, outcome=FetchOutcome.TIMEOUT, status=None, path="z"),
    attempt("js.example", 4, detail="outcome=needs_js"),
]


def seed(outbox: MemoryOutbox, attempts: list[object] = ATTEMPTS) -> None:  # type: ignore[assignment]
    for i, a in enumerate(attempts):
        outbox.add(record(a, shard=i % len(SHARDS)))  # type: ignore[arg-type]


# --- normal consumption ---------------------------------------------------------------


def test_bootstrap_folds_every_settled_bucket_into_the_pure_projection(world) -> None:  # type: ignore[no-untyped-def]
    outbox, store, clock = world
    seed(outbox)
    report = projector(outbox, store, clock).run_once()
    assert report.new_generation == "bootstrap"
    assert report.applied == len(ATTEMPTS)
    assert report.caught_up
    state = store.states["sources"]
    assert state.generation == 1
    assert state.next_bucket == outbox_bucket(clock.now - timedelta(minutes=10))
    assert_matches(store, expected(*ATTEMPTS))
    assert report.lag_s == pytest.approx(600, abs=60)


def test_only_settled_buckets_are_read(world) -> None:  # type: ignore[no-untyped-def]
    outbox, store, clock = world
    clock.now = T0 + timedelta(minutes=12)  # bucket B0+2 is the newest settled one
    seed(outbox)
    projector(outbox, store, clock).run_once()
    assert store.states["sources"].next_bucket == B0 + 2
    early = [a for a in ATTEMPTS if a.finished_at < T0 + timedelta(minutes=2)]
    assert_matches(store, expected(*early))


def test_other_event_types_are_ignored(world) -> None:  # type: ignore[no-untyped-def]
    outbox, store, clock = world
    outbox.add(record(ATTEMPTS[0]), replace(record(ATTEMPTS[1]), event_type="page.observed"))
    report = projector(outbox, store, clock).run_once()
    assert report.applied == 1
    assert report.invalid == 0


def test_incremental_cycles_equal_one_cycle(world) -> None:  # type: ignore[no-untyped-def]
    outbox, store, clock = world
    seed(outbox)
    p = projector(outbox, store, clock, max_buckets_per_cycle=7)
    reports: list[CycleReport] = []
    while not (reports and reports[-1].caught_up):
        reports.append(p.run_once())
    assert len(reports) > 1
    assert all(r.buckets <= 7 for r in reports)
    assert_matches(store, expected(*ATTEMPTS))


# --- idempotency, order, duplicates -------------------------------------------------------


def test_duplicate_fact_in_another_bucket_is_folded_once(world) -> None:  # type: ignore[no-untyped-def]
    outbox, store, clock = world
    seed(outbox)
    # A producer retry: same attempt, new event id, enqueued a few minutes later.
    outbox.add(record(ATTEMPTS[0], bucket=B0 + 5, shard=2))
    outbox.add(record(ATTEMPTS[0], bucket=B0 + 1, shard=1))  # and within one bucket
    report = projector(outbox, store, clock).run_once()
    assert report.duplicates == 2
    assert_matches(store, expected(*ATTEMPTS))


def test_out_of_order_facts_give_the_same_profile(world) -> None:  # type: ignore[no-untyped-def]
    outbox, store, clock = world
    shuffled = list(ATTEMPTS)
    random.Random(7).shuffle(shuffled)  # noqa: S311 -- a seeded test permutation
    # Enqueued in reverse time order: late facts land in early buckets.
    for i, a in enumerate(shuffled):
        outbox.add(record(a, bucket=B0 + 20 - i, shard=i % 3))
    projector(outbox, store, clock).run_once()
    assert_matches(store, expected(*ATTEMPTS))


def test_crash_after_profile_write_does_not_fold_the_bucket_twice(world) -> None:  # type: ignore[no-untyped-def]
    outbox, store, clock = world
    seed(outbox)
    store.fail_cas_after_put = 1  # profiles of the first bucket written, checkpoint not
    p = projector(outbox, store, clock)
    with pytest.raises(StorageUnavailableError):
        p.run_once()
    restarted = projector(outbox, store, clock)  # a new process: empty dedupe memory
    report = restarted.run_once()
    assert report.replayed > 0
    assert_matches(store, expected(*ATTEMPTS))


def test_failed_profile_write_is_retried_not_skipped(world) -> None:  # type: ignore[no-untyped-def]
    outbox, store, clock = world
    seed(outbox)
    store.fail_puts = 1
    p = projector(outbox, store, clock)
    with pytest.raises(StorageUnavailableError):
        p.run_once()
    report = p.run_once()  # same process: its dedupe memory must not drop the retried bucket
    assert report.duplicates == 0
    assert_matches(store, expected(*ATTEMPTS))


def test_malformed_rows_are_counted_and_skipped(world) -> None:  # type: ignore[no-untyped-def]
    outbox, store, clock = world
    seed(outbox)
    bad = record(attempt("bad.example", 2))
    outbox.add(
        replace(bad, envelope=b"{not json"),
        replace(bad, schema_version="9.0", idempotency_key="other"),
    )
    report = projector(outbox, store, clock).run_once()
    assert report.invalid == 2
    assert_matches(store, expected(*ATTEMPTS))


def test_rebuild_is_deterministic(world) -> None:  # type: ignore[no-untyped-def]
    outbox, _, clock = world
    seed(outbox)
    a, b = MemoryProfileStore(), MemoryProfileStore()
    projector(outbox, a, clock).run_once()
    projector(outbox, b, clock, max_buckets_per_cycle=3).run_once()
    while not projector(outbox, b, clock, max_buckets_per_cycle=3).run_once().caught_up:
        pass
    assert {d: profile_to_json(r.profile) for d, r in a.rows.items()} == {
        d: profile_to_json(r.profile) for d, r in b.rows.items()
    }


# --- generations ---------------------------------------------------------------------


def test_gap_starts_a_new_generation_from_retained_facts(world) -> None:  # type: ignore[no-untyped-def]
    outbox, store, clock = world
    seed(outbox)
    projector(outbox, store, clock).run_once()
    # The reader is down for 15 days: the outbox no longer holds its checkpoint.
    clock.now = T0 + timedelta(days=15)
    later = attempt("live.example", 15 * 24 * 60 - 30, path="later")
    outbox.add(record(later))
    p = projector(outbox, store, clock)
    report = p.run_once()
    while not p.run_once().caught_up:  # 14 days of retained buckets
        pass
    state = store.states["sources"]
    assert report.new_generation == "gap"
    assert (state.generation, state.gaps, state.rebuilds) == (2, 1, 1)
    assert state.last_gap_from is not None
    assert state.last_gap_to is not None
    assert state.last_gap_to == outbox_bucket(clock.now - timedelta(days=14) + H)
    # Old-generation rows are not built upon: the new profile holds only retained facts.
    assert_matches(store, expected(later))


def test_policy_change_rebuilds(world) -> None:  # type: ignore[no-untyped-def]
    outbox, store, clock = world
    seed(outbox)
    projector(outbox, store, clock).run_once()
    v2 = SourcePolicy(version="source-health/test-v2")
    report = projector(outbox, store, clock, policy=v2).run_once()
    assert report.new_generation == "policy"
    assert store.states["sources"].policy == "source-health/test-v2"
    assert {r.policy for r in store.rows.values()} == {"source-health/test-v2"}


def test_operator_rebuild_during_a_run_is_detected_and_followed(world) -> None:  # type: ignore[no-untyped-def]
    outbox, store, clock = world
    seed(outbox)
    running = projector(outbox, store, clock)

    def operator_rebuilds() -> None:  # the CLI, in another process, mid-cycle
        store.on_put = None
        assert projector(outbox, store, clock).rebuild() is not None

    store.on_put = operator_rebuilds
    assert running.run_once().conflict  # its checkpoint CAS lost
    assert store.states["sources"].generation == 2
    report = running.run_once()
    assert not report.conflict
    assert report.applied == len(ATTEMPTS)
    assert_matches(store, expected(*ATTEMPTS))


# --- failures --------------------------------------------------------------------------


def test_storage_outage_is_retried_in_process(world) -> None:  # type: ignore[no-untyped-def]
    outbox, store, clock = world
    seed(outbox)
    outbox.fail = True
    sleeps: list[float] = []
    cycles = iter(range(5))

    def stop() -> bool:
        if next(cycles, None) is None:
            return True
        if len(sleeps) == 3:
            outbox.fail = False  # Scylla is back
        return False

    projector(outbox, store, clock).run_forever(
        poll_interval_s=1.0, retry_max_s=4.0, stop=stop, sleep=sleeps.append
    )
    assert sleeps[:3] == [1.0, 2.0, 4.0]
    assert_matches(store, expected(*ATTEMPTS))


def test_state_store_outage_never_raises_out_of_the_loop(world) -> None:  # type: ignore[no-untyped-def]
    outbox, store, clock = world
    store.fail_reads = True
    sleeps: list[float] = []
    projector(outbox, store, clock).run_forever(
        poll_interval_s=1.0, retry_max_s=8.0, stop=lambda: len(sleeps) >= 6, sleep=sleeps.append
    )
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


# --- the profile ------------------------------------------------------------------------


def test_profile_size_is_bounded_whatever_the_history(world) -> None:  # type: ignore[no-untyped-def]
    outbox, store, clock = world
    clock.now = T0 + timedelta(hours=30)
    many = [attempt("busy.example", m, path=f"p{m}") for m in range(0, 24 * 60, 2)]
    for a in many:
        outbox.add(record(a))
    projector(outbox, store, clock).run_once()
    (row,) = store.rows.values()
    assert row.profile.facts == len(many)
    assert len(profile_to_json(row.profile)) < 1024


def test_json_round_trip_is_exact() -> None:
    p = None
    for a in ATTEMPTS[2:5]:
        p = fold(p, fact_from_attempt(a))
    assert p is not None
    p = fold(p, fact_from_attempt(attempt("dead.example", 9, final="https://parked.example/")))
    assert profile_from_json(profile_to_json(p)) == p


def test_moved_host_is_probe_only_and_keeps_its_target(world) -> None:  # type: ignore[no-untyped-def]
    outbox, store, clock = world
    clock.now = T0 + timedelta(hours=3)
    moves = [
        attempt("old.example", m, path=f"m{m}", final="https://new-mirror.example/")
        for m in (0, 30, 60, 90)
    ]
    for a in moves:
        outbox.add(record(a))
    projector(outbox, store, clock).run_once()
    (row,) = store.rows.values()
    assert row.profile.moved_to == "new-mirror.example"
    advice = advise(row.profile, clock.now)
    assert advice.state is SourceState.MOVED
    assert not advice.admit


def test_browser_capability_survives_the_projection(world) -> None:  # type: ignore[no-untyped-def]
    outbox, store, clock = world
    clock.now = T0 + timedelta(hours=3)
    for m in (0, 10, 20, 30):
        outbox.add(record(attempt("spa.example", m, path=f"a{m}", detail="outcome=needs_js")))
        outbox.add(
            record(attempt("spa.example", m + 1, path=f"a{m}", capability=FetchCapability.BROWSER))
        )
    projector(outbox, store, clock).run_once()
    (row,) = store.rows.values()
    assert advise(row.profile, clock.now).capability == "browser"
