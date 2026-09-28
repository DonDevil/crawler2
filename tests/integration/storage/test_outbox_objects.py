"""Outbox → relay → Redis Streams → idempotent consumer, object store, and retention (TTL)."""

from __future__ import annotations

import hashlib
import io
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis
from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events import EventEnvelope, encode_event
from antipiracy_contracts.events.media import MediaObserved
from antipiracy_contracts.events.web import FetchCompleted, PageObserved, UrlsDiscovered
from antipiracy_contracts.ids import EventId
from antipiracy_contracts.models.blobs import BlobRef
from antipiracy_contracts.models.web import PageObservation
from antipiracy_contracts.ownership import Component

from crawler2.core.configuration import EventSettings
from crawler2.storage.errors import IntegrityError
from crawler2.storage.events.consumer import (
    ConsumeOutcome,
    IdempotentConsumer,
    RedisStreamReader,
)
from crawler2.storage.events.publisher import (
    EventPublisher,
    OutboundEvent,
    RedisStreamPublisher,
    stream_name,
)
from crawler2.storage.events.relay import OutboxRelay
from crawler2.storage.layout import OUTBOX_SHARDS
from crawler2.storage.objectstore import S3ObjectStore
from crawler2.storage.objectstore.layout import raw_key
from crawler2.storage.scylla import ScyllaStorage
from tests.fixtures.contracts import (
    T0,
    event_of,
    fetch_attempt,
    links,
    media,
    media_observation,
    page_observation,
    url,
)

pytestmark = pytest.mark.integration


def _page_events(n: int) -> list[tuple[PageObservation, EventEnvelope[PageObserved]]]:
    tag = uuid.uuid4().hex[:8]
    out = []
    for i in range(n):
        o = page_observation(url(f"{tag}/{i}"), at=T0 + timedelta(seconds=i))
        out.append((o, event_of(PageObserved(observation=o), Component.CRAWLER_WORKER)))
    return out


def _relay(storage: ScyllaStorage, publisher: EventPublisher, relay_id: str = "it") -> OutboxRelay:
    return OutboxRelay(storage.outbox, publisher, relay_id=relay_id, settle_s=600)


def _wires(client: redis.Redis, stream: str) -> list[bytes]:
    entries: Any = client.xrange(stream)
    return [fields[b"envelope"] for _, fields in entries]


def _drain(reader: RedisStreamReader) -> list[bytes]:
    got: list[bytes] = []
    while entries := reader.read(count=500):
        got += [e.envelope for e in entries]
        reader.ack(entries)
    return got


class CrashAfter:
    """Publishes ``n`` events for real, then fails before the relay can mark them."""

    def __init__(self, inner: RedisStreamPublisher, n: int) -> None:
        self.inner, self.n, self.sent = inner, n, 0

    def publish(self, events: Sequence[OutboundEvent]) -> None:
        take = events[: max(0, self.n - self.sent)]
        self.inner.publish(take)
        self.sent += len(take)
        if len(take) < len(events):
            raise ConnectionError("simulated crash after publish, before mark")


def test_every_repository_event_reaches_its_stream(
    storage: ScyllaStorage, redis_client: redis.Redis, event_settings: EventSettings
) -> None:
    target = url(f"{uuid.uuid4().hex}/x")
    attempt = fetch_attempt(target, seed=uuid.uuid4().hex)
    page = page_observation(target, seed=uuid.uuid4().hex)
    discovered = UrlsDiscovered(
        page_observation_id=page.observation_id,
        page=target,
        page_version_id=page.page_version_id,
        links=links(3),
    )
    seen = media_observation(media(f"{uuid.uuid4().hex}.mp4"), page)
    fetched = event_of(FetchCompleted(attempt=attempt), Component.CRAWLER_WORKER)
    observed = event_of(PageObserved(observation=page), Component.CRAWLER_WORKER)
    extracted = event_of(discovered, Component.EXTRACTION)
    resolved = event_of(MediaObserved(observation=seen), Component.MEDIA_REGISTRY)
    storage.fetch_attempts.record(attempt, event=fetched)
    storage.pages.record(page, event=observed)
    storage.links.record(discovered, observed_at=T0, event=extracted)
    storage.media.record_observation(seen, event=resolved)

    _relay(storage, RedisStreamPublisher(redis_client, event_settings)).run_once()
    for event_type, wire in [
        (fetched.event_type, encode_event(fetched)),
        (observed.event_type, encode_event(observed)),
        (extracted.event_type, encode_event(extracted)),
        (resolved.event_type, encode_event(resolved)),
    ]:
        stream = stream_name(event_settings, event_type, 1)
        assert wire in _wires(redis_client, stream)  # byte-identical to what was committed


