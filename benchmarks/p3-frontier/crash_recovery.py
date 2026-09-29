#!/usr/bin/env python3
"""Crash recovery timeline (port of V1 `tests/benchmarks/crash_recovery.py`).

V1 simulated the crash by never completing; here worker A is a real
process that claims and is SIGKILLed mid-work. Then, on Redis TIME:

  claim(A) -> kill -> lease expiry -> recover() -> backoff -> claim(B, attempt 2)
  -> A's token is inert (complete = stale) -> B completes.

Also a bulk variant: K workers each holding a claim are killed at once;
every task must be reclaimed and completed, none lost, none doubled.
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

from crawler2.core.configuration import ExecutionQueue
from crawler2.frontier import Claim, CompleteOutcome

Q = ExecutionQueue.HTTP


def doomed_worker(args: argparse.Namespace, namespace: str, conn: Any) -> None:
    f = common.frontier(args, namespace, **settings(args))
    claim = f.claim(Q)
    conn.send(claim)
    time.sleep(3600)  # "fetching" until killed


def settings(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "default_interval_s": 0.0,
        "lease_ttl_s": args.lease_ttl,
        "base_backoff_s": args.base_backoff,
        "max_backoff_s": args.base_backoff * 8,
    }


def kill_holders(args: argparse.Namespace, namespace: str, n: int) -> list[Claim]:
    ctx = mp.get_context("fork")
    held, procs = [], []
    for _ in range(n):
        parent, child = ctx.Pipe()
        proc = ctx.Process(target=doomed_worker, args=(args, namespace, child))
        proc.start()
        held.append(parent.recv())
        procs.append(proc)
    for proc in procs:
        os.kill(proc.pid, signal.SIGKILL)  # type: ignore[arg-type]
        proc.join()
    return held


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--lease-ttl", type=float, default=2.0)
    p.add_argument("--base-backoff", type=float, default=0.5)
    p.add_argument("--bulk", type=int, default=50)
    common.add_redis_args(p)
    args = p.parse_args()

    rid = common.run_id()
    namespace = f"bench-crash-{rid}"
    f = common.frontier(args, namespace, **settings(args))
    f.admit(next(common.admissions(1, common.domains(1, rid))))

    [a] = kill_holders(args, namespace, 1)
    t_claim = a.claimed_at
    recovered_at = None
    while recovered_at is None:
        if f.recover().recovered:
            recovered_at = time.time()
        else:
            time.sleep(0.05)
    b = None
    while b is None:
        f.recover()
        b = f.claim(Q)
        if b is None:
            time.sleep(0.02)
    single = {
        "lease_ttl_s": args.lease_ttl,
        "base_backoff_s": args.base_backoff,
        "claim_a_attempt": a.attempt,
        "claim_b_attempt": b.attempt,
        "same_task": a.url_id == b.url_id,
        "lease_expiry_after_claim_s": round(a.lease_expires_at - t_claim, 3),
        "recovered_after_claim_s": round(recovered_at - t_claim, 3),
        "reclaimed_after_claim_s": round(b.claimed_at - t_claim, 3),
        "stale_a_heartbeat": f.heartbeat(a) is None,
        "stale_a_complete": f.complete(a).value,
        "b_complete": f.complete(b).value,
    }

    # Bulk: K holders killed at once.
    f.admit_many(common.admissions(args.bulk, common.domains(args.bulk, rid, "bulk")))
    held = kill_holders(args, namespace, args.bulk)
    t0 = time.time()
    done: set[str] = set()
    while len(done) < args.bulk and time.time() - t0 < 60:
        f.recover()
        claim = f.claim(Q)
        if claim is None:
            time.sleep(0.02)
            continue
        if f.complete(claim) is CompleteOutcome.COMPLETED:
            done.add(claim.url_id)
    stale = sum(f.complete(c) is CompleteOutcome.STALE for c in held)
    bulk = {
        "killed_holders": args.bulk,
        "reclaimed_and_completed": len(done),
        "all_recovered_within_s": round(time.time() - t0, 2),
        "old_tokens_rejected": stale,
        "active_at_end": f.stats().active_tasks,
    }
    passed = (
        single["same_task"]
        and single["claim_b_attempt"] == 2
        and single["stale_a_complete"] == "stale"
        and single["b_complete"] == "completed"
        and single["stale_a_heartbeat"]
        and bulk["reclaimed_and_completed"] == args.bulk
        and bulk["old_tokens_rejected"] == args.bulk
        and bulk["active_at_end"] == 0
        and not f.audit()
    )
    f.clear()
    common.write(
        {
            "tool": "p3 crash recovery",
            "summary": {"single": single, "bulk": bulk, "passed": passed},
        },
        "crash-recovery",
        args.output,
    )


if __name__ == "__main__":
    main()
