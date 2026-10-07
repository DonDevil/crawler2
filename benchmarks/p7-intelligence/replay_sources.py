"""Offline replay of P7 increment 1 (source health + fetch profile) on M1.

Pre-registered in docs/phases/p07-crawl-intelligence/design.md §5.3, with the
policy fixed in ``SourcePolicy`` (design §4.5) before this script ran.

Every M1 attempt is replayed in start order. The policy decides at an
attempt's start, from the attempts it had executed that finished before it.
A probe-only host that is not due suppresses the attempt; its outcome is then
**not** folded, because the policy would not have observed it. Read-only
over ``var/p7/m1-export``; output ``results/p7-sources-replay.json``.
"""

from __future__ import annotations

import heapq
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_m1 import read, reg, ts

from crawler2.intelligence.sources import (
    DEFAULT_POLICY,
    FetchFact,
    OutcomeClass,
    SourceProfile,
    advise,
    assess,
    fact_from_export,
    fold,
)

OUT = Path(__file__).parent / "results" / "p7-sources-replay.json"
SPLIT = datetime(2026, 10, 2, tzinfo=UTC)
ANSWERED = (OutcomeClass.OK, OutcomeClass.NEEDS_JS)


def main() -> int:
    seeds = {reg(r["url"]) for r in read("seeds")}
    rows = []
    seen: set[str] = set()
    for a in read("attempts"):
        if a["id"] in seen:  # duplicates are removed by fact id before folding
            continue
        seen.add(a["id"])
        f = fact_from_export(a)
        population = "seed" if reg(a["url"]) in seeds else "off_target"
        elapsed = (ts(a["finished_at"]) - ts(a["started_at"])).total_seconds()
        rows.append((ts(a["started_at"]), a["id"], f, population, elapsed, reg(a["url"])))
    rows.sort(key=lambda r: (r[0], r[1]))
    profiles: dict[str, SourceProfile] = {}
    pending: list[tuple[datetime, str, FetchFact]] = []  # executed, not yet finished
    m: dict[str, Counter[str]] = defaultdict(Counter)
    lost_urls: dict[str, set[str]] = defaultdict(set)
    host_site: dict[str, str] = {}
    for started, aid, f, population, elapsed, site in rows:
        while pending and pending[0][0] <= started:
            _, _, done = heapq.heappop(pending)
            profiles[done.domain_id] = fold(profiles.get(done.domain_id), done)
        host_site[f.domain_id] = site
        advice = advise(profiles.get(f.domain_id), started)
        run = "run1" if started < SPLIT else "run2"
        for key in (population, f"{population}_{run}", "all"):
            c = m[key]
            c["attempts"] += 1
            c[f"m1_{f.outcome.value}"] += 1
            c["m1_seconds"] += elapsed
            if not advice.admit:
                c["suppressed"] += 1
                c[f"suppressed_{f.outcome.value}"] += 1
                c["suppressed_seconds"] += elapsed
                if f.outcome in ANSWERED:
                    c["lost_answered"] += 1
                    lost_urls[key].add(aid)
                continue
            c["executed"] += 1
            c["probes"] += advice.probe
            # HOW: an http attempt that needed JS on a host already browser-first saves
            # one http fetch; a browser-first start where http answered costs one.
            if advice.capability == "browser":
                c["advised_browser"] += 1
                if f.capability == "http" and f.outcome is OutcomeClass.NEEDS_JS:
                    c["how_http_saved"] += 1
                if f.capability == "http" and f.outcome is OutcomeClass.OK:
                    c["how_browser_unneeded"] += 1
        heapq.heappush(pending, (f.at, aid, f))
    while pending:
        _, _, done = heapq.heappop(pending)
        profiles[done.domain_id] = fold(profiles.get(done.domain_id), done)
    end = max(r[0] for r in rows)
    final: dict[str, Counter[str]] = defaultdict(Counter)
    seed_hosts: list[dict[str, Any]] = []
    for dom, p in profiles.items():
        status = assess(p, end)
        population = "seed" if host_site[dom] in seeds else "off_target"
        final[population][status.state.value] += 1
        if population == "seed":
            seed_hosts.append(
                {
                    "site": host_site[dom],
                    "state": status.state.value,
                    "moved_to": p.moved_to,
                    "facts": p.facts,
                }
            )

    def summary(c: Counter[str]) -> dict[str, Any]:
        out: dict[str, Any] = {k: round(v, 1) if isinstance(v, float) else v for k, v in c.items()}
        if c["attempts"]:
            out["suppressed_share"] = round(c["suppressed"] / c["attempts"], 4)
            out["seconds_saved_share"] = round(c["suppressed_seconds"] / c["m1_seconds"], 4)
            out["lost_answered_share_of_suppressed"] = (
                round(c["lost_answered"] / c["suppressed"], 4) if c["suppressed"] else 0.0
            )
        return dict(sorted(out.items()))

    result = {
        "policy": DEFAULT_POLICY.version,
        "attempts": len(rows),
        "populations": {k: summary(v) for k, v in sorted(m.items())},
        "final_states": {k: dict(v) for k, v in final.items()},
        "seed_hosts": sorted(seed_hosts, key=lambda h: (h["state"], h["site"])),
    }
    OUT.write_text(json.dumps(result, indent=1, sort_keys=True))
    print(json.dumps({k: v for k, v in result.items() if k != "seed_hosts"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
