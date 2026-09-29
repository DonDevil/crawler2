"""Extraction over the one parsed tree (P5 design §3, §5-§9).

``extract`` parses the body once (``parse_page``) and then makes one
iterative pass over the tree that feeds every fact family: links, media,
metadata, inline scripts, visible and normalized text, and the DOM shape.
Script and text literals are scanned afterwards from strings collected in
that pass. The tree is never mutated, so the order of the families cannot
change the result.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Final

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.ids import PageRevisionId, PageVersionId, UrlId
from antipiracy_contracts.models.media import DiscoveryMethod, MediaKind, MediaReference
from antipiracy_contracts.models.web import DiscoveredLink, LinkRelation, PageHashes, UrlRef
from selectolax.lexbor import LexborNode

from crawler2.extraction import hashing
from crawler2.extraction.model import (
    BASELINE_RULES,
    ExtractStats,
    Limits,
    NormalizationRules,
    PageExtract,
    PageMetadata,
)
from crawler2.extraction.parse import ParsedPage, ParserFactory, parse_page
from crawler2.extraction.scripts import script_literals, text_urls
from crawler2.extraction.urls import (
    ALLOW_ALL,
    ASSET_EXTENSIONS,
    WEB_SCHEMES,
    LinkPolicy,
    clean_reference,
    media_kind,
    path_extension,
    resolve,
    scheme_of,
    to_url_ref,
)

TEXT_INVISIBLE_TAGS: Final = frozenset(
    {"script", "style", "noscript", "template", "head", "svg", "math", "iframe", "object", "canvas"}
)
"""Subtrees whose text never counts as visible."""

_METHOD_RANK: Final = {
    DiscoveryMethod.VIDEO_ELEMENT: 0,
    DiscoveryMethod.SOURCE_ELEMENT: 0,
    DiscoveryMethod.META_TAG: 1,
    DiscoveryMethod.PLAYER_CONFIG: 2,
    DiscoveryMethod.LINK: 3,
    DiscoveryMethod.SCRIPT_LITERAL: 4,
    DiscoveryMethod.NETWORK_CAPTURE: 5,
}
_MEDIA_ELEMENT_ATTRS: Final = ("src", "data-src", "data-video", "data-file")
_META_MEDIA: Final = frozenset(
    {
        "og:video",
        "og:video:url",
        "og:video:secure_url",
        "og:audio",
        "og:audio:url",
        "og:audio:secure_url",
        "twitter:player:stream",
    }
)
_META_FIELDS: Final = {
    "og:title": "og_title",
    "og:type": "og_type",
    "article:published_time": "published_at",
    "datepublished": "published_at",
    "article:modified_time": "modified_at",
    "og:updated_time": "modified_at",
    "datemodified": "modified_at",
    "robots": "robots",
}
_TEXT_LIMIT: Final = 300
_SHORT_LIMIT: Final = 100


def _short(value: str | None, limit: int = _TEXT_LIMIT) -> str | None:
    if not value:
        return None
    text = hashing.collapse(value)[:limit]
    return text or None


def _timestamp(value: str | None) -> datetime | None:
    """ISO-8601 → UTC; a value without offset is taken as UTC; anything else is dropped."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip()[:40])
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


