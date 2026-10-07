"""Per-seed-domain reassessment of the M1 export (P7 audit §5).

Read-only over ``var/p7/m1-export``. For each of the 50 seed registrable
domains: availability, fetch outcomes, redirects to other domains, repeat
observations, revisions, media pages, browser use; plus what the revisits
between run 1 and run 2 (4 days apart) revealed. Output:
``results/m1-reassess-seed.json``.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_m1 import code, read, reg, ts

SPLIT = datetime(2026, 10, 2, tzinfo=UTC)  # between M1 run 1 and run 2
OUT = Path(__file__).parent / "results" / "m1-reassess-seed.json"
LISTING_LINKS = 30  # same-site links on a page for the "listing-like" split (descriptive only)


def main() -> int:
    seeds = sorted({reg(r["url"]) for r in read("seeds")})
    seed_set = set(seeds)
    d: dict[str, Counter[str]] = defaultdict(Counter)
    codes: dict[str, Counter[str]] = defaultdict(Counter)
    status: dict[str, Counter[int]] = defaultdict(Counter)
    away: dict[str, Counter[str]] = defaultdict(Counter)
    for a in read("attempts"):
        dom = reg(a["url"])
        if dom not in seed_set:
            continue
        c = d[dom]
        c["attempts"] += 1
        c["run1" if ts(a["finished_at"]) < SPLIT else "run2"] += 1
        codes[dom][f"{a['capability']}:{code(a['detail'])}"] += 1
        st = a["http_status"]
        if st is not None:
            status[dom][st] += 1
            c["attempts_2xx"] += 200 <= st < 300
            c["s429"] += st == 429
        c["browser"] += a["capability"] == "browser"
        c["needs_js"] += code(a["detail"]) == "needs_js"
        if a["final_url"] and reg(a["final_url"]) != dom:
            away[dom][reg(a["final_url"])] += 1
    ok: dict[str, list[str]] = defaultdict(list)
    obs_n: Counter[str] = Counter()
    dom_of: dict[str, str] = {}
    for o in read("observations"):
        dom = reg(o["final_url"])
        if dom not in seed_set:
            continue
        u = o["final_url_id"]
        dom_of[u] = dom
        obs_n[u] += 1
        st, ct = o["http_status"], (o["content_type"] or "").lower()
        if st and 200 <= st < 300 and "html" in ct:
            ok[u].append(o["observed_at"])

    def span_h(v: list[str]) -> float:
        s = sorted(v)
        return (ts(s[-1]) - ts(s[0])).total_seconds() / 3600

    for u, n in obs_n.items():
        c = d[dom_of[u]]
        c["observed_urls"] += 1
        c["observed_ge2"] += n >= 2
        c["observed_ge3"] += n >= 3
    for u, v in ok.items():
        c = d[dom_of[u]]
        c["html2xx_urls"] += 1
        c["html2xx_ge2"] += len(v) >= 2
        c["html2xx_ge2_span24h"] += len(v) >= 2 and span_h(v) >= 24
        c["html2xx_ge3"] += len(v) >= 3
        c["html2xx_ge3_span24h"] += len(v) >= 3 and span_h(v) >= 24
    revs: Counter[str] = Counter()
    for r in read("revisions"):
        if r["url_id"] in dom_of:
            revs[r["url_id"]] += 1
    for u, n in revs.items():
        if n >= 2:
            d[dom_of[u]]["changed_urls"] += 1
            d[dom_of[u]]["changes"] += n - 1
    known: dict[str, set[str]] = defaultdict(set)
    for a in read("admissions"):
        if reg(a["url"]) in seed_set:
            known[reg(a["url"])].add(a["url"])
    first_link: dict[str, str] = {}
    pages: dict[str, list[tuple[str, str, frozenset[str]]]] = defaultdict(list)
    for e in read("extracts"):
        dom = reg(e["url"])
        if dom not in seed_set:
            continue
        d[dom]["media_pages"] += bool(e["media"])
        same = frozenset(lk[0] for lk in e["links"] if reg(lk[0]) == dom)
        known[dom] |= same
        t = e["observed_at"]
        for x in same:
            if x not in first_link or t < first_link[x]:
                first_link[x] = t
        pages[e["url_id"]].append((t, e["revision_id"], same))
    # change pairs: consecutive raw versions of a URL whose revision changed
    change_pairs: dict[str, Counter[str]] = defaultdict(Counter)
    # run1 -> run2 revisits: did a later extract link to a URL first seen right then?
    revisit: Counter[str] = Counter()
    revisit_dom: dict[str, Counter[str]] = defaultdict(Counter)
    for u, seq in pages.items():
        seq.sort()
        dom = dom_of.get(u)
        if dom is None:
            continue
        if revs[u] >= 2:
            # one entry per revision (its first sighting): a page.changed transition each
            firsts: dict[str, tuple[str, frozenset[str]]] = {}
            for t, rev, links in seq:
                firsts.setdefault(rev, (t, links))
            ordered = sorted(firsts.values(), key=lambda x: x[0])
            for (_, a_links), (t1, b_links) in pairwise(ordered):
                change_pairs[dom]["pairs"] += 1
                change_pairs[dom]["links_added"] += bool(b_links - a_links)
                change_pairs[dom]["new_url_revealed"] += any(
                    first_link[x] == t1 for x in b_links - a_links
                )
        v = ok.get(u, [])
        if not ({ts(t) < SPLIT for t in v} >= {True, False}):
            continue
        kind = "listing" if max(len(s) for _, _, s in seq) >= LISTING_LINKS else "other"
        revealed = {x for t, _, s in seq if ts(t) >= SPLIT for x in s if first_link[x] == t}
        revisit[kind] += 1
        revisit[f"{kind}_revealed_new"] += bool(revealed)
        revisit["new_urls"] += len(revealed)
        revisit_dom[dom][kind] += 1
        revisit_dom[dom]["new_urls"] += len(revealed)
    rows: list[dict[str, Any]] = []
    for dom in seeds:
        c = d[dom]
        rows.append(
            {
                "domain": dom,
                "ever_2xx": c["attempts_2xx"] > 0,
                "known_urls": len(known[dom]),
                **{k: c[k] for k in sorted(c)},
                "outcomes": dict(codes[dom].most_common(5)),
                "statuses": dict(status[dom].most_common(4)),
                "redirect_away": dict(away[dom].most_common(3)),
                "change_pairs": dict(change_pairs[dom]),
            }
        )
    live = [r for r in rows if r.get("html2xx_urls", 0) >= 50]
    res = {
        "seed_domains": len(seeds),
        "live_domains": [r["domain"] for r in live],
        "live_html2xx_urls": sum(r["html2xx_urls"] for r in live),
        "change_pairs_total": sum(r["change_pairs"].get("pairs", 0) for r in rows),
        "change_pairs_new_url": sum(r["change_pairs"].get("new_url_revealed", 0) for r in rows),
        "revisits_run1_to_run2": dict(revisit),
        "revisits_by_domain": {k: dict(v) for k, v in sorted(revisit_dom.items())},
        "rows": rows,
    }
    OUT.write_text(json.dumps(res, indent=1, sort_keys=True))
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
