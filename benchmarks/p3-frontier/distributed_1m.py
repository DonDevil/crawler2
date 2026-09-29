#!/usr/bin/env python3
"""1M-claim distributed run with random worker kills, pauses and sweeper kills.

Methodology (P3 phase document §21):

- N_TASKS tasks (default 1 000 000) over 1 000 domains, random P1
  priorities, no politeness interval, one admission per URL (never
  re-admitted), so every task must be completed **exactly once**.
- W worker processes: claim -> (0.5 %: fail, frontier schedules a retry;
  0.1 %: slow work with a heartbeat) -> complete. Every claim, heartbeat and
  outcome is appended to the worker's own log with an unbuffered write
  *before* the next Redis call, so a SIGKILL loses at most the reply of the
  in-flight call.
- Chaos: every 1-4 s a random worker is SIGKILLed and replaced; every
  ~10 s a random worker is SIGSTOPped for longer than the lease and then
  resumed (a zombie that still believes it owns its task); every ~15 s the
  recovery/promotion sweeper is SIGKILLed and restarted.

Definitions:

- **lost task**: admitted but neither completed nor still active at the end
  (frontier counters + audit).
- **duplicate completion**: two accepted completions for one task.
- **simultaneous ownership**: a claim of task X issued while an earlier
  claim of X was still valid, i.e. `B.claimed_at < A.valid_until` where
  A's validity ends at its last lease expiry (claim/heartbeat) or at its
  explicit fail/defer. Measured on Redis TIME from the logs.
- **legitimate reclaim**: a later claim after the earlier owner's lease
  expired without an outcome (killed or paused worker). Counted, not an
  error.
- **stale report**: a zombie's outcome rejected with `stale`. Counted, not
  an error; an *accepted* outcome from a superseded token is an error.
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import random
import signal
import sys
import tempfile
import time
from collections import defaultdict
from itertools import pairwise
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

from crawler2.core.configuration import ExecutionQueue

SETTINGS: dict[str, Any] = {
    "default_interval_s": 0.0,
    "lease_ttl_s": 2.0,
    "max_attempts": 25,
    "base_backoff_s": 0.05,
    "max_backoff_s": 0.5,
    "recover_batch": 2000,
    "promote_batch": 2000,
}


def worker(args: argparse.Namespace, namespace: str, log_path: str, seed: int) -> None:
    rng = random.Random(seed)
    f = common.frontier(args, namespace, **SETTINGS)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)

    def log(*fields: object) -> None:
        os.write(fd, (" ".join(map(str, fields)) + "\n").encode())

    idle = 0
    while True:
        claim = f.claim(ExecutionQueue.HTTP)
        if claim is None:
            idle += 1
            if idle % 50 == 0 and f.stats().active_tasks == 0:
                return
            time.sleep(0.01)
            continue
        idle = 0
        log("C", claim.url_id, claim.token, claim.attempt, claim.claimed_at, claim.lease_expires_at)
        roll = rng.random()
        if roll < args.slow_rate:
            time.sleep(0.6)
            renewed = f.heartbeat(claim)
            if renewed is None:
                log("H", claim.url_id, claim.token, "lost")
                continue
            claim = renewed
            log("H", claim.url_id, claim.token, claim.lease_expires_at)
            time.sleep(0.6)
        if roll > 1 - args.fail_rate:
            result = f.fail(claim, "synthetic")
            log("F", claim.url_id, claim.token, result.outcome.value, result.retry_at or 0)
        else:
            outcome = f.complete(claim)
            log("D", claim.url_id, claim.token, outcome.value)


def sweeper(args: argparse.Namespace, namespace: str) -> None:
    f = common.frontier(args, namespace, **SETTINGS)
    while True:
        f.recover()
        time.sleep(0.2)


def verify(logs: list[Path], admitted: int) -> dict[str, Any]:
    claims: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_token: dict[str, dict[str, Any]] = {}
    for path in logs:
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue  # torn last line of a killed worker
            kind, url_id, token = parts[0], parts[1], parts[2]
            if kind == "C" and len(parts) == 6:
                c = {"token": token, "at": float(parts[4]), "until": float(parts[5]), "end": None}
                claims[url_id].append(c)
                by_token[token] = c
            elif kind == "H" and token in by_token and parts[3] != "lost":
                by_token[token]["until"] = float(parts[3])
            elif kind == "F" and token in by_token and len(parts) == 5:
                by_token[token]["end"] = "fail:" + parts[3]
                if parts[3] == "retry_scheduled":
                    by_token[token]["until"] = min(by_token[token]["until"], float(parts[4]))
            elif kind == "D" and token in by_token:
                by_token[token]["end"] = "done:" + parts[3]

    overlaps = reclaims = retries = stale_reports = 0
    accepted_completions: dict[str, int] = defaultdict(int)
    completion_not_last = 0
    for url_id, cs in claims.items():
        cs.sort(key=lambda c: c["at"])
        for a, b in pairwise(cs):
            if b["at"] < a["until"] - 1e-6:
                overlaps += 1
            elif a["end"] is None or a["end"] == "done:stale":
                reclaims += 1
            else:
                retries += 1
        for i, c in enumerate(cs):
            if c["end"] == "done:completed":
                accepted_completions[url_id] += 1
                if i != len(cs) - 1:
                    completion_not_last += 1
            elif c["end"] in ("done:stale", "fail:stale"):
                stale_reports += 1
    return {
        "logged_claims": sum(len(cs) for cs in claims.values()),
        "tasks_claimed": len(claims),
        "simultaneous_ownership": overlaps,
        "legitimate_reclaims": reclaims,
        "retry_reclaims": retries,
        "stale_reports_rejected": stale_reports,
        "logged_duplicate_completions": sum(1 for n in accepted_completions.values() if n > 1),
        "accepted_completion_by_superseded_token": completion_not_last,
        "tasks_with_logged_completion": len(accepted_completions),
        "tasks_never_claimed": admitted - len(claims),
    }


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--tasks", type=int, default=1_000_000)
    p.add_argument("--domains", type=int, default=1000)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--fail-rate", type=float, default=0.005)
    p.add_argument("--slow-rate", type=float, default=0.001)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--timeout", type=float, default=1800)
    common.add_redis_args(p)
    args = p.parse_args()

    rng = random.Random(args.seed)
    rid = common.run_id()
    namespace = f"bench-1m-{rid}"
    r = common.client(args)
    f = common.frontier(args, namespace, **SETTINGS, max_depth={"http": args.tasks})
    names = common.domains(args.domains, rid)
    t0 = time.time()
    admitted = 0
    for offset in range(0, args.tasks, 10_000):
        batch = common.admissions(
            min(10_000, args.tasks - offset),
            names,
            priority=lambda _: rng.randint(0, 100),
            start=offset,
        )
        admitted += sum(x.accepted for x in f.admit_many(batch))
    admit_s = time.time() - t0
    print(f"admitted {admitted} in {admit_s:.1f}s", file=sys.stderr)

    chaos = {"worker_kills": 0, "worker_pauses": 0, "sweeper_kills": 0}
    with tempfile.TemporaryDirectory(prefix="p3-1m-") as tmp:
        logs: list[Path] = []
        seq = 0

        def spawn() -> mp.Process:
            nonlocal seq
            seq += 1
            path = Path(tmp) / f"worker-{seq}.log"
            logs.append(path)
            proc = mp.Process(target=worker, args=(args, namespace, str(path), rng.random()))
            proc.start()
            return proc

        def spawn_sweeper() -> mp.Process:
            proc = mp.Process(target=sweeper, args=(args, namespace), daemon=True)
            proc.start()
            return proc

        cpu0 = common.redis_cpu(r)
        start = time.time()
        workers = [spawn() for _ in range(args.workers)]
        sweep = spawn_sweeper()
        next_kill: float = start + rng.uniform(1, 4)
        next_pause: float = start + 10
        next_sweeper_kill: float = start + 15
        while time.time() - start < args.timeout:
            time.sleep(0.05)
            now = time.time()
            workers = [w for w in workers if w.is_alive()]
            stats = f.stats()
            if stats.active_tasks == 0:
                break
            if now >= next_kill and workers:
                victim = rng.choice(workers)
                os.kill(victim.pid, signal.SIGKILL)  # type: ignore[arg-type]
                victim.join()
                workers.remove(victim)
                chaos["worker_kills"] += 1
                next_kill = now + rng.uniform(1, 4)
            if now >= next_pause and workers:
                zombie = rng.choice(workers)
                os.kill(zombie.pid, signal.SIGSTOP)  # type: ignore[arg-type]
                time.sleep(SETTINGS["lease_ttl_s"] + 1.5)
                os.kill(zombie.pid, signal.SIGCONT)  # type: ignore[arg-type]
                chaos["worker_pauses"] += 1
                next_pause = time.time() + 10
            if now >= next_sweeper_kill:
                os.kill(sweep.pid, signal.SIGKILL)  # type: ignore[arg-type]
                sweep.join()
                sweep = spawn_sweeper()
                chaos["sweeper_kills"] += 1
                next_sweeper_kill = now + 15
            while len(workers) < args.workers:
                workers.append(spawn())
        elapsed = time.time() - start
        cpu1 = common.redis_cpu(r)
        for w in workers:
            w.join(timeout=30)
        os.kill(sweep.pid, signal.SIGKILL)  # type: ignore[arg-type]
        sweep.join()
        checks = verify(logs, admitted)

    stats = f.stats()
    c = stats.counters
    problems = f.audit()
    summary = {
        "tasks_admitted": admitted,
        "workers": args.workers,
        "elapsed_s": round(elapsed, 1),
        "claims_total": c.get("claimed", 0),
        "claims_per_sec": round(c.get("claimed", 0) / elapsed),
        "redis_cpu_pct": round(100 * (cpu1 - cpu0) / elapsed, 1),
        "chaos": chaos,
        "completed": c.get("completed", 0),
        "exhausted": c.get("exhausted", 0),
        "dead": c.get("dead", 0),
        "retried": c.get("retried", 0),
        "recovered_leases": c.get("recovered", 0),
        "stale_rejected": c.get("stale", 0),
        "anomalies": c.get("anomaly", 0),
        "active_at_end": stats.active_tasks,
        "lost_tasks": admitted
        - c.get("completed", 0)
        - c.get("exhausted", 0)
        - c.get("dead", 0)
        - stats.active_tasks,
        "duplicate_completions": c.get("completed", 0) - admitted + stats.active_tasks,
        "audit_problems": len(problems),
        **checks,
    }
    summary["gate_passed"] = (
        summary["lost_tasks"] == 0
        and summary["duplicate_completions"] == 0
        and summary["logged_duplicate_completions"] == 0
        and summary["simultaneous_ownership"] == 0
        and summary["accepted_completion_by_superseded_token"] == 0
        and summary["completed"] == admitted
        and summary["audit_problems"] == 0
    )
    f.clear()
    common.write(
        {
            "tool": "p3 distributed 1M",
            "label": args.label,
            "settings": SETTINGS,
            "admit_seconds": round(admit_s, 1),
            "summary": summary,
            "environment": common.environment(r),
        },
        "distributed-1m",
        args.output,
    )


if __name__ == "__main__":
    main()
