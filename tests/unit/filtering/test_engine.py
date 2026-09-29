"""P6 filter model, precedence, overrides and provenance (design §5-§7)."""

from __future__ import annotations

import random
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from crawler2.filtering.engine import FilterEngine, RulesetError, ruleset_digest
from crawler2.filtering.inputs import link_input, request_input
from crawler2.filtering.model import (
    Action,
    Classification,
    Context,
    FilterInput,
    Party,
    Policy,
    Rule,
    RuleKind,
    RuleSource,
)

AD = Classification.AD
CONTENT = Classification.CONTENT
MEDIA = Classification.MEDIA


def rule(pattern: str, klass: Classification = AD, **kw: Any) -> Rule:
    fields: dict[str, Any] = {
        "source": RuleSource.EASYLIST,
        "kind": RuleKind.HOST,
        "pattern": pattern,
        "classification": klass,
        "confidence": 0.95,
    }
    fields.update(kw)
    return Rule(**fields)


def engine(*rules: Rule, policy: Policy | None = None) -> FilterEngine:
    return FilterEngine.compile(rules, policy or Policy())


def link(url: str, source: str | None = None) -> FilterInput:
    inp = link_input(url, source_url=source)
    assert inp is not None
    return inp


def test_nothing_matches_is_allow_unknown_default() -> None:
    d = engine(rule("ads.test")).decide(link("https://site.test/page"))
    assert (d.classification, d.action, d.rule_id, d.candidates) == (
        Classification.UNKNOWN,
        Action.ALLOW,
        "default",
        0,
    )


def test_host_rule_matches_subdomains_on_label_boundaries_only() -> None:
    e = engine(rule("ads.test"))
    assert e.decide(link("https://x.ads.test/a")).action is Action.BLOCK
    assert e.decide(link("https://ads.test:8080/")).action is Action.BLOCK
    assert e.decide(link("https://badads.test/")).action is Action.ALLOW
    assert e.decide(link("https://ads.test.evil/")).action is Action.ALLOW


def test_longer_host_suffix_wins() -> None:
    e = engine(
        rule("site.test", CONTENT, source=RuleSource.OPERATOR, confidence=1.0),
        rule("ads.site.test", AD),
    )
    assert e.decide(link("https://ads.site.test/x")).classification is AD
    assert e.decide(link("https://www.site.test/x")).classification is CONTENT


def test_host_rule_beats_generic_url_pattern() -> None:
    """False-positive safety: a content-source host rule outranks a generic ad pattern."""
    e = engine(
        rule("/banner/*", AD, kind=RuleKind.URL_PATTERN),
        rule("cdn.test", MEDIA, source=RuleSource.OPERATOR, confidence=1.0),
    )
    d = e.decide(link("https://cdn.test/banner/movie.mp4"))
    assert d.classification is MEDIA
    assert d.action is Action.CLASSIFY
    assert d.candidates == 2


def test_protective_class_wins_an_exact_specificity_tie() -> None:
    e = engine(rule("x.test", AD), rule("x.test", MEDIA, source=RuleSource.EASYPRIVACY))
    assert e.decide(link("https://x.test/")).classification is MEDIA


def test_source_rank_then_rule_id_break_remaining_ties() -> None:
    a = rule("x.test", AD, source=RuleSource.V1_BLACKLIST, reason="v1")
    b = rule("x.test", AD, source=RuleSource.BUILT_IN, reason="builtin")
    assert engine(a, b).decide(link("https://x.test/")).reason == "builtin"
    c = rule("x.test", AD, source=RuleSource.EASYLIST, party=Party.ANY)
    d = rule("x.test", AD, source=RuleSource.EASYPRIVACY)
    winner = min(c, d, key=lambda r: r.rule_id)
    assert engine(c, d).decide(link("https://x.test/")).rule_id == winner.rule_id


def test_operator_explicit_action_overrides_everything_and_is_recorded() -> None:
    generic = rule("/ads/*", AD, kind=RuleKind.URL_PATTERN, important=True)
    override = rule(
        "cdn.test",
        MEDIA,
        source=RuleSource.OPERATOR,
        confidence=1.0,
        action=Action.ALLOW,
        reason="known_media_host",
    )
    d = engine(generic, override).decide(link("https://cdn.test/ads/1.mp4"))
    assert (d.action, d.classification, d.reason) == (Action.ALLOW, MEDIA, "known_media_host")
    assert d.override_of == generic.rule_id
    assert d.rule_source.startswith("operator")


