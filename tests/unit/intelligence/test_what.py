"""P7 WHAT: URL templates, site profiles, page kind and relevance (what.md)."""

from __future__ import annotations

import ast
import random
from dataclasses import replace
from pathlib import Path

import pytest
from antipiracy_contracts.models.web import UrlRef
from hypothesis import given, settings
from hypothesis import strategies as st

from crawler2.extraction.extract import extract
from crawler2.intelligence.what import (
    DEFAULT_WHAT_POLICY,
    PageFacts,
    PageKind,
    Relevance,
    TemplateRole,
    Vocabulary,
    analyze_page,
    build_site_profile,
    facts_from_export,
    facts_from_extract,
    load_vocabulary,
    tokenize_url,
)

HOST = "site.example"
B = f"https://{HOST}"
NAV = ((f"{B}/", "Home"), (f"{B}/about-us/", "About Us"), (f"{B}/contact-us/", "Contact"))
FILMS = [f"name {w} {1990 + i}" for i, w in enumerate("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN")]
SPAM = [f"best casino bonus guide {w}" for w in "abcdefghijklmnopqrst"]
QUIET_SPAM = [f"a practical guide to brand{n}" for n in range(15)]  # no vocabulary term


def slug(title: str) -> str:
    return "-".join(title.lower().split())


def film_url(t: str) -> str:
    return f"{B}/{slug(t)}/"


def film_page(t: str, extra: tuple[tuple[str, str], ...] = ()) -> PageFacts:
    name, year = t.rsplit(" ", 1)
    widget = [(film_url(f), f.title()) for f in FILMS[:3] if f != t]
    return PageFacts(
        url=film_url(t),
        title=f"{name.title()} ({year}) - Site 2026",
        og_type="article",
        published=True,
        text_chars=3_000,
        links=(*NAV, *widget, *extra),
    )


def post_page(t: str) -> PageFacts:
    return PageFacts(
        url=f"{B}/{slug(t)}/",
        title=f"{t.title()} - Site 2026",
        og_type="article",
        text_chars=6_000,
        links=(*NAV, (f"{B}/author/team/", "Team"), (f"{B}/2026/04/26/", "April 26, 2026")),
    )


def listing(n: int, titles: list[str]) -> PageFacts:
    return PageFacts(
        url=f"{B}/category/films/page/{n}/",
        title=f"Films - Site 2026 - Page {n} of 9",
        text_chars=2_000,
        links=(*NAV, *((film_url(t), t.title()) for t in titles)),
    )


def blog_listing() -> PageFacts:
    return PageFacts(
        url=f"{B}/category/blog/",
        title="Blog - Site 2026",
        text_chars=2_000,
        links=(*NAV, *((f"{B}/{slug(t)}/", t.title()) for t in QUIET_SPAM[:10])),
    )


def site_pages() -> list[PageFacts]:
    pages = [film_page(t) for t in FILMS]
    pages += [post_page(t) for t in SPAM + QUIET_SPAM]
    pages += [listing(i, FILMS[i * 8 : i * 8 + 12]) for i in range(1, 4)]
    pages.append(blog_listing())
    pages.append(
        PageFacts(
            url=f"{B}/",
            title="Site 2026 - Watch Films",
            text_chars=900,
            links=(*NAV[1:], *((film_url(t), t.title()) for t in FILMS[:30])),
        )
    )
    return pages


VOCAB = load_vocabulary()
PROFILE = build_site_profile(HOST, site_pages(), vocab=VOCAB)


def analyze(facts: PageFacts):  # type: ignore[no-untyped-def]
    return analyze_page(facts, PROFILE, vocab=VOCAB)


# ------------------------------------------------------------------------- tokenizer


