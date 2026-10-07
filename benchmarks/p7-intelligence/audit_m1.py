"""P7 data-sufficiency audit over the exported M1 history.

Reads ``var/p7/m1-export`` (see ``export_m1.py``) and prints/writes the
coverage measures the P7 audit and gate use (``audit.md`` §2-§3):
volume, domains, repeat observations, revisions/changes, capabilities,
outcomes, metadata and media coverage, provenance. Read-only; output
``benchmarks/p7-intelligence/results/m1-audit.json``.
"""

from __future__ import annotations

import gzip
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from crawler2.filtering.inputs import registrable_domain

EXPORT = Path("var/p7/m1-export")
OUT = Path(__file__).parent / "results" / "m1-audit.json"
YEAR = re.compile(r"(?<!\d)(19[5-9]\d|20[0-3]\d)(?!\d)")


def read(name: str) -> Iterator[dict[str, Any]]:
    with gzip.open(EXPORT / f"{name}.jsonl.gz", "rt", encoding="utf-8") as fh:
        for line in fh:
            yield json.loads(line)


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def reg(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return registrable_domain(host) or host


def code(detail: str | None) -> str:
    if not detail:
        return "-"
    for part in detail.split(";"):
        if part.startswith("outcome="):
            return part[8:]
    return "-"


def top(counter: Counter[str], n: int) -> list[tuple[str, int]]:
    """``most_common`` with ties broken by key, so reruns are identical."""
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def hist(values: list[int], edges: list[int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for lo, hi in zip(edges, [*edges[1:], None], strict=True):
        key = f"{lo}+" if hi is None else (f"{lo}" if hi == lo + 1 else f"{lo}-{hi - 1}")
        out[key] = sum(1 for v in values if v >= lo and (hi is None or v < hi))
    return out


def main() -> int:
    seeds = {
        reg(r["canonical_url"] if "canonical_url" in r else r.get("url", "")) for r in read("seeds")
    }
    seeds.discard("")
    res: dict[str, Any] = {"export": json.loads((EXPORT / "export.json").read_text())}

    # --- fetch attempts (fetch.completed) ---
    att_by_dom: Counter[str] = Counter()
    ok_by_dom: Counter[str] = Counter()
    dom_runs: dict[str, set[int]] = defaultdict(set)
    cap_outcome: Counter[tuple[str, str]] = Counter()
    codes: Counter[tuple[str, str]] = Counter()
    status: Counter[str] = Counter()
    caps_by_url: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    caps_by_dom: dict[str, Counter[str]] = defaultdict(Counter)
    redirects = 0
    times: list[datetime] = []
    elapsed: list[float] = []
    for a in read("attempts"):
        t = ts(a["finished_at"])
        times.append(t)
        d = reg(a["url"])
        run = 1 if t < datetime(2026, 10, 2, tzinfo=t.tzinfo) else 2
        att_by_dom[d] += 1
        dom_runs[d].add(run)
        c = code(a["detail"])
        cap_outcome[(a["capability"], a["outcome"])] += 1
        codes[(a["capability"], c)] += 1
        caps_by_dom[d][a["capability"]] += 1
        st = a["http_status"]
        status[
            "none"
            if st is None
            else f"{st // 100}xx"
            if st not in (403, 404, 429, 304)
            else str(st)
        ] += 1
        if st is not None and 200 <= st < 300 and c in ("ok", "media", "-"):
            ok_by_dom[d] += 1
        redirects += 1 if a["redirects"] else 0
        caps_by_url[a["url_id"]].append((a["finished_at"], a["capability"], c))
        elapsed.append((ts(a["finished_at"]) - ts(a["started_at"])).total_seconds())
    times.sort()
    # escalation: an http needs_js followed by a browser attempt on the same URL
    esc = Counter()
    for seq in caps_by_url.values():
        seq.sort()
        for (_, c1, o1), (_, c2, o2) in pairwise(seq):
            if c1 == "http" and o1 == "needs_js" and c2 == "browser":
                esc[f"needs_js->browser:{o2}"] += 1
    run_split = Counter(1 if t < datetime(2026, 10, 2, tzinfo=t.tzinfo) else 2 for t in times)
    res["attempts"] = {
        "total": len(times),
        "first": times[0].isoformat(),
        "last": times[-1].isoformat(),
        "by_run": dict(run_split),
        "capability_outcome": {f"{k[0]}/{k[1]}": v for k, v in sorted(cap_outcome.items())},
        "capability_code": {f"{k[0]}/{k[1]}": v for k, v in sorted(codes.items())},
        "status_class": dict(status.most_common()),
        "attempts_with_redirects": redirects,
        "escalations": dict(esc),
        "elapsed_s_median": statistics.median(elapsed),
        "registrable_domains": len(att_by_dom),
        "domains_with_attempts_ge": {
            k: sum(1 for v in att_by_dom.values() if v >= k)
            for k in (1, 3, 5, 10, 20, 30, 50, 100, 300, 1000)
        },
        "domains_in_both_runs": sum(1 for r in dom_runs.values() if len(r) == 2),
        "domains_in_both_runs_ge10": sum(
            1 for d, r in dom_runs.items() if len(r) == 2 and att_by_dom[d] >= 10
        ),
        "domains_with_browser_attempts": sum(1 for c in caps_by_dom.values() if c["browser"]),
        "domains_with_http_and_browser": sum(
            1 for c in caps_by_dom.values() if c["browser"] and c["http"]
        ),
        "domains_with_tor_attempts": sum(
            1 for c in caps_by_dom.values() if c["tor_http"] or c["tor"]
        ),
        "top_domains_share": [
            (d, n, round(n / len(times), 4)) for d, n in att_by_dom.most_common(15)
        ],
        "domains_with_2xx_ok": sum(1 for v in ok_by_dom.values() if v),
        "domains_with_failures_only_ge5": sum(
            1 for d, n in att_by_dom.items() if n >= 5 and not ok_by_dom[d]
        ),
        "seed_domains": len(seeds),
        "seed_domains_fetched": sum(1 for d in seeds if att_by_dom[d]),
        "seed_domain_attempts": sum(att_by_dom[d] for d in seeds),
        "seed_domains_with_2xx": sum(1 for d in seeds if ok_by_dom[d]),
    }
    del caps_by_url

    # --- observations (page.observed) ---
    obs_by_url: dict[str, list[str]] = defaultdict(list)
    html_ok_by_url: dict[str, list[str]] = defaultdict(list)
    url_of: dict[str, str] = {}
    ctype: Counter[str] = Counter()
    validators = 0
    n_obs = 0
    for o in read("observations"):
        n_obs += 1
        u = o["final_url_id"]
        url_of[u] = o["final_url"]
        obs_by_url[u].append(o["observed_at"])
        ct = (o["content_type"] or "").split(";")[0].strip().lower()
        ctype[ct or "none"] += 1
        validators += 1 if o["validators"] else 0
        st = o["http_status"]
        if st is not None and 200 <= st < 300 and "html" in ct:
            html_ok_by_url[u].append(o["observed_at"])
    counts = [len(v) for v in html_ok_by_url.values()]

    def span_h(v: list[str]) -> float:
        s = sorted(v)
        return (ts(s[-1]) - ts(s[0])).total_seconds() / 3600

    repeat3 = {u for u, v in html_ok_by_url.items() if len(v) >= 3}
    repeat2 = {u for u, v in html_ok_by_url.items() if len(v) >= 2}
    dom_of = {u: reg(url_of[u]) for u in html_ok_by_url}
    res["observations"] = {
        "total": n_obs,
        "urls": len(obs_by_url),
        "content_type_top": ctype.most_common(8),
        "with_validators": validators,
        "html_2xx_urls": len(html_ok_by_url),
        "html_2xx_observations_per_url": hist(counts, [1, 2, 3, 4, 5, 10]),
        "html_2xx_urls_ge2": len(repeat2),
        "html_2xx_urls_ge3": len(repeat3),
        "html_2xx_urls_ge3_span_ge24h": sum(1 for u in repeat3 if span_h(html_ok_by_url[u]) >= 24),
        "html_2xx_urls_ge2_span_ge24h": sum(1 for u in repeat2 if span_h(html_ok_by_url[u]) >= 24),
        "domains_with_ge3_url": len({dom_of[u] for u in repeat3}),
        "domains_with_ge2_url": len({dom_of[u] for u in repeat2}),
        "repeat3_urls_on_seed_domains": sum(1 for u in repeat3 if dom_of[u] in seeds),
        "repeat2_urls_on_seed_domains": sum(1 for u in repeat2 if dom_of[u] in seeds),
        "repeat3_top_domains": top(Counter(dom_of[u] for u in repeat3), 15),
    }

    # --- revisions (page.changed) ---
    revs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in read("revisions"):
        revs[r["url_id"]].append(r)
    n_rev = sum(len(v) for v in revs.values())
    changed = {u for u, v in revs.items() if len(v) >= 2}
    res["revisions"] = {
        "urls_with_revision": len(revs),
        "revisions": n_rev,
        "page_changed_events_equiv": n_rev,
        "true_changes": n_rev - len(revs),
        "urls_changed": len(changed),
        "urls_changed_among_ge2": len(changed & repeat2),
        "urls_changed_among_ge3": len(changed & repeat3),
        "changes_among_ge3": sum(len(revs[u]) - 1 for u in repeat3 if u in revs),
        "domains_with_change": len({dom_of[u] for u in changed if u in dom_of}),
        "seed_domain_urls_changed": sum(1 for u in changed if dom_of.get(u) in seeds),
        "seed_domains_with_change": len({dom_of[u] for u in changed if dom_of.get(u) in seeds}),
        "revisions_per_url": hist([len(v) for v in revs.values()], [1, 2, 3, 4, 5, 10]),
        "changed_top_domains": top(Counter(dom_of.get(u, "?") for u in changed), 15),
    }

    # --- extracts (urls.discovered / media.discovered / metadata) ---
    meta: Counter[str] = Counter()
    og_types: Counter[str] = Counter()
    links: list[int] = []
    media_pages = 0
    media_refs = 0
    media_kinds: Counter[str] = Counter()
    year_in_path = 0
    depth: Counter[int] = Counter()
    n_ext = 0
    seed_ext = 0
    seed_media_pages = 0
    for e in read("extracts"):
        n_ext += 1
        m = e.get("metadata") or {}
        for k in (
            "title",
            "lang",
            "canonical",
            "og_title",
            "og_type",
            "published_at",
            "modified_at",
            "robots",
        ):
            meta[k] += 1 if m.get(k) else 0
        if m.get("og_type"):
            og_types[m["og_type"]] += 1
        links.append(len(e["links"]))
        if e["media"]:
            media_pages += 1
            media_refs += len(e["media"])
            for _, kind, _ in e["media"]:
                media_kinds[kind or "?"] += 1
        path = urlsplit(e["url"]).path
        year_in_path += 1 if YEAR.search(path) else 0
        depth[min(len([p for p in path.split("/") if p]), 6)] += 1
        if reg(e["url"]) in seeds:
            seed_ext += 1
            seed_media_pages += 1 if e["media"] else 0
    links.sort()
    res["extracts"] = {
        "total": n_ext,
        "metadata_present": dict(meta),
        "og_type_top": og_types.most_common(10),
        "links_per_page": {
            "median": links[len(links) // 2],
            "p90": links[int(len(links) * 0.9)],
            "max": links[-1],
            "total": sum(links),
        },
        "pages_with_media": media_pages,
        "media_refs": media_refs,
        "media_kinds": dict(media_kinds),
        "pages_with_year_in_path": year_in_path,
        "path_depth": dict(sorted(depth.items())),
        "seed_domain_extracts": seed_ext,
        "seed_domain_pages_with_media": seed_media_pages,
    }

    # --- provenance ---
    origin: Counter[str] = Counter()
    for a in read("admissions"):
        origin[f"{a['origin']}/{a['action']}"] += 1
    search = list(read("search"))
    res["provenance"] = {
        "admissions_by_origin_action": dict(origin.most_common()),
        "search_results": len(search),
        "search_engines": dict(Counter(r.get("engine") for r in search)),
        "search_result_domains": len({reg(r["url"]) for r in search if r.get("url")}),
        "scope_sites": sum(1 for _ in read("scope")),
        "seed_rows": sum(1 for _ in read("seeds")),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
