"""WHAT: page kind, URL templates and page relevance (P7 design §6, what.md).

Pure and deterministic: no network, storage, clock or frontier. Inputs are
P5 page facts (``PageExtract``, or the same fields from the M1 export).
Outputs are features for a future WHEN; nothing here changes crawling.

- ``tokenize_url``: a URL's path as token classes (``{year}``, ``{num}``,
  ``{slug}``, CMS literals, ...) and the template string they form.
- ``build_site_profile``: per host, the retained URL templates with their
  role (content, mixed, spam, listing, unknown), the site's boilerplate links
  and title segments, and its dominant title script. Bounded whatever the
  input size, and independent of input order.
- ``analyze_page``: one page against its site profile → page kind (hub,
  detail, other), relevance (relevant, spam, unknown), the content-template
  links it carries, and the reason codes behind each decision.

Every vocabulary term is operator data (``data/what_vocabulary.toml``), and
no single term decides a result (what.md §5).
"""

from __future__ import annotations

import hashlib
import re
import tomllib
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qsl, unquote, urlsplit

from crawler2.extraction.model import PageExtract

DEFAULT_VOCABULARY_PATH: Final = Path(__file__).with_name("data") / "what_vocabulary.toml"


class PageKind(StrEnum):
    HUB = "hub"
    DETAIL = "detail"
    OTHER = "other"


class Relevance(StrEnum):
    RELEVANT = "relevant"
    SPAM = "spam"
    UNKNOWN = "unknown"


class TemplateRole(StrEnum):
    """What a site's URL template holds (design §6)."""

    CONTENT = "content"
    """Its members carry content evidence and almost no spam evidence."""
    MIXED = "mixed"
    """Content and spam share it (WordPress ``/<slug>/``): pages need their own check."""
    SPAM = "spam"
    LISTING = "listing"
    """Pagination, search, archive or CMS listing paths, and the site root."""
    UNKNOWN = "unknown"
    """Too little evidence either way."""


CONTENT_ROLES: Final = frozenset({TemplateRole.CONTENT, TemplateRole.MIXED})


@dataclass(frozen=True, slots=True)
class WhatPolicy:
    """Versioned thresholds (what.md §4.4 gives each value's origin)."""

    version: str = "what/v1"
    min_template_support: int = 20
    max_templates: int = 32
    max_profile_pages: int = 1_000
    max_links_per_page: int = 500
    max_profile_urls: int = 20_000
    boilerplate_share: float = 0.2
    min_boilerplate_pages: int = 10
    max_boilerplate_links: int = 256
    max_boilerplate_segments: int = 32
    max_segments: int = 10
    max_query_keys: int = 8
    hub_min_own_links: int = 8
    hub_strong_links: int = 24
    min_text_chars: int = 200
    content_share: float = 0.5
    minor_share: float = 0.2
    hub_min_link_evidence: int = 3
    max_result_links: int = 200
    max_terms: int = 8


DEFAULT_WHAT_POLICY: Final = WhatPolicy()

# Generic web/CMS path words (not content vocabulary).
_LISTING_WORDS: Final = frozenset(
    {"category", "categories", "tag", "tags", "author", "label", "archive", "archives",
     "genre", "genres", "browse", "topic", "topics"}
)  # fmt: skip
_LITERALS: Final = _LISTING_WORDS | frozenset(
    {"page", "search", "feed", "p", "amp", "comments", "wp-content", "wp-includes",
     "wp-admin", "wp-json", "cdn-cgi"}
)  # fmt: skip
_LANGS: Final = frozenset(
    {"en", "ta", "te", "hi", "ml", "kn", "bn", "mr", "pa", "gu", "ur", "fr", "de", "es",
     "pt", "it", "ru", "ja", "ko", "zh", "ar", "th", "vi", "id", "tr"}
)  # fmt: skip
_PAGINATION_KEYS: Final = frozenset({"page", "paged", "start", "offset", "updated-max"})
_SEARCH_KEYS: Final = frozenset({"s", "q", "query", "search", "term", "keyword"})
_YEAR: Final = re.compile(r"(?<!\d)(19[5-9]\d|20[0-3]\d)(?!\d)")
_EXT: Final = re.compile(r"^(.+)\.(html?|php|aspx?|jsp|shtml)$")
_SEP: Final = re.compile(r"[\s\-_./+]+")
_TITLE_SPLIT: Final = re.compile(r"\s+[-|\u2013\u2014:\u00b7\u00bb]+\s+|\s*\|\s*")
"""Title segment separators: hyphen, en/em dash, colon, middle dot, guillemet, bar."""


