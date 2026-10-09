"""P8 increment 3 on the real Scylla and Redis: one logical encode.requested per
(ContentId, spec) under races and crashes, the sweeper, suppression by
representation_status, and the representation.ready / encode.failed projection
through the fingerprinter streams (design §7)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis
from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events import decode_event_as, encode_event, new_event
from antipiracy_contracts.events.fingerprinting import (
    EncodeFailed,
    EncodeFailure,
    EncodeRequested,
    RepresentationReady,
)
from antipiracy_contracts.ids import EventId
from antipiracy_contracts.models.media import ContentKey, Media, MediaKind, ProbeStatus
from antipiracy_contracts.models.representations import Representation
from antipiracy_contracts.models.web import UrlRef
from antipiracy_contracts.ownership import Component

from crawler2.core.configuration import EventSettings, Settings
from crawler2.core.observability import Metrics
from crawler2.media.encode import (
    EncodeRequester,
    RepresentationLoop,
    RepresentationProjection,
    RequestResult,
)
from crawler2.media.model import (
    SCHEME,
    HashOutcome,
    HashRecord,
    MediaLatest,
    ProbeRecord,
    Validators,
)
from crawler2.media.store import ScyllaRegistryStore
from crawler2.storage.events.consumer import IdempotentConsumer, RedisStreamReader
from crawler2.storage.events.idempotency import idempotency_key
from crawler2.storage.events.publisher import stream_name
from crawler2.storage.repositories import RepresentationState
from crawler2.storage.scylla import ScyllaStorage
from tests.fixtures.contracts import SPEC, producer
from tests.fixtures.media import Background, Clock, Crash, Pause, outbox_rows, registry

pytestmark = pytest.mark.integration
ENCODER = producer(Component.ENCODER, instance="fp:encoder:1:" + "0" * 32)


def _content(run: str, tag: str = "a") -> ContentKey:
    return ContentKey.of(SCHEME, ContentDigest.of_bytes(f"{run}-{tag}".encode()))


def _media(run: str, i: int) -> Media:
    return Media.of(UrlRef.of(f"https://cdn{i}-{run}.example/v.mp4"), MediaKind.VIDEO_FILE)


class Rig:
    def __init__(self, storage: ScyllaStorage) -> None:
        self.storage = storage
        self.store = ScyllaRegistryStore(storage.session)
        self.clock = Clock(datetime.now(UTC).replace(microsecond=0))
        self.run = uuid.uuid4().hex[:8]
        self.started = datetime.now(UTC)

    def requester(self, name: str, **kw: Any) -> EncodeRequester:
        return EncodeRequester(
            self.store,
            self.storage.projections,
            self.storage.outbox,
            producer=registry(name),
            clock=self.clock,
            grace=timedelta(seconds=300),
            **kw,
        )

    def requested_rows(self, content: ContentKey) -> list[Any]:
        template = new_event(
            EncodeRequested(content=content, source=_media(self.run, 0)),
            producer=registry("x"),
            occurred_at=self.clock(),
        )
        return outbox_rows(self.storage, idempotency_key(template), since=self.started)

    def one_logical_request(self, content: ContentKey) -> tuple[str, int]:
        """Exactly one claim; every outbox copy carries its event_id and bytes."""
        claim = self.store.encode_claim(content.content_id, "default")
        assert claim is not None
        rows = self.requested_rows(content)
        assert {r.event_id for r in rows} == {claim.event_id}
        assert all(r.envelope == claim.envelope.encode() for r in rows)
        return claim.event_id, len(rows)


def test_two_host_claim_race_has_one_logical_request(storage: ScyllaStorage) -> None:
    rig = Rig(storage)
    content = _content(rig.run)
    pa, pb = Pause("after_index"), Pause("after_index")
    a = Background(
        lambda: rig.requester("A", hook=pa).request_content(content, _media(rig.run, 1), None)
    )
    assert pa.reached.wait(30)
    b = Background(
        lambda: rig.requester("B", hook=pb).request_content(content, _media(rig.run, 2), None)
    )
    assert pb.reached.wait(30)
    pa.resume.set()
    pb.resume.set()
    results = sorted([a.join().value, b.join().value])
    assert results == ["claimed_elsewhere", "emitted"]
    event_id, rows = rig.one_logical_request(content)
    assert rows == 1
    (status,) = storage.projections.representation_status(content.content_id)
    assert status.state is RepresentationState.REQUESTED
    assert str(status.request_event_id) == event_id


def test_byte_identical_media_one_claim_then_suppressed(storage: ScyllaStorage) -> None:
    rig = Rig(storage)
    content = _content(rig.run)
    requester = rig.requester("A")
    results = [requester.request_content(content, _media(rig.run, i), None) for i in range(3)]
    assert results == [RequestResult.EMITTED, RequestResult.SUPPRESSED, RequestResult.SUPPRESSED]
    assert rig.one_logical_request(content)[1] == 1


def test_crash_after_claim_before_outbox_then_sweeper_reemits_same_event(
    storage: ScyllaStorage,
) -> None:
    rig = Rig(storage)
    content = _content(rig.run)
    with pytest.raises(Crash):
        rig.requester("A", hook=Pause("after_claim", crash=True)).request_content(
            content, _media(rig.run, 1), "https://site.example/p"
        )
    claim = rig.store.encode_claim(content.content_id, "default")
    assert claim is not None
    assert not claim.emitted
    assert rig.requested_rows(content) == []  # the outbox write never happened
    # Another media with the same bytes: the claim exists, so no second request.
    other = rig.requester("B").request_content(content, _media(rig.run, 2), None)
    assert other is RequestResult.CLAIMED_ELSEWHERE
    sweeper = rig.requester("S")
    early = sweeper.sweep()
    assert early.pending >= 1
    assert early.reemitted == 0
    rig.clock.advance(301)
    report = sweeper.sweep()
    assert report.reemitted == 1
    event_id, rows = rig.one_logical_request(content)
    assert event_id == claim.event_id  # the stored event id, not a new one
    assert rows == 1
    stored = decode_event_as(claim.envelope, EncodeRequested)
    assert stored.payload.referer is not None
    assert stored.payload.required_spec is None
    assert rig.store.encode_claim(content.content_id, "default").emitted  # type: ignore[union-attr]
    assert sweeper.sweep().reemitted == 0


def test_crash_after_outbox_before_emitted_redelivers_one_event(storage: ScyllaStorage) -> None:
    rig = Rig(storage)
    content = _content(rig.run)
    # The outbox write succeeds and the marker never does: first the request, then a
    # sweep re-emitting the stored envelope, both crash before emitted_at.
    first = rig.requester("A", hook=Pause("before_emitted", crash=True))
    with pytest.raises(Crash):
        first.request_content(content, _media(rig.run, 1), None)
    rig.clock.advance(301)
    second = rig.requester("A2", hook=Pause("before_emitted", crash=True))
    with pytest.raises(Crash):
        second.sweep()
    rig.clock.advance(301)
    assert rig.requester("S").sweep().reemitted == 1
    _event_id, rows = rig.one_logical_request(content)  # copies share one event_id
    assert rows >= 1
    assert rig.store.encode_claim(content.content_id, "default").emitted  # type: ignore[union-attr]


def _representation(content: ContentKey) -> Representation:
    return Representation(
        representation_id=Representation.expected_id(content.content_id, SPEC),
        content_id=content.content_id,
        spec=SPEC,
        created_at=datetime.now(UTC) - timedelta(minutes=1),
        segment_count=12,
    )


def test_ready_suppresses_and_failed_never_re_requests(storage: ScyllaStorage) -> None:
    rig = Rig(storage)
    ready = _content(rig.run, "ready")
    storage.projections.apply_representation_ready(_representation(ready))
    assert rig.requester("A").request_content(ready, _media(rig.run, 1), None) is (
        RequestResult.SUPPRESSED
    )
    assert rig.store.encode_claim(ready.content_id, "default") is None

    failed = _content(rig.run, "failed")
    requester = rig.requester("A")
    assert requester.request_content(failed, _media(rig.run, 1), None) is RequestResult.EMITTED
    claim = rig.store.encode_claim(failed.content_id, "default")
    assert claim is not None
    storage.projections.apply_encode_failed(
        new_event(
            EncodeFailed(
                request_id=EventId(claim.event_id),
                content_id=failed.content_id,
                failure=EncodeFailure.SOURCE_UNREACHABLE,
                retryable=True,
                attempt=1,
            ),
            producer=ENCODER,
            occurred_at=datetime.now(UTC),
        )
    )
    (status,) = storage.projections.representation_status(failed.content_id)
    assert status.state is RepresentationState.FAILED
    # Another source with the same bytes: no alternative-source re-request (P8-D7).
    again = requester.request_content(failed, _media(rig.run, 2), None)
    assert again is RequestResult.CLAIMED_ELSEWHERE
    assert rig.one_logical_request(failed)[1] == 1


def test_ensure_recovers_a_crash_between_hash_and_claim(storage: ScyllaStorage) -> None:
    rig = Rig(storage)
    content = _content(rig.run)
    media = _media(rig.run, 1)
    probe = ProbeRecord(
        probe_id="p",
        media_id=str(media.media_id),
        locator_url=media.locator.url,
        kind=MediaKind.VIDEO_FILE,
        probed_at=rig.clock(),
        probe_status=ProbeStatus.PROBED,
        worker="w",
        fetch_outcome="media",
        etag='"e"',
    )
    ok = HashRecord(
        hash_id="h",
        media_id=probe.media_id,
        locator_url=probe.locator_url,
        hashed_at=rig.clock(),
        outcome=HashOutcome.OK,
        worker="w",
        probe_id="p",
        validators=Validators(None, '"e"', None),
        digest=str(content.digest),
    )
    latest = MediaLatest(media.media_id, probe=probe, hash=ok, ok_hash=ok)
    requester = rig.requester("A")
    requester.ensure(latest, media, "https://site.example/p")  # no claim yet → request
    event_id, rows = rig.one_logical_request(content)
    requester.ensure(latest, media, "https://site.example/p")  # now suppressed
    assert rig.one_logical_request(content) == (event_id, rows)


def test_representation_events_through_streams_are_applied_once(
    storage: ScyllaStorage,
    redis_client: redis.Redis,
    event_settings: EventSettings,
    settings: Settings,
) -> None:
    rig = Rig(storage)
    content = _content(rig.run, "stream")
    rep = _representation(content)
    ready = new_event(
        RepresentationReady(representation=rep), producer=ENCODER, occurred_at=rep.created_at
    )
    failed = new_event(
        EncodeFailed(
            request_id=EventId.new(),
            content_id=content.content_id,
            failure=EncodeFailure.TIMEOUT,
            retryable=True,
            attempt=1,
            spec=SPEC,
        ),
        producer=ENCODER,
        occurred_at=rep.created_at - timedelta(minutes=5),
    )
    ready_stream = stream_name(event_settings, "representation.ready", 1)
    failed_stream = stream_name(event_settings, "encode.failed", 1)
    group = f"media-registry-{rig.run}"
    metrics = Metrics(settings)
    projection = RepresentationProjection(storage.projections)
    readers = [
        RedisStreamReader(redis_client, ready_stream, group, "c1"),
        RedisStreamReader(redis_client, failed_stream, group, "c1"),
    ]
    for r in readers:
        r.ensure_group()
    for _ in range(2):  # redelivered: same event twice on each stream
        redis_client.xadd(ready_stream, {"envelope": encode_event(ready)})
        redis_client.xadd(failed_stream, {"envelope": encode_event(failed)})
    redis_client.xadd(ready_stream, {"envelope": b'{"garbage": true}'})
    loop = RepresentationLoop(
        [
            (
                readers[0],
                IdempotentConsumer(
                    group,
                    RepresentationReady,
                    projection.ready,
                    storage.processed_events,
                    metrics=metrics,
                ),
            ),
            (
                readers[1],
                IdempotentConsumer(
                    group,
                    EncodeFailed,
                    projection.failed,
                    storage.processed_events,
                    metrics=metrics,
                ),
            ),
        ],
        batch_size=10,
        block_ms=100,
        claim_idle_ms=60_000,
    )
    loop.run(max_idle_polls=2)
    statuses = {
        s.spec_key: s for s in storage.projections.representation_status(content.content_id)
    }
    (status,) = statuses.values()
    assert status.state is RepresentationState.READY  # ready supersedes the earlier failure
    assert status.representation == rep
    rendered = metrics.render()
    assert b'consumer="' + group.encode() + b'",outcome="applied"} 2.0' in rendered
    assert b'outcome="duplicate"} 2.0' in rendered
    for r in readers:
        assert redis_client.xpending(r.stream, group)["pending"] == 0  # all acknowledged


def test_cli_encode_sweep_and_representations_run(
    storage: ScyllaStorage,
    redis_client: redis.Redis,
    event_settings: EventSettings,
    keyspace: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from crawler2.media import cli

    rig = Rig(storage)
    content = _content(rig.run, "cli")
    monkeypatch.setenv("CRAWLER2_SCYLLA__KEYSPACE", keyspace)
    monkeypatch.setenv("CRAWLER2_EVENTS__STREAM_PREFIX", event_settings.stream_prefix)
    monkeypatch.setenv("CRAWLER2_MEDIA__CONSUMER_GROUP", f"mr-{rig.run}")
    monkeypatch.setenv("CRAWLER2_MEDIA__CLAIM_EMIT_GRACE_S", "1")
    with pytest.raises(Crash):  # a claim whose outbox write never happened
        rig.requester("A", hook=Pause("after_claim", crash=True)).request_content(
            content, _media(rig.run, 1), None
        )
    import time

    time.sleep(1.1)  # past the 1 s grace (the CLI uses Redis TIME)
    assert cli.main(["encode", "sweep", "--once"]) == 0
    assert '"reemitted": 1' in capsys.readouterr().out
    rig.one_logical_request(content)

    rep = _representation(content)
    ready = new_event(
        RepresentationReady(representation=rep), producer=ENCODER, occurred_at=rep.created_at
    )
    redis_client.xadd(
        stream_name(event_settings, "representation.ready", 1), {"envelope": encode_event(ready)}
    )
    assert cli.main(["representations", "run", "--exit-when-idle", "2"]) == 0
    states = {s.state for s in storage.projections.representation_status(content.content_id)}
    assert RepresentationState.READY in states