@pytest.mark.parametrize(
    ("url", "template"),
    [
        ("https://a.example/2025/11/bison-tamil-movie-download.html", "/{year}/{mm}/{slug}.html"),
        ("https://a.example/2024/05/17/", "/{year}/{mm}/{dd}/"),
        ("https://a.example/lens-2017/", "/{slug}-{year}/"),
        ("https://a.example/lens-2017", "/{slug}-{year}"),
        ("https://a.example/wolf-run-slots-at-playrand/", "/{slug}/"),
        ("https://a.example/12a-railway-colony-2025-tamil-movie", "/{slug}"),
        ("https://a.example/2025/11/bison-2025.html", "/{year}/{mm}/{slug}-{year}.html"),
        ("https://a.example/tamil-2015-movies/2/", "/{slug}/{num}/"),
        ("https://a.example/category/2015/page/3/", "/category/{year}/page/{num}/"),
        ("https://a.example/author/alfa-team/", "/author/{slug}/"),
        ("https://a.example/p/about-us.html", "/p/{slug}.html"),
        ("https://a.example/wp-login.php?redirect_to=x", "/{slug}.php"),
        ("https://a.example/12345.html", "/{num}.html"),
        ("https://a.example/ta/film-x/", "/{lang}/{slug}/"),
        ("https://a.example/", "/"),
        ("https://a.example", "/"),
        ("https://a.example/?s=", "/?s"),
        ("https://a.example/page/2/?s=x&utm_source=y", "/page/{num}/?s"),
        (
            "https://a.example/search?updated-max=2025&max-results=7&start=7",
            "/search?start&updated-max",
        ),
        ("https://a.example/film/?showComment=17&fbclid=x", "/{slug}/"),
        ("https://a.example/film/#comments", "/{slug}/"),
        ("HTTPS://A.EXAMPLE/Film-X/", "/{slug}/"),
    ],
)
def test_tokenize_url_templates(url: str, template: str) -> None:
    shape = tokenize_url(url)
    assert shape is not None
    assert shape.template == template
    assert shape.host == "a.example"


def test_tokenize_markers_and_slug_words() -> None:
    s = tokenize_url("https://a.example/category/2015/page/3/")
    assert s
    assert s.pagination
    assert s.listing
    assert s.structural
    s = tokenize_url("https://a.example/2025/11/")
    assert s
    assert s.date_archive
    assert not s.pagination
    s = tokenize_url("https://a.example/search/label/Tamil%20Movies?max-results=6")
    assert s
    assert s.search
    assert s.listing
    s = tokenize_url("https://a.example/tamil-2015-movies/2/")
    assert s
    assert s.pagination
    s = tokenize_url("https://a.example/lens-2017/")
    assert s
    assert not s.structural
    assert s.slug_text == "lens 2017"
    s = tokenize_url("https://a.example/2017/")
    assert s
    assert s.template == "/{year}/"
    assert s.date_archive


def test_encoded_and_non_latin_slugs_are_decoded() -> None:
    url = "https://a.example/%E0%B8%AA%E0%B8%A5%E0%B9%87%E0%B8%AD%E0%B8%95-%E0%B9%80%E0%B8%A7%E0%B9%87%E0%B8%9A/"
    s = tokenize_url(url)
    assert s
    assert s.template == "/{slug}/"
    assert "สล็อต" in s.slug_text
    bad = tokenize_url("https://a.example/%E0%B8/")  # truncated UTF-8 sequence
    assert bad
    assert bad.template == "/{slug}/"


@pytest.mark.parametrize(
    "url",
    ["", "not a url", "ftp://a.example/x", "mailto:a@b.example", "https://[::1/x", "//a.example/x"],
)
def test_malformed_urls_have_no_template(url: str) -> None:
    assert tokenize_url(url) is None


def test_deep_paths_are_bounded() -> None:
    s = tokenize_url("https://a.example/" + "/".join(f"seg-{i}" for i in range(40)) + "/")
    assert s
    assert len(s.tokens) == DEFAULT_WHAT_POLICY.max_segments
    assert s.tokens[-1] == "{deep}"


# ------------------------------------------------------------------------- vocabulary


def test_vocabulary_is_versioned_operator_data() -> None:
    assert VOCAB.version == "what-vocabulary/v1"
    assert VOCAB.spam.find("slots and betting") == ("betting", "slots")
    assert VOCAB.spam.find("alphabetical slotted") == ()  # whole words for Latin terms
    assert VOCAB.spam.find("ซุปเปอร์สล็อต เว็บตรง") == ("สล็อต", "เว็บตรง")  # substrings for Thai
    assert VOCAB.content.find("lens 2017 tamil movie") == ("movie", "tamil")


def test_vocabulary_is_replaceable() -> None:
    other = Vocabulary.from_mapping(
        {"vocabulary": {"version": "ops/v9"}, "content": {"terms": ["anime"]}, "spam": {}}
    )
    prof = build_site_profile(HOST, site_pages(), vocab=other)
    res = analyze_page(film_page(FILMS[0]), prof, vocab=other)
    assert res.vocabulary == prof.vocabulary == "ops/v9"
    assert "content_terms" not in res.relevance.positive


# ------------------------------------------------------------------------- site profile


