"""P7 WHEN readiness: offline audit of hub revisits in the M1 export (when.md).

Read-only over the M1 export (``var/p7/m1-export``, ``export_m1.py``).
Nothing is crawled and nothing is written to Scylla, Redis or MinIO. WHAT
(``what/v1``) is used as a frozen library; this tool changes none of its
rules. It produces no schedule, rate or priority.

Commands::

    when_audit.py audit     # results/when-audit.json (aggregates, per domain)
                            # + var/p7/when/events.jsonl.gz (one row per hub revisit)
    when_audit.py sample    # results/when-sample.json + var/p7/when/cards/<id>.txt;
                            # refuses while the data-sufficiency check fails
    when_audit.py hubs      # results/when-hub-proposal.json: the hub population a
                            # future hub-only collection would revisit (when.md §7)

Another dataset (e.g. the proposed hub-only collection, exported with
``export_m1.py --keyspace``): ``P7_EXPORT=<dir> P7_WHEN_OUT=<dir>``; results
are then named after its keyspace. The G5 hubs are excluded from a sample
only for ``crawler2_m1``, whose revisits overlap the G5 pairs in time.

Definitions (fixed before the audit was run, 2026-10-08; when.md §3):

- **Observation**: a ``page_observations`` row with HTTP 2xx and an HTML
  content type, keyed by its final URL, on the 11 live seed domains, whose
  ``page_version_id`` has a P5 extract. Observations without an extract are
  counted and skipped.
- **Visit window**: a URL's observations in time order are cut into windows.
  A window starts at the first observation that is at least ``MIN_GAP_S``
  (6 h, the shortest P6 revisit interval) after the previous window's start,
  and holds every later observation until the next window starts. Short-gap
  refetches (redirect variants, retries) therefore merge into one visit and
  lose no links.
- **Hub revisit (event)**: window *k* ≥ 2 of a URL that WHAT classifies as a
  hub (on its latest extract, as in the WHAT population). Its links are the
  union of the same-host anchor links of the window's observations. Its
  history is the union of the links of all earlier windows.
- **New-to-hub link**: in the window, not in the hub's history.
- **Novelty of a new-to-hub link** (site knowledge = every URL observed, and
  every link target seen, on the same registrable domain, timed by the first
  observation of the page version that carried it):
  ``known_before`` (known at or before the previous window's last
  observation), ``new_since_prev`` (first known on the site after the
  previous window, from another page first), ``first_seen_here`` (this window
  is its first sighting on the site).
- **Role** (from the event's own observation only; the target page is never
  used, because at runtime it has not been fetched yet): ``redirect`` (the
  URL was observed to redirect), ``boilerplate`` (a WHAT site-wide link),
  ``content`` (in WHAT's ``content_links``), ``content_bad`` (in a content or
  mixed template but excluded by WHAT's link evidence), otherwise
  ``other:<template role>``. ``variant`` marks a link whose spelling-
  normalized form (case, trailing slash) was known before the previous
  window.
- **Candidate link** (``when-candidate/v0``): new to the hub, novelty
  ``new_since_prev`` or ``first_seen_here``, role ``content``, not a variant.
  A **candidate event** has at least one. ``first_seen_here`` alone is the
  G5 "first discoverer" signal and is reported separately.
- Target-page WHAT relevance (from the target's latest extract, if it was
  ever fetched) is reported as evidence only. It is later information and is
  not part of the candidate definition.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from crawler2.intelligence.what import (
    CONTENT_ROLES,
    DEFAULT_WHAT_POLICY,
    PageFacts,
    PageKind,
    Relevance,
    SiteProfile,
    WhatResult,
    analyze_page,
    build_site_profile,
    facts_from_export,
    load_vocabulary,
    tokenize_url,
)

HERE = Path(__file__).parent
EXPORT = Path(os.environ.get("P7_EXPORT", "var/p7/m1-export"))
OUT = Path(os.environ.get("P7_WHEN_OUT", "var/p7/when"))
G5_SAMPLE = HERE / "results" / "g5-sample.json"
M1_KEYSPACE = "crawler2_m1"

CANDIDATE_VERSION = "when-candidate/v0"
AUDIT_VERSION = "when-audit/v1"
MIN_GAP_S = 6 * 3600
"""Visit windows: the shortest P6 static revisit interval (seeds, P6 D-3)."""
MAX_HUB_WINDOWS = 30
"""Window start times listed per hub."""
MAX_EVENT_LINKS = 60
"""Links listed per event row (counts are always complete)."""
GATE_MIN_CANDIDATE_EVENTS = 50
"""design §7: a labelled WHEN sample needs ≥ 50 hub revisits; here, candidates."""
SAMPLE_SALT = "p7-when-sample/v0"
SPLIT_SALT = "p7-when-split/v0"
SAMPLE_CANDIDATES = 80
"""Candidate events drawn for labelling (all of them when fewer)."""
SAMPLE_NEGATIVES = 80
"""Non-candidate events drawn for labelling (recall, false negatives)."""
MAX_DOMAIN_SHARE = 0.4
"""At most this share of each side of the sample from one domain."""
PROPOSAL_HUBS_PER_DOMAIN = 12
PROPOSAL_MIN_CONTENT_LINKS = 3
# audit §5.1: the 11 seed domains with >= 50 HTML 2xx pages (same list as what_sample.py)
LIVE = (
    "gofullmovies.com",
    "isaimini.com.in",
    "isaiminimix.com",
    "movflix.my",
    "moviesda.business",
    "moviesda.com.in",
    "moviesda16.best",
    "stripemovies.com",
    "tamilprint.co.in",
    "tamilprint.com.in",
    "tamilrockers2024.com",
)
INTERVAL_BUCKETS = ((24, "6-24h"), (72, "24-72h"), (168, "3-7d"), (10**9, ">=7d"))
NOVELTY = ("known_before", "new_since_prev", "first_seen_here")


# --------------------------------------------------------------------------- pure core


@dataclass(frozen=True, slots=True)
class Obs:
    """One raw observation (final URL) with the page version it produced."""

    url: str
    at: float
    pgv: str
    oid: str
    requested: str = ""


def epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def iso(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def site_of(url: str) -> str:
    """The registrable domain for the 11 live seed domains, else the host."""
    host = (urlsplit(url).hostname or "").lower()
    for d in LIVE:
        if host == d or host.endswith("." + d):
            return d
    return host


def norm_key(url: str) -> str:
    """Spelling-normalized form used for the ``variant`` flag only."""
    s = urlsplit(url)
    path = s.path.rstrip("/") or "/"
    return f"{(s.hostname or '').lower()}{path.lower()}?{s.query}"


def same_host_links(facts: PageFacts) -> frozenset[str]:
    """Same-host anchor targets without fragment and self (as WHAT, unbounded)."""
    page = facts.url.split("#", 1)[0]
    host = (urlsplit(page).hostname or "").lower()
    out = set()
    for target, _anchor in facts.links:
        u = target.split("#", 1)[0]
        if u == page:
            continue
        try:
            if (urlsplit(u).hostname or "").lower() != host:
                continue
        except ValueError:
            continue
        out.add(u)
    return frozenset(out)


def windows(obs: Iterable[Obs], min_gap_s: float = MIN_GAP_S) -> list[list[Obs]]:
    """Visit windows (module docstring). Duplicates (same id) and order do not matter."""
    ordered = sorted({o.oid: o for o in obs}.values(), key=lambda o: (o.at, o.oid))
    out: list[list[Obs]] = []
    for o in ordered:
        if not out or o.at - out[-1][0].at >= min_gap_s:
            out.append([o])
        else:
            out[-1].append(o)
    return out


def first_known(obs: Iterable[Obs], links_by_pgv: Mapping[str, frozenset[str]]) -> dict[str, float]:
    """Site knowledge: first time each URL was observed or seen as a link target."""
    known: dict[str, float] = {}
    pgv_first: dict[str, float] = {}
    for o in obs:
        for u in (o.url, o.requested):
            if u and (u not in known or o.at < known[u]):
                known[u] = o.at
        if o.pgv not in pgv_first or o.at < pgv_first[o.pgv]:
            pgv_first[o.pgv] = o.at
    for pgv, t in pgv_first.items():
        for u in links_by_pgv.get(pgv, ()):
            if u not in known or t < known[u]:
                known[u] = t
    return known


def first_known_keys(known: Mapping[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for u, t in known.items():
        k = norm_key(u)
        if k not in out or t < out[k]:
            out[k] = t
    return out


@dataclass(slots=True)
class Event:
    """One hub revisit: window ``k`` (≥ 2) against windows ``1..k-1``."""

    hub: str
    host: str
    site: str
    index: int
    prev_start: float
    prev_end: float
    start: float
    end: float
    observations: int
    page_versions: int
    changed: bool
    """A page version of the window differs from every one of the previous window."""
    links: int
    appeared: int
    """Links not in the previous window (not the whole history)."""
    disappeared: int
    new_to_hub: list[str]
    novelty: dict[str, str]
    role: dict[str, str]
    variant: set[str]
    anchor: dict[str, str]
    candidates: list[str]
    first_seen_here: list[str]
    truncated: bool
    kind_at_window: str

    @property
    def interval_h(self) -> float:
        return (self.start - self.prev_start) / 3600

    @property
    def candidate(self) -> bool:
        return bool(self.candidates)


def classify_link(
    url: str,
    prev_end: float,
    start: float,
    known: Mapping[str, float],
    known_keys: Mapping[str, float],
) -> tuple[str, bool]:
    """(novelty, variant) of a new-to-hub link of a window starting at ``start``."""
    t = known.get(url)
    if t is None or t <= prev_end:
        novelty = "known_before" if t is not None else "first_seen_here"
    else:
        novelty = "new_since_prev" if t < start else "first_seen_here"
    kt = known_keys.get(norm_key(url))
    variant = kt is not None and kt <= prev_end and novelty != "known_before"
    return novelty, variant


def link_role(
    url: str,
    content_links: frozenset[str] | set[str],
    profile: SiteProfile,
    redirects: frozenset[str] | set[str],
) -> str:
    if url in redirects:
        return "redirect"
    if url in profile.boilerplate_links:
        return "boilerplate"
    if url in content_links:
        return "content"
    shape = tokenize_url(url)
    role = profile.role(shape.template) if shape else None
    if role in CONTENT_ROLES:
        return "content_bad"
    return f"other:{role or 'none'}"


def hub_events(
    hub: str,
    obs: Iterable[Obs],
    facts_by_pgv: Mapping[str, PageFacts],
    profile: SiteProfile,
    known: Mapping[str, float],
    known_keys: Mapping[str, float],
    redirects: frozenset[str] | set[str],
    analyze: Any = None,
    min_gap_s: float = MIN_GAP_S,
) -> list[Event]:
    """Every revisit event of one hub (module docstring). Observations whose page
    version has no facts are ignored; a hub with fewer than two windows has none."""
    analyze = analyze or (lambda f: analyze_page(f, profile))
    usable = [o for o in obs if o.pgv in facts_by_pgv and o.url == hub]
    wins = windows(usable, min_gap_s)
    events: list[Event] = []
    history: set[str] = set()
    prev_links: set[str] = set()
    prev_pgvs: set[str] = set()
    for k, win in enumerate(wins):
        links: set[str] = set()
        content: set[str] = set()
        results: dict[str, WhatResult] = {}
        anchor: dict[str, str] = {}
        truncated = False
        for o in win:
            facts = facts_by_pgv[o.pgv]
            links |= same_host_links(facts)
            for target, text in facts.links:
                a = " ".join(text.split())[:120]
                u = target.split("#", 1)[0]
                if a and (u not in anchor or a < anchor[u]):
                    anchor[u] = a
            if o.pgv not in results:
                results[o.pgv] = analyze(facts)
            r = results[o.pgv]
            content |= set(r.content_links)
            truncated |= r.kind.own_content_links > len(r.content_links)
        pgvs = {o.pgv for o in win}
        if k > 0:
            prev = wins[k - 1]
            prev_end = prev[-1].at
            new = sorted(links - history)
            novelty: dict[str, str] = {}
            role: dict[str, str] = {}
            variant: set[str] = set()
            for u in new:
                nv, var = classify_link(u, prev_end, win[0].at, known, known_keys)
                novelty[u] = nv
                role[u] = link_role(u, content, profile, redirects)
                if var:
                    variant.add(u)
            cands = [
                u
                for u in new
                if novelty[u] != "known_before" and role[u] == "content" and u not in variant
            ]
            events.append(
                Event(
                    hub=hub,
                    host=profile.host,
                    site=site_of(hub),
                    index=k + 1,
                    prev_start=prev[0].at,
                    prev_end=prev_end,
                    start=win[0].at,
                    end=win[-1].at,
                    observations=len(win),
                    page_versions=len(pgvs),
                    changed=not pgvs <= prev_pgvs,
                    links=len(links),
                    appeared=len(links - prev_links),
                    disappeared=len(prev_links - links),
                    new_to_hub=new,
                    novelty=novelty,
                    role=role,
                    variant=variant,
                    anchor={u: anchor.get(u, "") for u in new},
                    candidates=cands,
                    first_seen_here=[u for u in cands if novelty[u] == "first_seen_here"],
                    truncated=truncated,
                    kind_at_window=str(results[win[0].pgv].kind.kind),
                )
            )
        history |= links
        prev_links = links
        prev_pgvs = pgvs
    return events


def interval_bucket(hours: float) -> str:
    for limit, name in INTERVAL_BUCKETS:
        if hours < limit:
            return name
    return INTERVAL_BUCKETS[-1][1]


def dist(values: Iterable[float]) -> dict[str, Any]:
    v = sorted(values)
    if not v:
        return {"n": 0}

    def q(p: float) -> float:
        return round(v[min(len(v) - 1, int(p * len(v)))], 2)

    return {
        "n": len(v),
        "min": round(v[0], 2),
        "p50": q(0.5),
        "p90": q(0.9),
        "max": round(v[-1], 2),
        "sum": round(sum(v), 2),
    }


@dataclass(slots=True)
class Credit:
    """Every hub event that has a link as candidate, and every page linking it."""

    hubs: set[str] = field(default_factory=set)
    events: int = 0
    first_hub: str = ""
    first_at: float = float("inf")


def credit_table(events: Iterable[Event]) -> dict[str, Credit]:
    out: dict[str, Credit] = defaultdict(Credit)
    for e in events:
        for u in e.candidates:
            c = out[u]
            c.hubs.add(e.hub)
            c.events += 1
            if (e.start, e.hub) < (c.first_at, c.first_hub):
                c.first_at, c.first_hub = e.start, e.hub
    return dict(out)


def _round_robin(rows: list[dict[str, Any]], n: int, cap: int) -> list[dict[str, Any]]:
    """Up to ``n`` rows, one domain at a time (sorted), each domain's rows in
    interval-bucket round robin and ``sha256(SALT|id)`` order, ``cap`` per domain."""
    by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_domain[r["site"]].append(r)
    queues: dict[str, list[dict[str, Any]]] = {}
    for d, rs in by_domain.items():
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in sorted(rs, key=lambda r: _h(SAMPLE_SALT, r["id"])):
            buckets[r["interval_bucket"]].append(r)
        order: list[dict[str, Any]] = []
        while any(buckets.values()):
            for b in sorted(buckets):
                if buckets[b]:
                    order.append(buckets[b].pop(0))
        queues[d] = order[:cap]
    out: list[dict[str, Any]] = []
    while len(out) < n and any(queues.values()):
        for d in sorted(queues):
            if queues[d] and len(out) < n:
                out.append(queues[d].pop(0))
    return out


def sample_events(
    events: list[dict[str, Any]],
    n_candidates: int = SAMPLE_CANDIDATES,
    n_negatives: int = SAMPLE_NEGATIVES,
    max_domain_share: float = MAX_DOMAIN_SHARE,
    exclude: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Deterministic sample of event rows for labelling (when.md §6).

    Candidate and non-candidate events are drawn separately, each by
    :func:`_round_robin` (domains, then interval buckets, then hash order), so
    no domain exceeds ``max_domain_share`` of its side and negatives are always
    present. Events of hubs in ``exclude`` are dropped. Each event is assigned
    ``dev`` or ``holdout`` by ``sha256(SPLIT_SALT|id)``."""
    rows = sorted(
        {e["id"]: e for e in events if e["hub"] not in exclude}.values(), key=lambda r: r["id"]
    )
    picked = [
        *_round_robin(
            [r for r in rows if r["candidate"]],
            n_candidates,
            max(1, int(max_domain_share * n_candidates)),
        ),
        *_round_robin(
            [r for r in rows if not r["candidate"]],
            n_negatives,
            max(1, int(max_domain_share * n_negatives)),
        ),
    ]
    return sorted(
        (
            r | {"split": "holdout" if int(_h(SPLIT_SALT, r["id"]), 16) % 2 else "dev"}
            for r in picked
        ),
        key=lambda r: r["id"],
    )


