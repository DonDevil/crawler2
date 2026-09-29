"""M1 closed loop on the fixture web (P6 design §1, §10; test plan #11, #21, #23).

seeds → P3 frontier → P4 fetch + record → outbox relay → P5 extraction →
urls.discovered → two P6 admission consumers (one group) → frontier → …
until the loop is quiescent. Real Redis, Scylla and MinIO; the fixture web
serves two hosts (127.0.0.1 rooted, localhost external) from one server.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import redis
from antipiracy_contracts.events.web import PageObserved, UrlsDiscovered
from antipiracy_contracts.models.web import UrlRef

from crawler2.core.configuration import (
    DiscoverySettings,
    EventSettings,
    ExecutionQueue,
    FrontierSettings,
    Settings,
)
from crawler2.core.configuration.settings import HttpFetchSettings
from crawler2.crawlers.http import HttpFetcher
from crawler2.crawlers.model import FetchRequest
from crawler2.crawlers.recorder import StorageRecorder
from crawler2.discovery.admission import Admitter, LinkAdmissionService, Scope
from crawler2.discovery.cli import AdmissionLoop
from crawler2.discovery.seeds import load_seeds
from crawler2.extraction.archival import ArchivalProfile
from crawler2.extraction.cli import ExtractionLoop
from crawler2.extraction.service import PageIntelligenceService
from crawler2.filtering.builtin import built_in_rules
from crawler2.filtering.model import Classification, Policy, Rule, RuleKind, RuleSource
from crawler2.filtering.store import RulesetHolder, SourceImport, publish, store_source
from crawler2.frontier.redis import RedisFrontier, connect_redis
from crawler2.storage.events.consumer import IdempotentConsumer, RedisStreamReader
from crawler2.storage.events.publisher import RedisStreamPublisher, stream_name
from crawler2.storage.events.relay import OutboxRelay
from crawler2.storage.objectstore import S3ObjectStore
from crawler2.storage.scylla import ScyllaStorage
from tests.fixtures.web import Dynamic, FixtureHandler, serve

pytestmark = pytest.mark.integration
WORKER = "it-1:http:1:" + "0" * 32
HTTP = HttpFetchSettings(connect_timeout_s=2, read_timeout_s=2, total_timeout_s=4)


def _html(*links: str) -> bytes:
    anchors = "".join(f'<li><a href="{u}">{u}</a></li>' for u in links)
    return f"<html><body><main><h1>page</h1><ul>{anchors}</ul></main></body></html>".encode()


def _routes(port_ref: list[int]) -> dict[str, Dynamic]:
    """Pages whose absolute links need the server's own port (known after start)."""

    def page(*links: str) -> Dynamic:
        def handle(h: FixtureHandler) -> None:
            port = port_ref[0]
            body = _html(*(u.format(port=port) for u in links))
            h.send_response(200)
            h.send_header("Content-Type", "text/html; charset=utf-8")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.write_body(body)

        return Dynamic(handle)

    return {
        "/": page(
            "/a",
            "/b",
            "/a",
            "http://localhost:{port}/ext",
            "http://ads.test/x",
            "https://www.facebook.com/sharer/sharer.php?u=x",
        ),
        "/a": page("/b", "/c", "/"),
        "/b": page("/a"),
        "/c": page("/a?page=2"),
        "/ext": page("http://localhost:{port}/ext2", "/b"),
        "/ext2": page(),
    }


def _ruleset(storage: ScyllaStorage, name: str) -> RulesetHolder:
    now = datetime.now(UTC) - timedelta(minutes=1)
    ad = Rule(
        source=RuleSource.OPERATOR,
        kind=RuleKind.HOST,
        pattern="ads.test",
        classification=Classification.AD,
        confidence=1.0,
        reason="ad_network",
        note="m1 fixture",
    )
    repo = storage.filter_rules
    builtin = store_source(
        repo,
        SourceImport(RuleSource.BUILT_IN, built_in_rules(), "b", "s", "t", {}),
        by="it",
        at=now,
    )
    op = store_source(
        repo, SourceImport(RuleSource.OPERATOR, [ad], "o", "s", "t", {}), by="it", at=now
    )
    record, _ = publish(
        repo, [("built_in", builtin.revision), ("operator", op.revision)], Policy(), by="it", at=now
    )
    assert repo.activate(record.ruleset_id, expected=None, by="it", at=now, name=name)
    holder = RulesetHolder(repo, name=name)
    holder.refresh()
    return holder


