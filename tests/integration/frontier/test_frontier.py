"""Frontier behaviour: V1 `tests/redis_frontier_test.py` ported to V2 semantics,
plus the P3 additions (queues, shared gate, scheduling, admission limits)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from itertools import pairwise

import pytest

from crawler2.core.configuration import ExecutionQueue as Q
from crawler2.frontier import (
    AdmitOutcome,
    CompleteOutcome,
    DeferOutcome,
    FailOutcome,
)
from crawler2.frontier.redis import RedisFrontier
from tests.integration.frontier.conftest import (
    Clock,
    FrontierFactory,
    admission,
    redis_client,
    url,
)

pytestmark = pytest.mark.integration


def at(clock: Clock, delay: float) -> datetime:
    return datetime.fromtimestamp(clock.now + delay, UTC)


def drain(frontier: RedisFrontier, queue: Q = Q.HTTP) -> list[str]:
    urls = []
    while (c := frontier.claim(queue)) is not None:
        urls.append(c.url)
        assert frontier.complete(c) is CompleteOutcome.COMPLETED
    return urls


# -- 1. basic lifecycle -------------------------------------------------------


def test_admitted_ready_claimed_heartbeat_completed(frontier: RedisFrontier, clock: Clock) -> None:
    assert frontier.admit(admission("a", 1)).outcome is AdmitOutcome.READY
    claim = frontier.claim(Q.HTTP)
    assert claim is not None
    assert (claim.url, claim.attempt, claim.claimed_at) == ("https://a.test/1", 1, clock.now)
    assert claim.lease_expires_at == clock.now + frontier.lease_ttl_s
    clock.advance(30)
    renewed = frontier.heartbeat(claim)
    assert renewed is not None
    assert renewed.lease_expires_at == clock.now + frontier.lease_ttl_s
    assert frontier.complete(renewed) is CompleteOutcome.COMPLETED
    assert frontier.claim(Q.HTTP) is None
    stats = frontier.stats()
    assert stats.active_tasks == 0
    assert stats.counters["admitted"] == stats.counters["completed"] == 1


def test_claim_on_empty_queue_returns_none(frontier: RedisFrontier) -> None:
    assert frontier.claim(Q.HTTP) is None


# -- 2. priority ----------------------------------------------------------------


def test_higher_priority_first_fifo_within_priority(frontier: RedisFrontier) -> None:
    frontier.admit(admission("a", 1, priority=10))
    frontier.admit(admission("b", 1, priority=90))
    frontier.admit(admission("c", 1, priority=50))
    frontier.admit(admission("c", 2, priority=90))
    frontier.admit(admission("a", 2, priority=90))
    assert drain(frontier) == [
        "https://b.test/1",
        "https://c.test/2",
        "https://a.test/2",
        "https://c.test/1",
        "https://a.test/1",
    ]


def test_priority_across_domains_added_later(frontier: RedisFrontier) -> None:
    """V1 test_priority_ordering_across_domains_via_domain_heads, P1 direction."""
    frontier.admit(admission("early", 1, priority=20))
    frontier.admit(admission("mid", 1, priority=40))
    frontier.admit(admission("late", 1, priority=80))
    assert [u.split("/")[2] for u in drain(frontier)] == ["late.test", "mid.test", "early.test"]


# -- 3. deduplication (temporary, D10) -----------------------------------------------


def test_duplicate_admission_while_active(frontier: RedisFrontier) -> None:
    assert frontier.admit(admission("a", 1)).outcome is AdmitOutcome.READY
    result = frontier.admit(admission("a", 1))
    assert (result.outcome, result.existing_state) == (AdmitOutcome.DUPLICATE, "ready")
    claim = frontier.claim(Q.HTTP)
    assert claim is not None
    leased = frontier.admit(admission("a", 1, priority=100))
    assert (leased.outcome, leased.existing_state) == (AdmitOutcome.DUPLICATE, "leased")
    assert frontier.claim(Q.HTTP) is None
    frontier.complete(claim)


def test_completed_url_is_admittable_again(frontier: RedisFrontier) -> None:
    """V2 removes V1's `urls:known`: a finished task leaves no tombstone."""
    for round_ in range(3):
        assert frontier.admit(admission("a", 1)).outcome is AdmitOutcome.READY
        claim = frontier.claim(Q.HTTP)
        assert claim is not None
        assert claim.attempt == 1, f"round {round_}"
        frontier.complete(claim)
    assert frontier.stats().counters["completed"] == 3