def test_crash_between_publish_and_mark_is_recovered_with_duplicates_only(
    storage: ScyllaStorage, redis_client: redis.Redis, event_settings: EventSettings
) -> None:
    written = _page_events(40)
    for observation, event in written:
        storage.pages.record(observation, event=event)

    publisher = RedisStreamPublisher(redis_client, event_settings)
    with pytest.raises(ConnectionError):
        _relay(storage, CrashAfter(publisher, 25), relay_id="host-1").run_once()
    # durable state survived the "crash": authoritative rows and outbox rows are there
    assert all(storage.pages.get(o.observation_id) == o for o, _ in written)

    _relay(storage, publisher, relay_id="host-2").run_once()  # another host resumes
    _relay(storage, publisher, relay_id="host-2").run_once()  # nothing left: no-op

    stream = stream_name(event_settings, "page.observed", 1)
    reader = RedisStreamReader(redis_client, stream, "extraction", "c1")
    reader.ensure_group()
    wires = _drain(reader)
    ids = {e.event_id for _, e in written}
    delivered = [w for w in wires if any(str(i).encode() in w for i in ids)]
    assert len(delivered) >= len(written)  # at-least-once; duplicates allowed

    applied: list[str] = []
    consumer = IdempotentConsumer(
        f"it-{uuid.uuid4().hex[:6]}",
        PageObserved,
        lambda e: applied.append(e.payload.observation.observation_id),
        storage.processed_events,
    )
    outcomes = [consumer.handle(w) for w in delivered]
    assert sorted(applied) == sorted(o.observation_id for o, _ in written)
    assert outcomes.count(ConsumeOutcome.DUPLICATE) == len(delivered) - len(written)
    # a full redelivery of everything is still harmless
    assert all(consumer.handle(w) is ConsumeOutcome.DUPLICATE for w in delivered)


def test_replay_republishes_and_consumers_stay_idempotent(
    storage: ScyllaStorage, redis_client: redis.Redis, event_settings: EventSettings
) -> None:
    written = _page_events(5)
    for observation, event in written:
        storage.pages.record(observation, event=event)
    publisher = RedisStreamPublisher(redis_client, event_settings)
    relay = _relay(storage, publisher)
    relay.run_once()
    now = datetime.now(UTC)
    report = relay.replay(since=now - timedelta(minutes=5), until=now)
    assert report.published >= len(written)
    stream = stream_name(event_settings, "page.observed", 1)
    ids = {str(e.event_id).encode() for _, e in written}
    wires = _wires(redis_client, stream)
    assert sum(any(i in w for i in ids) for w in wires) >= 2 * len(written)


def test_outbox_rows_spread_over_shards_and_carry_the_envelope(storage: ScyllaStorage) -> None:
    written = _page_events(400)  # P(some of 32 shards empty) ~ 1e-4
    for observation, event in written:
        storage.pages.record(observation, event=event)
    rows = storage.session.execute_raw(
        f"SELECT shard, event_id, idempotency_key, producer_component, TTL(envelope) AS ttl "
        f"FROM {storage.session.keyspace}.outbox"
    )
    mine = {e.event_id.uuid for _, e in written}
    ours = [r for r in rows if r.event_id in mine]
    assert len(ours) == len(written)
    assert len({r.shard for r in ours}) == OUTBOX_SHARDS
    assert all(r.producer_component == "crawler_worker" for r in ours)
    assert all(0 < r.ttl <= 1_209_600 for r in ours)  # bounded: 14-day TTL


# --- object store ------------------------------------------------------------------


def test_content_addressed_put_get_and_dedupe(object_store: S3ObjectStore) -> None:
    body = b"<html>" + uuid.uuid4().bytes + b"</html>"
    ref = object_store.put(body, media_type="text/html")
    assert ref.digest == ContentDigest.of_bytes(body)
    assert ref.size_bytes == len(body)
    assert ref.uri == f"s3://{object_store.bucket}/{raw_key(ref.digest)}"
    assert object_store.get(ref) == body

    again = object_store.put(io.BytesIO(body), media_type="text/html")  # file input
    assert again == ref  # same bytes → same identity, stored once
    streamed = object_store.put(iter([body[:3], body[3:]]), expected_digest=ref.digest)
    assert streamed.uri == ref.uri
    other = object_store.put(body + b"!")
    assert other.uri != ref.uri

    info = object_store.stat(ref.digest)
    assert info is not None
    assert (info.digest, info.size_bytes) == (ref.digest, len(body))