def test_profile_separates_item_and_spam_slug_templates() -> None:
    roles = {t.template: t.role for t in PROFILE.templates}
    assert roles["/{slug}-{year}/"] is TemplateRole.CONTENT  # /lens-2017/
    assert roles["/{slug}/"] is TemplateRole.SPAM  # /wolf-run-slots-at-playrand/
    assert PROFILE.content_templates == ("/{slug}-{year}/",)
    assert f"{B}/about-us/" in PROFILE.boilerplate_links
    assert "site 2026" in PROFILE.title_boilerplate
    assert PROFILE.script == "LATIN"
    assert PROFILE.policy == "what/v1"


def test_clean_content_template_is_content() -> None:
    prof = build_site_profile(HOST, [film_page(t) for t in FILMS], vocab=VOCAB)
    assert prof.role("/{slug}-{year}/") is TemplateRole.CONTENT


def mixed_site() -> list[PageFacts]:
    """Film posts without a slug year share /<slug>/ with spam posts."""
    films = [replace(film_page(t), url=f"{B}/{slug(t.rsplit(' ', 1)[0])}/") for t in FILMS]
    return films + [post_page(t) for t in SPAM]


def test_shared_slug_template_is_mixed_and_a_match_is_not_a_second_signal() -> None:
    prof = build_site_profile(HOST, mixed_site(), vocab=VOCAB)
    assert prof.role("/{slug}/") is TemplateRole.MIXED
    assert prof.pattern_templates() == frozenset({"/{slug}/"})
    film = analyze_page(mixed_site()[0], prof, vocab=VOCAB)
    assert film.relevance.positive == ("year",)  # one signal: undecided
    assert film.relevance.decision is Relevance.UNKNOWN
    quiet = analyze_page(post_page(QUIET_SPAM[0]), prof, vocab=VOCAB)
    assert quiet.relevance.negative == ("template_mismatch",)
    assert quiet.relevance.decision is Relevance.SPAM
    loud = analyze_page(post_page(SPAM[0]), prof, vocab=VOCAB)
    assert set(loud.relevance.negative) == {"spam_terms", "template_mismatch"}


def test_listed_template_without_evidence_is_content() -> None:
    """Film titles without years or vocabulary: the site's listings define the template."""
    names = [f"title {w}" for w in "abcdefghijklmnopqrstuvwxyz"]
    details = [PageFacts(url=f"{B}/{slug(n)}/", title=n.title(), text_chars=900, links=NAV)
               for n in names]  # fmt: skip
    lists = [
        PageFacts(url=f"{B}/page/{i}/", title=f"Page {i}", text_chars=900,
                  links=(*NAV, *((f"{B}/{slug(n)}/", "") for n in names[i * 6 : i * 6 + 9])))
        for i in range(4)
    ]  # fmt: skip
    prof = build_site_profile(HOST, details + lists, vocab=VOCAB)
    stat = next(t for t in prof.templates if t.template == "/{slug}/")
    assert stat.content == 0
    assert stat.listed == 26
    assert stat.role is TemplateRole.CONTENT
    assert prof.pattern_templates() == frozenset()
    res = analyze_page(details[0], prof, vocab=VOCAB)
    assert res.relevance.positive == ("content_template",)
    assert res.relevance.decision is Relevance.UNKNOWN


def test_small_sites_retain_no_template() -> None:
    prof = build_site_profile(HOST, [film_page(t) for t in FILMS[:5]], vocab=VOCAB)
    assert prof.templates == ()
    assert analyze_page(film_page(FILMS[0]), prof, vocab=VOCAB).template_role is None


def test_profile_ignores_other_hosts() -> None:
    foreign = replace(film_page(FILMS[0]), url="https://other.example/x-2019/")
    assert build_site_profile(HOST, [*site_pages(), foreign], vocab=VOCAB) == PROFILE


def test_profile_is_independent_of_order_and_duplicates() -> None:
    pages = site_pages()
    rng = random.Random(7)  # noqa: S311 - deterministic test data
    for _ in range(5):
        shuffled = pages + rng.sample(pages, 10)
        rng.shuffle(shuffled)
        assert build_site_profile(HOST, shuffled, vocab=VOCAB) == PROFILE


@settings(max_examples=60, deadline=None)
@given(st.permutations(site_pages()), st.lists(st.integers(0, 80), max_size=20))
def test_profile_and_results_are_order_independent(perm: list[PageFacts], dups: list[int]) -> None:
    pages = perm + [perm[i % len(perm)] for i in dups]
    prof = build_site_profile(HOST, pages, vocab=VOCAB)
    assert prof == PROFILE
    page = film_page(FILMS[5])
    shuffled = replace(page, links=tuple(reversed(page.links)) + page.links)
    assert analyze_page(shuffled, prof, vocab=VOCAB) == analyze(page)