def _norm(text: str | None) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold().strip()


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


# --------------------------------------------------------------------------- vocabulary


def _latin(term: str) -> bool:
    return all(not ch.isalpha() or unicodedata.name(ch, "").startswith("LATIN") for ch in term)


@dataclass(frozen=True, slots=True)
class _Matcher:
    words: re.Pattern[str] | None
    substrings: tuple[str, ...]

    @classmethod
    def of(cls, terms: Iterable[str]) -> _Matcher:
        normed = sorted({_norm(t) for t in terms if _norm(t)}, key=lambda t: (-len(t), t))
        latin = [t for t in normed if _latin(t)]
        rx = (
            re.compile(r"(?<!\w)(" + "|".join(re.escape(t) for t in latin) + r")(?!\w)")
            if latin
            else None
        )
        return cls(rx, tuple(t for t in normed if not _latin(t)))

    def find(self, text: str) -> tuple[str, ...]:
        hits = set(self.words.findall(text)) if self.words else set()
        hits.update(t for t in self.substrings if t in text)
        return tuple(sorted(hits))


@dataclass(frozen=True, slots=True)
class Vocabulary:
    """Operator data: content and spam terms, CMS hub-title patterns, utility terms."""

    version: str
    content: _Matcher
    spam: _Matcher
    utility: _Matcher
    hub_titles: tuple[re.Pattern[str], ...]

    @classmethod
    def from_mapping(cls, doc: Mapping[str, Any]) -> Vocabulary:
        structure = doc.get("structure", {})
        return cls(
            version=str(doc["vocabulary"]["version"]),
            content=_Matcher.of(doc.get("content", {}).get("terms", ())),
            spam=_Matcher.of(doc.get("spam", {}).get("terms", ())),
            utility=_Matcher.of(structure.get("utility_terms", ())),
            hub_titles=tuple(re.compile(p) for p in structure.get("hub_title_patterns", ())),
        )


def load_vocabulary(path: Path | None = None) -> Vocabulary:
    """Read a vocabulary file (default: the shipped ``what-vocabulary/v1``)."""
    return Vocabulary.from_mapping(tomllib.loads((path or DEFAULT_VOCABULARY_PATH).read_text()))


# --------------------------------------------------------------------------- page facts


@dataclass(frozen=True, slots=True)
class PageFacts:
    """The P5 facts WHAT reads from one page (no body, no visible text)."""

    url: str
    title: str | None = None
    og_title: str | None = None
    og_type: str | None = None
    published: bool = False
    text_chars: int = 0
    media: int = 0
    links: tuple[tuple[str, str], ...] = ()
    """Anchor links only: (target URL, anchor text)."""


def facts_from_extract(extract: PageExtract) -> PageFacts:
    m = extract.metadata
    return PageFacts(
        url=extract.page.url,
        title=m.title,
        og_title=m.og_title,
        og_type=m.og_type,
        published=m.published_at is not None,
        text_chars=extract.stats.text_chars,
        media=len(extract.media),
        links=tuple(
            (lk.target.url, lk.anchor_text or "") for lk in extract.links if lk.relation == "anchor"
        ),
    )


def facts_from_export(row: Mapping[str, Any]) -> PageFacts:
    """A ``page_extracts`` row of the M1 export (links as [url, relation, anchor])."""
    m = row.get("metadata") or {}
    return PageFacts(
        url=row["url"],
        title=m.get("title"),
        og_title=m.get("og_title"),
        og_type=m.get("og_type"),
        published=m.get("published_at") is not None,
        text_chars=int((row.get("stats") or {}).get("text_chars") or 0),
        media=len(row.get("media") or ()),
        links=tuple((u, a or "") for u, rel, a in row.get("links") or () if rel == "anchor"),
    )


