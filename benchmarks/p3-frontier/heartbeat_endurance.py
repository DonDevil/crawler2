#!/usr/bin/env python3
"""Heartbeat endurance (port of V1 `tests/benchmarks/heartbeat_endurance.py`).

Many claims whose work lasts far longer than the lease, while a recovery
sweep runs every `--recovery-interval` (as the real sweeper does):

- heartbeat ENABLED (`run_with_heartbeat`): no claim may be recovered and
  every completion must be accepted;
- heartbeat DISABLED (negative control): every claim is recovered and every
  late completion rejected as stale.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

from crawler2.core.configuration import ExecutionQueue
from crawler2.frontier import Claim, ClaimLostError, run_with_heartbeat
from crawler2.frontier.redis import RedisFrontier


async def fetch(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return "body"


async def run_mode(
    f: RedisFrontier, claims: list[Claim], work_s: float, heartbeat: bool
) -> dict[str, int]:
    outcomes = {"completed": 0, "stale": 0, "claim_lost": 0}

    async def one(claim: Claim) -> None:
        try:
            if heartbeat:
                _, claim = await run_with_heartbeat(f, claim, fetch(work_s))
            else:
                await fetch(work_s)
        except ClaimLostError:
            outcomes["claim_lost"] += 1
            return
        outcomes[(await asyncio.to_thread(f.complete, claim)).value] += 1

    await asyncio.gather(*(one(c) for c in claims))
    return outcomes


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--claims", type=int, default=200)
    p.add_argument("--work", type=float, default=30.0, help="seconds of work per claim")
    p.add_argument("--lease-ttl", type=float, default=3.0)
    p.add_argument("--recovery-interval", type=float, default=0.5)
    common.add_redis_args(p)
    args = p.parse_args()

    result: dict[str, Any] = {}
    for heartbeat in (True, False):
        rid = common.run_id()
        f = common.frontier(
            args,
            f"bench-hb-{rid}",
            default_interval_s=0.0,
            lease_ttl_s=args.lease_ttl,
            base_backoff_s=3600,
            max_backoff_s=3600,
        )
        f.admit_many(common.admissions(args.claims, common.domains(args.claims, rid)))
        claims = [c for c in (f.claim(ExecutionQueue.HTTP) for _ in range(args.claims)) if c]
        stop = threading.Event()
        recovered = [0]

        def sweep(
            f: RedisFrontier = f,
            stop: threading.Event = stop,
            recovered: list[int] = recovered,
        ) -> None:
            while not stop.is_set():
                recovered[0] += f.recover().recovered
                time.sleep(args.recovery_interval)

        sweeper = threading.Thread(target=sweep)
        sweeper.start()
        t0 = time.time()
        outcomes = asyncio.run(run_mode(f, claims, args.work, heartbeat))
        stop.set()
        sweeper.join()
        stats = f.stats()
        result["enabled" if heartbeat else "disabled"] = {
            "claims": len(claims),
            "work_s": args.work,
            "lease_ttl_s": args.lease_ttl,
            "work_to_lease_ratio": round(args.work / args.lease_ttl, 1),
            "recovered_by_sweep": recovered[0],
            **outcomes,
            "elapsed_s": round(time.time() - t0, 1),
            "audit_problems": len(f.audit()),
            "stale_counter": stats.counters.get("stale", 0),
        }
        f.clear()
    en, dis = result["enabled"], result["disabled"]
    result["passed"] = (
        en["recovered_by_sweep"] == 0
        and en["completed"] == en["claims"]
        and dis["recovered_by_sweep"] == dis["claims"]
        and dis["stale"] == dis["claims"]
        and en["audit_problems"] == dis["audit_problems"] == 0
    )
    common.write(
        {"tool": "p3 heartbeat endurance", "summary": result}, "heartbeat-endurance", args.output
    )


if __name__ == "__main__":
    main()
