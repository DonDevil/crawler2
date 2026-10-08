"""CPU cost of P7 WHAT on the M1 seed population (what.md §6.4).

Reads ``var/p7/what/pages.jsonl.gz`` into memory first (not timed), then
times ``build_site_profile`` per host and ``analyze_page`` per page. No
network or storage is involved. Output: ``results/what-bench.json``.
"""

from __future__ import annotations

import gzip
import json
import platform
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from crawler2.intelligence.what import (
    analyze_page,
    build_site_profile,
    facts_from_export,
    load_vocabulary,
    tokenize_url,
)

PAGES = Path("var/p7/what/pages.jsonl.gz")
OUT = Path(__file__).parent / "results" / "what-bench.json"


def main() -> None:
    vocab = load_vocabulary()
    by_host = defaultdict(list)
    with gzip.open(PAGES, "rt", encoding="utf-8") as fh:
        for line in fh:
            facts = facts_from_export(json.loads(line))
            shape = tokenize_url(facts.url)
            if shape is not None:
                by_host[shape.host].append(facts)
    build: dict[str, float] = {}
    profiles = {}
    for host in sorted(by_host):
        t = time.perf_counter()
        profiles[host] = build_site_profile(host, by_host[host], vocab=vocab)
        build[host] = time.perf_counter() - t
    per_page: list[float] = []
    kinds: Counter[str] = Counter()
    for host in sorted(by_host):
        for facts in by_host[host]:
            t = time.perf_counter()
            res = analyze_page(facts, profiles[host], vocab=vocab)
            per_page.append(time.perf_counter() - t)
            kinds[f"{res.kind.kind}/{res.relevance.decision}"] += 1
    per_page.sort()
    links = sorted(len(f.links) for fs in by_host.values() for f in fs)
    result = {
        "python": platform.python_version(),
        "machine": platform.machine(),
        "pages": len(per_page),
        "hosts": len(by_host),
        "anchor_links_per_page": {
            "median": statistics.median(links),
            "p99": links[int(0.99 * (len(links) - 1))],
            "max": links[-1],
        },
        "profile_build_ms": {h: round(s * 1000, 1) for h, s in build.items()},
        "profile_build_total_s": round(sum(build.values()), 3),
        "analyze_page_us": {
            "median": round(statistics.median(per_page) * 1e6, 1),
            "p99": round(per_page[int(0.99 * (len(per_page) - 1))] * 1e6, 1),
            "max": round(per_page[-1] * 1e6, 1),
        },
        "population_decisions": dict(sorted(kinds.items())),
    }
    OUT.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "profile_build_ms"}, indent=1))


if __name__ == "__main__":
    main()