# --------------------------------------------------------------------------- URL templates


@dataclass(frozen=True, slots=True)
class UrlShape:
    host: str
    template: str
    tokens: tuple[str, ...]
    slug_text: str
    """Decoded words of the slug segments, for vocabulary and year checks."""
    pagination: bool = False
    search: bool = False
    listing: bool = False
    date_archive: bool = False
    root: bool = False

    @property
    def structural(self) -> bool:
        """Listing-like by its URL alone."""
        return self.pagination or self.search or self.listing or self.date_archive or self.root


def _segment(seg: str, prev: str | None, first: bool) -> tuple[str, str]:
    """(token, slug words) for one decoded, casefolded path segment."""
    if _YEAR.fullmatch(seg):
        return "{year}", ""
    if prev == "{year}" and re.fullmatch(r"0[1-9]|1[0-2]", seg):
        return "{mm}", ""
    if prev == "{mm}" and re.fullmatch(r"0[1-9]|[12]\d|3[01]", seg):
        return "{dd}", ""
    if seg.isdigit():
        return "{num}", ""
    if m := _EXT.fullmatch(seg):
        stem, ext = m.groups()
        tok, words = _segment(stem, None, False)
        return f"{tok}.{ext}", words
    if seg in _LITERALS:
        return seg, ""
    if first and seg in _LANGS:
        return "{lang}", ""
    words = _SEP.sub(" ", seg).strip()
    # A slug that ends in a release year (``/lens-2017/``) is its own class: on
    # WordPress sites it separates item posts from other posts at ``/<slug>/``.
    if " " in words and _YEAR.fullmatch(words.rsplit(" ", 1)[1]):
        return "{slug}-{year}", words
    return "{slug}", words


def tokenize_url(url: str, policy: WhatPolicy = DEFAULT_WHAT_POLICY) -> UrlShape | None:
    """The URL's template, or ``None`` for a malformed or non-HTTP URL.

    Query values and non-structural keys (tracking, comment ids) are dropped;
    pagination and search keys are kept. The fragment is ignored. The trailing
    slash is part of the template (a site convention).
    """
    try:
        s = urlsplit(url.strip())
        host = (s.hostname or "").lower()
    except ValueError:
        return None
    if s.scheme.lower() not in ("http", "https") or not host:
        return None
    raw = [x for x in s.path.split("/") if x]
    tokens: list[str] = []
    words: list[str] = []
    prev: str | None = None
    for i, seg in enumerate(raw):
        if len(tokens) == policy.max_segments - 1 and i < len(raw) - 1:
            tokens.append("{deep}")
            break
        tok, w = _segment(_norm(unquote(seg, errors="replace")), prev, i == 0)
        tokens.append(tok)
        if w:
            words.append(w)
        prev = tok
    try:
        keys = sorted({_norm(k) for k, _ in parse_qsl(s.query, keep_blank_values=True)})
    except ValueError:
        keys = []
    kept = [k for k in keys if k in _PAGINATION_KEYS | _SEARCH_KEYS][: policy.max_query_keys]
    numbered = len(tokens) >= 2 and tokens[-1] == "{num}" and tokens[-2] != "{num}"
    template = "/" + "/".join(tokens)
    if tokens and s.path.endswith("/"):
        template += "/"
    if kept:
        template += "?" + "&".join(kept)
    dates = {"{year}", "{mm}", "{dd}"}
    return UrlShape(
        host=host,
        template=template,
        tokens=tuple(tokens),
        slug_text=" ".join(words),
        pagination="page" in tokens or numbered or bool(set(kept) & _PAGINATION_KEYS),
        search="search" in tokens or bool(set(kept) & _SEARCH_KEYS),
        listing=bool(set(tokens) & _LISTING_WORDS),
        date_archive=bool(tokens)
        and set(tokens) - {"page", "{num}"} <= dates
        and tokens[0] == "{year}",
        root=not tokens,
    )