def test_exception_cancels_ad_rules_but_not_important_ones() -> None:
    block = rule("/ad/*", AD, kind=RuleKind.URL_PATTERN)
    allow = rule("x.test", AD, exception=True)
    d = engine(block, allow).decide(link("https://x.test/ad/1"))
    assert (d.action, d.rule_id, d.override_of) == (Action.ALLOW, allow.rule_id, block.rule_id)
    important = rule("/ad/*", AD, kind=RuleKind.URL_PATTERN, important=True)
    d = engine(important, allow).decide(link("https://x.test/ad/1"))
    assert (d.action, d.rule_id) == (Action.BLOCK, important.rule_id)


def test_exception_does_not_cancel_non_ad_classifications() -> None:
    scope = rule(
        "social.test",
        CONTENT,
        source=RuleSource.BUILT_IN,
        confidence=1.0,
        action=Action.BLOCK,
        reason="out_of_scope",
    )
    allow = rule("social.test", AD, exception=True)
    d = engine(scope, allow).decide(link("https://social.test/"))
    assert (d.action, d.reason) == (Action.BLOCK, "out_of_scope")


def test_action_mapping_threshold_classes_and_explicit_action() -> None:
    low = rule("low.test", AD, confidence=0.6)
    tracker = rule("t.test", Classification.TRACKER)
    content = rule("c.test", CONTENT, confidence=1.0)
    explicit = rule("e.test", CONTENT, confidence=0.1, action=Action.BLOCK)
    e = engine(low, tracker, content, explicit)
    assert e.decide(link("https://low.test/")).action is Action.CLASSIFY
    assert e.decide(link("https://t.test/")).action is Action.BLOCK
    assert e.decide(link("https://c.test/")).action is Action.CLASSIFY
    assert e.decide(link("https://e.test/")).action is Action.BLOCK
    lenient = Policy(block_threshold=0.5)
    assert engine(low, policy=lenient).decide(link("https://low.test/")).action is Action.BLOCK
    no_link_blocks = Policy(block_classes={Context.LINK: frozenset()})
    d = engine(tracker, policy=no_link_blocks).decide(link("https://t.test/"))
    assert d.action is Action.CLASSIFY


def test_policy_is_part_of_the_ruleset_identity() -> None:
    r = rule("x.test")
    assert engine(r).ruleset_id != engine(r, policy=Policy(block_threshold=0.5)).ruleset_id


def test_party_modifiers_need_a_first_party() -> None:
    r = rule("/px/*", Classification.TRACKER, kind=RuleKind.URL_PATTERN, party=Party.THIRD)
    e = engine(r)
    assert (
        e.decide(link("https://cdn.other.test/px/1", "https://site.test/")).action is Action.BLOCK
    )
    assert e.decide(link("https://www.site.test/px/1", "https://site.test/")).action is Action.ALLOW
    assert e.decide(link("https://cdn.other.test/px/1")).action is Action.ALLOW


def test_registrable_domain_decides_third_party() -> None:
    inp = link("https://dl1.hotshare.click/f", "https://www.hotshare.click/")
    assert inp.third_party is False
    inp = link("https://a.example.co.uk/", "https://b.other.co.uk/")
    assert inp.third_party is True


def test_source_domain_constraints() -> None:
    r = rule(
        "/w/*",
        AD,
        kind=RuleKind.URL_PATTERN,
        source_domains=frozenset({"site.test"}),
        excluded_source_domains=frozenset({"safe.site.test"}),
    )
    e = engine(r)
    assert e.decide(link("https://x.test/w/1", "https://www.site.test/")).action is Action.BLOCK
    assert e.decide(link("https://x.test/w/1", "https://safe.site.test/")).action is Action.ALLOW
    assert e.decide(link("https://x.test/w/1", "https://other.test/")).action is Action.ALLOW


