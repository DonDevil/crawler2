#!/usr/bin/env python3
"""V1 side of the eligible-index decision benchmark — runs V1's own code.

Run with V1's interpreter; V1 is imported read-only (no bytecode written):

    PYTHONDONTWRITEBYTECODE=1 ~/anti_piracy/crawler/env/bin/python \
        benchmarks/p3-frontier/v1_scan_probe.py --v1-root ~/anti_piracy/crawler

Same scenario as `eligible_index.py`: N better-ranked domains claimed once
(gated by V1's global `rate_limit`, here 3600 s) and still holding work;
low-priority victim domains with open gates. V1 has no per-domain interval,
so each sample uses a fresh victim domain (a victim is gated after its
claim). `domain_scan_limit` = V1's production value, 250.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
import uuid
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--v1-root", required=True)
    p.add_argument("--sizes", default="50,250,260,1000,5000,20000")
    p.add_argument("--samples", type=int, default=500)
    p.add_argument("--domain-scan-limit", type=int, default=250)
    p.add_argument("--redis-port", type=int, default=16380)
    p.add_argument("--redis-db", type=int, default=2)
    p.add_argument("--output", required=True)
    args = p.parse_args()

    root = Path(args.v1_root).expanduser()
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tests" / "benchmarks"))
    common = importlib.import_module("common")  # V1 tests/benchmarks/common.py

    common.isolate_blacklist()
    rows = []
    for n in (int(x) for x in args.sizes.split(",")):
        rid = uuid.uuid4().hex[:8]
        f = common.build_frontier(
            "redis",
            rate_limit=3600.0,
            redis_port=args.redis_port,
            redis_db=args.redis_db,
            namespace=f"bench_v1scan_{rid}",
            domain_scan_limit=args.domain_scan_limit,
        )
        f.clear()
        for rep in range(2):
            for i in range(n):
                f.add_url(f"https://fill{i}-{rid}.example.test/{rep}", priority=1)
        if any(f.get_next_url() is None for _ in range(n)):
            raise RuntimeError("setup: a filler was not claimable")
        for i in range(args.samples):
            f.add_url(f"https://victim{i}-{rid}.example.test/0", priority=100)
        info0 = f.redis_conn.info("commandstats").get("cmdstat_evalsha", {})
        lat, found = [], 0
        for _ in range(args.samples):
            t0 = time.perf_counter()
            claim = f.get_next_url()
            lat.append(time.perf_counter() - t0)
            found += claim is not None and "victim" in claim.url
        info1 = f.redis_conn.info("commandstats").get("cmdstat_evalsha", {})
        calls = max(1, info1.get("calls", 0) - info0.get("calls", 0))
        f.clear()
        lat.sort()
        rows.append(
            {
                "gated_better_domains": n,
                "domain_scan_limit": args.domain_scan_limit,
                "victim_found": f"{found}/{args.samples}",
                "claim_latency": {
                    "p50_us": round(lat[len(lat) // 2] * 1e6, 1),
                    "p99_us": round(lat[int(len(lat) * 0.99)] * 1e6, 1),
                    "max_us": round(lat[-1] * 1e6, 1),
                },
                "server_us_per_claim": round(
                    (info1.get("usec", 0) - info0.get("usec", 0)) / calls, 1
                ),
            }
        )
        print(json.dumps(rows[-1]), file=sys.stderr)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"tool": "v1 domain_scan_limit probe", "summary": rows}, indent=2))


if __name__ == "__main__":
    main()