def _same_host_links(facts: PageFacts, host: str, policy: WhatPolicy) -> dict[str, str]:
    """Unique same-host anchor targets (fragment removed, self excluded) → anchor.

    Bounded by ``max_links_per_page``, choosing by URL hash, so a truncated set
    does not depend on link order. The anchor kept is the smallest non-empty one.
    """
    page = facts.url.split("#", 1)[0]
    out: dict[str, str] = {}
    for target, anchor in facts.links:
        u = target.split("#", 1)[0]
        if u == page:
            continue
        try:
            if (urlsplit(u).hostname or "").lower() != host:
                continue
        except ValueError:
            continue
        a = " ".join(anchor.split())[:300]
        if u not in out or (a and (not out[u] or a < out[u])):
            out[u] = a
    if len(out) > policy.max_links_per_page:
        keep = sorted(out, key=_h)[: policy.max_links_per_page]
        out = {u: out[u] for u in keep}
    return out


# --------------------------------------------------------------------------- evidence


def _script(text: str) -> str | None:
    """Dominant Unicode script of the letters in ``text`` (ties: alphabetical)."""
    c: Counter[str] = Counter(unicodedata.name(ch, "?").split()[0] for ch in text if ch.isalpha())
    return min(c, key=lambda k: (-c[k], k)) if c else None


@dataclass(frozen=True, slots=True)
class Evidence:
    content: tuple[str, ...] = ()
    """Positive reason codes: content_terms, year."""
    spam: tuple[str, ...] = ()
    """Negative reason codes: spam_terms, script_mismatch."""
    terms: tuple[str, ...] = ()


def _evidence(text: str, slug: str, site_script: str | None, vocab: Vocabulary) -> Evidence:
    joined = f"{_norm(text)} {_norm(slug)}".strip()
    pos: list[str] = []
    neg: list[str] = []
    content = vocab.content.find(joined)
    spam = vocab.spam.find(joined)
    if content:
        pos.append("content_terms")
    if _YEAR.search(joined):
        pos.append("year")
    if spam:
        neg.append("spam_terms")
    script = _script(_norm(text) or _norm(slug))
    if site_script and script and script != site_script:
        neg.append("script_mismatch")
    return Evidence(tuple(pos), tuple(neg), (*content, *spam))


def _title_core(title: str | None, boilerplate: frozenset[str]) -> str:
    parts = [p for p in _TITLE_SPLIT.split(_norm(title)) if p]
    return " ".join(p for p in parts if p not in boilerplate)


# --------------------------------------------------------------------------- site profile


@dataclass(frozen=True, slots=True)
class TemplateStat:
    template: str
    support: int
    """Distinct URLs of the site (pages and same-host link targets) in the template."""
    content: int
    """Members whose title (or anchor) and slug carry content evidence and no spam evidence."""
    spam: int
    listed: int
    """Members linked (non-boilerplate) from the site's listing pages."""
    role: TemplateRole


@dataclass(frozen=True, slots=True)
class SiteProfile:
    host: str
    policy: str
    vocabulary: str
    pages: int
    urls: int
    templates: tuple[TemplateStat, ...]
    """Retained templates, ordered by (-support, template)."""
    boilerplate_links: frozenset[str]
    title_boilerplate: frozenset[str]
    script: str | None

    def role(self, template: str) -> TemplateRole | None:
        for t in self.templates:
            if t.template == template:
                return t.role
        return None

    @property
    def content_templates(self) -> tuple[str, ...]:
        return tuple(t.template for t in self.templates if t.role in CONTENT_ROLES)

    def pattern_templates(self, policy: WhatPolicy = DEFAULT_WHAT_POLICY) -> frozenset[str]:
        """Templates whose members are expected to carry content evidence themselves:
        mixed ones, and content ones defined by evidence (not only by being listed)."""
        return frozenset(
            t.template
            for t in self.templates
            if t.role is TemplateRole.MIXED
            or (t.role is TemplateRole.CONTENT and t.content >= policy.content_share * t.support)
        )