def _h(salt: str, value: str) -> str:
    return hashlib.sha256(f"{salt}|{value}".encode()).hexdigest()


def event_id(e: Event) -> str:
    return _h("p7-when-event/v0", f"{e.hub}|{e.index}")[:12]


def event_row(
    e: Event, target_rel: Mapping[str, str], titles: Mapping[str, str] | None = None
) -> dict[str, Any]:
    titles = titles or {}
    nov = Counter(e.novelty.values())
    roles = Counter(e.role.values())
    new_since = [u for u in e.new_to_hub if e.novelty[u] != "known_before"]
    return {
        "id": event_id(e),
        "hub": e.hub,
        "site": e.site,
        "window": e.index,
        "prev_start": iso(e.prev_start),
        "prev_end": iso(e.prev_end),
        "start": iso(e.start),
        "end": iso(e.end),
        "interval_h": round(e.interval_h, 2),
        "interval_bucket": interval_bucket(e.interval_h),
        "observations": e.observations,
        "page_versions": e.page_versions,
        "changed": e.changed,
        "kind_at_window": e.kind_at_window,
        "links": e.links,
        "appeared_vs_prev": e.appeared,
        "disappeared_vs_prev": e.disappeared,
        "new_to_hub": len(e.new_to_hub),
        "novelty": dict(sorted(nov.items())),
        "roles": dict(sorted(roles.items())),
        "new_since_prev_roles": dict(sorted(Counter(e.role[u] for u in new_since).items())),
        "variants": len(e.variant),
        "candidate": e.candidate,
        "candidates": len(e.candidates),
        "first_seen_here": len(e.first_seen_here),
        "truncated": e.truncated,
        "candidate_links": [
            {"url": u, "novelty": e.novelty[u], "target_what": target_rel.get(u, "not_fetched")}
            for u in e.candidates[:MAX_EVENT_LINKS]
        ],
        "new_since_prev_links": [
            {
                "url": u,
                "novelty": e.novelty[u],
                "role": e.role[u],
                "anchor": e.anchor.get(u, ""),
                "target_title": titles.get(u),
            }
            for u in new_since[:MAX_EVENT_LINKS]
        ],
    }


