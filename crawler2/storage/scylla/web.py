"""Web memory repositories (W1-W14) on Scylla. Table design: p02-storage/schema.md.

Write pattern (ADR-012 §5, ADR-013):
1. authoritative rows (+ outbox row when an event is given) — one logged batch;
2. derived rows — concurrent idempotent upserts with convergent cell
   timestamps (``layout.latest_wins`` / ``earliest_wins``).
A crash between 1 and 2 leaves derived rows stale until the write is
replayed (at-least-once) or rebuilt (``rebuild_url``); never inconsistent
authoritative state.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from typing import Any

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events import EventEnvelope, EventPayload
from antipiracy_contracts.events.web import FetchCompleted, PageObserved, UrlsDiscovered
from antipiracy_contracts.ids import (
    DomainId,
    FetchAttemptId,
    ObservationId,
    PageVersionId,
    UrlId,
)
from antipiracy_contracts.models.web import (
    DiscoveredLink,
    Domain,
    FetchAttempt,
    FetchCapability,
    FetchOutcome,
    HttpValidators,
    LinkRelation,
    PageObservation,
    UrlRef,
)
from cassandra.query import BoundStatement

from crawler2.storage.layout import (
    DOMAIN_DAY_SHARDS,
    INLINK_SHARDS,
    URLS_BY_DOMAIN_SHARDS,
    day_bucket,
    earliest_wins,
    latest_wins,
    month_bucket,
    months_back,
    shard_of,
)
from crawler2.storage.repositories import (
    DomainRecord,
    FetchAttemptSummary,
    Inlink,
    KnownUrl,
    LatestObservation,
    ObservationSummary,
    PageVersionSighting,
)
from crawler2.storage.scylla.outbox import ScyllaOutbox
from crawler2.storage.scylla.session import Consistency, ScyllaSession, utc

AUTH = Consistency.AUTHORITATIVE_WRITE
DERIVED = Consistency.DERIVED_WRITE
EC = Consistency.EVENTUAL_READ
STRONG = Consistency.STRONG_READ

_LINK_CHUNK = 200
"""Rows per single-partition batch (stays far below batch_size_warn_threshold)."""


def require_payload[P: EventPayload](
    event: EventEnvelope[P] | None, matches: Callable[[P], bool], what: str
) -> None:
    if event is not None and not matches(event.payload):
        raise ValueError(f"event payload does not describe this {what}")


def write_authoritative[P: EventPayload](
    session: ScyllaSession,
    outbox: ScyllaOutbox,
    statements: list[BoundStatement],
    event: EventEnvelope[P] | None,
) -> None:
    """Pattern A: authoritative rows and their outbox row, all or nothing (batchlog)."""
    if event is None:
        session.execute_all(statements)
        return
    session.execute(session.logged_batch([*statements, outbox.statement(event)], AUTH))


# --- W1-W3 ---------------------------------------------------------------------

_FA_INSERT = (
    "INSERT INTO {ks}.fetch_attempts (fetch_attempt_id, url_id, domain_id, finished_at, doc) "
    "VALUES (?, ?, ?, ?, ?)"
)
_FA_GET = "SELECT doc FROM {ks}.fetch_attempts WHERE fetch_attempt_id = ?"
_FA_BY_DOMAIN = (
    "INSERT INTO {ks}.fetch_attempts_by_domain_day (domain_id, day, shard, finished_at, "
    "fetch_attempt_id, url_id, capability, outcome, http_status, bytes_received, elapsed_ms) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_FA_BY_URL = (
    "INSERT INTO {ks}.fetch_attempts_by_url (url_id, finished_at, fetch_attempt_id, "
    "capability, outcome, http_status, elapsed_ms) VALUES (?, ?, ?, ?, ?, ?, ?)"
)
_FA_SUMMARY_COLS = "fetch_attempt_id, finished_at, capability, outcome, http_status, elapsed_ms"
_FA_RECENT_URL = (
    f"SELECT url_id, {_FA_SUMMARY_COLS} FROM {{ks}}.fetch_attempts_by_url WHERE url_id = ? LIMIT ?"
)
_FA_DOMAIN_DAY = (
    f"SELECT url_id, {_FA_SUMMARY_COLS} FROM {{ks}}.fetch_attempts_by_domain_day "
    "WHERE domain_id = ? AND day = ? AND shard = ?"
)


def _attempt_summary(r: Any) -> FetchAttemptSummary:
    return FetchAttemptSummary(
        fetch_attempt_id=FetchAttemptId.from_uuid(r.fetch_attempt_id),
        url_id=UrlId.from_uuid(r.url_id),
        finished_at=utc(r.finished_at),
        capability=FetchCapability(r.capability),
        outcome=FetchOutcome(r.outcome),
        http_status=r.http_status,
        elapsed_ms=r.elapsed_ms,
    )


class ScyllaFetchAttemptRepository:
    def __init__(self, session: ScyllaSession, outbox: ScyllaOutbox) -> None:
        self._s = session
        self._outbox = outbox

    def record(
        self, attempt: FetchAttempt, *, event: EventEnvelope[FetchCompleted] | None = None
    ) -> None:
        require_payload(event, lambda p: p.attempt == attempt, "fetch attempt")
        url = attempt.requested
        auth = self._s.bind(
            _FA_INSERT,
            AUTH,
            (
                attempt.fetch_attempt_id.uuid,
                url.url_id.uuid,
                url.domain_id.uuid,
                attempt.finished_at,
                attempt.model_dump_json(),
            ),
        )
        write_authoritative(self._s, self._outbox, [auth], event)
        elapsed_ms = int((attempt.finished_at - attempt.started_at).total_seconds() * 1000)
        common = (attempt.capability.value, attempt.outcome.value, attempt.http_status)
        self._s.execute_all(
            [
                self._s.bind(
                    _FA_BY_DOMAIN,
                    DERIVED,
                    (
                        url.domain_id.uuid,
                        day_bucket(attempt.finished_at),
                        shard_of(attempt.fetch_attempt_id, DOMAIN_DAY_SHARDS),
                        attempt.finished_at,
                        attempt.fetch_attempt_id.uuid,
                        url.url_id.uuid,
                        *common,
                        attempt.bytes_received,
                        elapsed_ms,
                    ),
                ),
                self._s.bind(
                    _FA_BY_URL,
                    DERIVED,
                    (
                        url.url_id.uuid,
                        attempt.finished_at,
                        attempt.fetch_attempt_id.uuid,
                        *common,
                        elapsed_ms,
                    ),
                ),
            ]
        )

    def get(self, fetch_attempt_id: FetchAttemptId) -> FetchAttempt | None:
        rows = self._s.execute(self._s.bind(_FA_GET, EC, (fetch_attempt_id.uuid,)))
        return FetchAttempt.model_validate_json(rows[0].doc) if rows else None

    def recent_for_url(self, url_id: UrlId, *, limit: int = 20) -> list[FetchAttemptSummary]:
        rows = self._s.execute(self._s.bind(_FA_RECENT_URL, EC, (url_id.uuid, limit)))
        return [_attempt_summary(r) for r in rows]

    def for_domain_day(self, domain_id: DomainId, day: datetime) -> list[FetchAttemptSummary]:
        results = self._s.execute_many(
            self._s.bind(_FA_DOMAIN_DAY, EC, (domain_id.uuid, day_bucket(day), shard))
            for shard in range(DOMAIN_DAY_SHARDS)
        )
        summaries = [_attempt_summary(r) for rows in results for r in rows]
        return sorted(summaries, key=lambda s: s.finished_at, reverse=True)


# --- W11-W13 (shared by observation and discovery writes) ------------------------

_URL_FIRST = (
    "UPDATE {ks}.url_state USING TIMESTAMP ? SET url = ?, domain_id = ?, first_seen = ? "
    "WHERE url_id = ?"
)
_URL_LAST = (
    "UPDATE {ks}.url_state USING TIMESTAMP ? SET last_observed_at = ?, last_status = ?, "
    "last_page_version_id = ? WHERE url_id = ?"
)
_DOMAIN_URL_FIRST = (
    "UPDATE {ks}.urls_by_domain USING TIMESTAMP ? SET url = ?, first_seen = ? "
    "WHERE domain_id = ? AND shard = ? AND url_id = ?"
)
_DOMAIN_URL_LAST = (
    "UPDATE {ks}.urls_by_domain USING TIMESTAMP ? SET last_observed_at = ?, last_status = ? "
    "WHERE domain_id = ? AND shard = ? AND url_id = ?"
)
_DOMAIN_FIRST = (
    "UPDATE {ks}.domains USING TIMESTAMP ? SET host = ?, first_seen = ? WHERE domain_id = ?"
)
_URL_GET = (
    "SELECT url, domain_id, first_seen, last_observed_at, last_status, last_page_version_id "
    "FROM {ks}.url_state WHERE url_id = ?"
)
_DOMAIN_URLS = (
    "SELECT url_id, url, first_seen, last_observed_at, last_status FROM {ks}.urls_by_domain "
    "WHERE domain_id = ? AND shard = ?"
)
_DOMAIN_GET = "SELECT host, first_seen FROM {ks}.domains WHERE domain_id = ?"


def _discovery_statements(s: ScyllaSession, url: UrlRef, seen_at: datetime) -> list[BoundStatement]:
    ts = earliest_wins(seen_at)
    shard = shard_of(url.url_id, URLS_BY_DOMAIN_SHARDS)
    return [
        s.bind(_URL_FIRST, DERIVED, (ts, url.url, url.domain_id.uuid, seen_at, url.url_id.uuid)),
        s.bind(
            _DOMAIN_URL_FIRST,
            DERIVED,
            (ts, url.url, seen_at, url.domain_id.uuid, shard, url.url_id.uuid),
        ),
    ]


def _domain_statement(s: ScyllaSession, url: UrlRef, seen_at: datetime) -> BoundStatement:
    return s.bind(
        _DOMAIN_FIRST,
        DERIVED,
        (earliest_wins(seen_at), url.url.host, seen_at, url.domain_id.uuid),
    )


class ScyllaUrlRepository:
    def __init__(self, session: ScyllaSession) -> None:
        self._s = session

    def record_discovered(self, urls: Sequence[UrlRef], *, seen_at: datetime) -> None:
        statements: list[BoundStatement] = []
        domains: dict[DomainId, UrlRef] = {}
        for url in {u.url_id: u for u in urls}.values():
            statements += _discovery_statements(self._s, url, seen_at)
            domains.setdefault(url.domain_id, url)
        statements += [_domain_statement(self._s, u, seen_at) for u in domains.values()]
        self._s.execute_all(statements)

    def get(self, url_id: UrlId) -> KnownUrl | None:
        rows = self._s.execute(self._s.bind(_URL_GET, EC, (url_id.uuid,)))
        if not rows or rows[0].url is None:
            return None
        r = rows[0]
        return KnownUrl(
            url_id=url_id,
            url=r.url,
            domain_id=DomainId.from_uuid(r.domain_id),
            first_seen=utc(r.first_seen),
            last_observed_at=utc(r.last_observed_at) if r.last_observed_at else None,
            last_status=r.last_status,
            last_page_version_id=(
                PageVersionId.from_uuid(r.last_page_version_id) if r.last_page_version_id else None
            ),
        )

    def urls_of_domain(self, domain_id: DomainId) -> Iterator[KnownUrl]:
        for shard in range(URLS_BY_DOMAIN_SHARDS):
            statement = self._s.bind(_DOMAIN_URLS, EC, (domain_id.uuid, shard))
            for r in self._s.stream(statement):
                if r.url is None:
                    continue
                yield KnownUrl(
                    url_id=UrlId.from_uuid(r.url_id),
                    url=r.url,
                    domain_id=domain_id,
                    first_seen=utc(r.first_seen),
                    last_observed_at=utc(r.last_observed_at) if r.last_observed_at else None,
                    last_status=r.last_status,
                )

    def domain(self, domain_id: DomainId) -> DomainRecord | None:
        rows = self._s.execute(self._s.bind(_DOMAIN_GET, EC, (domain_id.uuid,)))
        if not rows or rows[0].host is None:
            return None
        return DomainRecord(
            domain=Domain(domain_id=domain_id, host=rows[0].host),
            first_seen=utc(rows[0].first_seen),
        )


# --- W4-W8, W14 ------------------------------------------------------------------

_OBS_INSERT = (
    "INSERT INTO {ks}.page_observations (observation_id, url_id, observed_at, doc) "
    "VALUES (?, ?, ?, ?)"
)
_OBS_BY_URL_INSERT = (
    "INSERT INTO {ks}.page_observations_by_url (url_id, month, observed_at, observation_id, "
    "doc) VALUES (?, ?, ?, ?, ?)"
)
_OBS_GET = "SELECT doc FROM {ks}.page_observations WHERE observation_id = ?"
_OBS_HISTORY = (
    "SELECT doc FROM {ks}.page_observations_by_url WHERE url_id = ? AND month = ? "
    "AND observed_at >= ? AND observed_at <= ? LIMIT ?"
)
_LATEST_SET = (
    "UPDATE {ks}.latest_observation_by_url USING TIMESTAMP ? SET observation_id = ?, "
    "observed_at = ?, http_status = ?, page_version_id = ?, content_type = ?, etag = ?, "
    "last_modified = ?, snapshot_uri = ? WHERE url_id = ?"
)
_LATEST_GET = (
    "SELECT observation_id, observed_at, http_status, page_version_id, content_type, etag, "
    "last_modified, snapshot_uri FROM {ks}.latest_observation_by_url WHERE url_id = ?"
)
_VERSION_FIRST = (
    "UPDATE {ks}.page_versions_by_url USING TIMESTAMP ? SET first_seen = ?, "
    "first_observation_id = ? WHERE url_id = ? AND month = ? AND page_version_id = ?"
)
_VERSION_LAST = (
    "UPDATE {ks}.page_versions_by_url USING TIMESTAMP ? SET last_seen = ?, body_digest = ?, "
    "body_size = ?, content_type = ? WHERE url_id = ? AND month = ? AND page_version_id = ?"
)
_VERSIONS_GET = (
    "SELECT page_version_id, first_seen, first_observation_id, last_seen, body_digest, "
    "body_size, content_type FROM {ks}.page_versions_by_url WHERE url_id = ? AND month = ?"
)
_DOMAIN_DAY_INSERT = (
    "INSERT INTO {ks}.observations_by_domain_day (domain_id, day, shard, observed_at, "
    "observation_id, url_id, http_status, page_version_id, content_type, body_size) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_DOMAIN_DAY_GET = (
    "SELECT observed_at, observation_id, url_id, http_status, page_version_id, content_type, "
    "body_size FROM {ks}.observations_by_domain_day WHERE domain_id = ? AND day = ? "
    "AND shard = ?"
)


class ScyllaPageObservationRepository:
    def __init__(self, session: ScyllaSession, outbox: ScyllaOutbox) -> None:
        self._s = session
        self._outbox = outbox

    def record(
        self, observation: PageObservation, *, event: EventEnvelope[PageObserved] | None = None
    ) -> None:
        require_payload(event, lambda p: p.observation == observation, "page observation")
        o = observation
        doc = o.model_dump_json()
        auth = [
            self._s.bind(
                _OBS_INSERT,
                AUTH,
                (o.observation_id.uuid, o.requested.url_id.uuid, o.observed_at, doc),
            ),
            self._s.bind(
                _OBS_BY_URL_INSERT,
                AUTH,
                (
                    o.requested.url_id.uuid,
                    month_bucket(o.observed_at),
                    o.observed_at,
                    o.observation_id.uuid,
                    doc,
                ),
            ),
        ]
        write_authoritative(self._s, self._outbox, auth, event)
        self._s.execute_all(self._derived(o))

    def _derived(self, o: PageObservation) -> list[BoundStatement]:
        """W5, W8, W12, W11, W13, W14 for one observation (also the rebuild step)."""
        s = self._s
        url = o.requested
        latest, earliest = latest_wins(o.observed_at), earliest_wins(o.observed_at)
        validators = o.validators or HttpValidators()
        month = month_bucket(o.observed_at)
        version_key = (o.final.url_id.uuid, month, o.page_version_id.uuid)
        domain_shard = shard_of(url.url_id, URLS_BY_DOMAIN_SHARDS)
        return [
            s.bind(
                _LATEST_SET,
                DERIVED,
                (
                    latest,
                    o.observation_id.uuid,
                    o.observed_at,
                    o.http_status,
                    o.page_version_id.uuid,
                    o.content_type,
                    validators.etag,
                    validators.last_modified,
                    o.snapshot.uri if o.snapshot else None,
                    url.url_id.uuid,
                ),
            ),
            s.partition_batch(
                [
                    s.bind(
                        _VERSION_FIRST,
                        DERIVED,
                        (earliest, o.observed_at, o.observation_id.uuid, *version_key),
                    ),
                    s.bind(
                        _VERSION_LAST,
                        DERIVED,
                        (
                            latest,
                            o.observed_at,
                            str(o.body_digest),
                            o.body_size,
                            o.content_type,
                            *version_key,
                        ),
                    ),
                ],
                DERIVED,
            ),
            s.partition_batch(
                [
                    *_discovery_statements(s, url, o.observed_at)[:1],
                    s.bind(
                        _URL_LAST,
                        DERIVED,
                        (
                            latest,
                            o.observed_at,
                            o.http_status,
                            o.page_version_id.uuid,
                            url.url_id.uuid,
                        ),
                    ),
                ],
                DERIVED,
            ),
            s.partition_batch(
                [
                    *_discovery_statements(s, url, o.observed_at)[1:],
                    s.bind(
                        _DOMAIN_URL_LAST,
                        DERIVED,
                        (
                            latest,
                            o.observed_at,
                            o.http_status,
                            url.domain_id.uuid,
                            domain_shard,
                            url.url_id.uuid,
                        ),
                    ),
                ],
                DERIVED,
            ),
            _domain_statement(s, url, o.observed_at),
            s.bind(
                _DOMAIN_DAY_INSERT,
                DERIVED,
                (
                    url.domain_id.uuid,
                    day_bucket(o.observed_at),
                    shard_of(url.url_id, DOMAIN_DAY_SHARDS),
                    o.observed_at,
                    o.observation_id.uuid,
                    url.url_id.uuid,
                    o.http_status,
                    o.page_version_id.uuid,
                    o.content_type,
                    o.body_size,
                ),
            ),
        ]

    def rebuild_url(self, url_id: UrlId, *, since: datetime, until: datetime) -> int:
        """Re-derive W5/W8/W11-W14 from the authoritative W6 history. Returns rows used."""
        count = 0
        for month in months_back(until, since):
            rows = self._s.stream(
                self._s.bind(_OBS_HISTORY, STRONG, (url_id.uuid, month, since, until, 2**31 - 1))
            )
            for r in rows:
                self._s.execute_all(self._derived(PageObservation.model_validate_json(r.doc)))
                count += 1
        return count

    def get(self, observation_id: ObservationId) -> PageObservation | None:
        rows = self._s.execute(self._s.bind(_OBS_GET, STRONG, (observation_id.uuid,)))
        return PageObservation.model_validate_json(rows[0].doc) if rows else None

    def latest(self, url_id: UrlId) -> LatestObservation | None:
        rows = self._s.execute(self._s.bind(_LATEST_GET, EC, (url_id.uuid,)))
        if not rows or rows[0].observation_id is None:
            return None
        r = rows[0]
        validators = (
            HttpValidators(etag=r.etag, last_modified=r.last_modified)
            if r.etag is not None or r.last_modified is not None
            else None
        )
        return LatestObservation(
            url_id=url_id,
            observation_id=ObservationId.from_uuid(r.observation_id),
            observed_at=utc(r.observed_at),
            http_status=r.http_status,
            page_version_id=PageVersionId.from_uuid(r.page_version_id),
            content_type=r.content_type,
            validators=validators,
            snapshot_uri=r.snapshot_uri,
        )

    def history(
        self, url_id: UrlId, *, since: datetime, until: datetime, limit: int = 100
    ) -> list[PageObservation]:
        found: list[PageObservation] = []
        for month in months_back(until, since):
            remaining = limit - len(found)
            if remaining <= 0:
                break
            rows = self._s.execute(
                self._s.bind(_OBS_HISTORY, EC, (url_id.uuid, month, since, until, remaining))
            )
            found += [PageObservation.model_validate_json(r.doc) for r in rows]
        return found

    def versions(
        self, url_id: UrlId, *, since: datetime, until: datetime
    ) -> list[PageVersionSighting]:
        results = self._s.execute_many(
            self._s.bind(_VERSIONS_GET, EC, (url_id.uuid, month))
            for month in months_back(until, since)
        )
        merged: dict[PageVersionId, PageVersionSighting] = {}
        for rows in results:
            for r in rows:
                if r.first_seen is None or r.last_seen is None:
                    continue  # half-written row of a concurrent write; completes on replay
                sighting = PageVersionSighting(
                    page_version_id=PageVersionId.from_uuid(r.page_version_id),
                    first_seen=utc(r.first_seen),
                    first_observation_id=ObservationId.from_uuid(r.first_observation_id),
                    last_seen=utc(r.last_seen),
                    body_digest=ContentDigest(r.body_digest),
                    body_size=r.body_size,
                    content_type=r.content_type,
                )
                known = merged.get(sighting.page_version_id)
                if known is None:
                    merged[sighting.page_version_id] = sighting
                else:
                    first = known if known.first_seen <= sighting.first_seen else sighting
                    last = known if known.last_seen >= sighting.last_seen else sighting
                    merged[sighting.page_version_id] = PageVersionSighting(
                        page_version_id=known.page_version_id,
                        first_seen=first.first_seen,
                        first_observation_id=first.first_observation_id,
                        last_seen=last.last_seen,
                        body_digest=last.body_digest,
                        body_size=last.body_size,
                        content_type=last.content_type,
                    )
        return sorted(merged.values(), key=lambda v: v.first_seen, reverse=True)

    def recent_for_domain_day(self, domain_id: DomainId, day: datetime) -> list[ObservationSummary]:
        results = self._s.execute_many(
            self._s.bind(_DOMAIN_DAY_GET, EC, (domain_id.uuid, day_bucket(day), shard))
            for shard in range(DOMAIN_DAY_SHARDS)
        )
        summaries = [
            ObservationSummary(
                observation_id=ObservationId.from_uuid(r.observation_id),
                url_id=UrlId.from_uuid(r.url_id),
                observed_at=utc(r.observed_at),
                http_status=r.http_status,
                page_version_id=PageVersionId.from_uuid(r.page_version_id),
                content_type=r.content_type,
                body_size=r.body_size,
            )
            for rows in results
            for r in rows
        ]
        return sorted(summaries, key=lambda s: s.observed_at, reverse=True)


# --- W9, W10 ---------------------------------------------------------------------

_LINK_INSERT = (
    "INSERT INTO {ks}.links_by_page_version (page_version_id, target_url_id, relation, "
    "target_url, anchor_text, nofollow) VALUES (?, ?, ?, ?, ?, ?)"
)
_LINKS_GET = (
    "SELECT target_url, relation, anchor_text, nofollow FROM {ks}.links_by_page_version "
    "WHERE page_version_id = ?"
)
_INLINK_FIRST = (
    "UPDATE {ks}.inlinks_by_url USING TIMESTAMP ? SET source_url = ?, first_seen = ? "
    "WHERE target_url_id = ? AND shard = ? AND source_url_id = ?"
)
_INLINK_LAST = (
    "UPDATE {ks}.inlinks_by_url USING TIMESTAMP ? SET last_seen = ?, last_page_version_id = ? "
    "WHERE target_url_id = ? AND shard = ? AND source_url_id = ?"
)
_INLINKS_GET = (
    "SELECT source_url_id, source_url, first_seen, last_seen, last_page_version_id "
    "FROM {ks}.inlinks_by_url WHERE target_url_id = ? AND shard = ?"
)


class ScyllaLinkRepository:
    def __init__(self, session: ScyllaSession, outbox: ScyllaOutbox) -> None:
        self._s = session
        self._outbox = outbox

    def record(
        self,
        discovered: UrlsDiscovered,
        *,
        observed_at: datetime,
        event: EventEnvelope[UrlsDiscovered] | None = None,
    ) -> None:
        """Pattern B (ADR-013): up to 10 000 links exceed one batch, so the link rows are
        written first and the outbox row last; a crash before it is completed by the
        redelivery of the ``page.observed`` that triggered extraction."""
        require_payload(event, lambda p: p == discovered, "link batch")
        s = self._s
        pgv = discovered.page_version_id.uuid
        rows = [
            s.bind(
                _LINK_INSERT,
                AUTH,
                (
                    pgv,
                    link.target.url_id.uuid,
                    link.relation.value,
                    link.target.url,
                    link.anchor_text,
                    link.nofollow,
                ),
            )
            for link in discovered.links
        ]
        s.execute_all(
            s.partition_batch(rows[i : i + _LINK_CHUNK], AUTH)
            for i in range(0, len(rows), _LINK_CHUNK)
        )
        if event is not None:
            self._outbox.enqueue(event)
        source = discovered.page
        earliest, latest = earliest_wins(observed_at), latest_wins(observed_at)
        shard = shard_of(source.url_id, INLINK_SHARDS)
        targets = {link.target.url_id for link in discovered.links}
        s.execute_all(
            s.partition_batch(
                [
                    s.bind(
                        _INLINK_FIRST,
                        DERIVED,
                        (earliest, source.url, observed_at, target.uuid, shard, source.url_id.uuid),
                    ),
                    s.bind(
                        _INLINK_LAST,
                        DERIVED,
                        (latest, observed_at, pgv, target.uuid, shard, source.url_id.uuid),
                    ),
                ],
                DERIVED,
            )
            for target in targets
        )

    def links_of(self, page_version_id: PageVersionId) -> list[DiscoveredLink]:
        rows = self._s.execute(self._s.bind(_LINKS_GET, EC, (page_version_id.uuid,)))
        return [
            DiscoveredLink(
                target=UrlRef.of(r.target_url),
                relation=LinkRelation(r.relation),
                anchor_text=r.anchor_text,
                nofollow=bool(r.nofollow),
            )
            for r in rows
        ]

    def inlinks(self, url_id: UrlId) -> Iterator[Inlink]:
        for shard in range(INLINK_SHARDS):
            for r in self._s.stream(self._s.bind(_INLINKS_GET, EC, (url_id.uuid, shard))):
                if r.first_seen is None or r.last_seen is None:
                    continue
                yield Inlink(
                    source_url_id=UrlId.from_uuid(r.source_url_id),
                    source_url=r.source_url,
                    first_seen=utc(r.first_seen),
                    last_seen=utc(r.last_seen),
                    last_page_version_id=PageVersionId.from_uuid(r.last_page_version_id),
                )


__all__ = [
    "ScyllaFetchAttemptRepository",
    "ScyllaLinkRepository",
    "ScyllaPageObservationRepository",
    "ScyllaUrlRepository",
]
