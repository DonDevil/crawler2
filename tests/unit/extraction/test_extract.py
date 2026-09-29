"""Extraction over one parsed tree (P5 design §2-§8, §16)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.models.media import MediaReference
from antipiracy_contracts.models.web import DiscoveredLink, LinkRelation, UrlRef
from selectolax.lexbor import LexborHTMLParser, LexborNode

from crawler2.extraction import extract as extract_module
from crawler2.extraction.extract import extract
from crawler2.extraction.model import Limits, PageExtract
from crawler2.extraction.parse import encoding_of, is_html

PAGE = UrlRef.of("https://site.example/dir/page")


def run(html: str | bytes, **kwargs: object) -> PageExtract:
    body = html.encode() if isinstance(html, str) else html
    return extract(body, page=PAGE, content_type="text/html", **kwargs)  # type: ignore[arg-type]


def links(e: PageExtract) -> list[tuple[str, str]]:
    return [(link.relation.value, link.target.url) for link in e.links]


def media(e: PageExtract) -> list[tuple[str, str, str]]:
    return [(m.kind.value, m.method.value, m.locator.url) for m in e.media]


# --- links -------------------------------------------------------------------


def test_link_sources_relations_and_resolution() -> None:
    e = run(
        """<html><head><link rel="Canonical" href="/dir/page"><meta http-equiv="refresh"
        content="5; URL='/next'"></head><body>
        <a href="a.html">A</a><a href="//other.example/x">B</a><area href="/map">
        <iframe src="https://player.example/embed/1"></iframe>
        </body></html>"""
    )
    assert links(e) == [
        ("anchor", "https://other.example/x"),
        ("iframe", "https://player.example/embed/1"),
        ("anchor", "https://site.example/dir/a.html"),
        ("canonical", "https://site.example/dir/page"),
        ("anchor", "https://site.example/map"),
        ("meta_refresh", "https://site.example/next"),
    ]
    assert e.metadata.canonical == "https://site.example/dir/page"


def test_frames_inside_a_frameset_are_iframe_links() -> None:
    e = run('<html><frameset><frame src="f.html"><frame src="/g"></frameset></html>')
    assert links(e) == [
        ("iframe", "https://site.example/dir/f.html"),
        ("iframe", "https://site.example/g"),
    ]


def test_unsupported_schemes_fragments_and_malformed_are_dropped_and_counted() -> None:
    e = run(
        '<a href="mailto:x@y.z">m</a><a href="javascript:void(0)">j</a><a href="data:,x">d</a>'
        '<a href="#top">t</a><a href="">e</a><a href="http://[::1">bad</a><a href="/ok">ok</a>'
        '<a href="page#frag">self</a>'
    )
    assert links(e) == [("anchor", "https://site.example/ok")]
    assert e.stats.dropped == {"empty": 1, "invalid": 1, "scheme": 3, "self": 2}


def test_duplicates_collapse_to_first_occurrence_and_output_is_sorted() -> None:
    e = run(
        '<a href="/b">first B</a><a href="/a" rel="nofollow ugc">A</a>'
        '<a href="/b#x">second B</a><a href="https://SITE.example:443/a">A again</a>'
    )
    assert [(link.target.url, link.anchor_text, link.nofollow) for link in e.links] == [
        ("https://site.example/a", "A", True),
        ("https://site.example/b", "first B", False),
    ]


def test_anchor_text_is_collapsed_and_bounded() -> None:
    e = run(f'<a href="/x">  Episode&nbsp;2 <b>HD</b>\n {"y" * 400}</a>')
    text = e.links[0].anchor_text
    assert text is not None
    assert text.startswith("Episode 2 HD y")
    assert len(text) == 300


def test_base_href_and_query_are_preserved() -> None:
    e = run(
        '<head><base href="https://cdn.example/root/"></head><a href="x?b=2&a=1&utm_source=z">x</a>'
    )
    assert links(e) == [("anchor", "https://cdn.example/root/x?b=2&a=1&utm_source=z")]


def test_script_and_text_literals_do_not_duplicate_markup_links() -> None:
    e = run(
        '<a href="https://x.example/a">https://x.example/a</a>'
        '<script>var u="https://x.example/a", v="https://x.example/b", s="https://x.example/app.js"</script>'
        "<p>mirror at https://m.example/c.</p>"
    )
    assert links(e) == [
        ("other", "https://m.example/c"),
        ("anchor", "https://x.example/a"),
        ("script_literal", "https://x.example/b"),
    ]
    assert e.stats.dropped["asset"] == 1


def test_link_policy_hook_is_applied() -> None:
    class DenyOther:
        def allow(self, source: UrlRef, target: UrlRef, relation: LinkRelation) -> bool:
            return target.domain_id == source.domain_id

    e = run('<a href="/in">i</a><a href="https://out.example/">o</a>', policy=DenyOther())
    assert links(e) == [("anchor", "https://site.example/in")]
    assert e.stats.dropped == {"policy": 1}


def test_link_cap_keeps_the_first_links_in_document_order() -> None:
    html = "".join(f'<a href="/p/{i:03d}">x</a>' for i in range(30))
    e = run(html, limits=Limits(max_links=10))
    assert [link.target.url for link in e.links] == [
        f"https://site.example/p/{i:03d}" for i in range(10)
    ]
    assert "links" in e.stats.truncated


# --- media -------------------------------------------------------------------


def test_media_sources_methods_and_kinds() -> None:
    e = run(
        """<head><meta property="og:video:secure_url" content="https://c.example/og.mp4">
        <meta property="og:video" content="https://embed.example/player/9"></head><body>
        <video src="/v/stream.m3u8" poster="/p.jpg"><source src="/v/a" type="video/mp4">
        <source src="/v/b.webm"></video><audio data-src="/a/song"></audio>
        <embed src="/f.mpd"><object data="/x.swf"></object><a href="/dl/movie.mkv">dl</a>
        <script>jwplayer("p").setup({file:"https:\\/\\/h.example\\/m.m3u8"}); var ad="https://ads.example/v.mp4";</script>
        <script type="application/ld+json">{"contentUrl": "https://j.example/f.mp4"}</script>
        </body>"""
    )
    assert media(e) == [
        ("video_file", "script_literal", "https://ads.example/v.mp4"),
        ("video_file", "meta_tag", "https://c.example/og.mp4"),
        ("hls_manifest", "player_config", "https://h.example/m.m3u8"),
        ("video_file", "player_config", "https://j.example/f.mp4"),
        ("unknown", "video_element", "https://site.example/a/song"),
        ("video_file", "link", "https://site.example/dl/movie.mkv"),
        ("dash_manifest", "video_element", "https://site.example/f.mpd"),
        ("video_file", "source_element", "https://site.example/v/a"),
        ("video_file", "source_element", "https://site.example/v/b.webm"),
        ("hls_manifest", "video_element", "https://site.example/v/stream.m3u8"),
    ]
    assert ("other", "https://embed.example/player/9") in links(e)
    assert all(m.locator.url != "https://site.example/x.swf" for m in e.media)
    assert next(m for m in e.media if m.locator.url.endswith("/v/a")).declared_type == "video/mp4"


def test_media_urls_are_not_page_links_and_strongest_method_wins() -> None:
    e = run('<a href="/v.mp4">link</a><script>x="/v.mp4"</script><video src="/v.mp4"></video>')
    assert links(e) == []
    assert media(e) == [("video_file", "video_element", "https://site.example/v.mp4")]


def test_media_references_are_valid_p1_values() -> None:
    e = run('<video src="/v.m3u8"></video><a href="/x">x</a>')
    for m in e.media:
        assert MediaReference.model_validate(m.model_dump()) == m
    for link in e.links:
        assert DiscoveredLink.model_validate(link.model_dump()) == link


# --- metadata ----------------------------------------------------------------


def test_metadata_fields() -> None:
    e = run(
        """<html lang="en-GB"><head><title>  Big
        Buck </title><meta property="og:title" content="BBB"><meta property="og:type"
        content="video.movie">
        <meta property="article:published_time" content="2026-09-01T10:00:00+02:00">
        <meta itemprop="dateModified" content="2026-09-02">
        <meta name="robots" content="noindex, nofollow"><meta name="description" content="x">
        </head><body><svg><title>icon</title></svg></body></html>"""
    )
    m = e.metadata
    assert (m.title, m.lang, m.og_title, m.og_type, m.robots) == (
        "Big Buck",
        "en-GB",
        "BBB",
        "video.movie",
        "noindex, nofollow",
    )
    assert m.published_at == datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    assert m.modified_at == datetime(2026, 9, 2, tzinfo=UTC)


def test_unparseable_dates_are_dropped() -> None:
    e = run('<meta property="article:published_time" content="last tuesday">')
    assert e.metadata.published_at is None


# --- decoding and robustness -------------------------------------------------


def test_encoding_detection() -> None:
    assert encoding_of(b"<html>", "text/html; charset=ISO-8859-1") == "iso8859-1"
    assert encoding_of(b'<meta charset="windows-1251">', None) == "cp1251"
    assert encoding_of(b"\xef\xbb\xbf<html>", "text/html; charset=latin-1") == "utf-8-sig"
    assert encoding_of(b"<html>", "text/html; charset=bogus") == "utf-8"


def test_is_html() -> None:
    assert is_html("text/html; charset=utf-8", b"")
    assert is_html("application/xhtml+xml", b"")
    assert not is_html("application/json", b"<html>")
    assert is_html(None, b"  <!-- c --><!DOCTYPE html><html>")
    assert not is_html(None, b'{"a": 1}')


def test_charset_decoding_affects_text_not_raw_hash() -> None:
    body = "<p>café</p>".encode("latin-1")
    latin = extract(body, page=PAGE, content_type="text/html; charset=latin-1")
    utf8 = extract(body, page=PAGE, content_type="text/html; charset=utf-8")
    assert latin.hashes.raw == utf8.hashes.raw == ContentDigest.of_bytes(body)
    assert latin.hashes.visible_text != utf8.hashes.visible_text


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"   ",
        b"not html at all",
        b"<html><body><div><p>unclosed",
        b"\xff\xfe\x00garbage\x80\x81",
        b"<a href='/x'>" * 5000,
        b"<div " + b"a" * 200_000 + b"='1'>x</div>",
        b"<script>" + b"x" * 3_000_000 + b"</script>",
    ],
    ids=[
        "empty",
        "blank",
        "text",
        "unclosed",
        "invalid-bytes",
        "many-anchors",
        "huge-attr",
        "huge-script",
    ],
)
def test_malformed_input_never_raises(body: bytes) -> None:
    e = extract(body, page=PAGE, content_type="text/html")
    assert e.hashes.raw == ContentDigest.of_bytes(body)


def test_deep_nesting_is_walked_iteratively() -> None:
    e = run("<div>" * 20_000 + "deep" + "</div>" * 20_000)
    assert e.stats.elements > 0  # no RecursionError


def test_limits_are_reported() -> None:
    big = b"<p>x</p>" * 1000
    e = extract(
        big, page=PAGE, content_type="text/html", limits=Limits(max_parse_bytes=100, max_elements=5)
    )
    assert set(e.stats.truncated) >= {"body", "elements"}
    scripts = run("<script>" + "a" * 50 + "</script>", limits=Limits(max_script_bytes=10))
    assert "scripts" in scripts.stats.truncated


def test_unicode_urls_and_text() -> None:
    e = run('<a href="/café?q=ü">x</a><a href="https://bücher.example/">y</a><p>日本</p>')
    assert links(e) == [
        ("anchor", "https://site.example/caf%C3%A9?q=%C3%BC"),
        ("anchor", "https://xn--bcher-kva.example/"),
    ]


def test_extraction_is_deterministic() -> None:
    html = '<a href="/b">b</a><a href="/a">a</a><video src="/z.mp4"></video><video src="/y.mp4">'
    assert run(html) == run(html)
    assert run(html).model_dump_json() == run(html).model_dump_json()


# --- single parse ------------------------------------------------------------


def test_one_parse_and_one_tree_feed_every_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observable single-parse invariant: exactly one parser construction per page, and every
    element handler and the text/shape walk see nodes of that one tree."""
    trees: list[LexborHTMLParser] = []

    def counting_parser(text: str) -> LexborHTMLParser:
        tree = LexborHTMLParser(text)
        trees.append(tree)
        return tree

    seen_parsers: set[int] = set()
    original = extract_module._Collector.element

    def spy(self: extract_module._Collector, tag: str, node: LexborNode) -> None:
        seen_parsers.add(id(node.parser))
        original(self, tag, node)

    monkeypatch.setattr(extract_module._Collector, "element", spy)
    e = run(
        '<title>t</title><a href="/l">l</a><video src="/v.mp4"></video><meta property="og:type" '
        'content="video.movie"><script>x="https://s.example/p"</script><p>text</p>',
        parser=counting_parser,
    )
    assert len(trees) == 1
    assert seen_parsers == {id(trees[0])}
    assert e.links
    assert e.media
    assert e.metadata.title
    assert e.metadata.og_type


def test_no_other_module_constructs_a_parser() -> None:
    import pathlib

    root = pathlib.Path(extract_module.__file__).parent
    users = sorted(p.name for p in root.glob("*.py") if "LexborHTMLParser" in p.read_text())
    assert users == ["parse.py"]
