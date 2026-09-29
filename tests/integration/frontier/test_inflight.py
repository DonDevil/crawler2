"""Global per-domain in-flight limit (P3 correction found by P4, ADR-019).

The limit bounds how many tasks of one domain are leased at the same time,
across every execution queue, worker and host. It is acquired atomically
by ``claim`` and released exactly once by ``complete``/``fail``/``defer``
or lease recovery; stale reports never release a slot.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
import redis

from crawler2.core.configuration import ExecutionQueue as Q
from crawler2.core.configuration import FrontierSettings
from crawler2.frontier import Claim, CompleteOutcome, DeferOutcome, FailOutcome
from crawler2.frontier.redis import RedisFrontier
from tests.integration.frontier.conftest import Clock, FrontierFactory, admission, url

pytestmark = pytest.mark.integration


def claim_all(frontier: RedisFrontier, queue: Q) -> list[Claim]:
    claims = []
    while (claim := frontier.claim(queue)) is not None:
        claims.append(claim)
    return claims


def test_limit_caps_simultaneous_leases_of_a_domain(make_frontier: FrontierFactory) -> None:
    f = make_frontier(max_inflight_per_domain=2)
    for i in range(5):
        f.admit(admission("hot", i))
    first = claim_all(f, Q.HTTP)
    assert len(first) == 2
    assert f.stats().saturated_domains == 1
    f.complete(first[0])  # one slot back -> exactly one more claim
    assert len(claim_all(f, Q.HTTP)) == 1


def test_all_queues_share_one_domain_counter(make_frontier: FrontierFactory) -> None:
    f = make_frontier(max_inflight_per_domain=2)
    for i, queue in enumerate([Q.HTTP, Q.BROWSER, Q.TOR, Q.HTTP, Q.BROWSER]):
        f.admit(admission("hot", i, queue=queue))
    taken = [c for q in (Q.HTTP, Q.BROWSER, Q.TOR) for c in claim_all(f, q)]
    assert len(taken) == 2  # not 2 per queue
    for claim in taken:
        f.complete(claim)
    taken = [c for q in (Q.TOR, Q.BROWSER, Q.HTTP) for c in claim_all(f, q)]
    assert len(taken) == 2


def test_saturated_domain_does_not_block_other_domains(make_frontier: FrontierFactory) -> None:
    f = make_frontier(max_inflight_per_domain=1)
    for i in range(3):
        f.admit(admission("hot", i, priority=90))
    f.admit(admission("cold", 0, priority=10))
    claims = claim_all(f, Q.HTTP)
    assert [c.domain_id for c in claims] == [url("hot", 0).domain_id, url("cold", 0).domain_id]


@pytest.mark.parametrize("release", ["complete", "fail", "fail_move", "defer"])
def test_every_report_releases_exactly_one_slot(
    make_frontier: FrontierFactory, release: str
) -> None:
    f = make_frontier(max_inflight_per_domain=1)
    f.admit(admission("hot", 0))
    f.admit(admission("hot", 1))
    (held,) = claim_all(f, Q.HTTP)
    if release == "complete":
        assert f.complete(held) is CompleteOutcome.COMPLETED
    elif release == "fail":
        assert f.fail(held, "x").outcome is FailOutcome.RETRY_SCHEDULED
    elif release == "fail_move":
        assert f.fail(held, "x", next_queue=Q.BROWSER).outcome is FailOutcome.RETRY_SCHEDULED
    else:
        assert f.defer(held).outcome is DeferOutcome.DEFERRED
    assert f.stats().saturated_domains == 0
    (nxt,) = claim_all(f, Q.HTTP)
    # the stale owner reports again: no slot may be released twice
    assert f.complete(held) is CompleteOutcome.STALE
    assert f.fail(held, "x").outcome is FailOutcome.STALE
    assert f.defer(held).outcome is DeferOutcome.STALE
    assert claim_all(f, Q.HTTP) == []  # still exactly one in flight
    assert f.complete(nxt) is CompleteOutcome.COMPLETED


def test_lease_expiry_releases_the_slot_and_zombies_cannot_double_release(
    make_frontier: FrontierFactory, clock: Clock
) -> None:
    f = make_frontier(max_inflight_per_domain=1, lease_ttl_s=5.0)
    for i in range(3):
        f.admit(admission("hot", i))
    (crashed,) = claim_all(f, Q.HTTP)  # this worker dies
    clock.advance(6)
    assert f.recover().recovered == 1
    (second,) = claim_all(f, Q.HTTP)
    assert second.url_id != crashed.url_id
    assert f.complete(crashed) is CompleteOutcome.STALE  # the zombie wakes up
    assert claim_all(f, Q.HTTP) == []  # the zombie released nothing
    f.complete(second)
    assert len(claim_all(f, Q.HTTP)) == 1


def test_dead_letter_releases_the_slot(make_frontier: FrontierFactory, clock: Clock) -> None:
    f = make_frontier(max_inflight_per_domain=1, lease_ttl_s=5.0, max_attempts=1)
    f.admit(admission("hot", 0))
    f.admit(admission("hot", 1))
    claim_all(f, Q.HTTP)
    clock.advance(6)
    assert f.recover().dead == 1
    assert len(claim_all(f, Q.HTTP)) == 1


def test_gate_and_limit_compose(make_frontier: FrontierFactory, clock: Clock) -> None:
    f = make_frontier(max_inflight_per_domain=2, default_interval_s=1.0)
    for i in range(4):
        f.admit(admission("hot", i))
    a = f.claim(Q.HTTP)
    assert a is not None
    assert f.claim(Q.HTTP) is None  # gated
    clock.advance(1.0)
    b = f.claim(Q.HTTP)
    assert b is not None
    clock.advance(1.0)
    assert f.claim(Q.HTTP) is None  # gate open but domain saturated
    f.complete(a)  # releasing re-indexes the domain (its gate has expired)
    assert f.claim(Q.HTTP) is not None


def test_zero_means_unlimited(make_frontier: FrontierFactory) -> None:
    f = make_frontier(max_inflight_per_domain=0)
    for i in range(6):
        f.admit(admission("hot", i))
    assert len(claim_all(f, Q.HTTP)) == 6
    assert f.stats().saturated_domains == 0


def test_per_domain_override(make_frontier: FrontierFactory) -> None:
    f = make_frontier(max_inflight_per_domain=1)
    f.set_domain_inflight_limit(url("big", 0).domain_id, 3)
    for i in range(4):
        f.admit(admission("big", i))
        f.admit(admission("small", i))
    claims = claim_all(f, Q.HTTP)
    by_domain = {d: sum(c.domain_id == d for c in claims) for d in {c.domain_id for c in claims}}
    assert by_domain == {url("big", 0).domain_id: 3, url("small", 0).domain_id: 1}
    f.set_domain_inflight_limit(url("big", 0).domain_id, None)


def test_concurrent_claimers_never_exceed_the_limit(redis_conn: redis.Redis) -> None:
    namespace = f"test-{uuid.uuid4().hex[:12]}"
    settings = FrontierSettings(default_interval_s=0.0, max_inflight_per_domain=2)
    setup = RedisFrontier(redis_conn, settings, namespace=namespace)
    for i in range(200):
        setup.admit(admission("hot", i, queue=(Q.HTTP, Q.BROWSER)[i % 2]))
    lock = threading.Lock()
    inflight = peak = done = 0
    errors: list[str] = []

    def worker(queue: Q) -> None:
        nonlocal inflight, peak, done
        f = RedisFrontier(redis_conn, settings, namespace=namespace)
        idle = 0
        while idle < 200:
            claim = f.claim(queue)
            if claim is None:
                idle += 1
                continue
            idle = 0
            with lock:
                inflight += 1
                peak = max(peak, inflight)
                if inflight > 2:
                    errors.append(f"{inflight} in flight")
            time.sleep(0.005)  # hold the lease so concurrent claims can overlap
            with lock:
                inflight -= 1  # decrement before the release becomes visible
            if f.complete(claim) is not CompleteOutcome.COMPLETED:
                errors.append("lost completion")
            with lock:
                done += 1

    threads = [
        threading.Thread(target=worker, args=((Q.HTTP, Q.BROWSER)[i % 2],)) for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert errors == []
        assert done == 200
        assert peak == 2
        assert setup.audit() == []
    finally:
        setup.clear()
