"""Offline evaluation of P7 WHAT on its labelled sample (what.md §5).

Read-only: ``var/p7/what/pages.jsonl.gz`` (``what_sample.py population``),
``results/what-sample.json`` and ``what/labels.csv``. Site profiles are built
from every population page of a host (no labels); each labelled page is then
analysed against its host's profile.

    what_eval.py --split dev        # rule development (any number of runs)
    what_eval.py --split holdout    # once, after the rules are frozen

Output: ``results/what-eval-<split>.json``. Relevance is scored on pages
labelled hub or detail with a relevant/spam label; ``unknown`` is an
abstention (it lowers recall and coverage, never precision).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from crawler2.intelligence.what import (
    DEFAULT_WHAT_POLICY,
    TemplateRole,
    analyze_page,
    build_site_profile,
    facts_from_export,
    load_vocabulary,
    tokenize_url,
)

HERE = Path(__file__).parent
PAGES = Path("var/p7/what/pages.jsonl.gz")
SAMPLE = HERE / "results" / "what-sample.json"
LABELS = HERE / "what" / "labels.csv"
KINDS = ("hub", "detail", "other")
REL = {"relevant_content": "relevant", "spam_or_irrelevant": "spam"}


def prf(pairs: list[tuple[str, str]], classes: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c in classes:
        tp = sum(1 for g, p in pairs if g == c and p == c)
        fp = sum(1 for g, p in pairs if g != c and p == c)
        fn = sum(1 for g, p in pairs if g == c and p != c)
        pr = tp / (tp + fp) if tp + fp else None
        rc = tp / (tp + fn) if tp + fn else None
        f1 = (
            2 * pr * rc / (pr + rc)
            if pr and rc
            else (0.0 if pr is not None and rc is not None else None)
        )
        out[c] = {
            "support": tp + fn,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": pr,
            "recall": rc,
            "f1": f1,
        }
    return out


def confusion(
    pairs: list[tuple[str, str]], gold: tuple[str, ...], pred: tuple[str, ...]
) -> dict[str, dict[str, int]]:
    c = Counter(pairs)
    return {g: {p: c[(g, p)] for p in pred} for g in gold}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--split", choices=["dev", "holdout", "all"], required=True)
    split = ap.parse_args().split
    vocab = load_vocabulary()
    sample = {r["id"]: r for r in json.loads(SAMPLE.read_text())["sample"]}
    labels = [
        lab
        for lab in csv.DictReader(LABELS.open())
        if split == "all" or sample[lab["id"]]["split"] == split
    ]
    want = {sample[lab["id"]]["url"]: lab for lab in labels}
    by_host: dict[str, list[Any]] = defaultdict(list)
    target: dict[str, Any] = {}
    with gzip.open(PAGES, "rt", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            facts = facts_from_export(row)
            shape = tokenize_url(facts.url)
            if shape is None:
                continue
            by_host[shape.host].append(facts)
            if facts.url in want:
                target[facts.url] = facts
    hosts = sorted({tokenize_url(u).host for u in want})  # type: ignore[union-attr]
    t0 = time.perf_counter()
    profiles = {h: build_site_profile(h, by_host[h], vocab=vocab) for h in hosts}
    build_s = time.perf_counter() - t0
    rows = []
    for url, lab in sorted(want.items(), key=lambda kv: kv[1]["id"]):
        res = analyze_page(target[url], profiles[tokenize_url(url).host], vocab=vocab)  # type: ignore[union-attr]
        rows.append(
            {
                "id": lab["id"],
                "domain": lab["domain"],
                "url": url,
                "gold_kind": lab["page_kind"],
                "kind": str(res.kind.kind),
                "kind_reasons": list(res.kind.reasons),
                "own_links": res.kind.own_links,
                "own_content_links": res.kind.own_content_links,
                "gold_relevance": lab["relevance"],
                "relevance": str(res.relevance.decision),
                "positive": list(res.relevance.positive),
                "negative": list(res.relevance.negative),
                "terms": list(res.relevance.terms),
                "template": res.template,
                "template_role": str(res.template_role) if res.template_role else None,
            }
        )

    def kind_block(rs: list[dict[str, Any]]) -> dict[str, Any]:
        pairs = [(r["gold_kind"], r["kind"]) for r in rs]
        return {
            "n": len(rs),
            "accuracy": sum(g == p for g, p in pairs) / len(rs) if rs else None,
            "per_class": prf(pairs, KINDS),
            "confusion": confusion(pairs, KINDS, KINDS),
        }

    def rel_block(rs: list[dict[str, Any]]) -> dict[str, Any]:
        rs = [r for r in rs if r["gold_kind"] in ("hub", "detail") and r["gold_relevance"] in REL]
        pairs = [(REL[r["gold_relevance"]], r["relevance"]) for r in rs]
        return {
            "n": len(rs),
            "decided": sum(p != "unknown" for _, p in pairs),
            "accuracy_all": sum(g == p for g, p in pairs) / len(rs) if rs else None,
            "per_class": prf(pairs, ("relevant", "spam")),
            "confusion": confusion(pairs, ("relevant", "spam"), ("relevant", "spam", "unknown")),
        }

    per_domain = {
        d: {"kind": kind_block(rs), "relevance": rel_block(rs)}
        for d, rs in sorted(
            defaultdict(
                list,
                {d: [r for r in rows if r["domain"] == d] for d in {r["domain"] for r in rows}},
            ).items()
        )
    }
    detail_rows = [r for r in rows if r["gold_kind"] == "detail"]
    templates = {}
    for h, prof in profiles.items():
        lab_details = [r for r in detail_rows if tokenize_url(r["url"]).host == h]  # type: ignore[union-attr]
        templates[h] = {
            "pages": prof.pages,
            "urls": prof.urls,
            "script": prof.script,
            "boilerplate_links": len(prof.boilerplate_links),
            "title_boilerplate": sorted(prof.title_boilerplate),
            "templates": [
                {
                    "template": t.template,
                    "support": t.support,
                    "content": t.content,
                    "spam": t.spam,
                    "role": str(t.role),
                }
                for t in prof.templates
            ],
            "labelled_details": dict(
                sorted(
                    Counter(
                        f"{r['template']} [{r['template_role']}] gold={r['gold_relevance']}"
                        for r in lab_details
                    ).items()
                )
            ),
        }
    contamination = sum(
        1
        for r in detail_rows
        if r["gold_relevance"] == "spam_or_irrelevant"
        and r["template_role"] == str(TemplateRole.CONTENT)
    )
    covered = sum(
        1
        for r in detail_rows
        if r["gold_relevance"] == "relevant_content" and r["template_role"] in ("content", "mixed")
    )
    n_relevant = sum(1 for r in detail_rows if r["gold_relevance"] == "relevant_content")
    n_spam = sum(1 for r in detail_rows if r["gold_relevance"] == "spam_or_irrelevant")
    result = {
        "split": split,
        "policy": DEFAULT_WHAT_POLICY.version,
        "vocabulary": vocab.version,
        "labels": len(rows),
        "kind": kind_block(rows),
        "relevance": rel_block(rows),
        "per_domain": per_domain,
        "templates": {
            "relevant_details_in_content_or_mixed_template": f"{covered}/{n_relevant}",
            "spam_details_in_content_template": f"{contamination}/{n_spam}",
            "hosts": templates,
        },
        "profile_build_seconds": round(build_s, 3),
        "rows": rows,
    }
    out = HERE / "results" / f"what-eval-{split}.json"
    out.write_text(json.dumps(result, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    k, r = result["kind"], result["relevance"]
    print(f"{split}: kind acc {k['accuracy']:.3f} n={k['n']}  rel decided {r['decided']}/{r['n']}")
    for c, m in k["per_class"].items():
        print(f"  kind {c:7s} P={m['precision']} R={m['recall']} F1={m['f1']} n={m['support']}")
    for c, m in r["per_class"].items():
        print(f"  rel  {c:8s} P={m['precision']} R={m['recall']} F1={m['f1']} n={m['support']}")
    print(
        "  templates:",
        result["templates"]["relevant_details_in_content_or_mixed_template"],
        "relevant details covered;",
        result["templates"]["spam_details_in_content_template"],
        "spam details in a content template",
    )
    for d, b in per_domain.items():
        kb, rb = b["kind"], b["relevance"]
        print(
            f"  {d:22s} kind {kb['accuracy']:.2f} (n={kb['n']})  "
            f"rel decided {rb['decided']}/{rb['n']} acc {rb['accuracy_all']}"
        )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
