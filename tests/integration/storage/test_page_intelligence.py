"""P5 on the real stack: V002 repository semantics and the full extraction loop
(page.observed → relay → Redis → crawler2-extract → rows + page.changed/urls/media events)."""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta
from typing import Any

import redis
from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events import decode_event, encode_event
from antipiracy_contracts.events.web import PageChanged, PageObserved
from antipiracy_contracts.ids import ObservationId, PageRevisionId, PageVersionId
from antipiracy_contracts.models.web import UrlRef
from antipiracy_contracts.ownership import Component

from crawler2.core.configuration import EventSettings, Settings
from crawler2.extraction.archival import ArchivalProfile
from crawler2.extraction.cli import ExtractionLoop
from crawler2.extraction.service import PageIntelligenceService
from crawler2.storage.events.consumer import IdempotentConsumer, RedisStreamReader
from crawler2.storage.events.publisher import RedisStreamPublisher, stream_name
from crawler2.storage.events.relay import OutboxRelay
from crawler2.storage.objectstore import S3ObjectStore
from crawler2.storage.repositories import (
    ExtractRecord,
    RetentionDecision,
    RevisionSighting,
    SnapshotDecision,
)
from crawler2.storage.scylla import ScyllaStorage
from tests.fixtures.contracts import T0, event_of, page_observation

INSTANCE = "dev-1:extraction:1:" + "0" * 32


def _page() -> UrlRef:
    return UrlRef.of(f"https://site.example/{uuid.uuid4().hex}/watch")


def _html(text: str, ad: str = "ad") -> bytes:
    return (
        f"<html><head><title>{text}</title></head><body><main><h1>{text}</h1>"
        f'<a href="/next">next</a><video src="/v/{text}.m3u8"></video></main>'
        f"<aside>{ad}</aside></body></html>"
    ).encode()


def _service(storage: ScyllaStorage, objects: S3ObjectStore) -> PageIntelligenceService:
    return PageIntelligenceService(
        objects=objects,
        links=storage.links,
        urls=storage.urls,
        pages=storage.page_intel,
        instance=INSTANCE,
        profile=ArchivalProfile(sample_rate=0.0),  # deterministic: no random retention
    )


def _observe(
    storage: ScyllaStorage, objects: S3ObjectStore, page: UrlRef, body: bytes, minutes: int
) -> Any:
    at = T0 + timedelta(minutes=minutes)
    snapshot = objects.put(body, media_type="text/html")
    obs = page_observation(page, at=at, body=body, snapshot=snapshot, seed=uuid.uuid4().hex)
    event = event_of(PageObserved(observation=obs), Component.CRAWLER_WORKER, at=at)
    storage.pages.record(obs, event=event)
    return event


def _stream(
    client: redis.Redis, settings: EventSettings, event_type: str, page: UrlRef
) -> list[Any]:
    """Events of one page only: the relay also publishes other tests' outbox rows."""
    entries: Any = client.xrange(stream_name(settings, event_type, 1))
    events = [decode_event(fields[b"envelope"]) for _, fields in entries]
    return [e for e in events if getattr(e.payload, "page", None) == page]


def _drain(loop: ExtractionLoop) -> int:
    total = 0
    while handled := loop.run_once():
        total += handled
    return total


def test_revision_rows_converge_regardless_of_order_and_replay(storage: ScyllaStorage) -> None:
    page = _page()
    digest = ContentDigest.of_bytes(b"normalized")
    revision = PageRevisionId.of(page.url_id, "html-normalized/v1", digest)
    sightings = [
        RevisionSighting(
            url_id=page.url_id,
            revision_id=revision,
            normalization="html-normalized/v1",
            normalized_digest=digest,
            observation_id=ObservationId.new(),
            page_version_id=PageVersionId.of(page.url_id, ContentDigest.of_bytes(bytes([i]))),
            observed_at=T0 + timedelta(minutes=m),
        )
        for i, m in enumerate([30, 5, 60, 5])
    ]
    for s in [*sightings, sightings[0]]:  # out of order + replay
        storage.page_intel.record_sighting(s)
    (row,) = storage.page_intel.revisions(page.url_id)
    assert (row.first_seen, row.last_seen) == (
        T0 + timedelta(minutes=5),
        T0 + timedelta(minutes=60),
    )
    assert row.last_observation_id == sightings[2].observation_id
    assert storage.page_intel.revision(page.url_id, revision) == row


