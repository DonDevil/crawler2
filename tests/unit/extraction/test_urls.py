"""Pure URL functions (P5 design §4)."""

from __future__ import annotations

import pytest
from antipiracy_contracts.models.media import MediaKind
from antipiracy_contracts.urls import canonicalize_url
from hypothesis import given
from hypothesis import strategies as st

from crawler2.extraction.urls import (
    clean_reference,
    media_kind,
    path_extension,
    resolve,
    scheme_of,
    to_url_ref,
)

BASE = "https://site.example/dir/page.html?x=1"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  /a  ", "/a"),
        ("\n/a\tb\r", "/ab"),
        ("\x00\x1f/a", "/a"),
        ("", None),
        ("   ", None),
        (None, None),
        ("/" + "a" * 9000, None),
    ],
)
def test_clean_reference(raw: str | None, expected: str | None) -> None:
    assert clean_reference(raw) == expected


@pytest.mark.parametrize(
    ("ref", "scheme"),
    [
        ("HTTP://x", "http"),
        ("mailto:a@b", "mailto"),
        ("javascript:void(0)", "javascript"),
        ("/path:with:colons", None),
        ("rel/a", None),
        ("//host/x", None),
    ],
)
def test_scheme_of(ref: str, scheme: str | None) -> None:
    assert scheme_of(ref) == scheme


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("other.html", "https://site.example/dir/other.html"),
        ("../up", "https://site.example/up"),
        ("/abs?q=2", "https://site.example/abs?q=2"),
        ("//cdn.example/x", "https://cdn.example/x"),
        ("http://other.example/a#frag", "http://other.example/a"),
        ("?y=2", "https://site.example/dir/page.html?y=2"),
        ("#top", None),
        ("mailto:a@b.c", None),
        ("javascript:alert(1)", None),
        ("data:text/html,<b>x</b>", None),
        ("tel:+123", None),
        ("ftp://files.example/x", None),
        ("blob:https://site.example/1", None),
        ("http://[::1", None),
    ],
)
def test_resolve(ref: str, expected: str | None) -> None:
    assert resolve(BASE, ref) == expected


def test_to_url_ref_is_p1_canonical_and_nothing_stronger() -> None:
    ref = to_url_ref("HTTPS://WWW.Site.Example:443/a/./b/../c?utm_source=x&b=2&a=1")
    assert ref is not None
    # www kept, query order kept, tracking parameter kept (P1 canonical v1).
    assert ref.url == "https://www.site.example/a/c?utm_source=x&b=2&a=1"
    assert ref.url_id == to_url_ref(ref.url).url_id  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "bad", ["https://user:pw@site.example/", "https:///nohost", "http://a..b/"]
)
def test_to_url_ref_rejects_non_identities(bad: str) -> None:
    assert to_url_ref(bad) is None


@pytest.mark.parametrize(
    ("url", "declared", "kind"),
    [
        ("https://c.example/v.m3u8?token=1", None, MediaKind.HLS_MANIFEST),
        ("https://c.example/v.MPD", None, MediaKind.DASH_MANIFEST),
        ("https://c.example/v.mp4?x=.html", None, MediaKind.VIDEO_FILE),
        ("https://c.example/seg/001.ts", None, MediaKind.VIDEO_FILE),
        ("https://c.example/a.mp3", None, MediaKind.AUDIO_FILE),
        ("https://c.example/play", "application/x-mpegURL", MediaKind.HLS_MANIFEST),
        ("https://c.example/play", "video/mp4; codecs=avc1", MediaKind.VIDEO_FILE),
        ("https://c.example/play", "audio/ogg", MediaKind.AUDIO_FILE),
        ("https://c.example/page.html", None, None),
        ("https://c.example/x.mp4.html", None, None),
        ("https://c.example/.mp4", None, None),
    ],
)
def test_media_kind(url: str, declared: str | None, kind: MediaKind | None) -> None:
    assert media_kind(url, declared) is kind


def test_path_extension_ignores_query_and_directories() -> None:
    assert path_extension("https://a.example/dir.v1/file?name=x.mp4") == ""
    assert path_extension("https://a.example/f.JS?v=1") == ".js"


@given(st.text(max_size=200))
def test_resolve_never_raises_and_only_yields_web_urls(ref: str) -> None:
    result = resolve(BASE, ref)
    assert result is None or (result.startswith(("http://", "https://")) and "#" not in result)


@given(st.text(max_size=200))
def test_canonical_refs_are_fixed_points(ref: str) -> None:
    resolved = resolve(BASE, ref)
    url_ref = to_url_ref(resolved) if resolved else None
    if url_ref is not None:
        assert canonicalize_url(url_ref.url) == url_ref.url
