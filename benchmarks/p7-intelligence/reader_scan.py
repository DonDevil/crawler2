"""P7 increment 2: projection-reader scan rate on the real outbox (ADR-023).

Runs a fresh projection over a **throwaway** keyspace (default: the P7
integration keyspace ``crawler2_it_p7_host``) from its oldest retained bucket
and reports buckets and facts per second. A full rebuild reads 14 d x 1,440 = 20,160
buckets x 32 shard partitions; this gives its order of magnitude on the dev
host (USB HDD, single Scylla node). It never touches a run's keyspace.

    scripts/test-intelligence.sh -k stopped      # creates/fills the keyspace once
    CRAWLER2_SCYLLA__CONTACT_POINTS=<scylla container ip> \
      env/bin/python benchmarks/p7-intelligence/reader_scan.py --keyspace crawler2_it_p7_host \
        --out benchmarks/p7-intelligence/results/p7-reader-scan.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from crawler2.core.configuration import Settings
from crawler2.intelligence.projection import ReaderConfig, SourceProjector
from crawler2.intelligence.store import ScyllaSourceProfileStore
from crawler2.storage.scylla import ScyllaStorage


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyspace", required=True)
    ap.add_argument("--max-buckets", type=int, default=600)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    if not args.keyspace.startswith("crawler2_it_"):
        raise SystemExit("refusing a non-test keyspace")
    storage = ScyllaStorage.open(Settings().scylla, keyspace=args.keyspace, instance="p7-bench")
    projector = SourceProjector(
        storage.outbox,
        ScyllaSourceProfileStore(storage.session),
        config=ReaderConfig(
            projection=f"bench-{uuid.uuid4().hex[:8]}",
            settle=timedelta(0),
            max_buckets_per_cycle=args.max_buckets,
        ),
    )
    t = time.perf_counter()
    first = projector.run_once()  # bootstrap + up to max_buckets buckets
    elapsed = time.perf_counter() - t
    doc = {
        "benchmark": "p7-reader-scan",
        "keyspace": args.keyspace,
        "buckets": first.buckets,
        "shard_reads": first.buckets * 32,
        "facts_applied": first.applied,
        "profile_writes": first.profile_writes,
        "seconds": round(elapsed, 2),
        "buckets_per_s": round(first.buckets / elapsed, 1),
        "full_rebuild_estimate_min": round(14 * 1440 / (first.buckets / elapsed) / 60, 1),
    }
    args.out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(json.dumps(doc, indent=2, sort_keys=True))
    storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
