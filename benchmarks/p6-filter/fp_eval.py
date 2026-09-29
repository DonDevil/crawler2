"""P6 Gate E: false-positive guard and per-class precision/recall (design §21).

Evaluates the labelled corpus (``tests/fixtures/filtering/labeled_corpus.json``)
against the full ruleset (built_in + V1 reviewed import + EasyList +
EasyPrivacy from ``var/filter-lists``) and, for comparison, against each
source alone.

Guard: no item labelled content/media/player/navigation may be BLOCKed by a
*generic* rule — any rule from a third-party list (easylist, easyprivacy,
ublock) or a built-in heuristic (host_label). A block by explicit policy
with provenance (operator rules, the reviewed ``out_of_scope`` rules) is
allowed and listed.

    env/bin/python benchmarks/p6-filter/fp_eval.py [--operator FILE]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).parent))

from throughput import load_rules  # noqa: E402

from crawler2.filtering.cli import operator_rules  # noqa: E402
from crawler2.filtering.engine import FilterEngine  # noqa: E402
from crawler2.filtering.inputs import link_input, redirect_inputs, request_input  # noqa: E402
from crawler2.filtering.model import (  # noqa: E402
    Action,
    Decision,
    FilterInput,
    Policy,
    Rule,
    RuleKind,
)

CORPUS = ROOT / "tests" / "fixtures" / "filtering" / "labeled_corpus.json"
PROTECTED = {"content", "media", "player", "navigation"}
GENERIC_SOURCES = {"easylist", "easyprivacy", "ublock"}
MIN_SUPPORT = 20


def to_input(item: dict[str, object]) -> FilterInput | None:
    url, page = str(item["url"]), item["first_party"]
    if item["context"] == "link":
        return link_input(url, source_url=str(page) if page else None)
    if item["context"] == "request":
        return request_input(url, str(item["resource_type"]), str(page) if page else None)
    inputs = redirect_inputs(str(page), [], url)
    return inputs[0] if inputs else None


def generic(engine: FilterEngine, d: Decision) -> bool:
    rule = engine.rule(d.rule_id)
    if rule is None:
        return False
    return rule.source.value in GENERIC_SOURCES or rule.kind is RuleKind.HOST_LABEL


def evaluate(rules: list[Rule], items: list[dict[str, object]]) -> dict[str, object]:
    engine = FilterEngine.compile(rules, Policy())
    labelled: Counter[str] = Counter()
    predicted: Counter[str] = Counter()
    correct: Counter[str] = Counter()
    blocks: Counter[str] = Counter()
    violations, policy_blocks = [], []
    confusion: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for item in items:
        label = str(item["label"])
        inp = to_input(item)
        if inp is None:
            continue
        d = engine.decide(inp)
        got = d.classification.value
        labelled[label] += 1
        confusion[label][got] += 1
        if got != "unknown":
            predicted[got] += 1
            correct[got] += got == label
        if d.action is Action.BLOCK:
            blocks[label] += 1
            if label in PROTECTED:
                entry = {
                    "url": item["url"],
                    "label": label,
                    "context": item["context"],
                    "rule_id": d.rule_id,
                    "source": d.rule_source,
                    "reason": d.reason,
                    "pattern": d.matched_pattern,
                }
                (violations if generic(engine, d) else policy_blocks).append(entry)
    per_class = {}
    for label in sorted(labelled):
        support = labelled[label]
        tp = correct[label]
        per_class[label] = {
            "support": support,
            "predicted": predicted[label],
            "precision": round(tp / predicted[label], 3) if predicted[label] else None,
            "recall": round(tp / support, 3),
            "blocked": blocks[label],
            "meaningful": support >= MIN_SUPPORT,
            "confusion": dict(confusion[label]),
        }
    return {
        "ruleset": engine.ruleset_id,
        "rules": engine.rule_count,
        "items": sum(labelled.values()),
        "per_class": per_class,
        "guard_violations": violations,
        "explicit_policy_blocks": policy_blocks,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator", default=None, help="operator rule file to include")
    parser.add_argument("--out", default=str(Path(__file__).parent / "results" / "fp_eval.json"))
    args = parser.parse_args()
    items = json.loads(CORPUS.read_text())["items"]
    rules, sources = load_rules()
    if args.operator:
        rules += operator_rules(Path(args.operator))
    by_source: defaultdict[str, list[Rule]] = defaultdict(list)
    for r in rules:
        by_source[r.source.value].append(r)
    result = {
        "corpus": {
            "file": str(CORPUS.relative_to(ROOT)),
            "items": len(items),
            "synthetic": sum(bool(i["synthetic"]) for i in items),
            "labels": dict(Counter(str(i["label"]) for i in items)),
        },
        "sources": sources,
        "operator_rules": args.operator,
        "full": evaluate(rules, items),
        "by_source": {s: evaluate(rs, items)["per_class"] for s, rs in sorted(by_source.items())},
    }
    result["guard_passed"] = not result["full"]["guard_violations"]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    full = result["full"]
    print(
        json.dumps(
            {
                "guard_passed": result["guard_passed"],
                "violations": full["guard_violations"],
                "policy_blocks": len(full["explicit_policy_blocks"]),
                "per_class": {
                    k: {x: v[x] for x in ("support", "precision", "recall", "blocked")}
                    for k, v in full["per_class"].items()
                },
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
