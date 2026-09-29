"""Frontier & scheduling (P3): what is executable now, where, and by whom.

Redis holds hot execution state only; durable knowledge of URLs lives in
Scylla (ADR-015, ADR-016).
"""

from crawler2.core.configuration import ExecutionQueue
from crawler2.frontier.errors import FrontierError, FrontierUnavailableError
from crawler2.frontier.heartbeat import ClaimLostError, run_with_heartbeat
from crawler2.frontier.model import (
    Admission,
    AdmitOutcome,
    AdmitResult,
    Claim,
    CompleteOutcome,
    DeferOutcome,
    DeferResult,
    FailOutcome,
    FailResult,
    Frontier,
    FrontierStats,
    RecoveryResult,
    admission_from_request,
    queue_for_capability,
)

__all__ = [
    "Admission",
    "AdmitOutcome",
    "AdmitResult",
    "Claim",
    "ClaimLostError",
    "CompleteOutcome",
    "DeferOutcome",
    "DeferResult",
    "ExecutionQueue",
    "FailOutcome",
    "FailResult",
    "Frontier",
    "FrontierError",
    "FrontierStats",
    "FrontierUnavailableError",
    "RecoveryResult",
    "admission_from_request",
    "queue_for_capability",
    "run_with_heartbeat",
]