class _Collector:
    """Accumulates the facts of one page; never shared across pages."""

    def __init__(self, page: ParsedPage, policy: LinkPolicy, limits: Limits) -> None:
        self.page = page
        self.policy = policy
        self.limits = limits
        self.links: dict[tuple[UrlId, LinkRelation], DiscoveredLink] = {}
        self.media: dict[UrlId, tuple[int, MediaReference]] = {}
        self.meta: dict[str, str] = {}
        self.scripts: list[tuple[str, bool]] = []
        self.visible: list[str] = []
        self.normalized: list[str] = []
        self.shape: list[str] = []
        self.dropped: Counter[str] = Counter()
        self.truncated: set[str] = set()
        self.canonical: str | None = None
        self.lang: str | None = None
        self._script_chars = 0
        self._refs: dict[str, UrlRef | None] = {}

    # -- references --------------------------------------------------------

    def ref(self, raw: str | None) -> UrlRef | None:
        """Resolve and canonicalize one raw reference (memoized per page)."""
        if raw is None:
            return None
        if raw in self._refs:
            return self._refs[raw]
        result: UrlRef | None = None
        cleaned = clean_reference(raw)
        if cleaned is None:
            self.dropped["empty"] += 1
        elif cleaned.startswith("#"):
            self.dropped["self"] += 1
        elif (scheme := scheme_of(cleaned)) is not None and scheme not in WEB_SCHEMES:
            self.dropped["scheme"] += 1
        else:
            absolute = resolve(self.page.base, cleaned)
            result = to_url_ref(absolute) if absolute is not None else None
            if result is None:
                self.dropped["invalid"] += 1
        self._refs[raw] = result
        return result

    def add_link(
        self,
        target: UrlRef,
        relation: LinkRelation,
        *,
        anchor_text: str | None = None,
        nofollow: bool = False,
    ) -> None:
        if target.url_id == self.page.page.url_id and relation is not LinkRelation.CANONICAL:
            self.dropped["self"] += 1
            return
        key = (target.url_id, relation)
        if key in self.links:
            return
        if len(self.links) >= self.limits.max_links:
            self.truncated.add("links")
            self.dropped["cap"] += 1
            return
        if not self.policy.allow(self.page.page, target, relation):
            self.dropped["policy"] += 1
            return
        self.links[key] = DiscoveredLink.model_construct(
            target=target, relation=relation, anchor_text=anchor_text, nofollow=nofollow
        )

    def add_media(
        self,
        locator: UrlRef,
        kind: MediaKind,
        method: DiscoveryMethod,
        declared_type: str | None = None,
    ) -> None:
        rank = _METHOD_RANK[method]
        existing = self.media.get(locator.url_id)
        if existing is not None and existing[0] <= rank:
            return
        if existing is None and len(self.media) >= self.limits.max_media:
            self.truncated.add("media")
            self.dropped["cap"] += 1
            return
        self.media[locator.url_id] = (
            rank,
            MediaReference.model_construct(
                locator=locator,
                kind=kind,
                method=method,
                declared_type=_short(declared_type, 200),
            ),
        )

    def link_or_media(
        self, target: UrlRef, relation: LinkRelation, media_method: DiscoveryMethod, **link: object
    ) -> None:
        kind = media_kind(target.url)
        if kind is not None:
            self.add_media(target, kind, media_method)
        else:
            self.add_link(target, relation, **link)  # type: ignore[arg-type]

    # -- element handlers --------------------------------------------------

    def element(self, tag: str, node: LexborNode) -> None:
        handler = _HANDLERS.get(tag)
        if handler is not None:
            handler(self, node, node.attributes)

    def _anchor(self, node: LexborNode, attrs: dict[str, str | None]) -> None:
        target = self.ref(attrs.get("href"))
        if target is None:
            return
        rel = (attrs.get("rel") or "").lower().split()
        self.link_or_media(
            target,
            LinkRelation.ANCHOR,
            DiscoveryMethod.LINK,
            anchor_text=_short(node.text(deep=True, separator=" ")),
            nofollow="nofollow" in rel,
        )

    def _frame(self, node: LexborNode, attrs: dict[str, str | None]) -> None:
        target = self.ref(attrs.get("src"))
        if target is not None:
            self.link_or_media(target, LinkRelation.IFRAME, DiscoveryMethod.VIDEO_ELEMENT)

    def _link(self, node: LexborNode, attrs: dict[str, str | None]) -> None:
        if "canonical" not in (attrs.get("rel") or "").lower().split():
            return
        target = self.ref(attrs.get("href"))
        if target is not None:
            self.add_link(target, LinkRelation.CANONICAL)
            if self.canonical is None:
                self.canonical = target.url

    def _meta(self, node: LexborNode, attrs: dict[str, str | None]) -> None:
        content = attrs.get("content")
        if not content:
            return
        if (attrs.get("http-equiv") or "").lower() == "refresh":
            _, sep, rest = content.partition("=") if "url" in content.lower() else ("", "", "")
            target = self.ref(rest.strip().strip("'\"")) if sep else None
            if target is not None:
                self.add_link(target, LinkRelation.META_REFRESH)
            return
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
        if key in _META_MEDIA:
            target = self.ref(content)
            if target is not None:
                self.link_or_media(target, LinkRelation.OTHER, DiscoveryMethod.META_TAG)
        elif key in _META_FIELDS:
            self.meta.setdefault(_META_FIELDS[key], content)

    def _media_element(self, node: LexborNode, attrs: dict[str, str | None]) -> None:
        for name in _MEDIA_ELEMENT_ATTRS:
            target = self.ref(attrs.get(name))
            if target is not None:
                kind = media_kind(target.url) or MediaKind.UNKNOWN
                self.add_media(target, kind, DiscoveryMethod.VIDEO_ELEMENT)

    def _source(self, node: LexborNode, attrs: dict[str, str | None]) -> None:
        parent = node.parent
        in_player = parent is not None and parent.tag in ("video", "audio")
        declared = attrs.get("type")
        for name in _MEDIA_ELEMENT_ATTRS:
            target = self.ref(attrs.get(name))
            if target is None:
                continue
            kind = media_kind(target.url, declared)
            if kind is None and in_player:
                kind = MediaKind.UNKNOWN
            if kind is not None:
                self.add_media(target, kind, DiscoveryMethod.SOURCE_ELEMENT, declared)

    def _embed(self, node: LexborNode, attrs: dict[str, str | None]) -> None:
        target = self.ref(attrs.get("src") or attrs.get("data"))
        if target is not None and (kind := media_kind(target.url, attrs.get("type"))) is not None:
            self.add_media(target, kind, DiscoveryMethod.VIDEO_ELEMENT, attrs.get("type"))

    def _script(self, node: LexborNode, attrs: dict[str, str | None]) -> None:
        if "src" in attrs:
            return
        text = node.text(deep=True)
        if not text:
            return
        budget = self.limits.max_page_script_bytes - self._script_chars
        limit = min(self.limits.max_script_bytes, budget)
        if len(text) > limit:
            self.truncated.add("scripts")
            text = text[: max(limit, 0)]
        if text:
            self._script_chars += len(text)
            self.scripts.append((text, "ld+json" in (attrs.get("type") or "").lower()))

    def _html(self, node: LexborNode, attrs: dict[str, str | None]) -> None:
        if self.lang is None:
            self.lang = _short(attrs.get("lang"), 35)

    # -- literals (after the walk) ------------------------------------------

    def literals(self, visible_text: str) -> None:
        markup = {url_id for url_id, _ in self.links}
        for text, json_ld in self.scripts:
            for literal in script_literals(text):
                target = self.ref(literal.value)
                if target is None:
                    continue
                kind = media_kind(target.url)
                if kind is not None:
                    method = (
                        DiscoveryMethod.PLAYER_CONFIG
                        if json_ld or literal.player_key
                        else DiscoveryMethod.SCRIPT_LITERAL
                    )
                    self.add_media(target, kind, method)
                elif not literal.media_only:
                    self._literal_link(target, LinkRelation.SCRIPT_LITERAL, markup)
        for url in text_urls(visible_text):
            target = self.ref(url)
            if target is None:
                continue
            kind = media_kind(target.url)
            if kind is not None:
                self.add_media(target, kind, DiscoveryMethod.LINK)
            else:
                self._literal_link(target, LinkRelation.OTHER, markup)

    def _literal_link(self, target: UrlRef, relation: LinkRelation, markup: set[UrlId]) -> None:
        if target.url_id in markup:
            return
        if path_extension(target.url) in ASSET_EXTENSIONS:
            self.dropped["asset"] += 1
            return
        self.add_link(target, relation)


