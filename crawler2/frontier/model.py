"""Frontier vocabulary shared by the frontier implementation and its callers.

Workers (P4) and admission producers (P6/P7) depend on these types and on
the ``Frontier`` protocol only, never on Redis structures (ADR-001 rule 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from antipiracy_contracts.base import DEFAULT_PRIORITY
from antipiracy_contracts.events.web import CrawlRequested
from antipiracy_contracts.ids import DomainId, UrlId
from antipiracy_contracts.models.web import FetchCapability, UrlRef

from crawler2.core.configuration import ExecutionQueue

MIN_PRIORITY = 0
MAX_PRIORITY = 100

_QUEUE_FOR_CAPABILITY = {
    FetchCapability.HTTP: ExecutionQueue.HTTP,
    FetchCapability.BROWSER: ExecutionQueue.BROWSER,
    FetchCapability.TOR_HTTP: ExecutionQueue.TOR,
    FetchCapability.TOR_BROWSER: ExecutionQueue.TOR,
}


def queue_for_capability(capability: FetchCapability | None) -> ExecutionQueue:
    """P1 capability (what a fetch can do) -> execution queue (ADR-015)."""
    if capability is None:
        return ExecutionQueue.HTTP
    return _QUEUE_FOR_CAPABILITY[capability]


@dataclass(frozen=True, slots=True)
class Admission:
    """A request to make one URL executable, now or at ``not_before``."""

    url: UrlRef
    queue: ExecutionQueue = ExecutionQueue.HTTP
    priority: int = DEFAULT_PRIORITY
    """P1 priority: 0 (lowest) .. 100 (highest)."""
    not_before: datetime | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not MIN_PRIORITY <= self.priority <= MAX_PRIORITY:
            raise ValueError(f"priority must be 0..100, got {self.priority}")
        if self.not_before is not None and self.not_before.tzinfo is None:
            raise ValueError("not_before must be timezone-aware")
        if len(self.reason) > 64:
            raise ValueError("reason is limited to 64 characters")


def admission_from_request(request: CrawlRequested) -> Admission:
    """Map a P1 ``crawl.requested`` payload onto an admission."""
    return Admission(
        url=request.url,
        queue=queue_for_capability(request.capability),
        priority=request.priority,
        not_before=request.not_before,
        reason=request.reason.value,
    )


class AdmitOutcome(StrEnum):
    READY = "ready"
    SCHEDULED = "scheduled"
    MERGED = "merged"
    """An active task existed; its priority was raised or its due time moved earlier."""
    DUPLICATE = "duplicate"
    """An active task existed and nothing needed to change."""
    REJECTED_FULL = "rejected_full"
    """The queue is at ``max_depth``; nothing was stored. The caller keeps the request."""


@dataclass(frozen=True, slots=True)
class AdmitResult:
    outcome: AdmitOutcome
    existing_state: str | None = None
    """For MERGED/DUPLICATE: the state of the task that already existed."""

    @property
    def accepted(self) -> bool:
        return self.outcome is not AdmitOutcome.REJECTED_FULL


@dataclass(frozen=True, slots=True)
class Claim:
    """Sole ownership of one task attempt, proven by ``token``."""

    url_id: UrlId
    url: str
    domain_id: DomainId
    queue: ExecutionQueue
    priority: int
    attempt: int
    """1-based; counts every claim of this task, including ones lost to crashes."""
    token: str
    lease_expires_at: float
    claimed_at: float
    """Redis ``TIME`` of the claim (epoch seconds)."""
    reason: str = ""


class CompleteOutcome(StrEnum):
    COMPLETED = "completed"
    STALE = "stale"
    """The token is not current: completed already, or the lease was recovered."""


class FailOutcome(StrEnum):
    RETRY_SCHEDULED = "retry_scheduled"
    EXHAUSTED = "exhausted"
    """No attempts left; the task is gone and the caller records the outcome."""
    STALE = "stale"


class DeferOutcome(StrEnum):
    DEFERRED = "deferred"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class FailResult:
    outcome: FailOutcome
    retry_at: float | None = None


@dataclass(frozen=True, slots=True)
class DeferResult:
    outcome: DeferOutcome
    retry_at: float | None = None


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    recovered: int
    """Expired leases rescheduled for another attempt."""
    dead: int
    """Expired leases whose attempts were exhausted (now dead letters)."""
    promoted: int
    """Due scheduled tasks moved to ready."""


@dataclass(frozen=True, slots=True)
class FrontierStats:
    depth: dict[ExecutionQueue, int]
    eligible_domains: dict[ExecutionQueue, int]
    scheduled: int
    leased: int
    gated_domains: int
    dead_letters: int
    counters: dict[str, int] = field(default_factory=dict)
    saturated_domains: int = 0
    """Domains at their in-flight limit (not claimable until a lease ends)."""

    @property
    def active_tasks(self) -> int:
        return sum(self.depth.values())


class Frontier(Protocol):
    """Worker- and producer-facing frontier interface (P3)."""

    def admit(self, admission: Admission) -> AdmitResult: ...

    def claim(self, queue: ExecutionQueue) -> Claim | None: ...

    def heartbeat(self, claim: Claim) -> Claim | None: ...

    def complete(self, claim: Claim) -> CompleteOutcome: ...

    def fail(
        self, claim: Claim, reason: str, *, next_queue: ExecutionQueue | None = None
    ) -> FailResult: ...

    def defer(self, claim: Claim, *, delay_s: float | None = None) -> DeferResult: ...

    def recover(self) -> RecoveryResult: ...

    @property
    def lease_ttl_s(self) -> float: ...
