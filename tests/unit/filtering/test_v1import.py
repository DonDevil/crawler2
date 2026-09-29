"""V1 blacklist import through the review manifest (design §8.1, audit §2).

The fixture is a byte-identical copy of V1 ``datasets/domain_blacklist.txt``
(sha256 f89692d1…, 98 entries), so the audit counts are pinned here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crawler2.filtering.engine import FilterEngine
from crawler2.filtering.inputs import link_input, request_input
from crawler2.filtering.model import (
    Action,
    Classification,
    Context,
    Decision,
    Policy,
    Rule,
    RuleSource,
)
from crawler2.filtering.v1import import Manifest, V1Report, normalize_entry, parse_blacklist

FIXTURE = Path(__file__).parents[2] / "fixtures" / "filtering" / "v1_domain_blacklist.txt"
SEED_HOSTS = [  # the six M1 seed hosts V1 had blacklisted (audit §2)
    "isaidub.love",
    "kuttymovies1.fit",
    "isaimini.com.in",
    "www.stripemovies.com",
    "moviedrivebd.com",
    "www.mp4moviez.diet",
]


@pytest.fixture(scope="module")
def imported() -> tuple[list[Rule], V1Report]:
    return parse_blacklist(FIXTURE.read_text("utf-8"), Manifest.load())


def test_audit_counts_are_reproduced(imported: tuple[list[Rule], V1Report]) -> None:
    rules, report = imported
    assert (report.lines, report.comments, report.entries) == (99, 1, 98)
    assert (report.imported, report.quarantined, report.rejected) == (60, 38, 0)
    assert (report.transformed, report.duplicated, report.unreviewed) == (0, 0, 0)
    assert report.ambiguous == 38
    assert dict(report.by_category) == {
        "ad_network": 10,
        "out_of_scope": 42,
        "unrelated_site": 8,
        "content_source": 21,
        "possible_media_source": 5,
        "heuristic_artefact": 5,
        "infra_ambiguous": 3,
        "ip_address": 2,
        "search_origin": 1,
        "reserved_name": 1,
    }
    assert len(rules) == 98
    assert all(r.source is RuleSource.V1_BLACKLIST for r in rules)


def test_import_is_deterministic(imported: tuple[list[Rule], V1Report]) -> None:
    again, _ = parse_blacklist(FIXTURE.read_text("utf-8"), Manifest.load())
    assert [r.to_doc() for r in again] == [r.to_doc() for r in imported[0]]


def test_quarantined_entries_never_block_seed_sites(imported: tuple[list[Rule], V1Report]) -> None:
    rules, _ = imported
    engine = FilterEngine.compile(rules, Policy())
    for host in SEED_HOSTS:
        inp = link_input(f"https://{host}/")
        assert inp is not None
        assert engine.decide(inp).action is Action.ALLOW, host
    quarantined = {r.pattern: r for r in rules if not r.enabled}
    assert quarantined["dl1.hotshare.click"].reason == "content_source"
    assert quarantined["dl1.hotshare.click"].origin.startswith("lines ")


def test_migrated_entries_keep_their_meaning(imported: tuple[list[Rule], V1Report]) -> None:
    engine = FilterEngine.compile(imported[0], Policy())

    def decide(url: str) -> Decision:
        inp = link_input(url, source_url="https://site.test/")
        assert inp is not None
        return engine.decide(inp)

    ad = decide("https://pagead.doubleclick.net/x")
    assert (ad.classification, ad.action, ad.reason) == (
        Classification.AD,
        Action.BLOCK,
        "ad_network",
    )
    scope = decide("https://en.wikipedia.org/wiki/X")
    assert (scope.classification, scope.action, scope.reason) == (
        Classification.CONTENT,
        Action.BLOCK,
        "out_of_scope",
    )
    unrelated = decide("https://www.nih.gov/")
    assert (unrelated.action, unrelated.confidence) == (Action.CLASSIFY, 0.6)
    # Out-of-scope rules apply to links only: an embedded YouTube player still loads.
    embed = request_input("https://www.youtube.com/embed/x", "subdocument", "https://site.test/")
    assert embed is not None
    assert engine.decide(embed).action is Action.ALLOW
    assert {c for r in imported[0] if r.reason == "out_of_scope" for c in r.contexts} == {
        Context.LINK
    }


def test_unreviewed_and_invalid_lines_are_reported_not_dropped() -> None:
    text = "# c\n\nnew-site.test\nhttps://Other.Test:8080/path\nnot a host\nnew-site.test\n"
    rules, report = parse_blacklist(text, Manifest.load())
    assert (report.unreviewed, report.quarantined, report.rejected) == (2, 2, 1)
    assert (report.transformed, report.duplicated) == (1, 1)
    assert report.rejects == ["line 5: not a host"]
    assert report.transforms == ["line 4: https://Other.Test:8080/path -> other.test"]
    assert all(not r.enabled and r.reason == "unreviewed" for r in rules)
    assert (
        next(r for r in rules if r.pattern == "new-site.test").origin == "lines 3,6: new-site.test"
    )


@pytest.mark.parametrize(
    ("entry", "host"),
    [
        ("Example.COM", "example.com"),
        ("example.com.", "example.com"),
        ("http://a.example.com/x?y", "a.example.com"),
        ("1.2.3.4", "1.2.3.4"),
        ("bad host", None),
        ("-bad.test", None),
    ],
)
def test_normalize_entry(entry: str, host: str | None) -> None:
    assert normalize_entry(entry) == host


def test_manifest_rejects_a_host_in_two_categories(tmp_path: Path) -> None:
    bad = tmp_path / "m.toml"
    bad.write_text(
        '[categories.a]\nverdict="migrate"\nreason="a"\nhosts=["x.test"]\n'
        '[categories.b]\nverdict="quarantine"\nreason="b"\nhosts=["x.test"]\n'
    )
    with pytest.raises(ValueError, match="two categories"):
        Manifest.load(bad)