_HANDLERS: Final = {
    "a": _Collector._anchor,
    "area": _Collector._anchor,
    "iframe": _Collector._frame,
    "frame": _Collector._frame,
    "link": _Collector._link,
    "meta": _Collector._meta,
    "video": _Collector._media_element,
    "audio": _Collector._media_element,
    "source": _Collector._source,
    "embed": _Collector._embed,
    "object": _Collector._embed,
    "script": _Collector._script,
    "html": _Collector._html,
}


def _hidden_ids(page: ParsedPage) -> set[int]:
    ids: set[int] = set()
    for node in page.tree.css("[hidden], [aria-hidden], [style]"):
        attrs = node.attributes
        style = (attrs.get("style") or "").lower().replace(" ", "")
        if (
            "hidden" in attrs
            or (attrs.get("aria-hidden") or "").strip().lower() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        ):
            ids.add(node.mem_id)
    return ids


def _selected_ids(page: ParsedPage, selectors: tuple[str, ...]) -> set[int]:
    if not selectors:
        return set()
    return {node.mem_id for node in page.tree.css(", ".join(selectors))}


def _walk(page: ParsedPage, out: _Collector, rules: NormalizationRules) -> int:
    """One preorder pass over every node; returns the element count."""
    hidden = _hidden_ids(page)
    boiler = _selected_ids(page, rules.boilerplate_selectors + rules.extra_selectors)
    limits = out.limits
    root = page.tree.root
    if root is None:
        return 0
    elements = 0
    stack: list[tuple[LexborNode, int, bool, bool]] = [(root, 0, False, False)]
    while stack:
        node, depth, invisible, excluded = stack.pop()
        sibling = node.next
        if sibling is not None:
            stack.append((sibling, depth, invisible, excluded))
        tag = node.tag
        if tag is None:
            continue
        if tag == "-text":
            if not invisible:
                text = node.text_content
                if text and not text.isspace():
                    out.visible.append(text)
                    if not excluded:
                        out.normalized.append(text)
            continue
        if tag[0] in "-_!":
            continue  # comments, doctype, processing instructions
        elements += 1
        if elements > limits.max_elements:
            out.truncated.add("elements")
            break
        out.shape.append(f"{min(depth, limits.max_depth)}:{tag}")
        out.element(tag, node)
        child = node.child
        if child is not None:
            mem_id = node.mem_id if hidden or boiler else 0
            stack.append(
                (
                    child,
                    depth + 1,
                    invisible or tag in TEXT_INVISIBLE_TAGS or mem_id in hidden,
                    excluded or mem_id in boiler,
                )
            )
    return elements