def test_exhausted_url_is_admittable_again(make_frontier: FrontierFactory) -> None:
    frontier = make_frontier(max_attempts=1)
    frontier.admit(admission("a", 1))
    claim = frontier.claim(Q.HTTP)
    assert claim is not None
    assert frontier.fail(claim, "boom").outcome is FailOutcome.EXHAUSTED
    assert frontier.admit(admission("a", 1)).outcome is AdmitOutcome.READY


def test_merge_raises_priority_of_ready_task(frontier: RedisFrontier) -> None:
    frontier.admit(admission("a", 1, priority=10))
    frontier.admit(admission("b", 1, priority=50))
    assert frontier.admit(admission("a", 1, priority=60)).outcome is AdmitOutcome.MERGED
    assert frontier.admit(admission("a", 1, priority=20)).outcome is AdmitOutcome.DUPLICATE
    assert drain(frontier) == ["https://a.test/1", "https://b.test/1"]


def test_merge_moves_scheduled_task_earlier(frontier: RedisFrontier, clock: Clock) -> None:
    frontier.admit(admission("a", 1, not_before=at(clock, 3600)))
    assert frontier.admit(admission("a", 1, not_before=at(clock, 7200))).outcome is (
        AdmitOutcome.DUPLICATE
    )
    assert frontier.admit(admission("a", 1, not_before=at(clock, 60))).outcome is (
        AdmitOutcome.MERGED
    )
    clock.advance(60)
    assert drain(frontier) == ["https://a.test/1"]