# --------------------------------------------------------------------------- export I/O


def read(name: str) -> Iterator[dict[str, Any]]:
    with gzip.open(EXPORT / f"{name}.jsonl.gz", "rt", encoding="utf-8") as fh:
        for line in fh:
            yield json.loads(line)


@dataclass(slots=True)
class Loaded:
    obs: list[Obs]
    facts_by_pgv: dict[str, PageFacts]
    latest: dict[str, PageFacts]
    revision_by_pgv: dict[str, str]
    published: dict[str, str]
    """Latest extract's ``published_at`` per URL, when P5 found one."""
    redirects: set[str]
    counts: Counter[str]


def load() -> Loaded:
    """One pass over observations, one over extracts; live seed domains only."""
    counts: Counter[str] = Counter()
    live = set(LIVE)
    obs: list[Obs] = []
    redirects: set[str] = set()
    for o in read("observations"):
        counts["observations_total"] += 1
        final = o.get("final_url") or ""
        if site_of(final) not in live:
            continue
        counts["observations_seed"] += 1
        if o.get("url") and o["url"] != final:
            redirects.add(o["url"])
        ctype = o.get("content_type") or ""
        if (o.get("http_status") or 0) // 100 != 2 or "html" not in ctype:
            continue
        if not o.get("page_version_id") or not o.get("observed_at"):
            counts["observations_malformed"] += 1
            continue
        obs.append(Obs(final, epoch(o["observed_at"]), o["page_version_id"], o["id"], o["url"]))
    want = {o.pgv for o in obs}
    facts_by_pgv: dict[str, PageFacts] = {}
    latest: dict[str, tuple[str, PageFacts]] = {}
    revision_by_pgv: dict[str, str] = {}
    published: dict[str, str] = {}
    for e in read("extracts"):
        url = e.get("url") or ""
        if site_of(url) not in live:
            continue
        f = facts_from_export(e)
        if e["page_version_id"] in want:
            facts_by_pgv[e["page_version_id"]] = f
            revision_by_pgv[e["page_version_id"]] = e.get("revision_id") or ""
        if url not in latest or e["observed_at"] > latest[url][0]:
            latest[url] = (e["observed_at"], f)
            pub = (e.get("metadata") or {}).get("published_at")
            if pub:
                published[url] = str(pub)
            else:
                published.pop(url, None)
    counts["observations_html_2xx"] = len(obs)
    counts["observations_without_extract"] = sum(1 for o in obs if o.pgv not in facts_by_pgv)
    return Loaded(
        obs=[o for o in obs if o.pgv in facts_by_pgv],
        facts_by_pgv=facts_by_pgv,
        latest={u: f for u, (_, f) in latest.items()},
        revision_by_pgv=revision_by_pgv,
        published=published,
        redirects=redirects,
        counts=counts,
    )