def extract_parsed(
    page: ParsedPage,
    body_digest: ContentDigest,
    *,
    rules: NormalizationRules = BASELINE_RULES,
    policy: LinkPolicy = ALLOW_ALL,
    limits: Limits | None = None,
) -> PageExtract:
    """Every P5 fact of one parsed page. Pure: no I/O, no clock."""
    limits = limits or Limits()
    out = _Collector(page, policy, limits)
    elements = _walk(page, out, rules)
    visible = hashing.visible_text_form(out.visible)
    out.literals(visible)
    normalized_text = hashing.normalized_text_form(out.normalized)

    links = tuple(
        sorted(out.links.values(), key=lambda link: (link.target.url, link.relation.value))
    )
    media = tuple(sorted((m for _, m in out.media.values()), key=lambda m: m.locator.url))
    media_lines = hashing.media_set_lines((m.kind.value, m.locator.url) for m in media)
    hashes = PageHashes.model_construct(
        raw=body_digest,
        normalized=hashing.normalized_digest(rules.scheme, normalized_text, media_lines),
        visible_text=hashing.visible_text_digest(visible),
        link_set=hashing.link_set_digest((link.relation.value, link.target.url) for link in links),
        media_set=hashing.media_set_digest(media_lines),
        structural=hashing.structural_digest(out.shape),
    )
    title_node = page.tree.css_first("head title")
    metadata = PageMetadata(
        title=_short(title_node.text(deep=True)) if title_node is not None else None,
        lang=out.lang,
        canonical=out.canonical,
        og_title=_short(out.meta.get("og_title")),
        og_type=_short(out.meta.get("og_type"), _SHORT_LIMIT),
        published_at=_timestamp(out.meta.get("published_at")),
        modified_at=_timestamp(out.meta.get("modified_at")),
        robots=_short(out.meta.get("robots"), _SHORT_LIMIT),
    )
    truncated = set(out.truncated)
    if page.truncated:
        truncated.add("body")
    url_id = page.page.url_id
    return PageExtract(
        page=page.page,
        page_version_id=PageVersionId.of(url_id, body_digest),
        normalization=rules.scheme,
        revision_id=PageRevisionId.of(url_id, rules.scheme, hashes.normalized),
        hashes=hashes,
        links=links,
        media=media,
        metadata=metadata,
        stats=ExtractStats(
            elements=elements,
            text_chars=len(visible),
            dropped=dict(sorted(out.dropped.items())),
            truncated=tuple(sorted(truncated)),
        ),
    )


def extract(
    body: bytes,
    *,
    page: UrlRef,
    content_type: str | None,
    rules: NormalizationRules = BASELINE_RULES,
    policy: LinkPolicy = ALLOW_ALL,
    limits: Limits | None = None,
    parser: ParserFactory | None = None,
) -> PageExtract:
    """Parse ``body`` once and extract every P5 fact (the single-parse entry point)."""
    limits = limits or Limits()
    parsed = parse_page(
        body,
        page=page,
        content_type=content_type,
        max_bytes=limits.max_parse_bytes,
        **({"parser": parser} if parser is not None else {}),
    )
    return extract_parsed(
        parsed, ContentDigest.of_bytes(body), rules=rules, policy=policy, limits=limits
    )
