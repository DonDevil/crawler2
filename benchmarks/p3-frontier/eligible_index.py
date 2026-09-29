#!/usr/bin/env python3
"""Eligible-domain index vs V1 `domain_scan_limit` (decision benchmark, §17).

Scenario per N (number of better-ranked domains, all currently gated):
N filler domains at high priority, each claimed once (so gated for an hour)
and still holding work; one low-priority "victim" domain with work and an
open gate. The correct answer of every claim is the victim.

Measured for V2 here (and for V1 by `v1_scan_probe.py`, same scenario, same
Redis): whether the victim is found, client-observed claim latency and
server-side EVALSHA time per claim, plus V2's gate-promotion burst (N gates
expiring at the same instant).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
from antipiracy_contracts.models.web import UrlRef

from crawler2.core.configuration import ExecutionQueue
from crawler2.frontier import Admission

Q = ExecutionQueue.HTTP


def probe(args: argparse.Namespace, n: int) -> dict[str, Any]:
    rid = common.run_id()
    r = common.client(args)
    f = common.frontier(
        args, f"bench-elig-{rid}", default_interval_s=3600.0, max_depth={"http": 10**7}
    )
    fillers = common.domains(n, rid, "fill")
    f.admit_many(
        a for d in range(2) for a in common.admissions(n, fillers, priority=90, start=d * n)
    )
    if any(f.claim(Q) is None for _ in range(n)):
        raise RuntimeError("setup: a filler was not claimable")
    victim = UrlRef.of(f"https://victim-{rid}.example.test/0")
    f.set_domain_interval(victim.domain_id, 0.0)
    f.admit_many(
        Admission(UrlRef.of(f"https://victim-{rid}.example.test/{i}"), priority=10)
        for i in range(args.samples)
    )
    lat: list[float] = []
    found = 0
    calls0, usec0 = common.evalsha_stats(r)
    for _ in range(args.samples):
        t0 = time.perf_counter()
        claim = f.claim(Q)
        lat.append(time.perf_counter() - t0)
        found += claim is not None and "victim" in claim.url
    calls1, usec1 = common.evalsha_stats(r)
    empty_t0 = time.perf_counter()
    for _ in range(args.samples):
        if f.claim(Q) is not None:
            raise RuntimeError("expected no eligible work")
    empty_us = (time.perf_counter() - empty_t0) / args.samples * 1e6
    f.clear()

    # Promotion burst: n gates expire in the same instant.
    f = common.frontier(
        args, f"bench-elig-b-{rid}", default_interval_s=1.0, max_depth={"http": 10**7}
    )
    f.admit_many(
        a for d in range(2) for a in common.admissions(n, fillers, priority=90, start=d * n)
    )
    for _ in range(n):
        f.claim(Q)
    time.sleep(1.05)
    burst: list[float] = []
    while True:
        t0 = time.perf_counter()
        claim = f.claim(Q)
        burst.append(time.perf_counter() - t0)
        if claim is None or len(burst) >= n:
            break
    f.clear()
    return {
        "gated_better_domains": n,
        "victim_found": f"{found}/{args.samples}",
        "claim_latency": common.percentiles(lat),
        "server_us_per_claim": round((usec1 - usec0) / max(1, calls1 - calls0), 1),
        "empty_claim_mean_us": round(empty_us, 1),
        "burst_first_claim_us": round(burst[0] * 1e6, 1),
        "burst_max_claim_us": round(max(burst) * 1e6, 1),
    }


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--sizes", default="50,250,260,1000,5000,20000")
    p.add_argument("--samples", type=int, default=500)
    common.add_redis_args(p)
    args = p.parse_args()
    rows = [probe(args, int(n)) for n in args.sizes.split(",")]
    common.write(
        {"tool": "p3 eligible-domain index", "summary": rows}, "eligible-index-v2", args.output
    )


if __name__ == "__main__":
    main()