def g5_hubs() -> frozenset[str]:
    """URLs of the G5 sample (held out). Only the sample's URLs are read, never labels."""
    if not G5_SAMPLE.exists():
        return frozenset()
    doc = json.loads(G5_SAMPLE.read_text())
    rows = doc.get("sample", doc) if isinstance(doc, dict) else doc
    return frozenset(r["url"] for r in rows if isinstance(r, dict) and "url" in r)


def keyspace() -> str:
    return str(json.loads((EXPORT / "export.json").read_text()).get("keyspace", ""))


def result_path(name: str) -> Path:
    """``results/when-<name>.json`` for M1, ``when-<name>-<keyspace>.json`` otherwise."""
    ks = keyspace()
    suffix = "" if ks == M1_KEYSPACE else f"-{ks}"
    return HERE / "results" / f"when-{name}{suffix}.json"


def what_population(
    data: Loaded, vocab: Any
) -> tuple[dict[str, SiteProfile], dict[str, WhatResult]]:
    """WHAT ``what/v1`` over the latest extract per URL (as the WHAT population)."""
    by_host_latest: dict[str, list[PageFacts]] = defaultdict(list)
    for url, f in data.latest.items():
        shape = tokenize_url(url)
        if shape is not None:
            by_host_latest[shape.host].append(f)
    profiles = {
        h: build_site_profile(h, pages, vocab=vocab) for h, pages in sorted(by_host_latest.items())
    }
    latest_result: dict[str, WhatResult] = {}
    for url, f in sorted(data.latest.items()):
        shape = tokenize_url(url)
        if shape is not None:
            latest_result[url] = analyze_page(f, profiles[shape.host], vocab=vocab)
    return profiles, latest_result


