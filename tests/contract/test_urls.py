import pytest
from antipiracy_contracts.urls import CanonicalUrl, InvalidUrlError, canonicalize_url
from hypothesis import given
from hypothesis import strategies as st


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("HTTP://Example.COM", "http://example.com/"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("https://example.com./a", "https://example.com/a"),
        ("https://example.com/a/./b/../c", "https://example.com/a/c"),
        ("https://example.com/a/b/..", "https://example.com/a/"),
        ("https://example.com/%7euser/%2fx", "https://example.com/~user/%2Fx"),
        ("https://example.com/a b/ü", "https://example.com/a%20b/%C3%BC"),
        ("https://example.com/p?b=2&a=1#frag", "https://example.com/p?b=2&a=1"),
        ("https://example.com/p?", "https://example.com/p"),
        ("https://example.com/100%", "https://example.com/100%25"),
        ("https://bücher.example/", "https://xn--bcher-kva.example/"),
        ("http://[2001:DB8::1]:8080/x", "http://[2001:db8::1]:8080/x"),
        ("  https://example.com/  ", "https://example.com/"),
    ],
)
def test_canonical_form(raw: str, canonical: str) -> None:
    assert canonicalize_url(raw) == canonical


def test_query_order_and_tracking_parameters_are_identity() -> None:
    # Semantic dedup is policy, not identity (ADR-008).
    assert canonicalize_url("https://e.x/?a=1&b=2") != canonicalize_url("https://e.x/?b=2&a=1")
    assert canonicalize_url("https://e.x/?utm_source=x") != canonicalize_url("https://e.x/")


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://example.com/",
        "javascript:alert(1)",
        "/relative/path",
        "https://user:pw@example.com/",
        "https:///nohost",
        "https://example.com:99999/",
        "https://a..b/",
        "https://example.com/" + "a" * 9000,
    ],
)
def test_rejected(raw: str) -> None:
    with pytest.raises(InvalidUrlError):
        canonicalize_url(raw)


def test_canonical_url_type_rejects_non_canonical_input() -> None:
    assert CanonicalUrl("https://example.com/") == "https://example.com/"
    with pytest.raises(InvalidUrlError, match="not canonical"):
        CanonicalUrl("https://EXAMPLE.com/")


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://example.com:8443/", "example.com"),
        ("http://[::1]:8080/", "[::1]"),
        ("http://10.0.0.1/", "10.0.0.1"),
    ],
)
def test_host(url: str, host: str) -> None:
    assert canonicalize_url(url).host == host


_segment = st.text(alphabet=st.characters(codec="utf-8", exclude_categories=["Cs", "Cc"]))


@given(
    scheme=st.sampled_from(["http", "HTTP", "https"]),
    host=st.from_regex(r"[A-Za-z0-9]{1,10}(\.[A-Za-z0-9]{1,10}){0,2}\.?", fullmatch=True),
    port=st.one_of(st.none(), st.integers(1, 65535)),
    segments=st.lists(st.one_of(_segment, st.sampled_from([".", "..", "%2e", "%41"])), max_size=5),
    query=st.one_of(st.none(), _segment),
)
def test_canonicalization_is_idempotent(
    scheme: str, host: str, port: int | None, segments: list[str], query: str | None
) -> None:
    path = "/" + "/".join(s.replace("?", "").replace("#", "") for s in segments)
    raw = f"{scheme}://{host}{'' if port is None else f':{port}'}{path}"
    if query is not None:
        raw += "?" + query.replace("#", "")
    canonical = canonicalize_url(raw)
    assert canonicalize_url(canonical) == canonical
    assert CanonicalUrl(canonical) == canonical
