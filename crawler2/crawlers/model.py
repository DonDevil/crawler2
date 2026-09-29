"""P4 fetch vocabulary: one attempt in (``FetchRequest``), facts out (``FetchResult``).

A fetcher performs exactly one attempt and reports what happened. It does
not retry, choose another capability, touch the frontier or storage, or
judge the site (P4 design §7-§9, ADR-017). ``FetchResult`` is crawler2
runtime vocabulary; the recorder projects it onto the P1 contracts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from antipiracy_contracts.ids import UrlId
from antipiracy_contracts.models.web import FetchCapability, HttpValidators


class Outcome(StrEnum):
    """How one attempt ended: facts, never strategy (design §9)."""

    OK = "ok"
    NOT_MODIFIED = "not_modified"
    HTTP_ERROR = "http_error"
    NEEDS_JS = "needs_js"
    BLOCKED = "blocked"
    CAPTCHA = "captcha"
    MEDIA = "media"
    TIMEOUT = "timeout"
    DNS_ERROR = "dns_error"
    NETWORK_ERROR = "network_error"
    TLS_ERROR = "tls_error"
    REDIRECT_ERROR = "redirect_error"
    INVALID_RESPONSE = "invalid_response"
    TOO_LARGE = "too_large"
    PROXY_UNAVAILABLE = "proxy_unavailable"
    FETCHER_UNAVAILABLE = "fetcher_unavailable"
    FETCHER_CRASH = "fetcher_crash"
    CANCELLED = "cancelled"

    @property
    def has_response(self) -> bool:
        return self in _RESPONSE_OUTCOMES

    @property
    def has_content(self) -> bool:
        """The attempt produced page content or a probed media response (gate "success")."""
        return self in (Outcome.OK, Outcome.NOT_MODIFIED, Outcome.MEDIA)


_RESPONSE_OUTCOMES = frozenset(
    {
        Outcome.OK,
        Outcome.NOT_MODIFIED,
        Outcome.HTTP_ERROR,
        Outcome.NEEDS_JS,
        Outcome.BLOCKED,
        Outcome.CAPTCHA,
        Outcome.MEDIA,
    }
)

AMBIGUOUS_OUTCOMES = frozenset({Outcome.TIMEOUT, Outcome.DNS_ERROR, Outcome.NETWORK_ERROR})
"""Could be the target or the local network: they feed the network-health trigger (V1 N2 §5)."""


class Expectation(StrEnum):
    PAGE = "page"
    MEDIA_PROBE = "media_probe"


class Conditional(StrEnum):
    NONE = "none"
    SENT = "sent"
    NOT_MODIFIED = "not_modified"


class BodyKind(StrEnum):
    HTML = "html"
    MANIFEST = "manifest"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class FetchRequest:
    url: str
    url_id: UrlId
    attempt: int = 1
    validators: HttpValidators | None = None
    expect: Expectation = Expectation.PAGE


@dataclass(frozen=True, slots=True)
class Hop:
    """One redirect response: its status and the absolute URL it pointed to."""

    status: int
    location: str


@dataclass(frozen=True, slots=True)
class MediaProbe:
    """What a media response revealed without its body (D2; persisted by P8)."""

    content_type: str | None
    declared_length: int | None
    total_length: int | None
    """From ``Content-Range: bytes a-b/total`` when the server honoured Range."""
    accept_ranges: str | None
    range_requested: str | None
    range_honoured: bool
    etag: str | None
    last_modified: str | None
    bytes_read: int
    container: str | None
    """Magic-number sniff of the first bytes: mp4, webm, mpegts, flv, mp3 or None."""


@dataclass(frozen=True, slots=True)
class RenderMetrics:
    requests: int = 0
    requests_aborted: int = 0
    """Aborted by the resource policy or the interception hook."""
    media_requests: int = 0
    transferred_bytes: int = 0
    """Encoded body bytes of every response the page loaded (Playwright sizes)."""
    context_pages: int = 0
    """Pages this context had served, including this one."""
    browser_pages: int = 0
    browser_restarts: int = 0


@dataclass(frozen=True, slots=True)
class Timings:
    total_s: float = 0.0
    ttfb_s: float | None = None
    """Until the last response's headers."""
    acquire_s: float | None = None
    """Browser: waiting for a page slot."""
    navigation_s: float | None = None
    dom_ready_s: float | None = None


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    kind: str
    """Exception type name (``ReadTimeout``, ``TargetClosedError``, …)."""
    message: str
    """Short, credential-free description."""


@dataclass(frozen=True, slots=True)
class FetchResult:
    outcome: Outcome
    capability: FetchCapability
    requested_url: str
    final_url: str | None = None
    redirects: tuple[Hop, ...] = ()
    status: int | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    content_type: str | None = None
    content_length: int | None = None
    body: bytes | None = None
    """Decoded page body (HTML, manifest), bounded; never set for media."""
    body_kind: BodyKind | None = None
    bytes_read: int = 0
    """Network bytes read for the whole attempt (all hops, before decompression)."""
    validators: HttpValidators | None = None
    conditional: Conditional = Conditional.NONE
    media: MediaProbe | None = None
    timings: Timings = field(default_factory=Timings)
    render: RenderMetrics | None = None
    error: ErrorInfo | None = None
    signals: tuple[str, ...] = ()
    """Markers behind needs_js/captcha/blocked, for P7 and debugging."""

    @property
    def detail(self) -> str:
        """Compact code for ``FetchAttempt.detail`` (P1 ``ShortText``)."""
        parts = [f"outcome={self.outcome.value}"]
        if self.signals:
            parts.append("signals=" + ",".join(self.signals[:4]))
        if self.error is not None:
            parts.append(f"error={self.error.kind}")
        return ";".join(parts)[:200]


class FetcherState(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class FetcherHealth:
    state: FetcherState
    reason: str = ""


class FetcherUnavailableError(Exception):
    """The fetcher cannot run an attempt at all (not the target's fault)."""


class Fetcher(Protocol):
    """One attempt per call; never raises for target behaviour (design §7)."""

    @property
    def capability(self) -> FetchCapability: ...

    @property
    def total_timeout_s(self) -> float: ...

    async def start(self) -> None: ...

    async def fetch(self, request: FetchRequest) -> FetchResult: ...

    def health(self) -> FetcherHealth: ...

    async def close(self) -> None: ...