def test_extract_and_retention_round_trip(storage: ScyllaStorage) -> None:
    page = _page()
    digest = ContentDigest.of_bytes(uuid.uuid4().bytes)
    record = ExtractRecord(
        page_version_id=PageVersionId.of(page.url_id, digest),
        url_id=page.url_id,
        extractor="p5-extract/v1",
        normalization="html-normalized/v1",
        revision_id=PageRevisionId.of(page.url_id, "html-normalized/v1", digest),
        normalized_digest=digest,
        observed_at=T0,
        doc='{"x": 1}',
    )
    storage.page_intel.record_extract(record)
    storage.page_intel.record_extract(record)
    assert storage.page_intel.extract(record.page_version_id) == record
    decision = RetentionDecision(
        digest=digest,
        observation_id=ObservationId.new(),
        decision=SnapshotDecision.SAMPLED_OUT,
        reason="sampled_out",
        decided_at=T0,
    )
    storage.page_intel.record_retention(decision)
    assert storage.page_intel.retention(digest) == [decision]


def test_full_loop_versions_last_seen_events_and_idempotent_replay(
    storage: ScyllaStorage,
    object_store: S3ObjectStore,
    redis_client: redis.Redis,
    event_settings: EventSettings,
) -> None:
    page = _page()
    publisher = RedisStreamPublisher(redis_client, event_settings)
    relay = OutboxRelay(storage.outbox, publisher, relay_id="p5-it", settle_s=600)
    settings = Settings(events=event_settings)
    reader = RedisStreamReader(
        redis_client,
        stream_name(event_settings, "page.observed", 1),
        "extraction",
        INSTANCE,
    )
    reader.ensure_group()
    consumer = IdempotentConsumer(
        f"extraction-{uuid.uuid4().hex[:8]}",
        PageObserved,
        _service(storage, object_store).handle,
        storage.processed_events,
    )
    loop = ExtractionLoop(reader, consumer, settings)

    first = _observe(storage, object_store, page, _html("Episode 1", ad="ad-1"), 0)
    others = [
        _observe(storage, object_store, page, _html("Episode 1", ad="ad-2 rotated"), 10),  # raw
        _observe(storage, object_store, page, _html("Episode 1", ad="ad-2 rotated"), 20),  # same
        _observe(storage, object_store, page, _html("Episode 2"), 30),  # meaningful
    ]
    relay.run_once()
    assert _drain(loop) >= 4
    relay.run_once()
    # Replay: the same page.observed published again is skipped by the consumer.
    redis_client.xadd(
        stream_name(event_settings, "page.observed", 1), {b"envelope": encode_event(first)}
    )
    assert _drain(loop) == 1
    relay.run_once()

    revisions = storage.page_intel.revisions(page.url_id)
    assert len(revisions) == 2
    older = revisions[1]
    assert (older.first_seen, older.last_seen) == (T0, T0 + timedelta(minutes=20))
    changed = [e.payload for e in _stream(redis_client, event_settings, "page.changed", page)]
    assert all(isinstance(p, PageChanged) for p in changed)
    assert {p.revision_id for p in changed} == {r.revision_id for r in revisions}
    assert len(changed) == 2
    media = _stream(redis_client, event_settings, "media.discovered", page)
    assert len(media) == 4  # one per eligible observation (P8 sightings)
    urls = _stream(redis_client, event_settings, "urls.discovered", page)
    assert len(urls) == 3  # one per new raw version
    version = first.payload.observation.page_version_id
    assert [link.target.url for link in storage.links.links_of(version)] == [
        "https://site.example/next"
    ]
    assert storage.urls.get(UrlRef.of("https://site.example/next").url_id) is not None
    assert storage.page_intel.extract(version) is not None
    # The relay publishes in shard order, so which sighting of a revision is processed first
    # is arbitrary; exactly one decision per revision says "new_revision" (design §12-§13).
    digests = {e.payload.observation.snapshot.digest for e in [first, *others]}
    reasons = [d.reason for digest in digests for d in storage.page_intel.retention(digest)]
    assert sorted(reasons) == ["new_revision", "new_revision", "sampled_out", "sampled_out"]


def test_concurrent_consumers_create_one_revision(
    storage: ScyllaStorage, object_store: S3ObjectStore
) -> None:
    page = _page()
    body = _html("Same content")
    events = [_observe(storage, object_store, page, body, 0) for _ in range(6)]
    services = [_service(storage, object_store) for _ in range(3)]
    threads = [
        threading.Thread(target=services[i % 3].process, args=(e,)) for i, e in enumerate(events)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    (revision,) = storage.page_intel.revisions(page.url_id)
    assert revision.first_seen == revision.last_seen == T0
