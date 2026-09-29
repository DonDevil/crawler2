"""ABP/uBO network-filter parsing and pattern semantics (design §8.2, audit §6).

The list below is hand-written for these tests; it is not an excerpt of
EasyList or any other third-party list.
"""

from __future__ import annotations

import pytest

from crawler2.filtering.abp import ImportReport, parse_list
from crawler2.filtering.engine import FilterEngine
from crawler2.filtering.inputs import link_input, request_input
from crawler2.filtering.model import (
    Action,
    Classification,
    Party,
    Policy,
    Rule,
    RuleKind,
    RuleSource,
)
from crawler2.filtering.patterns import PatternError, compile_pattern, host_only

LIST = """[Adblock Plus 2.0]
! Title: P6 test list
! Version: 202609290000
! Licence: https://example.invalid/licence
||adnet.test^
||adnet.test^
||cdn.test/ads/*$script,third-party
/banner/*/ad_$image,domain=site.test|~safe.site.test
@@||cdn.test/ads/allowed.js$script
||tracker.test^$important
||popup.test^$popup
site.test##.ad-box
site.test#@#.ad-box
site.test#?#div:has(> .ad)
site.test##+js(noeval)
site.test##^script:has-text(ad)
/^https:\\/\\/re\\.test\\/[a-z]+$/$script
||x.test^$removeparam=utm_source
||y.test^$redirect=noop.js
$third-party,xmlhttprequest,domain=z.test
@@||site.test^$document
||e.test^$domain=example.*
||ok.test^$1p,~image
"""


@pytest.fixture(scope="module")
def parsed() -> tuple[list[Rule], ImportReport]:
    return parse_list(LIST, RuleSource.EASYLIST)


def test_header_and_counts(parsed: tuple[list[Rule], ImportReport]) -> None:
    rules, report = parsed
    assert report.header["title"] == "P6 test list"
    assert report.header["licence"] == "https://example.invalid/licence"
    assert report.duplicates == 1
    assert report.rules == len(rules) == 6
    assert report.exceptions == 1
    assert report.host_rules == 3
    assert report.unsupported == {
        "cosmetic": 2,
        "cosmetic_procedural": 1,
        "scriptlet": 1,
        "html_filter": 1,
        "regex": 1,
        "option:removeparam": 1,
        "option:redirect": 1,
        "match_all": 1,
        "page_exception": 1,
        "option:domain_entity_or_invalid": 1,
        "popup_only": 1,
    }
    assert (
        sum(report.unsupported.values()) + report.rules + report.duplicates + report.comments
        == report.lines
    )


def test_rules_keep_provenance_and_options(parsed: tuple[list[Rule], ImportReport]) -> None:
    rules, _ = parsed
    by_pattern = {r.pattern: r for r in rules}
    host = by_pattern["adnet.test"]
    assert (host.kind, host.classification, host.source) == (
        RuleKind.HOST,
        Classification.AD,
        RuleSource.EASYLIST,
    )
    assert host.origin.startswith("line 5: ||adnet.test^")
    scripted = by_pattern["||cdn.test/ads/*"]
    assert scripted.resource_types == {"script"}
    assert scripted.party is Party.THIRD
    banner = by_pattern["/banner/*/ad_"]
    assert banner.source_domains == {"site.test"}
    assert banner.excluded_source_domains == {"safe.site.test"}
    assert by_pattern["tracker.test"].important
    ok = by_pattern["ok.test"]
    assert ok.party is Party.FIRST
    assert ok.excluded_types == {"image"}


def test_easyprivacy_rules_are_trackers() -> None:
    rules, _ = parse_list("||px.test^\n", RuleSource.EASYPRIVACY)
    assert rules[0].classification is Classification.TRACKER


def test_parsed_list_behaves_like_abp(parsed: tuple[list[Rule], ImportReport]) -> None:
    rules, _ = parsed
    e = FilterEngine.compile(rules, Policy())

    def req(url: str, kind: str, page: str = "https://page.test/") -> Action:
        inp = request_input(url, kind, page)
        assert inp is not None
        return e.decide(inp).action

    assert req("https://cdn.test/ads/x.js", "script") is Action.BLOCK
    assert req("https://cdn.test/ads/x.js", "script", "https://cdn.test/") is Action.ALLOW
    assert req("https://cdn.test/ads/allowed.js", "script") is Action.ALLOW
    assert req("https://cdn.test/ads/x.png", "image") is Action.ALLOW
    assert req("https://img.test/banner/1/ad_2.png", "image", "https://site.test/") is Action.BLOCK
    assert req("https://img.test/banner/1/ad_2.png", "image", "https://safe.site.test/") is (
        Action.ALLOW
    )
    link = link_input("https://sub.adnet.test/page")
    assert link is not None
    assert e.decide(link).action is Action.BLOCK


@pytest.mark.parametrize(
    ("pattern", "url", "matches"),
    [
        ("||ads.test^", "https://ads.test/", True),
        ("||ads.test^", "https://a.ads.test:81/x", True),
        ("||ads.test^", "https://ads.test.evil/", False),
        ("||ads.test/x", "https://bads.test/x", False),
        ("|https://a.test/", "https://a.test/x", True),
        ("|https://a.test/", "http://z.test/https://a.test/", False),
        ("/ad.js|", "https://a.test/ad.js", True),
        ("/ad.js|", "https://a.test/ad.js?x", False),
        ("/ad^", "https://a.test/ad?x=1", True),
        ("/ad^", "https://a.test/ad.js", False),
        ("/ad^", "https://a.test/ad", True),
        ("/a*/b", "https://x.test/a123/b", True),
        ("AD.JS", "https://x.test/ad.js", True),
    ],
)
def test_pattern_semantics(pattern: str, url: str, matches: bool) -> None:
    assert (compile_pattern(pattern).regex.search(url.lower()) is not None) is matches


def test_host_only_patterns_become_host_rules() -> None:
    assert host_only("||ads.test^") == "ads.test"
    assert host_only("||ads.test^|") == "ads.test"
    assert host_only("||ads.test") is None
    assert host_only("||ads.test/x^") is None


def test_index_tokens_are_bounded() -> None:
    assert compile_pattern("/banner/*/ad_").tokens == ("banner", "ad")
    assert compile_pattern("ads*track").tokens == ()
    assert compile_pattern("||cdn.test/x").tokens == ("cdn", "test")


@pytest.mark.parametrize("pattern", ["*", "|", "||", "^", "/re/", "a|b"])
def test_unusable_patterns_are_refused(pattern: str) -> None:
    with pytest.raises(PatternError):
        compile_pattern(pattern)
