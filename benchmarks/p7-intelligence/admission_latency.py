"""P7 increment 2: admission latency with and without source advice (design §9.4).

In-process: the P6 ``Admitter`` over an in-memory frontier and discovery store,
so only the cost P7 adds is measured (no Redis, no Scylla on the admission path
by design). Scenarios: P7 off; informed advice from a 10,000-host snapshot;
fallbacks (no snapshot, projection stale, snapshot loader hanging on a dead
store). Also the snapshot load cost (JSON decode of 10,000 rows).

    env/bin/python benchmarks/p7-intelligence/admission_latency.py \
        --out benchmarks/p7-intelligence/results/p7-admission-latency.json
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from antipiracy_contracts.models.web import FetchCapability, FetchOutcome, UrlRef

from crawler2.core.configuration import DiscoverySettings
from crawler2.discovery.admission import Admitter, Candidate, Origin
from crawler2.intelligence.advice import (
    ProfileSnapshot,
    SnapshotAdvisor,
    load_snapshot,
)
from crawler2.intelligence.projection import ProjectionState, StoredProfile
from crawler2.intelligence.sources import (
    DEFAULT_POLICY,
    SourceProfile,
    fact_from_attempt,
    fold,
)
from crawler2.storage.layout import outbox_bucket
from tests.unit.discovery.test_admission import FakeFrontier, _holder
from tests.unit.filtering.fakes import MemoryDiscovery
from tests.unit.intelligence.fakes import MemoryProfileStore, attempt

HOSTS = 10_000
BATCH = 50
BATCHES = 400
ROUNDS = 5


def _profiles(now: datetime, rng: random.Random) -> list[SourceProfile]:
    base = now - timedelta(hours=3)
    out = []
    for i in range(HOSTS):
        host = f"h{i}.bench.example"
        kind = rng.choice(("live", "dead", "spa"))
        if kind == "live":
            facts = [attempt(host, 170, base=base)]
        elif kind == "dead":
            facts = [
                attempt(host, m, path=f"d{m}", outcome=FetchOutcome.TIMEOUT, status=None, base=base)
                for m in (150, 160, 170)
            ]
        else:
            facts = [
                attempt(host, m, path=f"s{m}", detail="outcome=needs_js", base=base)
                for m in (140, 150, 160)
            ]
            facts += [
                attempt(host, m + 1, capability=FetchCapability.BROWSER, base=base)
                for m in (140, 150)
            ]
        p = None
        for a in facts:
            p = fold(p, fact_from_attempt(a))
        assert p is not None  # noqa: S101 -- facts is never empty
        out.append(p)
    return out


def _admitter(advisor: Any, now: datetime) -> Admitter:
    return Admitter(
        frontier=FakeFrontier(),
        discovery=MemoryDiscovery(),
        holder=_holder([]),
        settings=DiscoverySettings(revisit_after_s=0.0),
        decision_ttl_s=3600,
        clock=lambda: now,
        advisor=advisor,
    )


def _measure(make: Any, batches: list[list[Candidate]]) -> dict[str, float]:
    per_url: list[float] = []
    for _ in range(ROUNDS):
        admitter = make()  # fresh frontier/repo: every URL is new each round
        for batch in batches:
            t = time.perf_counter()
            admitter.admit(batch)
            per_url.append((time.perf_counter() - t) / len(batch) * 1e6)
    per_url.sort()
    return {
        "median_us_per_url": round(statistics.median(per_url), 2),
        "p99_us_per_url": round(per_url[int(len(per_url) * 0.99)], 2),
        "batches": len(per_url),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    rng = random.Random(20261007)  # noqa: S311 -- seeded workload
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    profiles = _profiles(now, rng)
    batches = [
        [
            Candidate(
                UrlRef.of(f"https://h{rng.randrange(HOSTS)}.bench.example/p{b}-{i}"), Origin.SEED
            )
            for i in range(BATCH)
        ]
        for b in range(BATCHES)
    ]
    fresh = ProjectionState(
        "sources", 1, DEFAULT_POLICY.version, outbox_bucket(now - timedelta(minutes=11))
    )
    stale = ProjectionState(
        "sources", 1, DEFAULT_POLICY.version, outbox_bucket(now - timedelta(hours=5))
    )

    def advisor(snapshot: ProfileSnapshot | None) -> SnapshotAdvisor:
        a = SnapshotAdvisor(
            lambda: snapshot,  # type: ignore[arg-type,return-value]
            max_lag=timedelta(hours=1),
            max_snapshot_age=timedelta(minutes=30),
            refresh_s=300,
            clock=lambda: now,
        )
        if snapshot is not None:
            a.refresh()
        return a

    by_host = {p.domain_id: p for p in profiles}
    release = threading.Event()

    def hanging() -> ProfileSnapshot:
        release.wait(60)
        raise ConnectionError("store unreachable")

    def hanging_advisor() -> SnapshotAdvisor:
        a = SnapshotAdvisor(
            hanging,
            max_lag=timedelta(hours=1),
            max_snapshot_age=timedelta(minutes=30),
            refresh_s=300,
        )
        a.start()
        return a

    scenarios = {
        "p7_off": lambda: _admitter(None, now),
        "p7_informed_10k_hosts": lambda: _admitter(
            advisor(ProfileSnapshot(fresh, by_host, now)), now
        ),
        "p7_fallback_no_snapshot": lambda: _admitter(advisor(None), now),
        "p7_fallback_projection_stale": lambda: _admitter(
            advisor(ProfileSnapshot(stale, by_host, now)), now
        ),
        "p7_fallback_store_hanging": lambda: _admitter(hanging_advisor(), now),
    }
    results = {name: _measure(make, batches) for name, make in scenarios.items()}
    release.set()

    store = MemoryProfileStore()
    store.states["sources"] = fresh
    store.put_profiles([StoredProfile(p, 1, DEFAULT_POLICY.version, 1) for p in profiles])
    t = time.perf_counter()
    loaded = load_snapshot(store, "sources", now=now, policy=DEFAULT_POLICY)
    load_s = time.perf_counter() - t
    assert len(loaded.profiles) == HOSTS  # noqa: S101 -- benchmark sanity check
    # The JSON codec is the per-row cost of the Scylla snapshot besides the scan itself.
    from crawler2.intelligence.projection import profile_from_json, profile_to_json

    docs = [profile_to_json(p) for p in profiles]
    t = time.perf_counter()
    for d in docs:
        profile_from_json(d)
    decode_s = time.perf_counter() - t

    off = results["p7_off"]["median_us_per_url"]
    doc = {
        "benchmark": "p7-admission-latency",
        "policy": DEFAULT_POLICY.version,
        "hosts": HOSTS,
        "batch": BATCH,
        "batches_per_round": BATCHES,
        "rounds": ROUNDS,
        "python": platform.python_version(),
        "machine": platform.machine(),
        "scenarios": results,
        "overhead_vs_off_median_us_per_url": {
            k: round(v["median_us_per_url"] - off, 2) for k, v in results.items()
        },
        "snapshot": {
            "rows": HOSTS,
            "decode_ms": round(decode_s * 1e3, 1),
            "bytes_per_row_json": round(sum(map(len, docs)) / len(docs)),
            "load_in_memory_ms": round(load_s * 1e3, 1),
        },
    }
    args.out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(json.dumps(doc, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
