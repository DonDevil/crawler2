"""Change-detection evaluation on real page pairs (P5 design §18-§19, exit gate).

    env/bin/python benchmarks/p5-extraction/pairs.py review CAP1 CAP2 --out var/p5-corpus/review
    env/bin/python benchmarks/p5-extraction/pairs.py score CAP1 CAP2 --labels LABELS --out FILE

``review`` pairs the two captures by requested URL (both 2xx HTML, same final
URL, bytes differ), draws a seeded stratified sample and writes one review
file per pair to local scratch: a line diff of the *whole* visible page text
(boilerplate included) and nothing about the rule's verdict, so labels are
made blind to the implementation. ``score`` joins the committed labels with
the rule (new revision ⇔ normalized hash changed) and reports the confusion
matrix. Page text never goes into git; the labels file holds only URLs,
body digests, the label and a one-line reason.
"""

from __future__ import annotations

import argparse
import difflib
import json
import random
import sys
from pathlib import Path

from antipiracy_contracts.models.web import UrlRef
from selectolax.lexbor import LexborHTMLParser

from crawler2.extraction.extract import extract
from crawler2.extraction.parse import encoding_of

sys.path.insert(0, str(Path(__file__).resolve().parent))
from corpus import Page, load

SEED = 20260929
PER_STRATUM = 30


def pairs(cap1: Path, cap2: Path) -> list[tuple[Page, Page]]:
    second = {p.url: p for p in load(cap2)}
    return [
        (a, second[a.url])
        for a in load(cap1)
        if a.url in second
        and a.final_url == second[a.url].final_url
        and a.sha256 != second[a.url].sha256
    ]


def normalized_changed(a: Page, b: Page) -> bool:
    ref = UrlRef.of(a.final_url)
    ea = extract(a.body, page=ref, content_type=a.content_type)
    eb = extract(b.body, page=ref, content_type=b.content_type)
    return ea.hashes.normalized != eb.hashes.normalized


def readable_text(page: Page) -> list[str]:
    """Review aid only (not the rule): visible-ish text, one line per text block."""
    tree = LexborHTMLParser(page.body.decode(encoding_of(page.body, page.content_type), "replace"))
    for node in tree.css("script, style, noscript, template, svg"):
        node.decompose()
    body = tree.body
    text = body.text(separator="\n") if body is not None else ""
    return [line.strip() for line in text.splitlines() if line.strip()]


def pair_id(a: Page, b: Page) -> str:
    return f"{a.sha256[:12]}-{b.sha256[:12]}"


def review(args: argparse.Namespace) -> None:
    found = pairs(args.cap1, args.cap2)
    predicted = [(a, b, normalized_changed(a, b)) for a, b in found]
    rng = random.Random(SEED)
    positives = [(a, b) for a, b, c in predicted if c]
    negatives = [(a, b) for a, b, c in predicted if not c]
    sample = rng.sample(positives, min(PER_STRATUM, len(positives))) + rng.sample(
        negatives, min(PER_STRATUM, len(negatives))
    )
    rng.shuffle(sample)  # the reviewer cannot tell the strata apart
    args.out.mkdir(parents=True, exist_ok=True)
    for index, (a, b) in enumerate(sample):
        diff = list(
            difflib.unified_diff(
                readable_text(a), readable_text(b), "capture-1", "capture-2", n=1, lineterm=""
            )
        )
        (args.out / f"{index:03d}_{pair_id(a, b)}.txt").write_text(
            f"url: {a.url}\n\n" + "\n".join(diff[:200]) + "\n"
        )
    summary = {
        "pairs_with_changed_bytes": len(found),
        "rule_positive": len(positives),
        "rule_negative": len(negatives),
        "sampled": len(sample),
        "seed": SEED,
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary))


def score(args: argparse.Namespace) -> None:
    labels = {row["pair"]: row for row in json.loads(args.labels.read_text())["pairs"]}
    found = {pair_id(a, b): (a, b) for a, b in pairs(args.cap1, args.cap2)}
    counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    rows = []
    for key, label in sorted(labels.items()):
        a, b = found[key]
        rule = normalized_changed(a, b)
        truth = label["label"] == "meaningful"
        cell = ("t" if rule == truth else "f") + ("p" if rule else "n")
        counts[cell] += 1
        rows.append(
            {"pair": key, "url": a.url, "label": label["label"], "rule_changed": rule, "cell": cell}
        )
    tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
    result = {
        **counts,
        "pairs": len(rows),
        "precision": round(tp / (tp + fp), 4) if tp + fp else None,
        "recall_in_sample": round(tp / (tp + fn), 4) if tp + fn else None,
        "rows": rows,
    }
    args.out.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["review", "score"])
    parser.add_argument("cap1", type=Path)
    parser.add_argument("cap2", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--labels", type=Path)
    args = parser.parse_args()
    review(args) if args.command == "review" else score(args)


if __name__ == "__main__":
    main()