def test_concurrent_admissions_store_one_task(make_frontier: FrontierFactory) -> None:
    """V1 test_concurrent_worker_adds."""
    frontiers = [make_frontier() for _ in range(8)]
    results: list[AdmitOutcome] = []

    def add(f: RedisFrontier) -> None:
        results.extend(f.admit(admission("a", i)).outcome for i in range(50))

    threads = [threading.Thread(target=add, args=(f,)) for f in frontiers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(AdmitOutcome.READY) == 50
    assert frontiers[0].stats().depth[Q.HTTP] == 50


# -- 4./5. politeness, per domain and across queues --------------------------------


def test_domain_interval_gates_second_claim(make_frontier: FrontierFactory, clock: Clock) -> None:
    frontier = make_frontier(default_interval_s=2.0)
    frontier.admit(admission("a", 1))
    frontier.admit(admission("a", 2))
    first = frontier.claim(Q.HTTP)
    assert first is not None
    assert frontier.claim(Q.HTTP) is None
    clock.advance(1.999)
    assert frontier.claim(Q.HTTP) is None
    clock.advance(0.001)
    second = frontier.claim(Q.HTTP)
    assert second is not None
    assert second.claimed_at - first.claimed_at >= 2.0
    frontier.complete(first)
    frontier.complete(second)


def test_gated_domain_does_not_block_lower_priority_domain(
    make_frontier: FrontierFactory,
) -> None:
    """V1 test_rate_limited_domain_does_not_block_lower_priority_eligible_domain."""
    frontier = make_frontier(default_interval_s=60.0)
    frontier.admit(admission("hot", 1, priority=100))
    frontier.admit(admission("hot", 2, priority=100))
    frontier.admit(admission("cold", 1, priority=1))
    claims = [frontier.claim(Q.HTTP), frontier.claim(Q.HTTP), frontier.claim(Q.HTTP)]
    assert [c.url if c else None for c in claims] == [
        "https://hot.test/1",
        "https://cold.test/1",
        None,
    ]


def test_domain_gate_is_shared_by_all_queues(make_frontier: FrontierFactory, clock: Clock) -> None:
    frontier = make_frontier(default_interval_s=0.3)
    for queue in Q:
        frontier.admit(admission("x", queue.value, queue=queue))
    frontier.admit(admission("y", 1, queue=Q.BROWSER))
    first = frontier.claim(Q.HTTP)
    assert first is not None
    # x is closed for every queue; browser gets the other domain instead.
    other = frontier.claim(Q.BROWSER)
    assert other is not None
    assert other.url == "https://y.test/1"
    assert frontier.claim(Q.TOR) is None
    assert frontier.claim(Q.SELENIUM) is None
    times = [first.claimed_at]
    for queue in (Q.BROWSER, Q.TOR, Q.SELENIUM):
        clock.advance(0.300001)  # just past the gate; the test clock is float arithmetic
        claim = frontier.claim(queue)
        assert claim is not None
        assert claim.domain_id == first.domain_id
        times.append(claim.claimed_at)
        assert frontier.claim(Q.SELENIUM if queue is not Q.SELENIUM else Q.TOR) is None
    assert all(b - a >= 0.3 - 1e-6 for a, b in pairwise(times))


def test_per_domain_interval_override(make_frontier: FrontierFactory, clock: Clock) -> None:
    frontier = make_frontier(default_interval_s=10.0)
    frontier.set_domain_interval(url("slow", 0).domain_id, 100.0)
    frontier.set_domain_interval(url("fast", 0).domain_id, 0.0)
    for i in range(3):
        frontier.admit(admission("slow", i))
        frontier.admit(admission("fast", i, priority=10))
    got = [c.url.split("/")[2] for c in iter(lambda: frontier.claim(Q.HTTP), None)]
    assert got == ["slow.test", "fast.test", "fast.test", "fast.test"]
    clock.advance(99)
    assert frontier.claim(Q.HTTP) is None
    clock.advance(1)
    assert frontier.claim(Q.HTTP) is not None
    frontier.set_domain_interval(url("slow", 0).domain_id, None)


def test_concurrent_claimers_respect_domain_interval(make_frontier: FrontierFactory) -> None:
    """Real Redis TIME, 8 threads on 4 queues, one domain: claims are >= interval apart."""
    frontier = make_frontier(real_clock=True, default_interval_s=0.05)
    for i in range(6):
        for queue in Q:
            frontier.admit(admission("shared", f"{queue.value}-{i}", queue=queue))
    stamps: list[float] = []
    lock = threading.Lock()
    stop = threading.Event()

    def worker(queue: Q) -> None:
        f = make_frontier(real_clock=True, default_interval_s=0.05)
        while not stop.is_set():
            claim = f.claim(queue)
            if claim is None:
                continue
            with lock:
                stamps.append(claim.claimed_at)
                if len(stamps) >= 24:
                    stop.set()
            f.complete(claim)

    threads = [threading.Thread(target=worker, args=(q,)) for q in Q for _ in range(2)]
    for t in threads:
        t.start()
    stop.wait(timeout=10)
    stop.set()
    for t in threads:
        t.join()
    stamps.sort()
    assert len(stamps) == 24
    gaps = [b - a for a, b in pairwise(stamps)]
    assert min(gaps) >= 0.05 - 1e-6


# -- 6.-9. ownership, heartbeat, expiry, crash recovery ----------------------------------


def test_no_duplicate_claims_under_concurrency(make_frontier: FrontierFactory) -> None:
    """V1 test_get_next_url_no_duplicates / ..._same_domain_never_duplicate."""
    seed = make_frontier()
    for i in range(400):
        seed.admit(admission(f"d{i % 3}", i))
    seen: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        f = make_frontier()
        while (c := f.claim(Q.HTTP)) is not None:
            with lock:
                seen.append(c.url_id)
            f.complete(c)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == len(set(seen)) == 400


def test_stale_owner_rejected_after_lease_recovery(frontier: RedisFrontier, clock: Clock) -> None:
    """V1 test_stale_claim_rejected_after_lease_reclaim."""
    frontier.admit(admission("a", 1))
    old = frontier.claim(Q.HTTP)
    assert old is not None
    clock.advance(frontier.lease_ttl_s + 1)
    assert frontier.recover().recovered == 1
    clock.advance(frontier.settings.max_backoff_s)
    new = frontier.claim(Q.HTTP)
    assert new is not None
    assert (new.attempt, new.url_id) == (2, old.url_id)
    assert new.token != old.token
    assert frontier.heartbeat(old) is None
    assert frontier.complete(old) is CompleteOutcome.STALE
    assert frontier.fail(old, "late").outcome is FailOutcome.STALE
    assert frontier.defer(old).outcome is DeferOutcome.STALE
    assert frontier.complete(new) is CompleteOutcome.COMPLETED


def test_heartbeat_keeps_lease_through_recovery(frontier: RedisFrontier, clock: Clock) -> None:
    frontier.admit(admission("a", 1))
    claim = frontier.claim(Q.HTTP)
    assert claim is not None
    for _ in range(5):
        clock.advance(frontier.lease_ttl_s * 0.9)
        renewed = frontier.heartbeat(claim)
        assert renewed is not None
        claim = renewed
        assert frontier.recover().recovered == 0
    assert frontier.complete(claim) is CompleteOutcome.COMPLETED


def test_expired_lease_is_recovered_and_reclaimable(frontier: RedisFrontier, clock: Clock) -> None:
    """Crash injection (V1 test_crash_injection_reclaim_requeues_below_max_retries)."""
    frontier.admit(admission("a", 1))
    assert frontier.claim(Q.HTTP) is not None  # worker "crashes": never reports
    clock.advance(frontier.lease_ttl_s - 0.001)
    assert frontier.recover().recovered == 0
    clock.advance(0.001)
    assert frontier.recover().recovered == 1
    assert frontier.claim(Q.HTTP) is None  # backoff
    clock.advance(frontier.settings.base_backoff_s)
    again = frontier.claim(Q.HTTP)
    assert again is not None
    assert again.attempt == 2
    frontier.complete(again)


def test_recovery_exhaustion_dead_letters(make_frontier: FrontierFactory, clock: Clock) -> None:
    """V1 test_crash_injection_reclaim_terminalizes_at_max_retries, without a permanent set."""
    frontier = make_frontier(max_attempts=2, base_backoff_s=1, max_backoff_s=1)
    frontier.admit(admission("a", 1))
    for expected in (1, 2):
        claim = frontier.claim(Q.HTTP)
        assert claim is not None
        assert claim.attempt == expected
        clock.advance(frontier.lease_ttl_s + 1)
        frontier.recover()
        clock.advance(1)
    stats = frontier.stats()
    assert (stats.dead_letters, stats.active_tasks, stats.counters["dead"]) == (1, 0, 1)
    [letter] = frontier.dead_letters()
    assert (letter["url"], letter["err"]) == ("https://a.test/1", "lease expired")
    # A dead letter does not deduplicate.
    assert frontier.admit(admission("a", 1)).outcome is AdmitOutcome.READY
    assert frontier.stats().dead_letters == 0


def test_dead_letters_are_bounded(make_frontier: FrontierFactory, clock: Clock) -> None:
    frontier = make_frontier(max_attempts=1, dead_max=3, dead_ttl_s=100)
    for i in range(5):
        frontier.admit(admission(f"d{i}", 1))
        assert frontier.claim(Q.HTTP) is not None
    clock.advance(frontier.lease_ttl_s + 1)
    frontier.recover()
    assert frontier.stats().dead_letters == 3
    clock.advance(101)
    frontier.recover()
    assert frontier.stats().dead_letters == 0


# -- 10. retry ------------------------------------------------------------------------


def test_fail_retries_with_growing_backoff_then_exhausts(
    make_frontier: FrontierFactory, clock: Clock
) -> None:
    """V1 test_mark_failed_retries_with_growing_backoff_then_fails_permanently."""
    frontier = make_frontier(max_attempts=4, base_backoff_s=5, max_backoff_s=12)
    frontier.admit(admission("a", 1))
    delays = []
    for attempt in (1, 2, 3):
        claim = frontier.claim(Q.HTTP)
        assert claim is not None
        assert claim.attempt == attempt
        result = frontier.fail(claim, "timeout")
        assert result.outcome is FailOutcome.RETRY_SCHEDULED
        assert result.retry_at is not None
        delays.append(result.retry_at - clock.now)
        clock.advance(delays[-1] - 0.001)
        assert frontier.claim(Q.HTTP) is None
        clock.advance(0.001)
    assert delays == [5, 10, 12]
    last = frontier.claim(Q.HTTP)
    assert last is not None
    assert frontier.fail(last, "timeout").outcome is FailOutcome.EXHAUSTED
    assert frontier.stats().active_tasks == 0


def test_failure_can_escalate_to_another_queue(frontier: RedisFrontier, clock: Clock) -> None:
    frontier.admit(admission("a", 1))
    claim = frontier.claim(Q.HTTP)
    assert claim is not None
    frontier.fail(claim, "needs javascript", next_queue=Q.BROWSER)
    stats = frontier.stats()
    assert (stats.depth[Q.HTTP], stats.depth[Q.BROWSER]) == (0, 1)
    clock.advance(frontier.settings.max_backoff_s)
    assert frontier.claim(Q.HTTP) is None
    escalated = frontier.claim(Q.BROWSER)
    assert escalated is not None
    assert escalated.attempt == 2  # one budget across queues (D4)
    frontier.complete(escalated)


def test_defer_keeps_attempt_budget(frontier: RedisFrontier, clock: Clock) -> None:
    """V1 test_claim_then_mark_deferred_leaves_attempt_budget_net_zero + fixed delay."""
    frontier.admit(admission("a", 1))
    for _ in range(5):
        claim = frontier.claim(Q.HTTP)
        assert claim is not None
        assert claim.attempt == 1
        result = frontier.defer(claim)
        assert result.outcome is DeferOutcome.DEFERRED
        assert result.retry_at == clock.now + frontier.settings.defer_delay_s
        clock.advance(frontier.settings.defer_delay_s)
    frontier.complete(frontier.claim(Q.HTTP))  # type: ignore[arg-type]


# -- 11./12. scheduled work -----------------------------------------------------------------


def test_future_task_not_claimable_early(frontier: RedisFrontier, clock: Clock) -> None:
    assert frontier.admit(admission("a", 1, not_before=at(clock, 10))).outcome is (
        AdmitOutcome.SCHEDULED
    )
    assert frontier.claim(Q.HTTP) is None
    clock.advance(9.999)
    assert frontier.claim(Q.HTTP) is None
    clock.advance(0.001)
    assert drain(frontier) == ["https://a.test/1"]


def test_past_not_before_is_ready_immediately(frontier: RedisFrontier, clock: Clock) -> None:
    assert frontier.admit(admission("a", 1, not_before=at(clock, -5))).outcome is (
        AdmitOutcome.READY
    )


def test_duplicate_promotion_is_harmless(frontier: RedisFrontier, clock: Clock) -> None:
    for i in range(10):
        frontier.admit(admission("a", i, not_before=at(clock, 5)))
    clock.advance(5)
    assert frontier.recover().promoted == 10
    assert frontier.recover().promoted == 0
    assert frontier.claim(Q.HTTP) is not None
    assert frontier.stats().counters.get("anomaly", 0) == 0
    frontier.clear()


def test_concurrent_promoters_move_each_task_once(
    make_frontier: FrontierFactory, clock: Clock
) -> None:
    seed = make_frontier(promote_batch=7)
    for i in range(300):
        seed.admit(admission(f"d{i % 10}", i, not_before=at(clock, 1)))
    clock.advance(1)
    promoted: list[int] = []
    lock = threading.Lock()

    def promoter() -> None:
        f = make_frontier(promote_batch=7)
        while (n := f.recover().promoted) > 0:
            with lock:
                promoted.append(n)

    threads = [threading.Thread(target=promoter) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(promoted) == 300
    assert len(drain(seed)) == 300


def test_scheduled_retry_joins_ready_queue_by_priority(
    frontier: RedisFrontier, clock: Clock
) -> None:
    """claimed -> failure -> retry -> scheduled -> ready -> claim."""
    frontier.admit(admission("a", 1, priority=80))
    claim = frontier.claim(Q.HTTP)
    assert claim is not None
    frontier.fail(claim, "503")
    frontier.admit(admission("b", 1, priority=20))
    clock.advance(frontier.settings.base_backoff_s)
    assert drain(frontier) == ["https://a.test/1", "https://b.test/1"]


# -- 13./14. queue depth limits, admission control ---------------------------------------


def test_full_queue_rejects_new_work_explicitly(
    make_frontier: FrontierFactory, clock: Clock
) -> None:
    frontier = make_frontier(max_depth={"http": 3, "browser": 1})
    assert frontier.admit(admission("a", 1)).accepted
    assert frontier.admit(admission("a", 2, not_before=at(clock, 60))).accepted  # counts
    claim = frontier.claim(Q.HTTP)
    assert frontier.admit(admission("a", 3)).accepted  # leased still counts
    rejected = frontier.admit(admission("a", 4))
    assert rejected.outcome is AdmitOutcome.REJECTED_FULL
    assert not rejected.accepted
    assert frontier.admit(admission("b", 1, queue=Q.BROWSER)).accepted  # queues independent
    # Admitted work is never blocked by the limit: retry and duplicates still work.
    assert claim is not None
    assert frontier.fail(claim, "x").outcome is FailOutcome.RETRY_SCHEDULED
    assert frontier.admit(admission("a", 1)).outcome is AdmitOutcome.DUPLICATE
    # A queue move may overfill the target; only new admissions there are refused.
    other = frontier.claim(Q.HTTP)
    assert other is not None
    frontier.fail(other, "js", next_queue=Q.BROWSER)
    assert frontier.stats().depth[Q.BROWSER] == 2
    assert frontier.admit(admission("b", 2, queue=Q.BROWSER)).outcome is (
        AdmitOutcome.REJECTED_FULL
    )
    assert frontier.stats().counters["rejected"] == 2
    assert frontier.admit(admission("a", 4)).accepted  # capacity freed by the move


def test_saturation_never_loses_admitted_tasks(make_frontier: FrontierFactory) -> None:
    frontier = make_frontier(max_depth={"http": 100})
    outcomes = [
        r.outcome for r in frontier.admit_many(admission(f"d{i % 7}", i) for i in range(250))
    ]
    assert outcomes.count(AdmitOutcome.READY) == 100
    assert outcomes.count(AdmitOutcome.REJECTED_FULL) == 150
    assert len(drain(frontier)) == 100
    assert frontier.stats().active_tasks == 0


# -- queues are independent ------------------------------------------------------------------


def test_queues_are_independent(frontier: RedisFrontier) -> None:
    frontier.admit(admission("a", 1, queue=Q.TOR))
    for queue in (Q.HTTP, Q.BROWSER, Q.SELENIUM):
        assert frontier.claim(queue) is None
    assert drain(frontier, Q.TOR) == ["https://a.test/1"]


# -- 17./18. eligibility beyond V1's scan window (D13) -------------------------------------


def test_eligible_domain_is_found_behind_many_gated_domains(
    make_frontier: FrontierFactory,
) -> None:
    """V1 returned nothing here once > domain_scan_limit (250) better domains were gated."""
    frontier = make_frontier(default_interval_s=3600.0)
    frontier.admit_many(
        admission(f"filler{i}", j, priority=100) for i in range(600) for j in (1, 2)
    )
    frontier.admit(admission("victim", 1, priority=0))
    fillers = [frontier.claim(Q.HTTP) for _ in range(600)]
    assert all(c is not None and "filler" in c.url for c in fillers)
    victim = frontier.claim(Q.HTTP)
    assert victim is not None
    assert victim.url == "https://victim.test/1"
    assert frontier.claim(Q.HTTP) is None
    frontier.clear()


def test_redis_client_must_decode_responses() -> None:
    import redis as redis_lib

    from crawler2.core.configuration import FrontierSettings

    with pytest.raises(ValueError, match="decode_responses"):
        RedisFrontier(redis_lib.Redis(), FrontierSettings(), namespace="x")
    assert redis_client().get_encoder().decode_responses
