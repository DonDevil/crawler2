"""Seed loader and search adapters (design §15, §16).

No live network: the result pages are synthetic.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path

import pytest
from antipiracy_contracts.models.web import UrlRef

from crawler2.core.configuration import SearchSettings
from crawler2.discovery.search.adapters import ADAPTERS, PageStatus
from crawler2.discovery.search.runner import SearchRunner, read_queries
from crawler2.discovery.seeds import load_seeds, parse_seed_file
from tests.unit.discovery.test_admission import World

T0 = datetime(2026, 9, 29, 12, tzinfo=UTC)


def test_seed_file_parsing_invents_nothing() -> None:
    lines = parse_seed_file(
        "# comment\n\nhttps://Site.test/Path?q=1\nsite2.test\nftp://x.test/\nhttps://site.test/Path?q=1\n"
    )
    assert [(s.line, s.error is None) for s in lines] == [
        (3, True),
        (4, False),
        (5, False),
        (6, True),
    ]
    assert lines[0].url is not None
    assert lines[0].url.url == "https://site.test/Path?q=1"
    assert lines[1].error == "no scheme (not invented)"


def test_seeds_are_recorded_rooted_and_admitted(tmp_path: Path) -> None:
    w = World()
    seeds = tmp_path / "seeds.txt"
    seeds.write_text(
        "https://seed-a.test/\nhttps://www.seed-b.test/x\nbad\nhttps://seed-a.test/\nhttps://ads.test/\n"
    )
    report = load_seeds(
        seeds,
        source="m1",
        metadata={"operator": "test"},
        admitter=w.admitter,
        scope=w.scope,
        repo=w.repo,
        clock=lambda: T0,
    )
    assert (report.valid, report.duplicates, len(report.invalid)) == (3, 1, 1)
    assert report.outcomes == {"admitted": 2, "blocked": 1}
    assert {a.url.url for a in w.frontier.admitted} == {
        "https://seed-a.test/",
        "https://www.seed-b.test/x",
    }
    assert all(a.priority == 70 and a.reason == "seed" for a in w.frontier.admitted)
    records = w.repo.seeds("m1")
    assert {r.line for r in records} == {1, 2, 5}
    assert all(
        r.metadata == {"operator": "test"} and r.file_sha256 == report.sha256 for r in records
    )
    assert {"seed-a.test", "seed-b.test"} <= {s.domain for s in w.repo.scope_sites()}
    again = load_seeds(
        seeds, source="m1", metadata={}, admitter=w.admitter, scope=w.scope, repo=w.repo
    )
    assert again.outcomes == {"blocked": 1, "merged": 2}  # active tasks: merged, never duplicated


def _bing_href(target: str) -> str:
    encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&&p=abc&u=a1{encoded}&ntb=1"


PAGES = {
    "duckduckgo": (
        '<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg='
        'https%3A%2F%2Fone.test%2Fa&rut=x">One</a><a class="result__snippet">first</a></div>'
        '<div class="result"><a class="result__a" href="https://two.test/b">Two</a></div>'
        '<div class="result"><a class="result__a" href="https://duckduckgo.com/y">ad</a></div>'
    ),
    "bing": (
        f'<li class="b_algo"><h2><a href="{_bing_href("https://one.test/a")}">One</a></h2>'
        '<div class="b_caption"><p>first</p></div></li>'
        '<li class="b_algo"><h2><a href="https://two.test/b">Two</a></h2></li>'
    ),
    "brave": (
        '<div class="snippet" data-type="web"><a href="https://one.test/a">One</a>'
        '<div class="snippet-description">first</div></div>'
        '<div class="snippet" data-type="web"><a href="https://two.test/b">Two</a></div>'
    ),
    "yandex": (
        '<li class="serp-item"><a class="OrganicTitle-Link" href="https://one.test/a">One</a></li>'
        '<li class="serp-item"><a class="Link" href="https://two.test/b">Two</a></li>'
    ),
    "ahmia": (
        '<li class="result"><a href="/search/redirect?search_term=x&redirect_url='
        'http://abcdefghij234567.onion/a">One</a><p>first</p></li>'
        '<li class="result"><a href="http://bcdefghijk234567.onion/b">Two</a></li>'
    ),
}


@pytest.mark.parametrize("engine", sorted(PAGES))
def test_adapters_parse_and_unwrap_results(engine: str) -> None:
    adapter = ADAPTERS[engine]()
    page = adapter.parse(200, "https://engine.test/search", PAGES[engine].encode(), max_results=20)
    assert page.status is PageStatus.RESULTS
    assert [h.rank for h in page.hits] == [0, 1]
    assert page.hits[0].title == "One"
    urls = [h.url for h in page.hits]
    if engine == "ahmia":
        assert urls == ["http://abcdefghij234567.onion/a", "http://bcdefghijk234567.onion/b"]
    else:
        assert urls == ["https://one.test/a", "https://two.test/b"]
    assert page.hits[0].returned_url != page.hits[0].url or engine in ("brave", "yandex")


@pytest.mark.parametrize(
    ("engine", "status", "final", "body"),
    [
        ("yandex", 200, "https://yandex.com/showcaptcha?x", b"<html></html>"),
        (
            "duckduckgo",
            200,
            "https://html.duckduckgo.com/html/",
            b'<div class="anomaly-modal"></div>',
        ),
        ("bing", 429, "https://www.bing.com/search", b""),
    ],
)
def test_verification_pages_are_reported_blocked(
    engine: str, status: int, final: str, body: bytes
) -> None:
    page = ADAPTERS[engine]().parse(status, final, body, max_results=20)
    assert page.status is PageStatus.BLOCKED


def test_ahmia_prelude_hidden_fields_are_forwarded() -> None:
    prelude = b'<form action="/search/"><input type="hidden" name="tk" value="42"></form>'
    request = ADAPTERS["ahmia"]().build_request("query", prelude)
    assert request.params == {"q": "query", "tk": "42"}


def test_query_file(tmp_path: Path) -> None:
    path = tmp_path / "q.txt"
    path.write_text("# operator queries\nfree  movie\n\nfree movie\nsecond\n")
    queries, digest = read_queries(path)
    assert queries == ["free movie", "second"]
    assert len(digest) == 64


def test_runner_records_provenance_admits_and_cools_down_blocked_engines() -> None:
    w = World()
    calls: list[str] = []

    def fetch(url: str, params: dict[str, str]) -> tuple[int, str, bytes]:
        calls.append(url)
        if "bing" in url:
            return 429, url, b""
        return 200, url, PAGES["duckduckgo"].replace("two.test", "ads.test").encode()

    runner = SearchRunner(
        SearchSettings(engines=["duckduckgo", "bing"], pause_s=0, blocked_cooldown_queries=2),
        fetchers={"clearnet": fetch},
        admitter=w.admitter,
        scope=w.scope,
        repo=w.repo,
        clock=lambda: T0,
        sleep=lambda _s: None,
    )
    first, second, third, fourth = runner.run(["q1", "q2", "q3", "q4"])
    assert first.engines == {"duckduckgo": "results", "bing": "blocked"}
    assert second.engines["bing"] == third.engines["bing"] == "cooldown"
    assert fourth.engines["bing"] == "blocked"
    assert first.outcomes == {"admitted": 1, "blocked": 1}
    stored = w.repo.search_results("q1", T0)
    assert [(r.engine, r.rank, r.url) for r in stored] == [
        ("duckduckgo", 0, "https://one.test/a"),
        ("duckduckgo", 1, "https://ads.test/b"),
    ]
    assert stored[0].adapter_version == "p6-search/v1"
    domains = {s.domain for s in w.repo.scope_sites()}
    assert "one.test" in domains
    assert "ads.test" not in domains
    assert all(a.priority == 60 for a in w.frontier.admitted)
    assert UrlRef.of("https://one.test/a").url_id in {a.url.url_id for a in w.frontier.admitted}


def test_unknown_engines_are_refused() -> None:
    w = World()
    with pytest.raises(ValueError, match="unknown search engines"):
        SearchRunner(
            SearchSettings(engines=["nope"]),
            fetchers={},
            admitter=w.admitter,
            scope=w.scope,
            repo=w.repo,
        )
