"""Run operator queries through the search adapters and admit the results (design §15).

Queries come only from an operator query file; nothing here derives a
query from a target. Every result is recorded with its provenance (F10)
before filtering and admission. An engine that answers with a CAPTCHA or
verification page sits out ``blocked_cooldown_queries`` queries (V1
behaviour); nothing is retried, solved or evaded.
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
from antipiracy_contracts.models.web import UrlRef
from antipiracy_contracts.urls import InvalidUrlError

from crawler2.core.configuration import SearchSettings
from crawler2.core.observability import Metrics, get_logger
from crawler2.discovery.admission import Admitter, Candidate, Origin, Outcome, Scope
from crawler2.discovery.search.adapters import (
    ADAPTERS,
    PageStatus,
    SearchAdapter,
    SearchHit,
    SearchPage,
)
from crawler2.filtering.inputs import host_of
from crawler2.storage.repositories import DiscoveryRepository, SearchResultRecord

_log = get_logger("discovery.search")

Fetch = Callable[[str, dict[str, str]], tuple[int, str, bytes]]
"""(url, params) → (status, final url, body)."""


def read_queries(path: Path) -> tuple[list[str], str]:
    """Operator query file: one query per line, ``#`` comments; returns (queries, sha256)."""
    raw = path.read_bytes()
    queries = []
    for line in raw.decode("utf-8").splitlines():
        text = " ".join(line.split())
        if text and not text.startswith("#") and text not in queries:
            queries.append(text[:300])
    return queries, hashlib.sha256(raw).hexdigest()


def http_fetch(timeout_s: float, user_agent: str, proxy: str | None = None) -> Fetch:
    client = httpx.Client(
        timeout=timeout_s,
        follow_redirects=True,
        headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"},
        proxy=proxy,
    )

    def fetch(url: str, params: dict[str, str]) -> tuple[int, str, bytes]:
        response = client.get(url, params=params)
        return response.status_code, str(response.url), response.content[: 2 * 1024 * 1024]

    return fetch


@dataclass
class QueryReport:
    query: str
    engines: dict[str, str] = field(default_factory=dict)
    """engine → results | empty | blocked | parse_error | error | cooldown | unavailable"""
    hits: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)


class SearchRunner:
    def __init__(
        self,
        settings: SearchSettings,
        *,
        fetchers: dict[str, Fetch],
        admitter: Admitter,
        scope: Scope,
        repo: DiscoveryRepository,
        metrics: Metrics | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        unknown = [e for e in settings.engines if e not in ADAPTERS]
        if unknown:
            raise ValueError(f"unknown search engines: {unknown}")
        self._s = settings
        self._adapters: list[SearchAdapter] = [ADAPTERS[e]() for e in settings.engines]
        self._fetchers = fetchers
        self._admitter = admitter
        self._scope = scope
        self._repo = repo
        self._clock = clock
        self._sleep = sleep
        self._cooldown: dict[str, int] = {}
        self._queries = (
            metrics.counter(
                "search_queries_total", "Engine queries by result", ["engine", "result"]
            )
            if metrics is not None
            else None
        )

    def run(self, queries: Sequence[str]) -> list[QueryReport]:
        return [self.run_query(q) for q in queries]

    def run_query(self, query: str) -> QueryReport:
        report = QueryReport(query)
        records: list[SearchResultRecord] = []
        candidates: dict[str, tuple[Candidate, str]] = {}
        for adapter in self._adapters:
            status, page = self._search(adapter, query)
            report.engines[adapter.name] = status
            if self._queries is not None:
                self._queries.labels(adapter.name, status).inc()
            now = self._clock()
            for hit in page.hits if page else ():
                ref = _ref(hit)
                records.append(_record(query, adapter, hit, ref, now))
                if ref is not None and str(ref.url_id) not in candidates:
                    candidates[str(ref.url_id)] = (Candidate(ref, Origin.SEARCH), adapter.name)
        report.hits = len(records)
        self._repo.record_search_results(records)
        outcomes = self._admitter.admit([c for c, _ in candidates.values()], revisit_after_s=0.0)
        report.outcomes = dict(sorted(Counter(o.value for o in outcomes.values()).items()))
        rooted = [
            host_of(c.url.url) or ""
            for c, _ in candidates.values()
            if outcomes.get(c.url.url_id) not in (Outcome.BLOCKED, Outcome.INVALID, None)
        ]
        self._scope.add(rooted, origin="search", at=self._clock(), detail=query[:200])
        return report

    def _search(self, adapter: SearchAdapter, query: str) -> tuple[str, SearchPage | None]:
        remaining = self._cooldown.get(adapter.name, 0)
        if remaining > 0:
            self._cooldown[adapter.name] = remaining - 1
            return "cooldown", None
        fetch = self._fetchers.get(adapter.network)
        if fetch is None:
            return "unavailable", None
        try:
            prelude = None
            if adapter.prelude_url:
                _, _, prelude = fetch(adapter.prelude_url, {})
            request = adapter.build_request(query, prelude)
            status, final_url, body = fetch(request.url, request.params)
        except httpx.HTTPError as exc:
            _log.warning("search_engine_error", engine=adapter.name, error=type(exc).__name__)
            return "error", None
        finally:
            self._sleep(self._s.pause_s)
        page = adapter.parse(status, final_url, body, max_results=self._s.max_results)
        if page.status is PageStatus.BLOCKED:
            self._cooldown[adapter.name] = self._s.blocked_cooldown_queries
            _log.warning("search_engine_blocked", engine=adapter.name, detail=page.detail)
        return page.status.value, page


def _ref(hit: SearchHit) -> UrlRef | None:
    try:
        ref = UrlRef.of(hit.url)
    except (InvalidUrlError, ValueError):
        return None
    return ref if host_of(ref.url) is not None else None


def _record(
    query: str, adapter: SearchAdapter, hit: SearchHit, ref: UrlRef | None, now: datetime
) -> SearchResultRecord:
    return SearchResultRecord(
        query=query,
        engine=adapter.name,
        adapter_version=adapter.version,
        rank=hit.rank,
        returned_url=hit.returned_url[:2000],
        url=ref.url if ref is not None else None,
        title=hit.title,
        snippet=hit.snippet,
        retrieved_at=now,
        outcome="canonical" if ref is not None else "invalid",
    )
