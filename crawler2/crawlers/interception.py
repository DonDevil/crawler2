"""Browser request-interception hook (P4 design §19).

The browser pool asks an interceptor about every request the page makes
(after its fixed resource policy). P4 ships only ``AllowAll``: no rules,
no blocklists, no host heuristics. The P6 filter engine implements
``RequestInterceptor`` and is plugged in by configuration; its decisions
are counted in the render metrics.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class InterceptAction(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class InterceptedRequest:
    url: str
    resource_type: str
    """Playwright resource type: document, script, xhr, fetch, stylesheet, image, media, …"""
    is_navigation: bool
    frame_url: str
    method: str


@dataclass(frozen=True, slots=True)
class InterceptDecision:
    action: InterceptAction
    rule_id: str | None = None
    reason: str | None = None


ALLOW = InterceptDecision(InterceptAction.ALLOW)


class RequestInterceptor(Protocol):
    def decide(self, request: InterceptedRequest) -> InterceptDecision:
        """Must be fast and must not block: it runs for every sub-request."""
        ...


class AllowAll:
    """The P4 default: observe nothing, block nothing."""

    def decide(self, request: InterceptedRequest) -> InterceptDecision:
        return ALLOW