def _role(shape: UrlShape, stat: tuple[int, int, int, int], p: WhatPolicy) -> TemplateRole:
    if shape.structural:
        return TemplateRole.LISTING
    support, content, spam, listed = stat
    c, s, li = content / support, spam / support, listed / support
    if c >= p.minor_share and s >= p.minor_share:
        return TemplateRole.MIXED
    if (c >= p.content_share or li >= p.content_share) and s < p.minor_share:
        return TemplateRole.CONTENT
    # Spam vocabulary misses brand names, so explicit spam evidence under-counts
    # spam: a fifth of the members with it, and few with content evidence, is enough.
    if s >= p.minor_share:
        return TemplateRole.SPAM
    return TemplateRole.UNKNOWN


def _pick_pages(host: str, pages: Iterable[PageFacts], policy: WhatPolicy) -> list[PageFacts]:
    """One page per URL (the smallest by content hash), at most ``max_profile_pages``
    chosen by URL hash: the same input set gives the same pages in any order."""
    best: dict[str, tuple[str, PageFacts]] = {}
    for p in pages:
        shape = tokenize_url(p.url, policy)
        if shape is None or shape.host != host:
            continue
        key = _h(repr(p))
        if p.url not in best or key < best[p.url][0]:
            best[p.url] = (key, p)
    urls = sorted(best, key=lambda u: (_h(u), u))[: policy.max_profile_pages]
    return [best[u][1] for u in sorted(urls)]


def build_site_profile(
    host: str,
    pages: Iterable[PageFacts],
    policy: WhatPolicy = DEFAULT_WHAT_POLICY,
    vocab: Vocabulary | None = None,
) -> SiteProfile:
    """The host's URL templates, boilerplate and script (what.md §4.2)."""
    vocab = vocab or load_vocabulary()
    host = host.lower()
    chosen = _pick_pages(host, pages, policy)
    n = len(chosen)
    link_count: Counter[str] = Counter()
    seg_count: Counter[str] = Counter()
    anchors: dict[str, str] = {}
    titles: dict[str, str] = {}
    page_links: dict[str, frozenset[str]] = {}
    for p in chosen:
        links = _same_host_links(p, host, policy)
        page_links[p.url] = frozenset(links)
        link_count.update(links.keys())
        for u, a in links.items():
            if a and (u not in anchors or a < anchors[u]):
                anchors[u] = a
        title = p.title or p.og_title
        titles[p.url] = title or ""
        seg_count.update({s for s in _TITLE_SPLIT.split(_norm(title)) if s})
    floor = max(policy.boilerplate_share * n, 2)
    boiler: frozenset[str] = frozenset()
    title_boiler: frozenset[str] = frozenset()
    if n >= policy.min_boilerplate_pages:
        ranked = sorted((u for u, c in link_count.items() if c >= floor),
                        key=lambda u: (-link_count[u], u))  # fmt: skip
        boiler = frozenset(ranked[: policy.max_boilerplate_links])
        segs = sorted((s for s, c in seg_count.items() if c >= floor),
                      key=lambda s: (-seg_count[s], s))  # fmt: skip
        title_boiler = frozenset(segs[: policy.max_boilerplate_segments])
    # Site script: the dominant script of titles with content evidence, else all titles.
    cores = {u: _title_core(t, title_boiler) for u, t in titles.items()}
    scripts: Counter[str] = Counter()
    fallback: Counter[str] = Counter()
    for core in cores.values():
        sc = _script(core)
        if sc is None:
            continue
        fallback[sc] += 1
        if _evidence(core, "", None, vocab).content:
            scripts[sc] += 1
    pool = scripts or fallback
    site_script = min(pool, key=lambda k: (-pool[k], k)) if pool else None
    # What the site's own listing pages list (their non-boilerplate links).
    listed_urls: set[str] = set()
    for u, targets in page_links.items():
        shape = tokenize_url(u, policy)
        if shape is not None and shape.structural:
            listed_urls.update(targets - boiler)
    # Templates over the distinct URLs: pages plus link targets, bounded by hash.
    urls = sorted(set(cores) | set(link_count), key=lambda u: (_h(u), u))
    urls = urls[: policy.max_profile_urls]
    support: Counter[str] = Counter()
    content: Counter[str] = Counter()
    spam: Counter[str] = Counter()
    listed: Counter[str] = Counter()
    shapes: dict[str, UrlShape] = {}
    for u in urls:
        shape = tokenize_url(u, policy)
        if shape is None:
            continue
        shapes.setdefault(shape.template, shape)
        support[shape.template] += 1
        ev = _evidence(cores.get(u) or anchors.get(u, ""), shape.slug_text, site_script, vocab)
        content[shape.template] += bool(ev.content) and not ev.spam
        spam[shape.template] += bool(ev.spam)
        listed[shape.template] += u in listed_urls
    kept = sorted((t for t, c in support.items() if c >= policy.min_template_support),
                  key=lambda t: (-support[t], t))[: policy.max_templates]  # fmt: skip
    return SiteProfile(
        host=host,
        policy=policy.version,
        vocabulary=vocab.version,
        pages=n,
        urls=len(urls),
        templates=tuple(
            TemplateStat(
                t,
                support[t],
                content[t],
                spam[t],
                listed[t],
                _role(shapes[t], (support[t], content[t], spam[t], listed[t]), policy),
            )
            for t in kept
        ),
        boilerplate_links=boiler,
        title_boilerplate=title_boiler,
        script=site_script,
    )


