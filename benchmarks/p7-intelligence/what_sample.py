"""P7 WHAT labelled sample (design §6, what.md §3). Not the G5 sample.

Read-only over the M1 export and the M1 snapshots in MinIO (bucket
``crawler2-m1``). Nothing is crawled; outputs go to ``var/p7/what``
(git-ignored: page facts and text) and ``results/what-sample.json`` (ids and
URLs only). Steps:

``population``   every seed-domain URL on the 11 live seed domains with a P5
                 extract (latest extract per URL) → ``var/p7/what/pages.jsonl.gz``.
``sample``       deterministic stratified sample (below) and the dev/holdout
                 split → ``results/what-sample.json``.
``materialize``  one label card per sampled page → ``var/p7/what/cards/<id>.txt``:
                 URL, metadata, same-site links with anchors, external hosts, and
                 the start of the P5 normalized main text (re-extracted from the
                 snapshot; the recomputed raw digest must match). The labeller
                 sees no WHAT feature or decision.

Sampling (fixed before labelling, 2026-10-08):

- strata = registrable domain x coarse URL shape (``coarse_shape``: digits,
  years, slugs and query keys collapsed). The shape is used for coverage only;
  it is not the WHAT tokenizer.
- per domain ``PER_DOMAIN`` pages: half allocated to shapes in proportion to
  their population (largest remainder), half round-robin over shapes in
  descending population, so rare kinds (home, search, archives, author pages,
  scripts) are covered. Within a shape, pages are taken in ``sha256(SALT|url)``
  order, which is arbitrary with respect to content.
- URLs in the G5 sample are excluded (the G5 labels are held out).
- split: ``holdout`` when ``sha256(SPLIT_SALT|url)`` is odd, else ``dev``.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.models.blobs import BlobRef
from antipiracy_contracts.models.web import UrlRef

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_m1 import read, reg

from crawler2.core.configuration import Settings
from crawler2.extraction.extract import _Collector, _walk
from crawler2.extraction.model import BASELINE_RULES, Limits
from crawler2.extraction.parse import parse_page
from crawler2.extraction.urls import ALLOW_ALL
from crawler2.storage.objectstore import S3ObjectStore

HERE = Path(__file__).parent
OUT = Path("var/p7/what")
PAGES = OUT / "pages.jsonl.gz"
CARDS = OUT / "cards"
SAMPLE = HERE / "results" / "what-sample.json"
G5_SAMPLE = HERE / "results" / "g5-sample.json"
BUCKET = "crawler2-m1"
# audit §5.1: the 11 seed domains with >= 50 HTML 2xx pages
LIVE = [
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
]
PER_DOMAIN = 26
SALT = "p7-what-sample/v1"
SPLIT_SALT = "p7-what-split/v1"


def _h(salt: str, url: str) -> str:
    return hashlib.sha256(f"{salt}|{url}".encode()).hexdigest()


def coarse_shape(url: str) -> str:
    """Stratification key only (not the WHAT tokenizer)."""
    s = urlsplit(url)
    out = []
    for seg in (x for x in s.path.split("/") if x):
        if re.fullmatch(r"(19|20)\d\d", seg):
            out.append("{year}")
        elif seg.isdigit():
            out.append("{num}")
        elif "." in seg:
            out.append("{w}" + seg[seg.rfind(".") :][:6])
        elif "-" in seg or "%" in seg or len(seg) > 15:
            out.append("{slug}")
        else:
            out.append(seg[:20])
    keys = sorted({k.split("=")[0] for k in s.query.split("&")}) if s.query else []
    return (s.hostname or "") + "/" + "/".join(out) + ("?" + "&".join(keys) if keys else "")


def cmd_population() -> None:
    live = set(LIVE)
    best: dict[str, dict[str, Any]] = {}
    for e in read("extracts"):
        if reg(e["url"]) in live and (
            e["url"] not in best or e["observed_at"] > best[e["url"]]["observed_at"]
        ):
            best[e["url"]] = e
    pgv = {e["page_version_id"] for e in best.values()}
    obs: dict[str, tuple[int, str]] = {}
    for o in read("observations"):
        if o["page_version_id"] in pgv:
            obs.setdefault(o["page_version_id"], (o["body_size"], o["content_type"] or ""))
    OUT.mkdir(parents=True, exist_ok=True)
    keep = ("url", "url_id", "page_version_id", "revision_id", "observed_at")
    keep += ("links", "media", "metadata", "stats")
    with gzip.open(PAGES, "wt", encoding="utf-8") as fh:
        for url in sorted(best):
            e = best[url]
            size, ctype = obs.get(e["page_version_id"], (0, ""))
            row = {k: e[k] for k in keep}
            row |= {"raw": e["hashes"]["raw"], "size": size, "content_type": ctype}
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    print(f"population {len(best)}: {dict(sorted(Counter(reg(u) for u in best).items()))}")


def pages() -> list[dict[str, Any]]:
    with gzip.open(PAGES, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def cmd_sample() -> None:
    g5 = {p["url"] for p in json.loads(G5_SAMPLE.read_text())["sample"]}
    by_dom: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    population: Counter[str] = Counter()
    excluded = 0
    for p in pages():
        population[reg(p["url"])] += 1
        if p["url"] in g5:
            excluded += 1
            continue
        by_dom[reg(p["url"])][coarse_shape(p["url"])].append(p["url"])
    chosen: list[dict[str, str]] = []
    for dom in sorted(by_dom):
        shapes = by_dom[dom]
        for v in shapes.values():
            v.sort(key=lambda u: _h(SALT, u))
        order = sorted(shapes, key=lambda s: (-len(shapes[s]), s))
        total = sum(len(v) for v in shapes.values())
        quota = min(PER_DOMAIN, total)
        prop = quota // 2
        exact = {s: prop * len(shapes[s]) / total for s in order}
        alloc = {s: int(exact[s]) for s in order}
        rest = sorted(order, key=lambda s: (-(exact[s] - alloc[s]), s))
        for s in rest[: prop - sum(alloc.values())]:
            alloc[s] += 1
        taken: dict[str, int] = dict(alloc)
        while sum(taken.values()) < quota:
            for s in order:
                if sum(taken.values()) >= quota:
                    break
                if taken[s] < len(shapes[s]):
                    taken[s] += 1
        for s in order:
            for u in shapes[s][: taken[s]]:
                split = "holdout" if int(_h(SPLIT_SALT, u), 16) % 2 else "dev"
                pid = _h(SALT, u)[:12]
                chosen.append({"id": pid, "domain": dom, "shape": s, "split": split, "url": u})
    chosen.sort(key=lambda r: (r["domain"], r["id"]))
    SAMPLE.write_text(
        json.dumps(
            {
                "population": dict(sorted(population.items())),
                "population_total": sum(population.values()),
                "excluded_g5_urls": excluded,
                "rule": f"per domain {PER_DOMAIN}: half proportional to coarse shape, half "
                f"round-robin over shapes; within a shape sha256({SALT}|url) order; "
                f"split holdout iff sha256({SPLIT_SALT}|url) is odd",
                "splits": dict(sorted(Counter(r["split"] for r in chosen).items())),
                "sample": chosen,
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
    print(f"sample {len(chosen)} ({Counter(r['split'] for r in chosen)}), g5 excluded {excluded}")


def _text(store: S3ObjectStore, p: dict[str, Any]) -> str:
    hexd = p["raw"].removeprefix("sha256:")
    ref = BlobRef(
        uri=f"s3://{BUCKET}/raw/sha256/{hexd[:2]}/{hexd[2:4]}/{hexd}",
        digest=ContentDigest(p["raw"]),
        size_bytes=p["size"],
    )
    body = store.get(ref)
    if "sha256:" + hashlib.sha256(body).hexdigest() != p["raw"]:
        raise SystemExit(f"{p['url']}: snapshot digest differs from the recorded one")
    limits = Limits()
    parsed = parse_page(
        body,
        page=UrlRef.of(p["url"]),
        content_type=p["content_type"],
        max_bytes=limits.max_parse_bytes,
    )
    col = _Collector(parsed, ALLOW_ALL, limits)
    _walk(parsed, col, BASELINE_RULES)
    return " | ".join(" ".join(c.split()) for c in col.normalized if c.strip())


def cmd_materialize() -> None:
    sample = json.loads(SAMPLE.read_text())["sample"]
    want = {r["url"]: r for r in sample}
    CARDS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        store = S3ObjectStore(Settings().minio, scratch_dir=Path(scratch), bucket=BUCKET)
        for p in pages():
            r = want.get(p["url"])
            if r is None:
                continue
            host = urlsplit(p["url"]).hostname
            same: dict[str, str] = {}
            ext: Counter[str] = Counter()
            for u, rel, anchor in p["links"]:
                if rel != "anchor":
                    continue
                h = urlsplit(u).hostname
                if h == host:
                    same.setdefault(u, (anchor or "")[:50])
                else:
                    ext[h or "?"] += 1
            m = p["metadata"]
            top_ext = sorted(ext.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
            prefix = len(f"{urlsplit(p['url']).scheme}://{host}")
            lines = [
                f"id {r['id']}  domain {r['domain']}",
                f"url {p['url']}",
                f"title: {m.get('title')}",
                f"og_title: {m.get('og_title')}  og_type: {m.get('og_type')}",
                f"published {m.get('published_at')}  modified {m.get('modified_at')}",
                f"text_chars {p['stats']['text_chars']}  media {len(p['media'])}  "
                f"same-site links {len(same)}  external links {sum(ext.values())}",
                "external hosts: " + ", ".join(f"{h} x{n}" for h, n in top_ext),
                f"same-site links (sorted, first 60 of {len(same)}):",
                *[f"  {u[prefix:][:90]}  [{a}]" for u, a in sorted(same.items())[:60]],
                "main text (first 900 chars):",
                "  " + _text(store, p)[:900],
            ]
            (CARDS / f"{r['id']}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"materialized {len(want)} cards in {CARDS}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("step", choices=["population", "sample", "materialize"])
    step = ap.parse_args().step
    {"population": cmd_population, "sample": cmd_sample, "materialize": cmd_materialize}[step]()


if __name__ == "__main__":
    main()
