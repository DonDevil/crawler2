"""Page revisions, last_seen, change events and archival through the service (design §10-§14)."""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import cast

import pytest
from antipiracy_contracts.events import EventEnvelope
from antipiracy_contracts.events.web import PageObserved
from antipiracy_contracts.ids import ObservationId
from antipiracy_contracts.models.web import UrlRef
from antipiracy_contracts.ownership import Component

from crawler2.extraction.archival import ArchivalProfile, decide
from crawler2.extraction.service import PageIntelligenceService, Result
from crawler2.storage.repositories import LinkRepository, SnapshotDecision, UrlRepository
from tests.fixtures.contracts import T0, event_of, page_observation
from tests.unit.extraction.fakes import FakeLinks, FakeObjects, FakePages, FakeUrls

PAGE = UrlRef.of("https://site.example/watch/1")


def html(text: str = "Episode 1", ad: str = "ad-1", video: str = "/v/1.m3u8") -> bytes:
    return (
        f'<html><body><main><h1>{text}</h1><a href="/next">next</a><video src="{video}"></video>'
        f"</main><aside>{ad}</aside></body></html>"
    ).encode()


class World:
    def __init__(self, profile: ArchivalProfile | None = None) -> None:
        self.objects = FakeObjects()
        self.links = FakeLinks()
        self.urls = FakeUrls()
        self.pages = FakePages()
        self.service = self.make_service(profile)

    def make_service(self, profile: ArchivalProfile | None = None) -> PageIntelligenceService:
        return PageIntelligenceService(
            objects=self.objects,  # type: ignore[arg-type]
            links=cast(LinkRepository, self.links),
            urls=cast(UrlRepository, self.urls),
            pages=self.pages,
            instance="dev-1:extraction:1:" + "0" * 32,
            profile=profile or ArchivalProfile(sample_rate=0.0),
        )

    def observed(
        self, body: bytes, minutes: int, *, status: int = 200, seed: str | None = None
    ) -> EventEnvelope[PageObserved]:
        at = T0 + timedelta(minutes=minutes)
        snapshot = self.objects.put_bytes(body)
        obs = page_observation(PAGE, at=at, body=body, snapshot=snapshot, status=status, seed=seed)
        return event_of(PageObserved(observation=obs), Component.CRAWLER_WORKER, at=at)


def test_first_observation_creates_a_revision_and_emits_everything() -> None:
    w = World()
    outcome = w.service.process(w.observed(html(), 0))
    assert (outcome.result, outcome.new_revision) == (Result.EXTRACTED, True)
    (revision,) = w.pages.revisions(PAGE.url_id)
    assert revision.first_seen == revision.last_seen == T0
    assert len(w.pages.changed()) == 1
    assert len(w.links.recorded) == 1
    assert w.links.recorded[0][1] is not None
    assert {e.event_type for e in w.pages.outbox} == {"page.changed", "media.discovered"}
    assert len(w.pages.extracts) == 1


def test_same_content_updates_last_seen_without_a_new_revision() -> None:
    w = World()
    w.service.process(w.observed(html(), 0))
    outcome = w.service.process(w.observed(html(), 30))
    assert (outcome.result, outcome.new_revision) == (Result.REUSED, False)
    (revision,) = w.pages.revisions(PAGE.url_id)
    assert (revision.first_seen, revision.last_seen) == (T0, T0 + timedelta(minutes=30))
    assert len(w.pages.changed()) == 1
    assert w.objects.gets == 1  # identical bytes are not fetched or parsed again
    assert len(w.links.recorded) == 1  # links are per raw version


def test_raw_only_change_is_not_a_new_revision() -> None:
    w = World()
    w.service.process(w.observed(html(ad="ad-1"), 0))
    outcome = w.service.process(w.observed(html(ad="ad-2 rotated"), 10))
    assert (outcome.result, outcome.new_revision) == (Result.EXTRACTED, False)
    (revision,) = w.pages.revisions(PAGE.url_id)
    assert revision.last_seen == T0 + timedelta(minutes=10)
    assert revision.first_page_version_id != revision.last_page_version_id
    assert len(w.pages.changed()) == 1


def test_meaningful_change_creates_a_revision_and_emits_page_changed() -> None:
    w = World()
    w.service.process(w.observed(html("Episode 1"), 0))
    outcome = w.service.process(w.observed(html("Episode 2"), 10))
    assert outcome.new_revision
    assert len(w.pages.revisions(PAGE.url_id)) == 2
    first, second = w.pages.changed()
    assert first.revision_id != second.revision_id
    assert second.hashes.normalized != first.hashes.normalized


def test_multiple_sequential_revisions_and_revert() -> None:
    w = World()
    for minute, text in enumerate(["A", "B", "C", "A"]):
        w.service.process(w.observed(html(text), minute))
    revisions = w.pages.revisions(PAGE.url_id)
    assert len(revisions) == 3  # the revert to A re-sights A (ADR-020)
    revision_a = next(r for r in revisions if r.first_seen == T0)
    assert revision_a.last_seen == T0 + timedelta(minutes=3)
    assert len(w.pages.changed()) == 3


def test_out_of_order_processing_cannot_move_last_seen_backwards() -> None:
    w = World()
    w.service.process(w.observed(html(), 60))
    w.service.process(w.observed(html(ad="late"), 5))
    (revision,) = w.pages.revisions(PAGE.url_id)
    assert (revision.first_seen, revision.last_seen) == (
        T0 + timedelta(minutes=5),
        T0 + timedelta(minutes=60),
    )


