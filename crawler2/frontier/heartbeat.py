"""Lease renewal for long-running attempts (port of V1 ``core/claim_heartbeat.py``).

A short lease detects crashed workers quickly; a live worker proves it is
alive by renewing every ``lease_ttl / 3`` (two spare renewals before
expiry). Renewal returning ``None`` means the claim was recovered or
completed elsewhere: the work is cancelled and ``ClaimLostError`` raised —
its result is no longer authoritative and must not be reported. A Redis
outage during renewal is not a lost claim: it is retried on the next tick
and the work continues (docs/phases/p03-frontier-scheduling §12, §19).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from crawler2.core.observability import get_logger
from crawler2.frontier.errors import FrontierUnavailableError
from crawler2.frontier.model import Claim, Frontier

_MIN_INTERVAL_S = 0.05
_log = get_logger("frontier.heartbeat")


class ClaimLostError(Exception):
    """The claim is no longer this worker's; do not complete/fail/defer it."""

    def __init__(self, claim: Claim) -> None:
        self.claim = claim
        super().__init__(f"claim lost for {claim.url_id} (attempt {claim.attempt})")


def default_interval(lease_ttl_s: float) -> float:
    return max(_MIN_INTERVAL_S, lease_ttl_s / 3.0)


async def run_with_heartbeat[T](
    frontier: Frontier,
    claim: Claim,
    work: Awaitable[T],
    *,
    interval_s: float | None = None,
) -> tuple[T, Claim]:
    """Await ``work`` while renewing ``claim``; return its result and the latest claim."""
    interval = max(_MIN_INTERVAL_S, interval_s or default_interval(frontier.lease_ttl_s))
    task = asyncio.ensure_future(work)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval)
            if done:
                return task.result(), claim
            try:
                renewed = await asyncio.to_thread(frontier.heartbeat, claim)
            except FrontierUnavailableError as exc:
                _log.warning("heartbeat_unavailable", url_id=claim.url_id, error=str(exc))
                continue
            if renewed is None:
                raise ClaimLostError(claim)
            claim = renewed
    finally:
        if not task.done():
            task.cancel()
