"""Property-based state machine over the claim lifecycle (bounded for CI).

A reference model tracks which URLs are active, their queue, attempt count,
and the one token allowed to own each; after every step the real Redis
frontier must agree with the model and pass ``audit()``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
)

from crawler2.core.configuration import ExecutionQueue as Q
from crawler2.core.configuration import FrontierSettings
from crawler2.frontier import (
    Admission,
    AdmitOutcome,
    Claim,
    CompleteOutcome,
    DeferOutcome,
    FailOutcome,
)
from crawler2.frontier.redis import RedisFrontier
from tests.integration.frontier.conftest import Clock, redis_client, url

pytestmark = pytest.mark.integration

QUEUES = (Q.HTTP, Q.BROWSER)
URLS = [url(f"d{i % 3}", i) for i in range(7)]
INTERVAL = 1.0
MAX_ATTEMPTS = 3
MAX_DEPTH = {Q.HTTP: 4, Q.BROWSER: 3}
SETTINGS = FrontierSettings(
    default_interval_s=INTERVAL,
    lease_ttl_s=10.0,
    max_attempts=MAX_ATTEMPTS,
    base_backoff_s=2.0,
    max_backoff_s=4.0,
    defer_delay_s=1.0,
    max_depth=dict(MAX_DEPTH),
)
CLIENT = redis_client()


@dataclass
class Task:
    queue: Q
    attempts: int = 0
    dead: bool = False


class FrontierMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.clock = Clock()
        self.f = RedisFrontier(
            CLIENT, SETTINGS, namespace=f"sm-{uuid.uuid4().hex[:10]}", clock=self.clock
        )
        self.tasks: dict[str, Task] = {}
        self.owner: dict[str, Claim] = {}  # url_id -> the only claim allowed to act
        self.issued: list[Claim] = []
        self.last_claim: dict[str, float] = {}

    # -- helpers -------------------------------------------------------------

    def active(self, queue: Q | None = None) -> list[str]:
        return [
            i for i, t in self.tasks.items() if not t.dead and (queue is None or t.queue == queue)
        ]

    def is_owner(self, claim: Claim) -> bool:
        current = self.owner.get(claim.url_id)
        return current is not None and current.token == claim.token

    # -- rules ---------------------------------------------------------------

    @rule(
        i=st.integers(0, len(URLS) - 1),
        queue=st.sampled_from(QUEUES),
        priority=st.integers(0, 100),
        delay=st.sampled_from([None, 0.5, 5.0]),
    )
    def admit(self, i: int, queue: Q, priority: int, delay: float | None) -> None:
        ref = URLS[i]
        not_before = None if delay is None else datetime.fromtimestamp(self.clock.now + delay, UTC)
        result = self.f.admit(Admission(ref, queue, priority, not_before))
        task = self.tasks.get(ref.url_id)
        if task is not None and not task.dead:
            assert result.outcome in (AdmitOutcome.MERGED, AdmitOutcome.DUPLICATE)
        elif len(self.active(queue)) >= MAX_DEPTH[queue]:
            assert result.outcome is AdmitOutcome.REJECTED_FULL
        else:
            # Temporary dedup: a finished or dead URL is admittable again.
            assert result.outcome in (AdmitOutcome.READY, AdmitOutcome.SCHEDULED)
            self.tasks[ref.url_id] = Task(queue)

    @rule(queue=st.sampled_from(QUEUES))
    def claim(self, queue: Q) -> None:
        claim = self.f.claim(queue)
        if claim is None:
            return
        task = self.tasks.get(claim.url_id)
        assert task is not None
        assert not task.dead
        assert task.queue is queue
        assert claim.url_id not in self.owner, "two simultaneous owners"
        last = self.last_claim.get(claim.domain_id)
        assert last is None or claim.claimed_at - last >= INTERVAL - 1e-6, "politeness"
        self.last_claim[claim.domain_id] = claim.claimed_at
        task.attempts += 1
        assert claim.attempt == task.attempts
        self.owner[claim.url_id] = claim
        self.issued.append(claim)

    @precondition(lambda self: self.issued)
    @rule(data=st.data())
    def heartbeat(self, data: st.DataObject) -> None:
        claim = data.draw(st.sampled_from(self.issued))
        renewed = self.f.heartbeat(claim)
        if self.is_owner(claim):
            assert renewed is not None
            self.owner[claim.url_id] = renewed
        else:
            assert renewed is None

    @precondition(lambda self: self.issued)
    @rule(data=st.data())
    def complete(self, data: st.DataObject) -> None:
        claim = data.draw(st.sampled_from(self.issued))
        owner = self.is_owner(claim)
        outcome = self.f.complete(claim)
        if owner:
            assert outcome is CompleteOutcome.COMPLETED
            del self.owner[claim.url_id]
            del self.tasks[claim.url_id]
        else:
            assert outcome is CompleteOutcome.STALE

    @precondition(lambda self: self.issued)
    @rule(data=st.data(), move=st.sampled_from([None, *QUEUES]))
    def fail(self, data: st.DataObject, move: Q | None) -> None:
        claim = data.draw(st.sampled_from(self.issued))
        owner = self.is_owner(claim)
        result = self.f.fail(claim, "synthetic", next_queue=move)
        if not owner:
            assert result.outcome is FailOutcome.STALE
            return
        del self.owner[claim.url_id]
        task = self.tasks[claim.url_id]
        if task.attempts < MAX_ATTEMPTS:
            assert result.outcome is FailOutcome.RETRY_SCHEDULED
            task.queue = move or task.queue
        else:
            assert result.outcome is FailOutcome.EXHAUSTED
            del self.tasks[claim.url_id]

    @precondition(lambda self: self.issued)
    @rule(data=st.data())
    def defer(self, data: st.DataObject) -> None:
        claim = data.draw(st.sampled_from(self.issued))
        owner = self.is_owner(claim)
        result = self.f.defer(claim)
        if owner:
            assert result.outcome is DeferOutcome.DEFERRED
            del self.owner[claim.url_id]
            self.tasks[claim.url_id].attempts -= 1
        else:
            assert result.outcome is DeferOutcome.STALE

    # Binary fractions keep the model clock and the Lua clock bit-identical.
    @rule(seconds=st.sampled_from([0.125, 0.5, 1.0, 3.0, 11.0]))
    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)

    @rule()
    def recover(self) -> None:
        expected_recovered = expected_dead = 0
        for url_id, claim in list(self.owner.items()):
            if claim.lease_expires_at <= self.clock.now:
                del self.owner[url_id]
                task = self.tasks[url_id]
                if task.attempts < MAX_ATTEMPTS:
                    expected_recovered += 1
                else:
                    expected_dead += 1
                    task.dead = True
        result = self.f.recover()
        assert (result.recovered, result.dead) == (expected_recovered, expected_dead)
        again = self.f.recover()  # duplicate sweeps are harmless
        assert (again.recovered, again.dead) == (0, 0)

    # -- invariants ------------------------------------------------------------

    @invariant()
    def frontier_matches_model(self) -> None:
        assert self.f.audit() == []
        stats = self.f.stats()
        for queue in QUEUES:
            assert stats.depth[queue] == len(self.active(queue))
        assert stats.leased == len(self.owner)
        assert stats.dead_letters == sum(t.dead for t in self.tasks.values())

    def teardown(self) -> None:
        try:
            # Liveness: with enough time every active task can be claimed and
            # finished; nothing is stranded or silently gone.
            for _ in range(30):
                self.clock.advance(100)
                self.recover()
                for queue in QUEUES:
                    while (claim := self.f.claim(queue)) is not None:
                        assert claim.url_id not in self.owner
                        assert self.f.complete(claim) is CompleteOutcome.COMPLETED
                        del self.tasks[claim.url_id]
                if not self.active():
                    break
            assert self.active() == []
            assert self.f.stats().active_tasks == 0
            assert self.f.audit() == []
        finally:
            self.f.clear()


FrontierMachine.TestCase.settings = settings(
    max_examples=60,
    stateful_step_count=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
test_frontier_state_machine = FrontierMachine.TestCase