# --------------------------------------------------------------------------- page analysis


@dataclass(frozen=True, slots=True)
class PageKindResult:
    kind: PageKind
    reasons: tuple[str, ...]
    own_links: int
    """Same-host links that are not site boilerplate."""
    own_content_links: int
    """Of those, links into a content or mixed template without spam evidence."""


@dataclass(frozen=True, slots=True)
class RelevanceResult:
    decision: Relevance
    positive: tuple[str, ...]
    negative: tuple[str, ...]
    terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WhatResult:
    """Features for a future WHEN (what.md §4.5). No schedule, rate or priority."""

    url: str
    host: str
    template: str | None
    template_role: TemplateRole | None
    kind: PageKindResult
    relevance: RelevanceResult
    content_links: tuple[str, ...]
    """Own links into the site's content templates, sorted, at most ``max_result_links``."""
    policy: str
    vocabulary: str


def analyze_page(
    facts: PageFacts,
    profile: SiteProfile,
    policy: WhatPolicy = DEFAULT_WHAT_POLICY,
    vocab: Vocabulary | None = None,
) -> WhatResult:
    """Page kind and relevance of one page of ``profile.host`` (what.md §4.3)."""
    vocab = vocab or load_vocabulary()
    shape = tokenize_url(facts.url, policy)
    host = profile.host
    links = _same_host_links(facts, host, policy) if shape and shape.host == host else {}
    own = {u: a for u, a in links.items() if u not in profile.boilerplate_links}
    roles = {t.template: t.role for t in profile.templates}
    pattern = profile.pattern_templates(policy)
    own_content: list[str] = []
    link_pos = link_neg = 0
    for u, a in own.items():
        ls = tokenize_url(u, policy)
        if ls is None:
            continue
        ev = _evidence(a, ls.slug_text, profile.script, vocab)
        link_role = roles.get(ls.template)
        # A link into a template whose members carry content evidence, but which
        # carries none itself, does not match the template's pattern: it counts against.
        bad = (
            bool(ev.spam)
            or link_role is TemplateRole.SPAM
            or (ls.template in pattern and not ev.content)
        )
        link_pos += bool(ev.content) and not bad
        link_neg += bad
        if link_role in CONTENT_ROLES and not bad:
            own_content.append(u)
    core = _title_core(facts.title or facts.og_title, profile.title_boilerplate)
    page_ev = _evidence(core, shape.slug_text if shape else "", profile.script, vocab)
    role = roles.get(shape.template) if shape else None
    kind, kind_reasons = _kind(facts, shape, role, links, own, own_content, core, page_ev,
                               vocab, policy)  # fmt: skip
    expects = bool(shape) and shape.template in pattern  # type: ignore[union-attr]
    relevance = _relevance(
        kind, role, expects, page_ev, len(own_content), link_pos, link_neg, policy
    )
    return WhatResult(
        url=facts.url,
        host=host,
        template=shape.template if shape else None,
        template_role=role,
        kind=PageKindResult(kind, kind_reasons, len(own), len(own_content)),
        relevance=relevance,
        content_links=tuple(sorted(own_content)[: policy.max_result_links]),
        policy=policy.version,
        vocabulary=vocab.version,
    )