def test_replay_of_the_same_event_is_idempotent() -> None:
    w = World()
    event = w.observed(html(), 0)
    w.service.process(event)
    rows_before = dict(w.pages.rows)
    outcome = w.service.process(event)
    assert outcome.new_revision is False
    assert w.pages.rows == rows_before
    assert len(w.pages.changed()) == 1


def test_crash_after_extract_before_sighting_still_emits_page_changed() -> None:
    w = World()
    event = w.observed(html(), 0)

    def crash(*args: object, **kwargs: object) -> None:
        raise ConnectionError("scylla down")

    original = w.pages.record_sighting
    w.pages.record_sighting = crash  # type: ignore[method-assign]
    with pytest.raises(ConnectionError):
        w.service.process(event)
    w.pages.record_sighting = original  # type: ignore[method-assign]
    assert len(w.pages.extracts) == 1
    assert not w.pages.rows
    w.service.process(event)  # redelivery
    assert len(w.pages.revisions(PAGE.url_id)) == 1
    assert len(w.pages.changed()) == 1


def test_concurrent_identical_observations_converge_on_one_revision() -> None:
    """Two workers see the same new content at once: both may emit page.changed (same
    idempotency key), but exactly one revision row exists (ADR-020)."""
    w = World()
    events = [w.observed(html(), 0, seed="host-a"), w.observed(html(), 0, seed="host-b")]
    w.pages.before_write = threading.Barrier(2)  # both read "absent" before either writes
    services = [w.make_service(), w.make_service()]
    threads = [
        threading.Thread(target=s.process, args=(e,)) for s, e in zip(services, events, strict=True)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(w.pages.revisions(PAGE.url_id)) == 1
    keys = {c.revision_id for c in w.pages.changed()}
    assert len(keys) == 1
    assert len(w.pages.changed()) == 2


@pytest.mark.parametrize(
    ("status", "content_type", "snapshot", "result"),
    [
        (404, "text/html", True, Result.SKIPPED_STATUS),
        (200, "application/json", True, Result.SKIPPED_TYPE),
        (200, "text/html", False, Result.NO_SNAPSHOT),
    ],
)
def test_ineligible_observations_are_skipped(
    status: int, content_type: str, snapshot: bool, result: Result
) -> None:
    w = World()
    event = w.observed(html(), 0, status=status)
    obs = event.payload.observation.model_copy(
        update={
            "content_type": content_type,
            "snapshot": event.payload.observation.snapshot if snapshot else None,
        }
    )
    event = event.model_copy(update={"payload": PageObserved(observation=obs)})
    assert w.service.process(event).result is result
    assert not w.pages.rows
    assert not w.pages.outbox


def test_missing_content_type_is_sniffed() -> None:
    w = World()
    event = w.observed(b'{"not": "html"}', 0)
    obs = event.payload.observation.model_copy(update={"content_type": None})
    event = event.model_copy(update={"payload": PageObserved(observation=obs)})
    assert w.service.process(event).result is Result.SKIPPED_TYPE


def test_extractor_failure_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World()

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("extractor bug")

    monkeypatch.setattr("crawler2.extraction.service.extract", boom)
    assert w.service.process(w.observed(html(), 0)).result is Result.FAILED
    assert not w.pages.rows


def test_archival_decisions_are_recorded_per_observation() -> None:
    w = World(ArchivalProfile(sample_rate=0.0))
    w.service.process(w.observed(html(), 0))
    w.service.process(w.observed(html(), 1))
    decisions = [(d.decision, d.reason) for d in w.pages.decisions]
    assert decisions == [
        (SnapshotDecision.RETAIN, "new_revision"),
        (SnapshotDecision.SAMPLED_OUT, "sampled_out"),
    ]


def test_archival_policy_rules() -> None:
    oid = ObservationId.new()
    assert decide(
        ArchivalProfile(evidence_candidate=True, sample_rate=0),
        observation_id=oid,
        new_revision=False,
    ) == (
        SnapshotDecision.RETAIN,
        "evidence_candidate",
    )
    assert (
        decide(ArchivalProfile(high_value=True), observation_id=oid, new_revision=True)[1]
        == "high_value_change"
    )
    assert (
        decide(ArchivalProfile(sample_rate=1.0), observation_id=oid, new_revision=False)[1]
        == "sampled"
    )
    assert (
        decide(ArchivalProfile(sample_rate=0.0), observation_id=oid, new_revision=False)[0]
        is SnapshotDecision.SAMPLED_OUT
    )
    ids = [ObservationId.new() for _ in range(4000)]
    kept = sum(
        decide(ArchivalProfile(sample_rate=0.25), observation_id=i, new_revision=False)[0]
        is SnapshotDecision.RETAIN
        for i in ids
    )
    assert 800 < kept < 1200
    assert all(
        decide(ArchivalProfile(sample_rate=0.25), observation_id=i, new_revision=False)
        == decide(ArchivalProfile(sample_rate=0.25), observation_id=i, new_revision=False)
        for i in ids[:50]
    )
    with pytest.raises(ValueError, match="sample_rate"):
        ArchivalProfile(sample_rate=1.5)