def test_profile_state_is_bounded() -> None:
    policy = replace(DEFAULT_WHAT_POLICY, max_profile_pages=300, max_links_per_page=50,
                     max_profile_urls=2_000)  # fmt: skip
    rng = random.Random(1)  # noqa: S311 - deterministic test data
    pages = []
    for i in range(2_000):
        depth = rng.randint(1, 4)
        path = "/".join(f"s{rng.randint(0, 9)}-x" for _ in range(depth))
        links = tuple((f"{B}/l-{rng.randint(0, 10**6)}/{i}/", "") for _ in range(400))
        pages.append(PageFacts(url=f"{B}/{path}/p-{i}/", title=f"t {i}", links=links))
    prof = build_site_profile(HOST, pages, policy=policy, vocab=VOCAB)
    assert prof.pages == 300
    assert prof.urls <= 2_000
    assert len(prof.templates) <= policy.max_templates
    assert len(prof.boilerplate_links) <= policy.max_boilerplate_links
    assert len(prof.title_boilerplate) <= policy.max_boilerplate_segments


# ------------------------------------------------------------------------- page kind


def test_film_post_is_a_relevant_detail() -> None:
    res = analyze(film_page(FILMS[4]))
    assert res.kind.kind is PageKind.DETAIL
    assert res.relevance.decision is Relevance.RELEVANT
    assert set(res.relevance.positive) == {"year", "content_template"}
    assert res.template == "/{slug}-{year}/"
    assert res.template_role is TemplateRole.CONTENT


def test_spam_post_with_vocabulary_is_spam() -> None:
    res = analyze(post_page(SPAM[0]))
    assert res.kind.kind is PageKind.DETAIL
    assert res.relevance.decision is Relevance.SPAM
    assert set(res.relevance.negative) == {"spam_terms", "spam_template"}
    assert "casino" in res.relevance.terms


def test_spam_post_without_vocabulary_is_spam_by_its_template() -> None:
    res = analyze(post_page(QUIET_SPAM[0]))
    assert res.relevance.decision is Relevance.SPAM
    assert res.relevance.negative == ("spam_template",)


def test_non_latin_title_on_a_latin_site_is_negative_evidence() -> None:
    page = PageFacts(url=f"{B}/x/y-z", title="ซุปเปอร์ เว็บไซต์ - Site 2026", text_chars=4_000,
                     links=NAV)  # fmt: skip
    res = analyze(page)
    assert "script_mismatch" in res.relevance.negative
    assert res.relevance.decision is Relevance.SPAM


def test_one_keyword_never_decides_alone() -> None:
    page = PageFacts(url=f"{B}/x/films", title="Movie", text_chars=4_000, links=NAV)
    res = analyze(page)
    assert res.relevance.positive == ("content_terms",)
    assert res.relevance.decision is Relevance.UNKNOWN
    film_named_bet = film_page("bet 2024")  # a film whose title is a spam word
    res = analyze(film_named_bet)
    assert res.relevance.decision is not Relevance.SPAM


def test_listing_page_is_a_relevant_hub() -> None:
    res = analyze(listing(5, FILMS[10:30]))
    assert res.kind.kind is PageKind.HUB
    assert "pagination" in res.kind.reasons
    assert "listing_path" in res.kind.reasons
    assert res.relevance.decision is Relevance.RELEVANT
    assert len(res.content_links) == 20
    assert res.content_links == tuple(sorted(res.content_links))


def test_hub_at_a_generic_slug_url_is_found_by_its_links() -> None:
    page = PageFacts(url=f"{B}/tamil-hd-movies/", title="Tamil HD Movies - Site 2026",
                     text_chars=2_000,
                     links=(*NAV, *((film_url(t), t.title()) for t in FILMS)))  # fmt: skip
    res = analyze(page)
    assert res.kind.kind is PageKind.HUB
    assert res.kind.reasons[0] == "many_page_links"


def test_spam_listing_is_a_spam_hub() -> None:
    res = analyze(blog_listing())
    assert res.kind.kind is PageKind.HUB
    assert res.relevance.decision is Relevance.SPAM
    assert "spam_links" in res.relevance.negative


def test_detail_with_related_links_stays_a_detail() -> None:
    related = tuple((film_url(t), t.title()) for t in FILMS[10:20])
    res = analyze(film_page(FILMS[2], related))
    assert res.kind.own_links >= DEFAULT_WHAT_POLICY.hub_min_own_links
    assert res.kind.kind is PageKind.DETAIL
    assert res.kind.reasons == ("item_title", "related_links")