def _kind(
    facts: PageFacts,
    shape: UrlShape | None,
    role: TemplateRole | None,
    links: Mapping[str, str],
    own: Mapping[str, str],
    own_content: list[str],
    core: str,
    page_ev: Evidence,
    vocab: Vocabulary,
    p: WhatPolicy,
) -> tuple[PageKind, tuple[str, ...]]:
    if shape is None:
        return PageKind.OTHER, ("malformed_url",)
    if not links and facts.text_chars < p.min_text_chars:
        return PageKind.OTHER, ("empty_page",)
    slug_words = shape.slug_text.split()
    if 0 < len(slug_words) <= 2 and vocab.utility.find(_norm(shape.slug_text)):
        return PageKind.OTHER, ("utility_page",)
    markers: list[str] = []
    if shape.pagination:
        markers.append("pagination")
    if shape.search:
        markers.append("search")
    if shape.listing:
        markers.append("listing_path")
    if shape.date_archive:
        markers.append("date_archive")
    if shape.root and links:
        markers.append("site_root")
    if any(rx.search(_norm(facts.title or facts.og_title)) for rx in vocab.hub_titles):
        markers.append("hub_title")
    if len(own) >= p.hub_min_own_links:
        # An item page (its own title/slug names an item) with related-post or
        # in-text links stays a detail unless it links a hub's volume of content.
        item = bool(page_ev.content) and not markers and role is not TemplateRole.LISTING
        if item and len(own_content) < p.hub_strong_links:
            return PageKind.DETAIL, ("item_title", "related_links")
        extra = ["content_template_links"] if len(own_content) * 2 >= len(own) else []
        return PageKind.HUB, ("many_page_links", *extra, *markers)
    if markers:
        return PageKind.HUB, (*markers, "few_page_links")
    if (
        not shape.root
        and len(core.split()) <= 3
        and vocab.utility.find(core)
        and not page_ev.content
    ):
        return PageKind.OTHER, ("utility_page",)
    reasons: list[str] = []
    if role in CONTENT_ROLES:
        reasons.append(f"{role}_template")
    if page_ev.content:
        reasons.append("item_title")
    if facts.og_type in ("article", "video.movie", "video.episode", "music.album"):
        reasons.append("article_metadata")
    if facts.published:
        reasons.append("published_date")
    if reasons:
        return PageKind.DETAIL, tuple(reasons)
    if facts.text_chars >= p.min_text_chars:
        return PageKind.DETAIL, ("single_page_site" if shape.root else "text_page",)
    return PageKind.OTHER, ("no_signal",)


def _relevance(
    kind: PageKind,
    role: TemplateRole | None,
    expects: bool,
    page_ev: Evidence,
    own_content: int,
    link_pos: int,
    link_neg: int,
    p: WhatPolicy,
) -> RelevanceResult:
    pos = list(page_ev.content)
    neg = list(page_ev.spam)
    if role is TemplateRole.CONTENT:
        pos.append("content_template")
    elif role is TemplateRole.SPAM:
        neg.append("spam_template")
    if expects and kind is PageKind.DETAIL and not page_ev.content:
        # The page misses its template's content pattern. (A match is not a second
        # positive: it would restate the page's own evidence.)
        neg.append("template_mismatch")
    if kind is PageKind.HUB:
        if link_pos >= p.hub_min_link_evidence and link_pos >= 2 * link_neg:
            pos.append("content_links")
        elif link_neg >= p.hub_min_link_evidence and link_neg >= 2 * link_pos:
            neg.append("spam_links")
        if own_content >= p.hub_min_link_evidence and own_content >= 2 * link_neg:
            pos.append("content_template_links")
    if len(neg) >= 2 or (neg and not pos):
        decision = Relevance.SPAM
    elif (len(pos) >= 2 and not neg) or (len(pos) >= 3 and len(neg) == 1):
        decision = Relevance.RELEVANT
    else:
        decision = Relevance.UNKNOWN
    return RelevanceResult(decision, tuple(pos), tuple(neg), page_ev.terms[: p.max_terms])
