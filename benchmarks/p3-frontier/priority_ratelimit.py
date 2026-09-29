#!/usr/bin/env python3
"""Priority x politeness interaction (port of V1 `tests/benchmarks/priority_ratelimit.py`).

Default scenario `urgent:90:2,normal:50:2,bulk:10:1` with a 2 s interval
(V1: `urgent:1:2,normal:5:2,bulk:10:1`; P1 priorities are higher-first):
urgent is claimed first and is then gated, so the lower-priority but
eligible domains are claimed during its gate instead of blocking; once the
gates reopen, priority order resumes. Run twice: all tasks on `http`, and
the same tasks alternated between `http` and `browser` with one claimer per
queue — the shared gate must give the same per-domain spacing.
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

QUEUES = (ExecutionQueue.HTTP, ExecutionQueue.BROWSER)


def parse(spec: str) -> list[tuple[str, int, int]]:
    out = []
    for item in spec.split(","):
        name, pri, count = item.split(":")
        out.append((name, int(pri), int(count)))
    return out


def run(args: argparse.Namespace, split_queues: bool) -> dict[str, Any]:
    rid = common.run_id()
    f = common.frontier(args, f"bench-prl-{rid}", default_interval_s=args.rate_limit)
    total = 0
    for name, pri, count in parse(args.scenario):
        for i in range(count):
            queue = QUEUES[i % 2] if split_queues else ExecutionQueue.HTTP
            ref = UrlRef.of(f"https://{name}-{rid}.example.test/{i}")
            f.admit(Admission(ref, queue=queue, priority=pri))
            total += 1
    order: list[dict[str, Any]] = []
    t0 = time.time()
    while len(order) < total and time.time() - t0 < 30:
        progressed = False
        for queue in QUEUES if split_queues else QUEUES[:1]:
            claim = f.claim(queue)
            if claim is None:
                continue
            progressed = True
            order.append(
                {
                    "domain": claim.url.split("/")[2].split("-")[0],
                    "queue": queue.value,
                    "priority": claim.priority,
                    "claimed_at": claim.claimed_at,
                }
            )
            f.complete(claim)
        if not progressed:
            time.sleep(0.01)
    f.clear()
    start = order[0]["claimed_at"] if order else 0.0
    last: dict[str, float] = {}
    min_gap = float("inf")
    for e in order:
        if e["domain"] in last:
            min_gap = min(min_gap, e["claimed_at"] - last[e["domain"]])
        last[e["domain"]] = e["claimed_at"]
        e["t_s"] = round(e.pop("claimed_at") - start, 3)
    return {
        "order": order,
        "min_same_domain_gap_s": round(min_gap, 4),
        "all_claimed": len(order) == total,
    }


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--scenario", default="urgent:90:2,normal:50:2,bulk:10:1")
    p.add_argument("--rate-limit", type=float, default=2.0)
    common.add_redis_args(p)
    args = p.parse_args()
    single = run(args, split_queues=False)
    split = run(args, split_queues=True)
    expected_first_round = ["urgent", "normal", "bulk"]
    summary = {
        "rate_limit_s": args.rate_limit,
        "single_queue": single,
        "two_queues_shared_gate": split,
        "passed": (
            [e["domain"] for e in single["order"][:3]] == expected_first_round
            and [e["domain"] for e in single["order"][3:]] == ["urgent", "normal"]
            and single["order"][3]["t_s"] >= args.rate_limit
            and single["min_same_domain_gap_s"] >= args.rate_limit
            and split["min_same_domain_gap_s"] >= args.rate_limit
            and single["all_claimed"]
            and split["all_claimed"]
        ),
    }
    common.write(
        {"tool": "p3 priority/rate-limit", "summary": summary}, "priority-ratelimit", args.output
    )


if __name__ == "__main__":
    main()
