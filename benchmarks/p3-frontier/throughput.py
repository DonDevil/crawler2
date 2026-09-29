#!/usr/bin/env python3
"""Throughput ceiling: N independent worker processes, claim -> complete.

Port of V1 `tests/benchmarks/distributed_benchmark.py` (the source of the
~13.7k URLs/s V1 ceiling in `throughput-ceiling-audit.md`): same shape
(200 000 URLs round-robin over 40 domains, one priority, no rate limit,
no retries, 30 s cap, `multiprocessing.Process` workers each with their
own Redis connection) and the same headline metric,
`claims_per_sec = total claims / wall time from spawning the workers
until all have exited`.

A *claim operation* is one `claim(http)` that returned a task (one Lua
script) followed by its `complete` (one Lua script) — V1's
`get_next_url()` + `mark_visited()`. V1 additionally ran a client-side
blacklist check per claim; V2 has none (filtering is P6).

Also reports: steady-state rate (first to last claim), per-op latency
percentiles, Redis CPU (delta-based), EVALSHA server time, and
correctness: duplicate completions and lost tasks.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

from crawler2.core.configuration import ExecutionQueue
from crawler2.frontier import CompleteOutcome


def worker(args: argparse.Namespace, namespace: str, out: str) -> None:
    f = common.frontier(args, namespace, default_interval_s=0.0)
    claim_lat: list[float] = []
    done_lat: list[float] = []
    completed: list[str] = []
    first = last = 0.0
    idle = 0
    deadline = time.time() + args.duration
    clock = time.perf_counter
    while time.time() < deadline:
        t0 = clock()
        claim = f.claim(ExecutionQueue.HTTP)
        t1 = clock()
        if claim is None:
            idle += 1
            if idle > args.max_idle_polls or f.stats().active_tasks == 0:
                break
            time.sleep(args.poll_interval)
            continue
        idle = 0
        outcome = f.complete(claim)
        t2 = clock()
        claim_lat.append(t1 - t0)
        done_lat.append(t2 - t1)
        if outcome is CompleteOutcome.COMPLETED:
            completed.append(claim.url_id)
        first = first or time.time()
        last = time.time()
    Path(out).write_text(
        json.dumps(
            {
                "claims": len(claim_lat),
                "completed": completed,
                "claim_lat": claim_lat,
                "complete_lat": done_lat,
                "first": first,
                "last": last,
            }
        )
    )


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--urls", type=int, default=200_000)
    p.add_argument("--domains", type=int, default=40)
    p.add_argument("--duration", type=float, default=30.0)
    p.add_argument("--poll-interval", type=float, default=0.01)
    p.add_argument("--max-idle-polls", type=int, default=500)
    common.add_redis_args(p)
    args = p.parse_args()

    rid = common.run_id()
    namespace = f"bench-tp-{rid}"
    r = common.client(args)
    f = common.frontier(args, namespace, default_interval_s=0.0, max_depth={"http": args.urls})
    t0 = time.time()
    admitted = common.admit_all(f, common.admissions(args.urls, common.domains(args.domains, rid)))
    admit_s = time.time() - t0

    with tempfile.TemporaryDirectory() as tmp:
        outs = [str(Path(tmp) / f"w{i}.json") for i in range(args.workers)]
        cpu0, (calls0, usec0) = common.redis_cpu(r), common.evalsha_stats(r)
        start = time.time()
        procs = [mp.Process(target=worker, args=(args, namespace, o)) for o in outs]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join()
        elapsed = time.time() - start
        cpu1, (calls1, usec1) = common.redis_cpu(r), common.evalsha_stats(r)
        results: list[dict[str, Any]] = [json.loads(Path(o).read_text()) for o in outs]

    claims = sum(w["claims"] for w in results)
    owners: dict[str, int] = {}
    for w in results:
        for url_id in w["completed"]:
            owners[url_id] = owners.get(url_id, 0) + 1
    duplicates = sum(1 for n in owners.values() if n > 1)
    stats = f.stats()
    lost = admitted - len(owners) - stats.active_tasks
    first = min(w["first"] for w in results if w["first"])
    last = max(w["last"] for w in results)
    claim_lat = [x for w in results for x in w["claim_lat"]]
    done_lat = [x for w in results for x in w["complete_lat"]]
    summary = {
        "workers": args.workers,
        "urls": args.urls,
        "domains": args.domains,
        "rate_limit": "none (default_interval_s=0)",
        "max_inflight_per_domain": f.settings.max_inflight_per_domain,
        "claims": claims,
        "claims_per_sec": round(claims / elapsed),
        "steady_claims_per_sec": round(claims / (last - first)) if last > first else None,
        "elapsed_s": round(elapsed, 2),
        "redis_cpu_pct": round(100 * (cpu1 - cpu0) / elapsed, 1),
        "evalsha_calls": calls1 - calls0,
        "evalsha_us_per_call": round((usec1 - usec0) / max(1, calls1 - calls0), 2),
        "claim_latency": common.percentiles(claim_lat),
        "complete_latency": common.percentiles(done_lat),
        "duplicate_completions": duplicates,
        "lost_tasks": lost,
        "remaining_active": stats.active_tasks,
        "audit_problems": len(f.audit()) if stats.active_tasks < 50_000 else "skipped",
    }
    f.clear()
    common.write(
        {
            "tool": "p3 throughput",
            "label": args.label,
            "admit": {"admitted": admitted, "seconds": round(admit_s, 2)},
            "summary": summary,
            "environment": common.environment(r),
        },
        f"throughput-w{args.workers}",
        args.output,
    )


if __name__ == "__main__":
    main()