def test_wrong_digest_is_rejected_and_nothing_is_stored(object_store: S3ObjectStore) -> None:
    body = uuid.uuid4().bytes * 3
    wrong = ContentDigest.of_bytes(b"something else")
    with pytest.raises(IntegrityError, match="supplied digest"):
        object_store.put(body, expected_digest=wrong)
    assert not object_store.exists(ContentDigest.of_bytes(body))


def test_reads_are_verified_against_the_reference(object_store: S3ObjectStore) -> None:
    ref = object_store.put(uuid.uuid4().bytes)
    lying = BlobRef(uri=ref.uri, digest=ContentDigest.of_bytes(b"x"), size_bytes=ref.size_bytes)
    with pytest.raises(IntegrityError):
        object_store.get(lying)
    shorter = ref.model_copy(update={"size_bytes": ref.size_bytes - 1})
    with pytest.raises(IntegrityError):
        object_store.get(shorter)


def test_server_rejects_a_payload_that_does_not_match_its_signed_hash(
    object_store: S3ObjectStore,
) -> None:
    body = uuid.uuid4().bytes
    key = raw_key(ContentDigest.of_bytes(body))
    response = object_store._request(
        "put",
        "PUT",
        f"/{object_store.bucket}/{key}",
        headers={"Content-Length": str(len(body))},
        body=body,
        payload_sha256=hashlib.sha256(b"not the body").hexdigest(),
    )
    assert response.status == 400
    assert b"XAmzContentSHA256Mismatch" in response.body
    assert not object_store.exists(ContentDigest.of_bytes(body))


def test_an_existing_object_is_never_overwritten(object_store: S3ObjectStore) -> None:
    body = uuid.uuid4().bytes
    digest = ContentDigest.of_bytes(body)
    impostor = b"different bytes under the same key"
    object_store._request(  # plant a corrupt object at the content address
        "put",
        "PUT",
        f"/{object_store.bucket}/{raw_key(digest)}",
        headers={"Content-Length": str(len(impostor))},
        body=impostor,
        payload_sha256=hashlib.sha256(impostor).hexdigest(),
    )
    with pytest.raises(IntegrityError, match="refusing to overwrite"):
        object_store.put(body)


def test_blobref_persists_through_a_page_observation(
    storage: ScyllaStorage, object_store: S3ObjectStore
) -> None:
    body = b"<html>snapshot " + uuid.uuid4().bytes + b"</html>"
    ref = object_store.put(body, media_type="text/html")
    observation = page_observation(url(f"{uuid.uuid4().hex}/s"), body=body, snapshot=ref)
    storage.pages.record(observation)
    stored = storage.pages.get(observation.observation_id)
    assert stored is not None
    assert stored.snapshot == ref
    assert object_store.get(stored.snapshot) == body
    latest = storage.pages.latest(observation.requested.url_id)
    assert latest is not None
    assert latest.snapshot_uri == ref.uri


# --- retention -----------------------------------------------------------------------


def test_ttl_applies_to_windows_and_markers_but_not_to_authoritative_rows(
    storage: ScyllaStorage,
) -> None:
    target = url(f"{uuid.uuid4().hex}/ttl")
    attempt = fetch_attempt(target, seed=uuid.uuid4().hex)
    storage.fetch_attempts.record(attempt)
    ks = storage.session.keyspace
    (auth,) = storage.session.execute_raw(
        f"SELECT TTL(doc) AS ttl FROM {ks}.fetch_attempts "
        f"WHERE fetch_attempt_id = {attempt.fetch_attempt_id.uuid}"
    )
    assert auth.ttl is None  # authoritative: retained
    (window,) = storage.session.execute_raw(
        f"SELECT TTL(outcome) AS ttl FROM {ks}.fetch_attempts_by_url "
        f"WHERE url_id = {target.url_id.uuid}"
    )
    assert 2_592_000 - 600 < window.ttl <= 2_592_000  # 30-day operational window
    storage.processed_events.mark_processed(
        "it-ttl", "k", event_id=str(EventId.new()), event_type="page.observed", at=T0
    )
    (marker,) = storage.session.execute_raw(
        f"SELECT TTL(event_id) AS ttl FROM {ks}.processed_events "
        "WHERE consumer = 'it-ttl' AND idempotency_key = 'k'"
    )
    assert 1_209_600 - 600 < marker.ttl <= 1_209_600  # 14 days = outbox redelivery horizon