# --------------------------------------------------------------------------- audit


def cmd_audit() -> dict[str, Any]:
    vocab = load_vocabulary()
    data = load()
    profiles, latest_result = what_population(data, vocab)
    target_rel = {u: str(r.relevance.decision) for u, r in latest_result.items()}
    titles = {
        u: (f.title or f.og_title or "")[:160]
        + (f" | published_at {data.published[u]}" if u in data.published else "")
        for u, f in data.latest.items()
    }

    links_by_pgv = {p: same_host_links(f) for p, f in data.facts_by_pgv.items()}
    known_by_site: dict[str, dict[str, float]] = {}
    obs_by_site: dict[str, list[Obs]] = defaultdict(list)
    for o in data.obs:
        obs_by_site[site_of(o.url)].append(o)
    for site, so in obs_by_site.items():
        known_by_site[site] = first_known(so, links_by_pgv)
    keys_by_site = {s: first_known_keys(k) for s, k in known_by_site.items()}

    obs_by_url: dict[str, list[Obs]] = defaultdict(list)
    for o in data.obs:
        obs_by_url[o.url].append(o)
    cache: dict[int, WhatResult] = {}

    def analyzer(profile: SiteProfile) -> Any:
        def run(f: PageFacts) -> WhatResult:
            key = id(f)  # one PageFacts object per page version
            if key not in cache:
                cache[key] = analyze_page(f, profile, vocab=vocab)
            return cache[key]

        return run

    per_site: dict[str, Counter[str]] = defaultdict(Counter)
    events: list[Event] = []
    detail_events: list[Event] = []
    url_windows: dict[str, int] = {}
    kinds: dict[str, str] = {}
    for url in sorted(obs_by_url):
        site = site_of(url)
        c = per_site[site]
        uo = obs_by_url[url]
        c["urls"] += 1
        c["observations"] += len(uo)
        if len(uo) > 1:
            c["urls_multi_obs"] += 1
        res = latest_result.get(url)
        kind = str(res.kind.kind) if res else "none"
        kinds[url] = kind
        nw = len(windows(uo))
        url_windows[url] = nw
        if nw > 1:
            c["urls_multi_window"] += 1
            c[f"urls_multi_window_{kind}"] += 1
        if kind == PageKind.HUB:
            c["hubs"] += 1
            if len(uo) > 1:
                c["hubs_multi_obs"] += 1
        if nw < 2 or res is None:
            continue
        profile = profiles[res.host]
        ev = hub_events(
            url,
            uo,
            data.facts_by_pgv,
            profile,
            known_by_site[site],
            keys_by_site[site],
            data.redirects,
            analyze=analyzer(profile),
        )
        (events if kind == PageKind.HUB else detail_events).extend(ev)

    rows = [event_row(e, target_rel, titles) for e in events]
    non_hub_candidates = [
        {"url": e.hub, "kind": e.kind_at_window, "window": e.index, "candidates": e.candidates[:10]}
        for e in detail_events
        if e.candidate
    ]
    OUT.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT / "events.jsonl.gz", "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")

    def summarize(evs: list[Event]) -> dict[str, Any]:
        hubs = sorted({e.hub for e in evs})
        cand_by_hub = Counter(e.hub for e in evs if e.candidate)
        nov: Counter[str] = Counter()
        role_new: Counter[str] = Counter()
        for e in evs:
            for u in e.new_to_hub:
                nov[e.novelty[u]] += 1
                if e.novelty[u] != "known_before":
                    role_new[e.role[u]] += 1
        return {
            "revisited_urls": len(hubs),
            "events": len(evs),
            "events_changed": sum(e.changed for e in evs),
            "events_with_new_to_hub": sum(bool(e.new_to_hub) for e in evs),
            "candidate_events": sum(e.candidate for e in evs),
            "first_seen_here_events": sum(bool(e.first_seen_here) for e in evs),
            "candidate_links": sum(len(e.candidates) for e in evs),
            "distinct_candidate_links": len({u for e in evs for u in e.candidates}),
            "hubs_zero_candidate_events": sum(1 for h in hubs if cand_by_hub[h] == 0),
            "hubs_one_candidate_event": sum(1 for h in hubs if cand_by_hub[h] == 1),
            "hubs_repeated_candidate_events": sum(1 for h in hubs if cand_by_hub[h] >= 2),
            "interval_h": dist(e.interval_h for e in evs),
            "interval_buckets": dict(
                sorted(Counter(interval_bucket(e.interval_h) for e in evs).items())
            ),
            "candidate_events_by_interval": dict(
                sorted(Counter(interval_bucket(e.interval_h) for e in evs if e.candidate).items())
            ),
            "new_to_hub_per_event": dist(len(e.new_to_hub) for e in evs),
            "appeared_vs_prev_per_event": dist(e.appeared for e in evs),
            "disappeared_vs_prev_per_event": dist(e.disappeared for e in evs),
            "candidates_per_event": dist(len(e.candidates) for e in evs),
            "candidates_per_candidate_event": dist(len(e.candidates) for e in evs if e.candidate),
            "new_to_hub_novelty": dict(sorted(nov.items())),
            "new_since_prev_roles": dict(sorted(role_new.items())),
            "variants": sum(len(e.variant) for e in evs),
            "truncated_events": sum(e.truncated for e in evs),
            "kind_at_window": dict(sorted(Counter(e.kind_at_window for e in evs).items())),
        }

    by_site_events: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        by_site_events[e.site].append(e)
    domains = {}
    for site in sorted(per_site):
        domains[site] = dict(sorted(per_site[site].items())) | {
            "hub_events": summarize(by_site_events.get(site, []))
        }

    # Crediting: who links each candidate link (hub events and all latest pages).
    credit = credit_table(events)
    linkers: dict[str, list[str]] = defaultdict(list)
    cand_set = set(credit)
    for url, f in data.latest.items():
        for u in same_host_links(f) & cand_set:
            linkers[u].append(url)
    pages_per_site = Counter(site_of(u) for u in data.latest)
    credit_rows: list[dict[str, Any]] = []
    for u, cr in sorted(credit.items()):
        lk = linkers.get(u, [])
        lk_kinds = Counter(kinds.get(x) or _kind_of(latest_result, x) for x in lk)
        credit_rows.append(
            {
                "url": u,
                "site": site_of(u),
                "credited_hubs": len(cr.hubs),
                "credited_events": cr.events,
                "first_hub": cr.first_hub,
                "first_hub_is_root": urlsplit(cr.first_hub).path in ("", "/"),
                "latest_pages_linking": len(lk),
                "latest_linking_kinds": dict(sorted(lk_kinds.items())),
                "share_of_site_pages_linking": round(
                    len(lk) / max(1, pages_per_site[site_of(u)]), 4
                ),
                "target_what": target_rel.get(u, "not_fetched"),
            }
        )
    credit_summary = {
        "distinct_candidate_links": len(credit_rows),
        "credited_hubs_per_link": dist(r["credited_hubs"] for r in credit_rows),
        "links_credited_to_one_hub": sum(r["credited_hubs"] == 1 for r in credit_rows),
        "links_credited_to_several_hubs": sum(r["credited_hubs"] >= 2 for r in credit_rows),
        "first_hub_is_root": sum(r["first_hub_is_root"] for r in credit_rows),
        "latest_pages_linking": dist(r["latest_pages_linking"] for r in credit_rows),
        "widget_like_links": sum(r["share_of_site_pages_linking"] >= 0.05 for r in credit_rows),
        "target_what": dict(sorted(Counter(r["target_what"] for r in credit_rows).items())),
        "per_site": {
            s: {
                "links": sum(1 for r in credit_rows if r["site"] == s),
                "several_hubs": sum(
                    1 for r in credit_rows if r["site"] == s and r["credited_hubs"] >= 2
                ),
            }
            for s in sorted({r["site"] for r in credit_rows})
        },
    }

    hub_tot = summarize(events)
    cand_domains = Counter(e.site for e in events if e.candidate)
    g5 = g5_hubs()
    sufficiency = {
        "candidate_hub_events": hub_tot["candidate_events"],
        "required": GATE_MIN_CANDIDATE_EVENTS,
        "sufficient": hub_tot["candidate_events"] >= GATE_MIN_CANDIDATE_EVENTS,
        "domains_with_candidate_events": len(cand_domains),
        "largest_domain_share": round(max(cand_domains.values()) / sum(cand_domains.values()), 3)
        if cand_domains
        else None,
        "g5_hubs_among_revisited": len({e.hub for e in events} & g5),
        "candidate_hub_events_outside_g5": sum(e.candidate and e.hub not in g5 for e in events),
    }
    doc = {
        "audit": AUDIT_VERSION,
        "candidate_definition": CANDIDATE_VERSION,
        "what_policy": DEFAULT_WHAT_POLICY.version,
        "what_vocabulary": vocab.version,
        "export": json.loads((EXPORT / "export.json").read_text()),
        "min_gap_h": MIN_GAP_S / 3600,
        "counts": dict(sorted(data.counts.items())),
        "population": {
            "urls": len(obs_by_url),
            "urls_multi_obs": sum(1 for v in obs_by_url.values() if len(v) > 1),
            "urls_multi_window": sum(1 for n in url_windows.values() if n > 1),
            "urls_multi_window_by_kind": dict(
                sorted(Counter(kinds[u] for u, n in url_windows.items() if n > 1).items())
            ),
            "hubs": sum(1 for k in kinds.values() if k == PageKind.HUB),
            "hubs_multi_obs": sum(
                1 for u, k in kinds.items() if k == PageKind.HUB and len(obs_by_url[u]) > 1
            ),
        },
        "hub_events": hub_tot,
        "non_hub_events": summarize(detail_events),
        "candidate_events_by_page_kind": {
            "hub": hub_tot["candidate_events"],
            "non_hub": sum(e.candidate for e in detail_events),
        },
        "non_hub_candidate_events": non_hub_candidates,
        "credit": credit_summary,
        "credit_links": credit_rows,
        "sufficiency": sufficiency,
        "domains": domains,
        "hubs": [
            {
                "hub": h,
                "site": site_of(h),
                "windows": url_windows[h],
                "observations": len(obs_by_url[h]),
                "first": iso(min(o.at for o in obs_by_url[h])),
                "last": iso(max(o.at for o in obs_by_url[h])),
                "window_starts": [iso(w[0].at) for w in windows(obs_by_url[h])[:MAX_HUB_WINDOWS]],
                "page_versions": len({o.pgv for o in obs_by_url[h]}),
                "revisions": len(
                    {data.revision_by_pgv.get(o.pgv, "") for o in obs_by_url[h]} - {""}
                ),
                "events": sum(1 for e in events if e.hub == h),
                "candidate_events": sum(1 for e in events if e.hub == h and e.candidate),
                "candidate_links": sum(len(e.candidates) for e in events if e.hub == h),
            }
            for h in sorted({e.hub for e in events})
        ],
    }
    result_path("audit").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    return doc


