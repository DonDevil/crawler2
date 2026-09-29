"""False-positive guard on the labelled corpus with the rules that live in git (Gate E).

The full evaluation (with EasyList/EasyPrivacy, which are not vendored) is
``benchmarks/p6-filter/fp_eval.py``. Here: built_in + the reviewed V1
import must never block a protected item except by explicit, provenance-
carrying out-of-scope policy.
"""

from __future__ import annotations

import json
from pathlib import Path

from crawler2.filtering.builtin import built_in_rules
from crawler2.filtering.engine import FilterEngine
from crawler2.filtering.inputs import link_input, redirect_inputs, request_input
from crawler2.filtering.model import Action, FilterInput, Policy
from crawler2.filtering.v1import import Manifest, parse_blacklist

FIXTURES = Path(__file__).parents[2] / "fixtures" / "filtering"
PROTECTED = {"content", "media", "player", "navigation"}


def _input(item: dict[str, object]) -> FilterInput | None:
    url, page = str(item["url"]), item["first_party"]
    if item["context"] == "link":
        return link_input(url, source_url=str(page) if page else None)
    if item["context"] == "request":
        return request_input(url, str(item["resource_type"]), str(page) if page else None)
    inputs = redirect_inputs(str(page), [], url)
    return inputs[0] if inputs else None


def test_protected_items_are_only_blocked_by_explicit_policy() -> None:
    corpus = json.loads((FIXTURES / "labeled_corpus.json").read_text())["items"]
    assert len(corpus) >= 300
    v1, _ = parse_blacklist((FIXTURES / "v1_domain_blacklist.txt").read_text(), Manifest.load())
    engine = FilterEngine.compile(built_in_rules() + v1, Policy())
    blocked = []
    for item in corpus:
        inp = _input(item)
        assert inp is not None, item["url"]
        decision = engine.decide(inp)
        if item["label"] in PROTECTED and decision.action is Action.BLOCK:
            blocked.append((item["url"], decision.reason, decision.rule_source.split("@")[0]))
    assert blocked, "the social/reference items exercise the out_of_scope policy"
    assert {(reason, source) for _, reason, source in blocked} <= {
        ("out_of_scope", "built_in"),
        ("out_of_scope", "v1_blacklist"),
    }
