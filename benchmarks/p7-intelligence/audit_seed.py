"""Seed-domain (piracy source) breakdown of the P7 audit; see audit_m1.py."""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_m1 import code, read, reg, ts

# G2 windows (audit §3.2): A = before A_END, B = from B_START; time between them
# belongs to neither. M1 runs 1/2: one split between the runs (4 days apart).
# The focused run (benchmarks.md §2) pre-registers A = its first 24 h and
# B = from 48 h on, i.e. ≥ 24 h apart.
A_END = datetime.fromisoformat(os.environ.get("P7_WINDOW_A_END", "2026-10-02T00:00:00+00:00"))
B_START = datetime.fromisoformat(os.environ.get("P7_WINDOW_B_START", A_END.isoformat()))
OUT = Path(__file__).parent / "results" / os.environ.get("P7_AUDIT_SEED_OUT", "m1-audit-seed.json")


def window(t: datetime) -> int | None:
    return 1 if t < A_END else 2 if t >= B_START else None


def main() -> int:
    seeds = {reg(r["url"]) for r in read("seeds")}
    att: Counter[str] = Counter()
    ok: Counter[str] = Counter()
    runs: dict[str, set[int]] = defaultdict(set)
    caps: dict[str, Counter[str]] = defaultdict(Counter)
    codes: Counter[str] = Counter()
    for a in read("attempts"):
        d = reg(a["url"])
        if d not in seeds:
            continue
        att[d] += 1
        w = window(ts(a["finished_at"]))
        if w is not None:
            runs[d].add(w)
        c = code(a["detail"])
        caps[d][a["capability"] + ":" + c] += 1
        codes[a["capability"] + ":" + c] += 1
        if c in ("ok", "media"):
            ok[d] += 1
    obs: dict[str, list[str]] = defaultdict(list)
    url_of: dict[str, str] = {}
    for o in read("observations"):
        ct = (o["content_type"] or "").lower()
        st = o["http_status"]
        if st and 200 <= st < 300 and "html" in ct and reg(o["final_url"]) in seeds:
            obs[o["final_url_id"]].append(o["observed_at"])
            url_of[o["final_url_id"]] = o["final_url"]
    revs: Counter[str] = Counter()
    for r in read("revisions"):
        if r["url_id"] in obs:
            revs[r["url_id"]] += 1

    def span(u: str) -> float:
        s = sorted(obs[u])
        return (ts(s[-1]) - ts(s[0])).total_seconds() / 3600

    def both(u: str) -> bool:
        return {window(ts(t)) for t in obs[u]} >= {1, 2}

    ge2 = [u for u in obs if len(obs[u]) >= 2]
    ge3 = [u for u in obs if len(obs[u]) >= 3]
    changed = [u for u in ge2 if revs[u] >= 2]

    def depth(u: str) -> int:
        return min(len([p for p in urlsplit(url_of[u]).path.split("/") if p]), 3)

    res = {
        "seed_domains": len(seeds),
        "fetched": len(att),
        "attempts": sum(att.values()),
        "domains_attempts_ge": {k: sum(1 for v in att.values() if v >= k) for k in (10, 30, 100)},
        "domains_with_ok": sum(1 for v in ok.values() if v),
        "domains_ok_ge30": sum(1 for v in ok.values() if v >= 30),
        "domains_both_runs": sum(1 for r in runs.values() if len(r) == 2),
        "domains_both_runs_ge30": sum(1 for d, r in runs.items() if len(r) == 2 and att[d] >= 30),
        "domains_with_browser": sum(
            1 for c in caps.values() if any(k.startswith("browser") for k in c)
        ),
        "domains_needs_js": sum(1 for c in caps.values() if c["http:needs_js"]),
        "capability_codes": dict(codes.most_common()),
        "html_2xx_urls": len(obs),
        "urls_ge2": len(ge2),
        "urls_ge2_both_runs": sum(1 for u in ge2 if both(u)),
        "urls_ge3": len(ge3),
        "urls_ge3_span_ge24h": sum(1 for u in ge3 if span(u) >= 24),
        "domains_with_ge3_url": len({reg(url_of[u]) for u in ge3}),
        "urls_changed": len(changed),
        "urls_changed_both_runs": sum(1 for u in changed if both(u)),
        "domains_with_change": dict(Counter(reg(url_of[u]) for u in changed)),
        "changed_by_path_depth": dict(Counter(depth(u) for u in changed)),
        "ge2_by_path_depth": dict(Counter(depth(u) for u in ge2)),
        "ge2_per_domain": dict(Counter(reg(url_of[u]) for u in ge2).most_common()),
    }
    OUT.write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
