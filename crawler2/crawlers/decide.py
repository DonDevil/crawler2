"""Outcome → frontier operation: the static default profile (design §22, ADR-017).

A pure function, so the whole mapping is unit-testable and P7 can later
replace the escalation choice with learned fetch profiles without
touching fetchers or the runtime loop. It never retries by itself: every
retry or capability change goes through the frontier (``fail``), which
counts it against the task's one attempt budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from crawler2.core.configuration import ExecutionQueue
from crawler2.crawlers.model import AMBIGUOUS_OUTCOMES, FetchResult, Outcome


class Operation(StrEnum):
    COMPLETE = "complete"
    FAIL = "fail"
    DEFER = "defer"


@dataclass(frozen=True, slots=True)
class Decision:
    operation: Operation
    reason: str
    next_queue: ExecutionQueue | None = None
    consumes_attempt: bool = True

    @property
    def escalates(self) -> bool:
        return self.next_queue is not None


_COMPLETE = frozenset(
    {
        Outcome.OK,
        Outcome.NOT_MODIFIED,
        Outcome.MEDIA,
        Outcome.CAPTCHA,
        Outcome.TOO_LARGE,
        Outcome.REDIRECT_ERROR,
    }
)
_FAIL = frozenset({Outcome.INVALID_RESPONSE, Outcome.TLS_ERROR, Outcome.FETCHER_CRASH})
_DEFER = frozenset({Outcome.PROXY_UNAVAILABLE, Outcome.FETCHER_UNAVAILABLE, Outcome.CANCELLED})
_RETRYABLE_STATUS = frozenset({408, 429})


def decide(result: FetchResult, queue: ExecutionQueue, *, network_offline: bool) -> Decision:
    outcome = result.outcome
    reason = outcome.value
    if outcome in _DEFER:
        return Decision(Operation.DEFER, reason, consumes_attempt=False)
    if outcome in AMBIGUOUS_OUTCOMES:
        if network_offline:  # V1 N2 §6: only a probe-confirmed outage refunds the attempt
            return Decision(Operation.DEFER, "local_network_offline", consumes_attempt=False)
        return Decision(Operation.FAIL, reason)
    if outcome in _COMPLETE:
        return Decision(Operation.COMPLETE, reason)
    if outcome in _FAIL:
        return Decision(Operation.FAIL, reason)
    if outcome is Outcome.NEEDS_JS:
        if queue is ExecutionQueue.HTTP:
            return Decision(Operation.FAIL, reason, next_queue=ExecutionQueue.BROWSER)
        return Decision(Operation.COMPLETE, reason)  # rendered already, or no tor browser
    if outcome is Outcome.BLOCKED:
        if result.status in _RETRYABLE_STATUS:
            return Decision(Operation.FAIL, f"blocked_{result.status}")
        return Decision(Operation.COMPLETE, reason)  # recorded; no escalation, no bypass
    if outcome is Outcome.HTTP_ERROR:
        status = result.status or 0
        if status >= 500 or status in _RETRYABLE_STATUS:
            return Decision(Operation.FAIL, f"http_{status}")
        return Decision(Operation.COMPLETE, f"http_{status}")
    raise AssertionError(f"unmapped outcome {outcome}")  # pragma: no cover
