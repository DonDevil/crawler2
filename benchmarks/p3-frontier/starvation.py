#!/usr/bin/env python3
"""Domain starvation (port of V1 `tests/benchmarks/domain_starvation.py`).

Fairness property, from V1 `domain-starvation-audit.md` §2 (kept as-is):
*starvation* is a domain with valid, currently non-gated queued work that
receives zero claims over a deterministic run, or whose wait is unbounded
in a variable the operator does not control. Ordinary strict-priority
delay is not starvation; "infinite higher-priority work at interval 0
starves lower priority" is the documented policy (audit §6, mechanism J)
and is reported, not failed.

Correction to V1's `scan-limit-window` scenario: V1 ran it at
`rate_limit = 0`, where strict priority alone starves the victim (V1's own
local-frontier control showed 0/400 too), so it never isolated the K-window
(mechanism I). Here it runs with a politeness interval, so every filler is
periodically gated and a frontier with full visibility must reach the
victim. V1 is measured on the same scenario with `--rate-limit 1`.

Scenarios: finite, rate-limit-skip, replenish, scan-window, retries,
multi-worker, recovery, cross-queue (new: one domain shared by two queues).
All use real Redis TIME.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
from antipiracy_contracts.models.web import UrlRef

from crawler2.core.configuration import ExecutionQueue
from crawler2.frontier import Admission, Claim

HTTP, BROWSER = ExecutionQueue.HTTP, ExecutionQueue.BROWSER


class Scenario:
    def __init__(self, args: argparse.Namespace, **settings: Any) -> None:
        self.args, self.rid = args, common.run_id()
        self.f = common.frontier(args, f"bench-starve-{self.rid}", **settings)
        self.seq = 0

    def host(self, name: str) -> str:
        return f"{name}-{self.rid}.example.test"

    def add(self, name: str, priority: int, count: int = 1, queue: ExecutionQueue = HTTP) -> None:
        for _ in range(count):
            self.seq += 1
            ref = UrlRef.of(f"https://{self.host(name)}/p{self.seq}")
            self.f.admit(Admission(ref, queue=queue, priority=priority))

    def name(self, claim: Claim) -> str:
        return claim.url.split("/")[2].removesuffix(f"-{self.rid}.example.test")

    def loop(
        self,
        num_claims: int,
        on_claim: Callable[[Claim], None] | None = None,
        *,
        max_idle_polls: int = 200,
        poll: float = 0.02,
        queue: ExecutionQueue = HTTP,
    ) -> list[dict[str, Any]]:
        log: list[dict[str, Any]] = []
        idle, t0 = 0, time.time()
        while len(log) < num_claims:
            claim = self.f.claim(queue)
            if claim is None:
                idle += 1
                if idle > max_idle_polls:
                    break
                time.sleep(poll)
                continue
            idle = 0
            log.append({"domain": self.name(claim), "t": time.time() - t0})
            if on_claim:
                on_claim(claim)
            else:
                self.f.complete(claim)
        return log

    def done(self) -> None:
        self.f.clear()


def fairness(log: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    last: dict[str, float] = {}
    for e in log:
        row = rows.setdefault(e["domain"], {"claims": 0, "first_t": e["t"], "max_wait_s": 0.0})
        row["claims"] += 1
        if e["domain"] in last:
            row["max_wait_s"] = round(max(row["max_wait_s"], e["t"] - last[e["domain"]]), 3)
        last[e["domain"]] = e["t"]
    return rows


def finite(args: argparse.Namespace) -> dict[str, Any]:
    s = Scenario(args, default_interval_s=0.0)
    s.add("high", 90, 5)
    s.add("low", 10, 3)
    order = [e["domain"] for e in s.loop(100)]
    s.done()
    return {
        "order": order,
        "all_claimed": len(order) == 8,
        "passed": order == ["high"] * 5 + ["low"] * 3,
    }


def rate_limit_skip(args: argparse.Namespace) -> dict[str, Any]:
    s = Scenario(args, default_interval_s=60.0)
    s.add("hot", 100)
    first = s.loop(1)
    s.add("hot", 100)
    s.add("cold", 1)
    nxt = s.loop(1, max_idle_polls=0)
    s.done()
    got = [e["domain"] for e in first + nxt]
    return {
        "order": got,
        "skipped_not_blocked": got == ["hot", "cold"],
        "passed": got == ["hot", "cold"],
    }


def replenish(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for interval in (0.0, 0.05):
        s = Scenario(args, default_interval_s=interval)
        s.add("a", 90, 5)
        s.add("b", 10, 10)

        def on_claim(c: Claim, s: Scenario = s) -> None:
            s.f.complete(c)
            if s.name(c) == "a":
                s.add("a", 90, 5)

        log = s.loop(300 if interval == 0 else 60, on_claim)
        rows = fairness(log)
        b = rows.get("b", {"claims": 0})
        out[f"interval_{interval}"] = {"claims": len(log), "a": rows.get("a"), "b": b}
        s.done()
    out["b_starved_at_interval_0"] = out["interval_0.0"]["b"]["claims"] == 0
    out["b_drained_at_interval_0.05"] = out["interval_0.05"]["b"]["claims"] == 10
    out["note"] = (
        "interval 0 + infinite higher-priority work starving B is the strict-priority policy"
    )
    out["passed"] = out["b_drained_at_interval_0.05"]
    return out


def scan_window(args: argparse.Namespace) -> dict[str, Any]:
    s = Scenario(args, default_interval_s=1.0)
    fillers = [f"fill{i}" for i in range(args.fillers)]
    for name in fillers:
        s.add(name, 90, 2)
    s.add("victim", 10, 5)

    def on_claim(c: Claim) -> None:
        s.f.complete(c)
        name = s.name(c)
        if name != "victim":
            s.add(name, 90)  # every filler stays non-empty forever

    log = s.loop(args.scan_claims, max_idle_polls=500)
    rows = fairness(log)
    victim = rows.get("victim", {"claims": 0})
    s.done()
    return {
        "fillers": args.fillers,
        "v1_domain_scan_limit_for_reference": 250,
        "interval_s": 1.0,
        "claims": len(log),
        "victim": victim,
        "victim_starved": victim["claims"] == 0,
        "passed": victim["claims"] > 0,
    }


def retries(args: argparse.Namespace) -> dict[str, Any]:
    s = Scenario(
        args, default_interval_s=0.0, max_attempts=5, base_backoff_s=0.05, max_backoff_s=0.2
    )
    s.add("a", 90, 3)
    s.add("b", 10, 10)

    def on_claim(c: Claim) -> None:
        if s.name(c) == "a":
            s.f.fail(c, "always fails")
        else:
            s.f.complete(c)

    rows = fairness(s.loop(100, on_claim))
    s.done()
    return {
        "rows": rows,
        "passed": rows.get("b", {}).get("claims") == 10 and rows["a"]["claims"] == 15,
    }


def multi_worker(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    ok = True
    for workers in (1, 2, 4, 8):
        s = Scenario(args, default_interval_s=0.0)
        s.add("high", 90, 200)
        s.add("low", 10, 20)
        seen: list[str] = []
        lock = threading.Lock()

        def run(s: Scenario = s, seen: list[str] = seen, lock: threading.Lock = lock) -> None:
            f = common.frontier(args, f"bench-starve-{s.rid}", default_interval_s=0.0)
            while (c := f.claim(HTTP)) is not None:
                with lock:
                    seen.append(c.url_id)
                f.complete(c)

        threads = [threading.Thread(target=run) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        dup = len(seen) - len(set(seen))
        out[f"workers_{workers}"] = {"claims": len(seen), "duplicates": dup}
        ok = ok and dup == 0 and len(seen) == 220
        s.done()
    out["passed"] = ok
    return out


def recovery(args: argparse.Namespace) -> dict[str, Any]:
    s = Scenario(
        args,
        default_interval_s=0.0,
        lease_ttl_s=1.0,
        base_backoff_s=0.05,
        max_backoff_s=0.05,
        max_attempts=10,
    )
    s.add("a", 90, 1)
    s.add("b", 10, 5)
    counts = {"a": 0, "b": 0}
    for _ in range(4):
        c = s.f.claim(HTTP)
        while c is not None and s.name(c) != "a":
            counts["b"] += 1
            s.f.complete(c)
            c = s.f.claim(HTTP)
        if c is not None:
            counts["a"] += 1  # abandoned: crashed worker
        time.sleep(1.1)
        s.f.recover()
        time.sleep(0.06)
        s.f.recover()
        c = s.f.claim(HTTP)
        if c is not None:
            if s.name(c) == "b":
                counts["b"] += 1
                s.f.complete(c)
            else:
                counts["a"] += 1
    s.done()
    return {"claims": counts, "passed": counts["b"] == 5}


def cross_queue(args: argparse.Namespace) -> dict[str, Any]:
    """One domain, http work replenished by 4 busy http workers, one browser task."""
    interval = 0.1
    s = Scenario(args, default_interval_s=interval)
    s.add("shared", 50, 4, queue=HTTP)
    s.add("shared", 50, 1, queue=BROWSER)
    stop = threading.Event()
    http_claims = [0]
    t0 = time.time()
    got: dict[str, float] = {}

    def http_worker() -> None:
        f = common.frontier(args, f"bench-starve-{s.rid}", default_interval_s=interval)
        while not stop.is_set():
            c = f.claim(HTTP)
            if c is None:
                continue  # busy-poll: worst case for the browser worker
            http_claims[0] += 1
            f.complete(c)
            s.add("shared", 50, queue=HTTP)

    def browser_worker() -> None:
        f = common.frontier(args, f"bench-starve-{s.rid}", default_interval_s=interval)
        while not stop.is_set():
            c = f.claim(BROWSER)
            if c is not None:
                got["t"] = time.time() - t0
                f.complete(c)
                stop.set()
            time.sleep(0.02)

    threads = [threading.Thread(target=http_worker) for _ in range(4)]
    threads.append(threading.Thread(target=browser_worker))
    for t in threads:
        t.start()
    stop.wait(timeout=args.cross_queue_timeout)
    stop.set()
    for t in threads:
        t.join()
    s.done()
    wait = got.get("t")
    return {
        "interval_s": interval,
        "http_claims_before_browser": http_claims[0],
        "browser_wait_s": round(wait, 3) if wait is not None else None,
        "gate_openings_before_browser": round(wait / interval) if wait is not None else None,
        "note": "work-conserving: whichever queue claims first after the gate opens wins",
        "passed": wait is not None,
    }


SCENARIOS = {
    "finite": finite,
    "rate-limit-skip": rate_limit_skip,
    "replenish": replenish,
    "scan-window": scan_window,
    "retries": retries,
    "multi-worker": multi_worker,
    "recovery": recovery,
    "cross-queue": cross_queue,
}


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("scenarios", nargs="*", help=f"subset of {sorted(SCENARIOS)} (default: all)")
    p.add_argument(
        "--fillers", type=int, default=260, help="scan-window filler domains (> V1 K=250)"
    )
    p.add_argument("--scan-claims", type=int, default=800)
    p.add_argument("--cross-queue-timeout", type=float, default=30.0)
    common.add_redis_args(p)
    args = p.parse_args()
    results: dict[str, Any] = {name: SCENARIOS[name](args) for name in args.scenarios or SCENARIOS}
    results["passed"] = all(r["passed"] for r in results.values())
    common.write({"tool": "p3 starvation", "summary": results}, "starvation", args.output)


if __name__ == "__main__":
    main()
