"""Web-memory repositories (W1-W14) against real Scylla: access patterns, replay, LWW."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from antipiracy_contracts.events import encode_event
from antipiracy_contracts.events.web import FetchCompleted, PageObserved, UrlsDiscovered
from antipiracy_contracts.ids import FetchAttemptId, ObservationId
from antipiracy_contracts.models.web import DiscoveredLink, FetchOutcome, LinkRelation, UrlRef
from antipiracy_contracts.ownership import Component

from crawler2.storage.layout import month_bucket
from crawler2.storage.scylla import ScyllaStorage
from crawler2.storage.scylla.session import Consistency
from crawler2.storage.scylla.web import write_authoritative
from tests.fixtures.contracts import T0, event_of, fetch_attempt, links, page_observation, url

pytestmark = pytest.mark.integration
STRONG = Consistency.STRONG_READ


def _unique(path: str) -> UrlRef:
    return url(f"{uuid.uuid4().hex}/{path}", host=f"h{uuid.uuid4().hex[:8]}.example")


def test_fetch_attempts_point_url_and_domain_day(storage: ScyllaStorage) -> None:
    target = _unique("a")
    failed = fetch_attempt(target, at=T0, seed="f1", outcome=FetchOutcome.TIMEOUT)
    ok = fetch_attempt(target, at=T0 + timedelta(minutes=5), seed="f2")
    for attempt in (failed, ok, ok):  # the replay of `ok` must not duplicate anything
        storage.fetch_attempts.record(attempt)

    assert storage.fetch_attempts.get(ok.fetch_attempt_id) == ok
    recent = storage.fetch_attempts.recent_for_url(target.url_id)
    assert [a.fetch_attempt_id for a in recent] == [ok.fetch_attempt_id, failed.fetch_attempt_id]
    assert recent[1].outcome is FetchOutcome.TIMEOUT
    assert recent[0].elapsed_ms == 850
    day = storage.fetch_attempts.for_domain_day(target.domain_id, T0)
    assert [a.fetch_attempt_id for a in day] == [ok.fetch_attempt_id, failed.fetch_attempt_id]
    assert storage.fetch_attempts.get(FetchAttemptId.new()) is None


def test_page_observation_roundtrip_history_and_replay(storage: ScyllaStorage) -> None:
    target = _unique("page")
    v1 = page_observation(target, at=T0, body=b"one")
    v1_again = page_observation(target, at=T0 + timedelta(days=40), body=b"one")
    v2 = page_observation(target, at=T0 + timedelta(days=41), body=b"two")
    for o in (v1, v1_again, v2, v2, v1):  # duplicates and out-of-order replays
        storage.pages.record(o)

    assert storage.pages.get(v2.observation_id) == v2
    assert storage.pages.get(ObservationId.new()) is None
    history = storage.pages.history(
        target.url_id, since=T0, until=T0 + timedelta(days=60), limit=10
    )
    assert [o.observation_id for o in history] == [
        v2.observation_id,
        v1_again.observation_id,
        v1.observation_id,
    ]
    assert (
        len(storage.pages.history(target.url_id, since=T0, until=T0 + timedelta(days=60), limit=2))
        == 2
    )

    latest = storage.pages.latest(target.url_id)
    assert latest is not None
    assert latest.observation_id == v2.observation_id  # by observed_at, not arrival order
    assert latest.validators == v2.validators

    versions = storage.pages.versions(target.url_id, since=T0, until=T0 + timedelta(days=60))
    assert [v.page_version_id for v in versions] == [v2.page_version_id, v1.page_version_id]
    first = versions[1]  # v1 seen in two months: merged
    assert (first.first_seen, first.last_seen) == (v1.observed_at, v1_again.observed_at)
    assert first.first_observation_id == v1.observation_id

    known = storage.urls.get(target.url_id)
    assert known is not None
    assert (known.first_seen, known.last_observed_at) == (T0, v2.observed_at)
    assert known.last_page_version_id == v2.page_version_id
    domain = storage.urls.domain(target.domain_id)
    assert domain is not None
    assert (domain.domain.host, domain.first_seen) == (target.url.host, T0)
    day = storage.pages.recent_for_domain_day(target.domain_id, v2.observed_at)
    assert [s.observation_id for s in day] == [v2.observation_id]


def test_latest_wins_regardless_of_arrival_order(storage: ScyllaStorage) -> None:
    target = _unique("lww")
    newer = page_observation(target, at=T0 + timedelta(hours=1), body=b"new")
    older = page_observation(target, at=T0, body=b"old")
    storage.pages.record(newer)
    storage.pages.record(older)  # a late delivery of an older fact
    latest = storage.pages.latest(target.url_id)
    assert latest is not None
    assert latest.observation_id == newer.observation_id
    known = storage.urls.get(target.url_id)
    assert known is not None
    assert known.first_seen == older.observed_at  # earliest wins for first_seen


def test_derived_rows_are_rebuildable_after_a_crash(storage: ScyllaStorage) -> None:
    """Simulate a crash after the authoritative batch, before the derived writes."""
    target = _unique("crash")
    observation = page_observation(target, at=T0)
    auth = [
        storage.session.bind(
            "INSERT INTO {ks}.page_observations (observation_id, url_id, observed_at, doc) "
            "VALUES (?, ?, ?, ?)",
            STRONG,
            (
                observation.observation_id.uuid,
                target.url_id.uuid,
                T0,
                observation.model_dump_json(),
            ),
        ),
        storage.session.bind(
            "INSERT INTO {ks}.page_observations_by_url (url_id, month, observed_at, "
            "observation_id, doc) VALUES (?, ?, ?, ?, ?)",
            STRONG,
            (
                target.url_id.uuid,
                month_bucket(T0),
                T0,
                observation.observation_id.uuid,
                observation.model_dump_json(),
            ),
        ),
    ]
    write_authoritative(storage.session, storage.outbox, auth, None)
    assert storage.pages.latest(target.url_id) is None
    assert storage.pages.rebuild_url(target.url_id, since=T0, until=T0 + timedelta(days=1)) == 1
    latest = storage.pages.latest(target.url_id)
    assert latest is not None
    assert latest.observation_id == observation.observation_id
    assert storage.urls.get(target.url_id) is not None


def test_links_outlinks_inlinks_and_replay(storage: ScyllaStorage) -> None:
    host = f"l{uuid.uuid4().hex[:8]}.example"
    hub = url("hub", host)
    outlinks = (*links(451, host=host), DiscoveredLink(target=hub, relation=LinkRelation.ANCHOR))
    sources = [url(f"src/{i}", host) for i in range(3)]
    for source in sources:
        page = page_observation(source, at=T0)
        discovered = UrlsDiscovered(
            page_observation_id=page.observation_id,
            page=source,
            page_version_id=page.page_version_id,
            links=outlinks,
        )
        storage.links.record(discovered, observed_at=T0)
        storage.links.record(discovered, observed_at=T0)  # replay
        out = storage.links.links_of(page.page_version_id)
        assert len(out) == len(outlinks)  # > one 200-row chunk, no duplicates
        assert set(out) == set(outlinks)

    inlinks = list(storage.links.inlinks(hub.url_id))
    assert {i.source_url_id for i in inlinks} == {s.url_id for s in sources}
    assert all(i.first_seen == T0 for i in inlinks)


def test_discovered_urls_are_known_per_domain(storage: ScyllaStorage) -> None:
    host = f"d{uuid.uuid4().hex[:8]}.example"
    found = [url(f"u/{i}", host) for i in range(300)]
    storage.urls.record_discovered(found, seen_at=T0 + timedelta(hours=2))
    storage.urls.record_discovered(found[:10], seen_at=T0)  # seen earlier by another host
    known = {k.url_id: k for k in storage.urls.urls_of_domain(found[0].domain_id)}
    assert set(known) == {u.url_id for u in found}
    assert known[found[0].url_id].first_seen == T0
    assert known[found[50].url_id].first_seen == T0 + timedelta(hours=2)
    assert known[found[0].url_id].last_observed_at is None


def test_outbox_row_is_written_with_the_entity(storage: ScyllaStorage) -> None:
    target = _unique("evt")
    observation = page_observation(target, at=T0)
    event = event_of(PageObserved(observation=observation), Component.CRAWLER_WORKER)
    storage.pages.record(observation, event=event)
    attempt = fetch_attempt(target, seed=uuid.uuid4().hex)
    with pytest.raises(ValueError, match="does not describe"):
        storage.fetch_attempts.record(
            attempt,
            event=event_of(
                FetchCompleted(attempt=fetch_attempt(target, seed="other")),
                Component.CRAWLER_WORKER,
            ),
        )
    assert storage.fetch_attempts.get(attempt.fetch_attempt_id) is None  # nothing written
    rows = storage.session.execute_raw(
        f"SELECT event_type, envelope FROM {storage.session.keyspace}.outbox "
        f"WHERE event_id = {event.event_id.uuid} ALLOW FILTERING"
    )
    assert [(r.event_type, r.envelope.encode()) for r in rows] == [
        ("page.observed", encode_event(event))
    ]