def test_beyond_the_end_pagination_is_a_hub_with_few_links() -> None:
    res = analyze(listing(99, []))
    assert res.kind.kind is PageKind.HUB
    assert res.kind.reasons[-1] == "few_page_links"


def test_empty_and_link_free_pages_are_other() -> None:
    for page in (
        PageFacts(url=f"{B}/wp-includes/blocks.php"),
        PageFacts(url=f"{B}/wp-login.php?redirect_to=x", title="Bot Verification", text_chars=37),
    ):
        res = analyze(page)
        assert res.kind.kind is PageKind.OTHER
        assert res.kind.reasons == ("empty_page",)
        assert res.kind.own_links == 0
        assert res.content_links == ()


def test_utility_pages_are_other_but_long_slugs_are_not() -> None:
    contact = PageFacts(url=f"{B}/contact.php", title="Site - Tamil HD Movies", text_chars=600,
                        links=NAV)  # fmt: skip
    assert analyze(contact).kind.reasons == ("utility_page",)
    post = post_page("how ufabet mobile login became essential")
    assert analyze(post).kind.kind is PageKind.DETAIL


def test_single_page_site_is_a_detail() -> None:
    page = PageFacts(url="https://55club.site.example/", title="55 Club Login", text_chars=1_200)
    prof = build_site_profile("55club.site.example", [page], vocab=VOCAB)
    res = analyze_page(page, prof, vocab=VOCAB)
    assert res.kind.kind is PageKind.DETAIL
    assert res.kind.reasons == ("single_page_site",)


def test_extremely_large_link_sets_are_bounded() -> None:
    links = tuple((f"{B}/film-{i}-2020/", f"Film {i} (2020)") for i in range(50_000))
    page = PageFacts(url=f"{B}/", title="Home", text_chars=900, links=links + links)
    res = analyze(page)
    assert res.kind.own_links == DEFAULT_WHAT_POLICY.max_links_per_page
    assert len(res.content_links) <= DEFAULT_WHAT_POLICY.max_result_links
    assert analyze(replace(page, links=tuple(reversed(page.links)))) == res


def test_malformed_page_url_and_links() -> None:
    res = analyze(PageFacts(url="https://[::1/x", title="x"))
    assert res.kind.reasons == ("malformed_url",)
    assert res.template is None
    page = replace(film_page(FILMS[1]), links=(*NAV, ("https://[::1/broken", "x"), ("", "")))
    assert analyze(page).kind.kind is PageKind.DETAIL


def test_same_input_same_output() -> None:
    page = listing(2, FILMS[:12])
    assert analyze(page) == analyze(page)
    assert build_site_profile(HOST, site_pages(), vocab=VOCAB) == PROFILE


# ------------------------------------------------------------------------- adapters


def test_adapters_read_p5_facts() -> None:
    html = (
        b"<html><head><title>Lens (2017) - Site 2026</title>"
        b'<meta property="og:type" content="article"></head><body><main>'
        b'<a href="/about-us/">About Us</a> <a href="/film-a-1990/">Film A</a>'
        b'<a href="https://other.example/x">x</a><p>'
        + b"text " * 100
        + b"</p></main></body></html>"
    )
    ex = extract(html, page=UrlRef.of(f"{B}/lens-2017/"), content_type="text/html")
    facts = facts_from_extract(ex)
    assert facts.url == f"{B}/lens-2017/"
    assert facts.title == "Lens (2017) - Site 2026"
    assert facts.og_type == "article"
    assert facts.text_chars > 0
    assert (f"{B}/film-a-1990/", "Film A") in facts.links
    row = {
        "url": ex.page.url,
        "metadata": ex.metadata.model_dump(mode="json"),
        "stats": ex.stats.model_dump(mode="json"),
        "media": [],
        "links": [[lk.target.url, lk.relation, lk.anchor_text] for lk in ex.links],
    }
    assert facts_from_export(row) == facts
    assert analyze(facts).relevance.decision is Relevance.RELEVANT


def test_what_stays_pure() -> None:
    """B.5 #1: WHAT reads facts only; no storage, frontier, worker, discovery or Redis."""
    forbidden = ("crawler2.frontier", "crawler2.crawlers", "crawler2.discovery",
                 "crawler2.storage", "crawler2.filtering.store", "redis", "cassandra")  # fmt: skip
    path = Path(__file__).resolve().parents[3] / "crawler2" / "intelligence" / "what.py"
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert not name.startswith(forbidden), name
