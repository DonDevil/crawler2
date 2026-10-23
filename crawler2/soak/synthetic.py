"""Deterministic synthetic workload for dry runs and tests (mode ``synthetic`` only).

Emits envelopes shaped like the real contract events into the ledger, so reconciliation,
duplicate and stuck-work detection are exercised without Redis, a crawler or a matcher.
Fault knobs let tests prove that each defect class is detected. Nothing here is evidence
about the real pipeline.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from crawler2.soak.reconcile import EvidenceResult, Ledger, TailResult


@dataclass
class Faults:
    lose_every: int = 0
    """Every Nth encode request never gets a result (lost work)."""
    duplicate_effect_every: int = 0
    """Every Nth request gets two different representation ids (duplicate business effect)."""
    redeliver_every: int = 0
    """Every Nth event is delivered twice (explained redelivery)."""
    finalize_every: int = 0
    """Every Nth completed request also yields a finalized evidence id."""
    skip_types: set[str] = field(default_factory=set)
    gap_at_poll: int | None = None


def _env(etype: str, event_id: str, now: float, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": etype,
        "occurred_at": datetime.fromtimestamp(now, UTC).isoformat(),
        "payload": payload,
    }


class SyntheticWorkload:
    def __init__(
        self,
        ledger: Ledger,
        *,
        per_poll: int = 5,
        seed: int = 7,
        faults: Faults | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._ledger = ledger
        self._per_poll = per_poll
        self._rng = random.Random(seed)
        self._faults = faults or Faults()
        self._n = 0
        self._polls = 0
        self._pending: list[tuple[int, int]] = []  # (due poll, request number)
        self._now = now

    def start_positions(self) -> dict[str, dict[str, Any]]:
        return {}

    def _emit(self, env: dict[str, Any], result: TailResult, now: float) -> None:
        if env["event_type"] in self._faults.skip_types:
            return
        verdict = self._ledger.ingest(env, now)
        result.ingested += verdict == "new"
        result.redeliveries += verdict == "redelivery"
        every = self._faults.redeliver_every
        if every and self._n % every == 0 and self._ledger.ingest(env, now) == "redelivery":
            result.redeliveries += 1

    def poll(self, positions: dict[str, dict[str, Any]]) -> TailResult:
        now = self._now() if self._now else __import__("time").time()
        self._polls += 1
        result = TailResult()
        if self._faults.gap_at_poll == self._polls:
            result.gaps.append(
                {"stream": "synthetic", "event_type": "encode.requested", "entries_lost": 3}
            )
        f = self._faults
        for _ in range(self._per_poll):
            self._n += 1
            n = self._n
            cid = hashlib.sha256(f"content-{n}".encode()).hexdigest()[:32]
            self._emit(
                _env(
                    "encode.requested", f"req-{n}", now, {"content": {"content_id": f"cnt_{cid}"}}
                ),
                result,
                now,
            )
            lost = bool(f.lose_every and n % f.lose_every == 0)
            if not lost:
                self._pending.append((self._polls + self._rng.randint(1, 2), n))
        due = [p for p in self._pending if p[0] <= self._polls]
        self._pending = [p for p in self._pending if p[0] > self._polls]
        for _, n in due:
            cid = hashlib.sha256(f"content-{n}".encode()).hexdigest()[:32]
            rep = {
                "representation": {"content_id": f"cnt_{cid}", "representation_id": f"rep_{cid}"}
            }
            self._emit(_env("representation.ready", f"rdy-{n}", now, {**rep}), result, now)
            if f.duplicate_effect_every and n % f.duplicate_effect_every == 0:
                other = {
                    "representation": {
                        "content_id": f"cnt_{cid}",
                        "representation_id": f"rep_other_{cid}",
                    }
                }
                self._emit(_env("representation.ready", f"rdy-dup-{n}", now, other), result, now)
            if f.finalize_every and n % f.finalize_every == 0:
                self._emit(
                    _env(
                        "evidence.finalized",
                        f"fin-{n}",
                        now,
                        {
                            "seal": {
                                "evidence_id": f"evd_{n}",
                                "manifest": {"digest": f"sha256:{cid}"},
                            }
                        },
                    ),
                    result,
                    now,
                )
            self._emit(
                _env("match.found", f"mf-{n}", now, {"match": {"match_id": f"mat_{n}"}}),
                result,
                now,
            )
        return result


class SyntheticVerifier:
    """Stands in for the evidence validator in synthetic runs; ``bad`` ids fail verification."""

    def __init__(self, bad: set[str] | None = None, missing: set[str] | None = None) -> None:
        self._bad, self._missing = bad or set(), missing or set()

    def verify(self, evidence_ids: list[str]) -> EvidenceResult:
        res = EvidenceResult(checked=len(evidence_ids))
        res.mismatched = [i for i in evidence_ids if i in self._bad]
        res.missing = [i for i in evidence_ids if i in self._missing]
        return res
