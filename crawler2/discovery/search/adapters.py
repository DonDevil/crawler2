"""Search-engine adapters ported from V1 ``search_engines/*`` (design §15, audit §3).

Adapters are pure: they build a request and parse a result page. They
report facts (rank, URL, title, snippet) and never score or prioritise.
A CAPTCHA or verification page is reported as ``blocked``; nothing tries
to solve or evade it.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Final, Literal
from urllib.parse import parse_qs, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

ADAPTER_VERSION: Final = "p6-search/v1"


class PageStatus(StrEnum):
    RESULTS = "results"
    EMPTY = "empty"
    BLOCKED = "blocked"
    PARSE_ERROR = "parse_error"


@dataclass(frozen=True, slots=True)
class SearchRequest:
    url: str
    params: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SearchHit:
    rank: int
    """0-based position among this page's usable results."""
    returned_url: str
    """The href as the engine returned it (before unwrapping)."""
    url: str
    """The unwrapped absolute target URL."""
    title: str | None
    snippet: str | None


@dataclass(frozen=True, slots=True)
class SearchPage:
    status: PageStatus
    hits: tuple[SearchHit, ...] = ()
    detail: str = ""


def _text(node: LexborNode | None, limit: int = 500) -> str | None:
    if node is None:
        return None
    text = " ".join(node.text(separator=" ").split())
    return text[:limit] or None


class SearchAdapter:
    name: ClassVar[str]
    network: ClassVar[Literal["clearnet", "tor"]] = "clearnet"
    version: ClassVar[str] = ADAPTER_VERSION
    result_selector: ClassVar[str]
    """One element per organic result."""
    link_selector: ClassVar[str] = "a[href]"
    snippet_selector: ClassVar[str | None] = None
    blocked_markers: ClassVar[tuple[str, ...]] = ("captcha",)
    prelude_url: ClassVar[str | None] = None
    """Fetched first when the search form needs hidden fields (Ahmia)."""

    def build_request(self, query: str, prelude: bytes | None = None) -> SearchRequest:
        raise NotImplementedError

    def unwrap(self, href: str, base: str) -> str | None:
        """The engine's redirect wrapper removed; None for engine-internal links."""
        absolute = urljoin(base, href)
        return absolute if absolute.startswith(("http://", "https://")) else None

    def is_blocked(self, status: int, final_url: str, body: bytes) -> bool:
        if status in (403, 429):
            return True
        head = body[:20_000].lower()
        final = final_url.lower()
        return any(m.encode() in head or m in final for m in self.blocked_markers)

    def parse(self, status: int, final_url: str, body: bytes, *, max_results: int) -> SearchPage:
        if self.is_blocked(status, final_url, body):
            return SearchPage(PageStatus.BLOCKED, detail=f"status={status}")
        if status >= 400:
            return SearchPage(PageStatus.PARSE_ERROR, detail=f"status={status}")
        tree = LexborHTMLParser(body)
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for result in tree.css(self.result_selector):
            anchor = result.css_first(self.link_selector)
            if anchor is None:
                continue
            href = anchor.attributes.get("href") or ""
            target = self.unwrap(href, final_url)
            if not target or target in seen or self._internal(target):
                continue
            seen.add(target)
            snippet = result.css_first(self.snippet_selector) if self.snippet_selector else None
            hits.append(SearchHit(len(hits), href, target, _text(anchor, 300), _text(snippet)))
            if len(hits) >= max_results:
                break
        return SearchPage(PageStatus.RESULTS if hits else PageStatus.EMPTY, tuple(hits))

    def _internal(self, url: str) -> bool:
        return False


class DuckDuckGo(SearchAdapter):
    name = "duckduckgo"
    result_selector = "div.result"
    link_selector = "a.result__a[href]"
    snippet_selector = ".result__snippet"
    blocked_markers = ("captcha", "anomaly-modal", "/anomaly.js")

    def build_request(self, query: str, prelude: bytes | None = None) -> SearchRequest:
        return SearchRequest("https://html.duckduckgo.com/html/", {"q": query})

    def unwrap(self, href: str, base: str) -> str | None:
        absolute = urljoin(base, href)
        parts = urlsplit(absolute)
        if parts.path.startswith("/l/"):
            target = parse_qs(parts.query).get("uddg", [None])[0]
            return target if target and target.startswith(("http://", "https://")) else None
        return super().unwrap(href, base)

    def _internal(self, url: str) -> bool:
        return (urlsplit(url).hostname or "").endswith("duckduckgo.com")


