"""Build ``FilterInput``s from P1 URLs, links, browser requests and redirects (design §3, §4).

The registrable domain (eTLD+1) decides third-party relationships. P1
keeps it out of identity (``DomainId`` is the host); here it is a derived
attribute from the bundled Public Suffix List, cached per host.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from importlib.metadata import version
from typing import Final

from antipiracy_contracts.models.web import LinkRelation
from publicsuffixlist import PublicSuffixList

from crawler2.filtering.model import Context, FilterInput, resource_type_of

PSL_VERSION: Final = version("publicsuffixlist")
"""Part of every ruleset id: a PSL change can change third-party answers."""

_PSL: Final = PublicSuffixList()

LINK_RESOURCE_TYPE: Final = {
    LinkRelation.ANCHOR: "document",
    LinkRelation.META_REFRESH: "document",
    LinkRelation.CANONICAL: "document",
    LinkRelation.OTHER: "document",
    LinkRelation.IFRAME: "subdocument",
    LinkRelation.SCRIPT_LITERAL: "other",
}


def host_of(url: str) -> str | None:
    """Lowercase host of an absolute http(s)/ws(s) URL without port or userinfo, else None."""
    scheme_end = url.find("://")
    if scheme_end <= 0 or url[:scheme_end].lower() not in ("http", "https", "ws", "wss"):
        return None
    start = scheme_end + 3
    end = len(url)
    for sep in "/?#":
        i = url.find(sep, start)
        if i != -1 and i < end:
            end = i
    authority = url[start:end]
    at = authority.rfind("@")
    if at != -1:
        authority = authority[at + 1 :]
    if authority.startswith("["):
        close = authority.find("]")
        host = authority[1:close] if close != -1 else ""
    else:
        host = authority.partition(":")[0]
    host = host.rstrip(".").lower()
    return host or None


@lru_cache(maxsize=65_536)
def registrable_domain(host: str) -> str:
    """eTLD+1 of ``host``; IP addresses and hosts without a known suffix are their own."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return host
    return _PSL.privatesuffix(host) or host


def is_third_party(host: str, source_host: str | None) -> bool | None:
    if source_host is None:
        return None
    if host == source_host:
        return False
    return registrable_domain(host) != registrable_domain(source_host)


def link_input(
    url: str, *, source_url: str | None = None, relation: LinkRelation = LinkRelation.ANCHOR
) -> FilterInput | None:
    """A discovered link (source page = first party), or a seed/search URL (no source)."""
    host = host_of(url)
    if host is None:
        return None
    source_host = host_of(source_url) if source_url else None
    return FilterInput(
        context=Context.LINK,
        url=url,
        host=host,
        resource_type=LINK_RESOURCE_TYPE[relation],
        source_host=source_host,
        third_party=is_third_party(host, source_host),
        field="link",
    )


def request_input(url: str, playwright_type: str, page_url: str | None) -> FilterInput | None:
    """A browser request; the first party is the page (main frame) that issued it."""
    host = host_of(url)
    if host is None:
        return None
    source_host = host_of(page_url) if page_url else None
    return FilterInput(
        context=Context.REQUEST,
        url=url,
        host=host,
        resource_type=resource_type_of(playwright_type),
        source_host=source_host,
        third_party=is_third_party(host, source_host),
        field="request",
    )


def redirect_inputs(requested_url: str, hops: list[str], final_url: str) -> list[FilterInput]:
    """Every redirect hop location and the final URL, relative to the requested URL.

    ``hops`` are the ``Location`` targets in order (P1 ``RedirectHop.location``);
    the final URL is added only when it differs from the last hop.
    """
    source_host = host_of(requested_url)
    targets = [(f"redirect_hop[{i}]", url) for i, url in enumerate(hops)]
    if not hops or hops[-1] != final_url:
        targets.append(("final", final_url))
    inputs = []
    for name, url in targets:
        host = host_of(url)
        if host is None:
            continue
        inputs.append(
            FilterInput(
                context=Context.REDIRECT,
                url=url,
                host=host,
                resource_type="document",
                source_host=source_host,
                third_party=is_third_party(host, source_host),
                field=name,
            )
        )
    return inputs
