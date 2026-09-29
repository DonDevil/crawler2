"""Supported script-literal syntax (P5 design §7), exactly."""

from __future__ import annotations

from crawler2.extraction.scripts import script_literals, text_urls


def values(text: str) -> list[str]:
    return [lit.value for lit in script_literals(text)]


def test_absolute_literals_including_json_escapes() -> None:
    js = r'''a="https://x.example/p?q=1"; b='http:\/\/y.example\/v.m3u8'; c="https://z.example/"'''
    assert values(js) == [
        "https://x.example/p?q=1",
        "http://y.example/v.m3u8",
        "https://z.example/",
    ]


def test_protocol_relative_and_relative_media_literals() -> None:
    lits = list(
        script_literals('p("//cdn.example/a/b.js"); s={file:"/v/movie.mp4?t=2"}; x="rel.m3u8"')
    )
    assert [(lit.value, lit.media_only) for lit in lits] == [
        ("//cdn.example/a/b.js", False),
        ("/v/movie.mp4?t=2", True),
        ("rel.m3u8", True),
    ]


def test_player_keys_are_detected() -> None:
    lits = list(
        script_literals(
            'jwplayer().setup({file: "https://c.example/m.m3u8"}); var u = x + "https://d.example/a"'
        )
    )
    assert [lit.player_key for lit in lits] == [True, False]


def test_trailing_punctuation_is_trimmed() -> None:
    assert values('go("https://x.example/a"),(https://x.example/b).') == [
        "https://x.example/a",
        "https://x.example/b",
    ]


def test_unsupported_syntax_yields_nothing() -> None:
    js = 'var u = "https:" + "/" + "/x.example/" + id; var t = `${base}/v.mp4`; atob("aHR0cHM6")'
    assert values(js) == []


def test_text_urls() -> None:
    assert list(text_urls("see https://a.example/x, or http://b.example.")) == [
        "https://a.example/x",
        "http://b.example",
    ]


def test_json_ld_vocabulary_identifiers_are_not_links() -> None:
    ld = '{"@context": "https://schema.org", "@type": ["https://schema.org/VideoObject"], "url": "https://s.example/p"}'
    assert values(ld) == ["https://s.example/p"]