class Bing(SearchAdapter):
    name = "bing"
    result_selector = "li.b_algo"
    link_selector = "h2 a[href]"
    snippet_selector = ".b_caption p"
    blocked_markers = ("captcha", "/challenge", "b_captcha")

    def build_request(self, query: str, prelude: bytes | None = None) -> SearchRequest:
        return SearchRequest("https://www.bing.com/search", {"q": query})

    def unwrap(self, href: str, base: str) -> str | None:
        absolute = urljoin(base, href)
        parts = urlsplit(absolute)
        if (parts.hostname or "").endswith("bing.com") and parts.path.startswith("/ck/a"):
            encoded = parse_qs(parts.query).get("u", [None])[0]
            if not encoded:
                return None
            if encoded.startswith("a1"):
                encoded = encoded[2:]
            try:
                padded = encoded + "=" * (-len(encoded) % 4)
                target = base64.urlsafe_b64decode(padded).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                return None
            return target if target.startswith(("http://", "https://")) else None
        return super().unwrap(href, base)

    def _internal(self, url: str) -> bool:
        return (urlsplit(url).hostname or "").endswith("bing.com")


class Brave(SearchAdapter):
    name = "brave"
    result_selector = "div.snippet[data-type='web']"
    link_selector = "a[href]"
    snippet_selector = ".snippet-description, .generic-snippet"
    blocked_markers = ("captcha", "/pow-captcha")

    def build_request(self, query: str, prelude: bytes | None = None) -> SearchRequest:
        return SearchRequest("https://search.brave.com/search", {"q": query, "source": "web"})

    def _internal(self, url: str) -> bool:
        return (urlsplit(url).hostname or "").endswith("brave.com")


class Yandex(SearchAdapter):
    name = "yandex"
    result_selector = "li.serp-item"
    link_selector = "a.OrganicTitle-Link[href], a.Link[href]"
    snippet_selector = ".OrganicText"
    blocked_markers = ("showcaptcha", "smartcaptcha", "checkcaptcha")

    def build_request(self, query: str, prelude: bytes | None = None) -> SearchRequest:
        return SearchRequest("https://yandex.com/search/", {"text": query})

    def _internal(self, url: str) -> bool:
        host = urlsplit(url).hostname or ""
        return host.endswith(("yandex.com", "yandex.ru", "ya.ru"))


class Ahmia(SearchAdapter):
    name = "ahmia"
    result_selector = "li.result"
    link_selector = "a[href]"
    snippet_selector = "p"
    prelude_url = "https://ahmia.fi/"

    def build_request(self, query: str, prelude: bytes | None = None) -> SearchRequest:
        params = {"q": query}
        if prelude:
            form = LexborHTMLParser(prelude).css_first("form[action='/search/']")
            for field_node in form.css("input[type='hidden']") if form else []:
                name = field_node.attributes.get("name")
                if name:
                    params[name] = field_node.attributes.get("value") or ""
        return SearchRequest("https://ahmia.fi/search/", params)

    def unwrap(self, href: str, base: str) -> str | None:
        absolute = urljoin(base, href)
        target = parse_qs(urlsplit(absolute).query).get("redirect_url", [None])[0]
        if target and target.startswith(("http://", "https://")):
            return target
        return absolute if ".onion" in (urlsplit(absolute).hostname or "") else None


class Torch(SearchAdapter):
    name = "torch"
    network = "tor"
    result_selector = "dl, div.result"
    link_selector = "a[href]"
    mirrors: ClassVar[tuple[str, ...]] = (
        "http://torchqfmuhpqteg5nww33wztcfxcly2rl3kwsk6zxja7gi5awgsk7qad.onion/",
        "http://torchs7vpa4w6ddmgj56yseropeo5y47ixktki57a45l7zmwxrffnnqd.onion/",
        "http://torchac4wwv4sd3qt73xjxvz6wxiande4mhsvbhi4icsui7lgwh4kbqd.onion/",
    )

    def build_request(self, query: str, prelude: bytes | None = None) -> SearchRequest:
        return SearchRequest(
            urljoin(self.mirrors[0], "search.htm"), {"P": query, "DEFAULTOP": "and"}
        )

    def _internal(self, url: str) -> bool:
        host = urlsplit(url).hostname or ""
        return any(host == urlsplit(m).hostname for m in self.mirrors)


ADAPTERS: Final[dict[str, type[SearchAdapter]]] = {
    a.name: a for a in (DuckDuckGo, Bing, Brave, Yandex, Ahmia, Torch)
}
