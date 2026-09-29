"""The P5 hash set: canonical forms, stability and sensitivity (P5 design §9)."""

from __future__ import annotations

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.models.web import UrlRef

from crawler2.extraction import hashing
from crawler2.extraction.extract import extract
from crawler2.extraction.model import NormalizationRules, PageExtract

PAGE = UrlRef.of("https://site.example/watch/bbb")


def page(
    *,
    main: str = "<h1>Big Buck Bunny</h1><p>Episode 1 of the series.</p>",
    player: str = '<video src="/v/bbb.m3u8"></video>',
    ad: str = '<div class="ad">Buy now!</div>',
    nav: str = '<a href="/">Home</a> <a href="/new">New</a>',
    footer: str = "&copy; 2026",
    script: str = "var t=1;",
    extra_head: str = "",
) -> bytes:
    return (
        f"<!doctype html><html><head><title>BBB</title>{extra_head}<script>{script}</script>"
        f"<style>.a{{}}</style></head><body><nav>{nav}</nav><main>{main}{player}</main>"
        f"<aside>{ad}</aside><footer>{footer}</footer></body></html>"
    ).encode()


def run(body: bytes, rules: NormalizationRules | None = None) -> PageExtract:
    kwargs = {"rules": rules} if rules else {}
    return extract(body, page=PAGE, content_type="text/html", **kwargs)  # type: ignore[arg-type]


BASE = run(page())


def test_same_page_twice_gives_identical_hashes() -> None:
    assert run(page()).hashes == BASE.hashes
    assert run(page()).revision_id == BASE.revision_id


def test_raw_hash_is_the_exact_body_digest() -> None:
    assert BASE.hashes.raw == ContentDigest.of_bytes(page())


def test_ad_boilerplate_script_and_markup_noise_keep_the_normalized_hash() -> None:
    noisy = [
        page(ad='<div class="ad">Casino bonus 500%</div>'),  # aside rotation
        page(nav='<a href="/">Home</a> <a href="/trending">Trending today</a>'),  # nav rotation
        page(footer="&copy; 2027 rendered at 12:00:01"),  # footer timestamp
        page(script="var t=987654321; window.adSlot='x9';"),  # volatile inline script
        page(extra_head='<meta name="csrf" content="a8f3">'),  # volatile head markup
        page(
            main="<h1>Big Buck Bunny</h1><p hidden>session 1f2e</p><p>Episode 1 of the series.</p>"
        ),
        page(main="<h1>Big  Buck\nBunny</h1><p>Episode 1 of the   series.</p>"),  # whitespace
        page(main="<h1>BIG BUCK BUNNY</h1><p>Episode 1 of the series.</p>"),  # case only
        page(main="<h1>Big Buck Bun​ny</h1><p>Episode 1 of the series.</p>"),  # zero-width
    ]
    for body in noisy:
        e = run(body)
        assert e.hashes.raw != BASE.hashes.raw
        assert e.hashes.normalized == BASE.hashes.normalized, body
        assert e.revision_id == BASE.revision_id


def test_meaningful_text_change_changes_the_normalized_hash() -> None:
    e = run(page(main="<h1>Big Buck Bunny</h1><p>Episode 2 of the series.</p>"))
    assert e.hashes.normalized != BASE.hashes.normalized
    assert e.hashes.visible_text != BASE.hashes.visible_text
    assert e.revision_id != BASE.revision_id


def test_link_addition_and_removal_change_the_link_set_hash() -> None:
    added = run(page(nav='<a href="/">Home</a> <a href="/new">New</a> <a href="/x">X</a>'))
    removed = run(page(nav='<a href="/">Home</a>'))
    retargeted = run(page(nav='<a href="/">Home</a> <a href="/new2">New</a>'))
    for e in (added, removed, retargeted):
        assert e.hashes.link_set != BASE.hashes.link_set
    # Links are not part of the normalized form: same visible text, same revision.
    assert retargeted.hashes.normalized == BASE.hashes.normalized


def test_media_addition_and_removal_change_media_set_and_normalized_hashes() -> None:
    added = run(page(player='<video src="/v/bbb.m3u8"></video><video src="/v/ep2.mp4"></video>'))
    removed = run(page(player=""))
    swapped = run(page(player='<video src="/v/other.m3u8"></video>'))
    for e in (added, removed, swapped):
        assert e.hashes.media_set != BASE.hashes.media_set
        assert e.hashes.normalized != BASE.hashes.normalized  # a swapped video is meaningful


def test_dom_structure_change_changes_only_the_structural_hash() -> None:
    e = run(
        page(
            main="<div><h1>Big Buck Bunny</h1></div>"
            "<section><p>Episode 1 of the series.</p></section>"
        )
    )
    assert e.hashes.structural != BASE.hashes.structural
    assert e.hashes.normalized == BASE.hashes.normalized
    assert e.hashes.link_set == BASE.hashes.link_set


def test_structural_hash_ignores_text_and_attributes() -> None:
    e = run(page(main='<h1 class="x" id="y">Other</h1><p data-k="1">Different text.</p>'))
    assert e.hashes.structural == BASE.hashes.structural


def test_visible_text_rules() -> None:
    nbsp, acute, fullwidth_a, shy, zwsp = "\u00a0", "\u0301", "\uff21", "\u00ad", "\u200b"
    assert hashing.visible_text_form(["  A" + nbsp + " b\n", "Ce" + acute + " "]) == "A b C\u00e9"
    assert (
        hashing.normalized_text_form([fullwidth_a + shy + " B" + zwsp, " \u00c9"]) == "a b \u00e9"
    )
    hidden = run(
        b"<p>shown</p><p style='display: none'>x</p><p aria-hidden='true'>y</p>"
        b"<noscript>z</noscript><template>t</template><p>tail</p>"
    )
    plain = run(b"<p>shown</p><p>tail</p>")
    assert hidden.hashes.visible_text == plain.hashes.visible_text


def test_boilerplate_is_visible_text_but_not_normalized_content() -> None:
    e = run(page(footer="Changed footer"))
    assert e.hashes.visible_text != BASE.hashes.visible_text
    assert e.hashes.normalized == BASE.hashes.normalized


def test_injected_rules_extend_normalization_and_change_the_scheme() -> None:
    rules = NormalizationRules(scheme="html-normalized-test/v1", extra_selectors=(".promo",))
    a = run(page(main='<h1>Big Buck Bunny</h1><p class="promo">Deal A</p>'), rules)
    b = run(page(main='<h1>Big Buck Bunny</h1><p class="promo">Deal B</p>'), rules)
    assert a.hashes.normalized == b.hashes.normalized
    assert a.normalization == "html-normalized-test/v1"
    baseline_a = run(page(main='<h1>Big Buck Bunny</h1><p class="promo">Deal A</p>'))
    assert baseline_a.revision_id != a.revision_id  # the scheme is part of the identity


def test_digests_are_tagged_and_stable_across_processes() -> None:
    # Fixed expected values: any change to a canonical form must bump its tag.
    assert hashing.visible_text_digest("") == hashing.digest_lines(hashing.VISIBLE_TEXT_TAG, [""])
    assert (
        str(hashing.link_set_digest([("anchor", "https://a.example/")]))
        == "sha256:"
        + __import__("hashlib")
        .sha256(b"antipiracy/page-hash/link-set/v1\nanchor\thttps://a.example/\n")
        .hexdigest()
    )
    assert hashing.link_set_digest(
        [("a", "u2"), ("a", "u1"), ("a", "u1")]
    ) == hashing.link_set_digest([("a", "u1"), ("a", "u2")])