def test_m1_closed_loop_on_the_fixture_web(
    storage: ScyllaStorage,
    object_store: S3ObjectStore,
    redis_client: redis.Redis,
    event_settings: EventSettings,
    tmp_path: Path,
) -> None:
    port_ref = [0]
    name = f"m1-{uuid.uuid4().hex[:8]}"
    settings = Settings(events=event_settings)
    frontier = RedisFrontier(
        connect_redis(settings.redis),
        FrontierSettings(default_interval_s=0.0, base_backoff_s=0.0, max_backoff_s=0.0),
        namespace=f"p6-{uuid.uuid4().hex[:12]}",
    )
    holder = _ruleset(storage, name)
    admitter = Admitter(
        frontier=frontier,
        discovery=storage.discovery,
        holder=holder,
        settings=DiscoverySettings(),
        decision_ttl_s=3600,
    )
    scope = Scope(storage.discovery, refresh_s=0.0)
    relay = OutboxRelay(
        storage.outbox,
        RedisStreamPublisher(redis_client, event_settings),
        relay_id=name,
        settle_s=600,
    )
    extraction_reader = RedisStreamReader(
        redis_client, stream_name(event_settings, PageObserved.EVENT_TYPE, 1), "extraction", WORKER
    )
    extraction_reader.ensure_group()
    extraction = ExtractionLoop(
        extraction_reader,
        IdempotentConsumer(
            f"extraction-{name}",
            PageObserved,
            PageIntelligenceService(
                objects=object_store,
                links=storage.links,
                urls=storage.urls,
                pages=storage.page_intel,
                instance=WORKER,
                profile=ArchivalProfile(sample_rate=0.0),
            ).handle,
            storage.processed_events,
        ),
        settings,
    )
    service = LinkAdmissionService(admitter, scope, storage.pages, storage.discovery, holder)
    admission = []
    for consumer in ("adm-a", "adm-b"):
        reader = RedisStreamReader(
            redis_client,
            stream_name(event_settings, UrlsDiscovered.EVENT_TYPE, 1),
            f"discovery-{name}",
            consumer,
        )
        reader.ensure_group()
        admission.append(
            AdmissionLoop(
                reader,
                IdempotentConsumer(
                    f"discovery-{name}", UrlsDiscovered, service.handle, storage.processed_events
                ),
                settings,
                holder,
            )
        )
    recorder = StorageRecorder(
        storage.fetch_attempts,
        storage.pages,
        object_store,
        worker=WORKER,
        interceptions=storage.discovery,
    )

    async def fetch(url: str, ref: UrlRef) -> object:
        fetcher = HttpFetcher(HTTP)
        try:
            return await fetcher.fetch(FetchRequest(url, ref.url_id))
        finally:
            await fetcher.close()

    fetched: list[str] = []
    handled = {"adm-a": 0, "adm-b": 0}
    try:
        with serve(_routes(port_ref)) as site:
            port_ref[0] = int(site.base_url.rsplit(":", 1)[1])
            seeds = tmp_path / "seeds.txt"
            seeds.write_text(site.url("/") + "\n")
            report = load_seeds(
                seeds,
                source=name,
                metadata={"test": "m1"},
                admitter=admitter,
                scope=scope,
                repo=storage.discovery,
            )
            assert report.outcomes == {"admitted": 1}
            for _ in range(12):
                progress = 0
                while (claim := frontier.claim(ExecutionQueue.HTTP)) is not None:
                    ref = UrlRef.of(claim.url)
                    started = datetime.now(UTC) - timedelta(seconds=1)
                    result = asyncio.run(fetch(claim.url, ref))
                    recorder.record(ref, result, started_at=started, finished_at=datetime.now(UTC))  # type: ignore[arg-type]
                    frontier.complete(claim)
                    fetched.append(claim.url)
                    progress += 1
                relay.run_once()
                while extraction.run_once():
                    progress += 1
                relay.run_once()
                for loop, consumer in zip(admission, handled, strict=True):
                    count = loop.run_once()
                    handled[consumer] += count
                    progress += count
                for loop, consumer in zip(admission, handled, strict=True):
                    while count := loop.run_once():
                        handled[consumer] += count
                        progress += count
                if progress == 0:
                    break
            base, ext = site.base_url, f"http://localhost:{port_ref[0]}"
            assert sorted(fetched) == sorted(
                [
                    f"{base}/",
                    f"{base}/a",
                    f"{base}/b",
                    f"{base}/c",
                    f"{base}/a?page=2",
                    f"{ext}/ext",
                ]
            ), "every in-scope page once, the external link once as a leaf, nothing else"
            assert not site.state.requests_to("/ext2")
    finally:
        problems = frontier.audit()
        frontier.clear()
    assert problems == []
    assert sum(handled.values()) >= 5  # one urls.discovered per fetched page with links
    discovery = storage.discovery
    ads = discovery.decisions(UrlRef.of("http://ads.test/x").url_id)
    assert [(d.outcome, d.action) for d in ads] == [("blocked", "block")]
    social = discovery.decisions(UrlRef.of("https://www.facebook.com/sharer/sharer.php?u=x").url_id)
    assert '"reason": "out_of_scope"' in social[0].decision
    leaf = discovery.admission_states([UrlRef.of(f"http://localhost:{port_ref[0]}/ext").url_id])
    assert next(iter(leaf.values())).last_origin == "leaf"
    assert "127.0.0.1" in {s.domain for s in discovery.scope_sites()}
    assert discovery.seeds(name)[0].metadata == {"test": "m1"}