def test_resource_type_and_context_constraints() -> None:
    r = rule("x.test", AD, resource_types=frozenset({"script"}))
    e = engine(r)
    script = request_input("https://x.test/a.js", "script", "https://site.test/")
    image = request_input("https://x.test/a.png", "image", "https://site.test/")
    assert script is not None
    assert image is not None
    assert e.decide(script).action is Action.BLOCK
    assert e.decide(image).action is Action.ALLOW
    only_links = rule("y.test", AD, contexts=frozenset({Context.LINK}))
    req = request_input("https://y.test/a.js", "script", "https://site.test/")
    assert req is not None
    assert engine(only_links).decide(req).action is Action.ALLOW


def test_decision_carries_provenance() -> None:
    r = rule("x.test", AD, reason="ad_network").with_revision("abc123")
    d = engine(r).decide(link("https://x.test/"))
    assert d.rule_source == "easylist@abc123"
    assert (d.matched_pattern, d.matched_field, d.reason) == ("x.test", "link", "ad_network")
    assert d.ruleset.startswith("rs-")
    assert d.to_doc()["rule_id"] == r.rule_id


def test_disabled_and_selector_rules_are_not_compiled() -> None:
    off = rule("x.test", enabled=False)
    sel = rule(".widget", CONTENT, kind=RuleKind.SELECTOR, source=RuleSource.OPERATOR)
    e = engine(off, sel)
    assert e.rule_count == 0
    assert e.decide(link("https://x.test/")).action is Action.ALLOW


def test_duplicate_rules_are_refused() -> None:
    with pytest.raises(RulesetError):
        engine(rule("x.test"), rule("x.test", reason="again"))


def test_expected_digest_is_verified() -> None:
    with pytest.raises(RulesetError):
        FilterEngine.compile([rule("x.test")], Policy(), expected_id="rs-wrong")


def test_rule_id_is_stable_and_ignores_assertion_fields() -> None:
    a = rule("x.test", AD, confidence=0.9, reason="a")
    b = rule("x.test", MEDIA, confidence=0.1, reason="b", origin="line 9")
    assert a.rule_id == b.rule_id
    assert a.rule_id != rule("x.test", party=Party.THIRD).rule_id
    assert Rule.from_doc(a.to_doc()) == a


def test_rule_validation() -> None:
    with pytest.raises(ValueError, match="confidence"):
        rule("x.test", confidence=1.5)
    with pytest.raises(ValueError, match="resource types"):
        rule("x.test", resource_types=frozenset({"bogus"}))
    with pytest.raises(ValueError, match="only allow"):
        rule("x.test", exception=True, action=Action.BLOCK)


RULES = [
    rule("ads.test"),
    rule("site.test", CONTENT, source=RuleSource.OPERATOR, confidence=1.0),
    rule("/banner/*", kind=RuleKind.URL_PATTERN),
    rule("||cdn.test/ad^", kind=RuleKind.URL_PATTERN),
    rule("cdn.test", exception=True),
    rule("adserver", kind=RuleKind.HOST_LABEL, source=RuleSource.BUILT_IN, confidence=0.6),
    rule("site.test/ads/", kind=RuleKind.PATH, source=RuleSource.OPERATOR),
]
URLS = [
    "https://ads.test/x",
    "https://www.site.test/banner/1",
    "https://site.test/ads/1",
    "https://cdn.test/ad?x=1",
    "https://adserver.other.test/",
    "https://plain.test/",
]


@settings(max_examples=50, deadline=None)
@given(st.randoms(use_true_random=False))
def test_compilation_is_deterministic_under_any_input_order(rnd: random.Random) -> None:
    shuffled = RULES[:]
    rnd.shuffle(shuffled)
    reference = engine(*RULES)
    other = engine(*shuffled)
    assert other.ruleset_id == reference.ruleset_id == ruleset_digest(RULES, Policy())
    for url in URLS:
        assert other.decide(link(url)) == reference.decide(link(url))


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=200), st.sampled_from(["script", "image", "xhr", "document", "weird"]))
def test_untrusted_input_never_raises(text: str, kind: str) -> None:
    e = engine(*RULES)
    for url in (text, "https://" + text, "http://a.test/" + text):
        for inp in (link_input(url), request_input(url, kind, "https://site.test/")):
            if inp is not None:
                e.decide(inp)
