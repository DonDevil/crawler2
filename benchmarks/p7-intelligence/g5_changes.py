"""G5: is ``page.changed`` a useful scheduling signal? (P7 audit §3.2, decision D-9)

Read-only over the M1 export and the M1 snapshots in MinIO (bucket
``crawler2-m1``); nothing is crawled or written outside ``var/`` and
``results/``. Three steps:

``sample``       the 325 seed-domain ``page.changed`` transitions (first sighting
                 of consecutive revisions of a URL), a deterministic stratified
                 sample (domain x automatic new-URL flag), written to
                 ``results/g5-sample.json`` (ids and URLs only).
``materialize``  for every sampled pair: both snapshots, re-extracted with P5
                 (the recomputed normalized digests must equal the recorded
                 ones), and a diff for the labeller in ``var/p7/g5/<pair>.txt``
                 (git-ignored: page text). The automatic new-URL flag is **not**
                 shown, so labels do not copy the derived signal.
``evaluate``     reads ``g5/labels.csv`` and writes ``results/g5-result.json``.

Label schema: docs/phases/p07-crawl-intelligence/g5.md.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import sys
import tempfile
from collections import Counter, defaultdict
from itertools import pairwise
from pathlib import Path
from typing import Any

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.models.blobs import BlobRef
from antipiracy_contracts.models.web import UrlRef

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_m1 import read, reg

from crawler2.core.configuration import Settings
from crawler2.extraction.extract import _Collector, _walk, extract
from crawler2.extraction.model import BASELINE_RULES, Limits
from crawler2.extraction.parse import parse_page
from crawler2.extraction.urls import ALLOW_ALL
from crawler2.storage.objectstore import S3ObjectStore

HERE = Path(__file__).parent
SAMPLE = HERE / "results" / "g5-sample.json"
LABELS = HERE / "g5" / "labels.csv"
RESULT = HERE / "results" / "g5-result.json"
PAIRS = Path("var/p7/g5")
BUCKET = "crawler2-m1"
PER_STRATUM = 15  # non-revealing pairs drawn from each large domain
USEFUL = {"new_content_url", "new_media", "material_content"}
LABEL_SET = USEFUL | {"template_churn", "cosmetic_noise", "metadata_only", "spam_or_irrelevant"}


def _pairs() -> list[dict[str, Any]]:
    seeds = {reg(r["url"]) for r in read("seeds")}
    revs: Counter[str] = Counter()
    for r in read("revisions"):
        revs[r["url_id"]] += 1
    pgv: dict[str, dict[str, Any]] = {}
    for o in read("observations"):
        pgv.setdefault(o["page_version_id"], o)
    first_link: dict[str, str] = {}
    pages: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in read("extracts"):
        dom = reg(e["url"])
        if dom not in seeds:
            continue
        same = {lk[0] for lk in e["links"] if reg(lk[0]) == dom}
        for x in same:
            if x not in first_link or e["observed_at"] < first_link[x]:
                first_link[x] = e["observed_at"]
        if revs[e["url_id"]] >= 2:
            pages[e["url_id"]].append({**e, "domain": dom, "same": same})
    out = []
    for url_id, seq in pages.items():
        firsts: dict[str, dict[str, Any]] = {}
        for e in sorted(seq, key=lambda x: x["observed_at"]):
            firsts.setdefault(e["revision_id"], e)
        for a, b in pairwise(sorted(firsts.values(), key=lambda x: x["observed_at"])):
            added = b["same"] - a["same"]
            pid = hashlib.sha256(f"{url_id}|{a['revision_id']}|{b['revision_id']}".encode())
            out.append(
                {
                    "pair": pid.hexdigest()[:12],
                    "domain": a["domain"],
                    "url": a["url"],
                    "a": _side(a, pgv),
                    "b": _side(b, pgv),
                    "auto_new_url": any(first_link[x] == b["observed_at"] for x in added),
                    "auto_links_added": len(added),
                }
            )
    return sorted(out, key=lambda p: p["pair"])


def _side(e: dict[str, Any], pgv: dict[str, dict[str, Any]]) -> dict[str, Any]:
    o = pgv[e["page_version_id"]]
    return {
        "observed_at": e["observed_at"],
        "revision_id": e["revision_id"],
        "page_version_id": e["page_version_id"],
        "raw": e["hashes"]["raw"],
        "normalized": e["hashes"]["normalized"],
        "size": o["body_size"],
        "content_type": o["content_type"],
    }


def cmd_sample() -> None:
    pairs = _pairs()
    strata: dict[tuple[str, bool], list[dict[str, Any]]] = defaultdict(list)
    for p in pairs:
        strata[(p["domain"], p["auto_new_url"])].append(p)
    chosen = []
    for (dom, new), items in sorted(strata.items()):
        take = items if new or len(items) <= PER_STRATUM else items[:PER_STRATUM]
        chosen += [{**p, "stratum": f"{dom}|{'new' if new else 'none'}"} for p in take]
    SAMPLE.write_text(
        json.dumps(
            {
                "population": len(pairs),
                "strata": {f"{d}|{'new' if n else 'none'}": len(v) for (d, n), v in strata.items()},
                "rule": f"all pairs with an automatic new-URL flag; all others in strata of "
                f"≤ {PER_STRATUM}; else the first {PER_STRATUM} by pair id (sha256)",
                "sample": chosen,
            },
            indent=1,
            sort_keys=True,
        )
    )
    print(f"population {len(pairs)}, sample {len(chosen)}")


def _facts(store: S3ObjectStore, url: str, side: dict[str, Any]) -> dict[str, Any]:
    hexd = side["raw"].removeprefix("sha256:")
    ref = BlobRef(
        uri=f"s3://{BUCKET}/raw/sha256/{hexd[:2]}/{hexd[2:4]}/{hexd}",
        digest=ContentDigest(side["raw"]),
        size_bytes=side["size"],
    )
    body = store.get(ref)
    page = UrlRef.of(url)
    full = extract(body, page=page, content_type=side["content_type"])
    if str(full.hashes.normalized) != side["normalized"]:
        raise SystemExit(f"{url}: recomputed normalized digest differs from the recorded one")
    limits = Limits()
    parsed = parse_page(
        body, page=page, content_type=side["content_type"], max_bytes=limits.max_parse_bytes
    )
    col = _Collector(parsed, ALLOW_ALL, limits)
    _walk(parsed, col, BASELINE_RULES)
    dom = reg(url)
    return {
        "chunks": [" ".join(c.split()) for c in col.normalized if c.strip()],
        "links": {
            (lk.target.url, (lk.anchor_text or "")[:60])
            for lk in full.links
            if reg(lk.target.url) == dom
        },
        "media": {m.locator.url for m in full.media},
        "title": full.metadata.title,
        "modified": full.metadata.modified_at,
    }


def cmd_materialize() -> None:
    sample = json.loads(SAMPLE.read_text())["sample"]
    PAIRS.mkdir(parents=True, exist_ok=True)
    settings = Settings()
    with tempfile.TemporaryDirectory() as scratch:
        store = S3ObjectStore(settings.minio, scratch_dir=Path(scratch), bucket=BUCKET)
        for p in sample:
            a = _facts(store, p["url"], p["a"])
            b = _facts(store, p["url"], p["b"])
            text = list(difflib.unified_diff(a["chunks"], b["chunks"], lineterm="", n=0))[2:]
            links_added = sorted(b["links"] - a["links"])
            links_removed = sorted(a["links"] - b["links"])
            lines = [
                f"pair {p['pair']}  {p['url']}",
                f"A {p['a']['observed_at']}  B {p['b']['observed_at']}",
                f"title A: {a['title']}",
                f"title B: {b['title']}",
                f"modified A/B: {a['modified']} / {b['modified']}",
                f"media added {sorted(b['media'] - a['media'])[:5]} removed "
                f"{sorted(a['media'] - b['media'])[:5]}",
                f"same-site links +{len(links_added)} -{len(links_removed)}",
                *[f"  + {u}  [{t}]" for u, t in links_added[:15]],
                *[f"  - {u}  [{t}]" for u, t in links_removed[:8]],
                f"normalized text diff ({len(text)} lines, first 30):",
                *[f"  {line[:160]}" for line in text[:30]],
            ]
            (PAIRS / f"{p['pair']}.txt").write_text("\n".join(lines) + "\n")
    print(f"materialized {len(sample)} pairs in {PAIRS}")


def cmd_evaluate() -> None:
    meta = json.loads(SAMPLE.read_text())
    by_pair = {p["pair"]: p for p in meta["sample"]}
    with LABELS.open() as fh:
        labels = {r["pair"]: r for r in csv.DictReader(fh)}
    missing = set(by_pair) - set(labels)
    bad = {r["label"] for r in labels.values()} - LABEL_SET
    if missing or bad:
        raise SystemExit(f"unlabelled {sorted(missing)}; unknown labels {sorted(bad)}")
    useful = {k: labels[k]["label"] in USEFUL for k in by_pair}
    n = len(useful)
    # population-weighted precision: each stratum's useful share times its population size
    strata: dict[str, list[bool]] = defaultdict(list)
    for k, p in by_pair.items():
        strata[p["stratum"]].append(useful[k])
    pop = meta["strata"]
    weighted = sum(pop[s] * sum(v) / len(v) for s, v in strata.items()) / sum(
        pop[s] for s in strata
    )
    auto = {k: by_pair[k]["auto_new_url"] for k in by_pair}
    tp = sum(useful[k] and auto[k] for k in by_pair)
    fp = sum(auto[k] and not useful[k] for k in by_pair)
    fn = sum(useful[k] and not auto[k] for k in by_pair)
    result = {
        "labelled_pairs": n,
        "population": meta["population"],
        "label_counts": dict(Counter(r["label"] for r in labels.values() if r["pair"] in by_pair)),
        "page_changed_precision_sample": round(sum(useful.values()) / n, 3),
        "page_changed_precision_population_weighted": round(weighted, 3),
        "by_domain": {
            d: {
                "pairs": sum(1 for p in by_pair.values() if p["domain"] == d),
                "useful": sum(useful[k] for k, p in by_pair.items() if p["domain"] == d),
            }
            for d in sorted({p["domain"] for p in by_pair.values()})
        },
        "by_page_kind": dict(
            Counter(f"{labels[k]['page_kind']}:{'useful' if useful[k] else 'not'}" for k in by_pair)
        ),
        "derived_new_url_signal": {
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
            "true_negative": n - tp - fp - fn,
            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "recall_in_sample": round(tp / (tp + fn), 3) if tp + fn else None,
        },
    }
    RESULT.write_text(json.dumps(result, indent=1, sort_keys=True))
    print(json.dumps(result, indent=1, sort_keys=True))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("step", choices=["sample", "materialize", "evaluate"])
    {"sample": cmd_sample, "materialize": cmd_materialize, "evaluate": cmd_evaluate}[
        ap.parse_args().step
    ]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