def _kind_of(results: Mapping[str, WhatResult], url: str) -> str:
    r = results.get(url)
    return str(r.kind.kind) if r else "none"


# --------------------------------------------------------------------------- sample


def cmd_sample(force_dry_run: bool) -> int:
    doc = json.loads(result_path("audit").read_text())
    suff = doc["sufficiency"]
    if not suff["sufficient"] and not force_dry_run:
        print(
            f"refused: {suff['candidate_hub_events']} candidate hub revisits < "
            f"{suff['required']} (when.md §5). No sample is drawn; see the collection proposal."
        )
        return 2
    with gzip.open(OUT / "events.jsonl.gz", "rt", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    exclude = g5_hubs() if keyspace() == M1_KEYSPACE else frozenset()
    picked = sample_events(rows, exclude=exclude)
    if force_dry_run:
        print(f"dry run: {len(picked)} events would be sampled; nothing written")
        return 0
    out = result_path("sample")
    out.write_text(
        json.dumps(
            {
                "salt": SAMPLE_SALT,
                "split_salt": SPLIT_SALT,
                "candidate_definition": CANDIDATE_VERSION,
                "sample": [
                    {k: r[k] for k in ("id", "hub", "site", "window", "interval_bucket", "split")}
                    for r in picked
                ],
            },
            indent=1,
        )
        + "\n"
    )
    cards = OUT / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    for r in picked:
        (cards / f"{r['id']}.txt").write_text(card(r))
    with (OUT / "labels-template.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "label", "note"])
        for r in picked:
            w.writerow([r["id"], "", ""])
    print(f"sampled {len(picked)} events → {out}, cards in {cards}")
    return 0


def card(r: Mapping[str, Any]) -> str:
    """Label card: no candidate flag, no WHAT role, no target relevance (when.md §6)."""
    lines = [
        f"event {r['id']}  hub {r['hub']}",
        f"previous visit {r['prev_start']} .. {r['prev_end']}",
        f"this visit     {r['start']} .. {r['end']}  ({r['interval_h']} h later)",
        f"links on hub: {r['links']}; not in the previous visit: {r['appeared_vs_prev']}; "
        f"gone since the previous visit: {r['disappeared_vs_prev']}",
        "",
        "links new to this hub and first seen on the site since the previous visit:",
    ]
    for x in r["new_since_prev_links"]:
        lines.append(f"  {x['url']}")
        lines.append(f"      anchor: {x.get('anchor') or '-'}")
        lines.append(f"      target page title (if ever fetched): {x.get('target_title') or '-'}")
    if not r["new_since_prev_links"]:
        lines.append("  (none)")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- hubs


def cmd_hubs() -> dict[str, Any]:
    """The hub population of the proposed hub-only collection (when.md §7).

    Per live seed domain: WHAT hubs on their latest extract, not spam, not a
    pagination page, and either the bare site root or with at least
    ``PROPOSAL_MIN_CONTENT_LINKS`` content-template links. The root comes
    first, then the most content-template links, then URL hash; at most
    ``PROPOSAL_HUBS_PER_DOMAIN`` per domain. Uses no label of any sample."""
    vocab = load_vocabulary()
    data = load()
    _profiles, latest = what_population(data, vocab)
    last_seen: dict[str, float] = {}
    for o in data.obs:
        d = site_of(o.url)
        last_seen[d] = max(last_seen.get(d, 0.0), o.at)
    eligible: dict[str, list[tuple[bool, int, str, str]]] = defaultdict(list)
    for url, r in sorted(latest.items()):
        shape = tokenize_url(url)
        if shape is None or r.kind.kind != PageKind.HUB or shape.pagination:
            continue
        if r.relevance.decision == Relevance.SPAM:
            continue
        root = shape.root and not shape.search
        if not root and r.kind.own_content_links < PROPOSAL_MIN_CONTENT_LINKS:
            continue
        eligible[site_of(url)].append((root, r.kind.own_content_links, url, shape.template))
    sites = {}
    total = 0
    for d in LIVE:
        rows = sorted(eligible.get(d, []), key=lambda x: (not x[0], -x[1], _h("hub", x[2])))
        chosen = rows[:PROPOSAL_HUBS_PER_DOMAIN]
        total += len(chosen)
        sites[d] = {
            "hubs_eligible": len(rows),
            "last_html_observation": iso(last_seen[d]) if d in last_seen else None,
            "chosen": [
                {"url": u, "root": root, "content_template_links": n, "template": t}
                for root, n, u, t in chosen
            ],
        }
    doc = {
        "what_policy": DEFAULT_WHAT_POLICY.version,
        "what_vocabulary": vocab.version,
        "export_keyspace": keyspace(),
        "criteria": {
            "kind": "hub (latest extract)",
            "relevance": "not spam",
            "pagination": "excluded",
            "min_content_template_links": PROPOSAL_MIN_CONTENT_LINKS,
            "root": "the bare site root is always eligible (not search-root variants)",
            "per_domain": PROPOSAL_HUBS_PER_DOMAIN,
        },
        "hubs": total,
        "fetches_per_day_at_6h": total * 4,
        "sites": sites,
    }
    result_path("hub-proposal").write_text(json.dumps(doc, indent=1) + "\n")
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("audit")
    sub.add_parser("hubs")
    sp = sub.add_parser("sample")
    sp.add_argument("--dry-run", action="store_true", help="count only, even if insufficient")
    args = ap.parse_args()
    if args.cmd == "audit":
        doc = cmd_audit()
        print(json.dumps({"population": doc["population"], "sufficiency": doc["sufficiency"]}))
        return 0
    if args.cmd == "hubs":
        doc = cmd_hubs()
        print(json.dumps({d: v["hubs_eligible"] for d, v in doc["sites"].items()}))
        print(f"hubs {doc['hubs']}, fetches/day at 6 h: {doc['fetches_per_day_at_6h']}")
        return 0
    return cmd_sample(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
