"""Media registry storage (M1-M6) on Scylla. Same write pattern as web.py."""

from __future__ import annotations

from datetime import datetime

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events import EventEnvelope
from antipiracy_contracts.events.media import MediaObserved
from antipiracy_contracts.ids import ContentId, MediaId, ObservationId, PageVersionId, UrlId
from antipiracy_contracts.models.media import Media, MediaKind, MediaObservation, ProbeStatus
from cassandra.query import BoundStatement

from crawler2.storage.layout import (
    CONTENT_SHARDS,
    MEDIA_MONTH_SHARDS,
    earliest_wins,
    latest_wins,
    month_bucket,
    months_back,
    shard_of,
)
from crawler2.storage.repositories import (
    ContentVersion,
    MediaOnPage,
    MediaRecord,
    MediaSighting,
    MediaWithContent,
)
from crawler2.storage.scylla.outbox import ScyllaOutbox
from crawler2.storage.scylla.session import Consistency, ScyllaSession, utc
from crawler2.storage.scylla.web import require_payload, write_authoritative

AUTH = Consistency.AUTHORITATIVE_WRITE
DERIVED = Consistency.DERIVED_WRITE
EC = Consistency.EVENTUAL_READ

_OBS_INSERT = (
    "INSERT INTO {ks}.media_observations (page_observation_id, media_id, observed_at, doc) "
    "VALUES (?, ?, ?, ?)"
)
_BY_MEDIA_INSERT = (
    "INSERT INTO {ks}.media_observations_by_media (media_id, month, shard, observed_at, "
    "page_observation_id, page_url_id, page_url, page_version_id, probe_status, content_id) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_MEDIA_FIRST = "UPDATE {ks}.media USING TIMESTAMP ? SET first_seen = ? WHERE media_id = ?"
_MEDIA_LAST = (
    "UPDATE {ks}.media USING TIMESTAMP ? SET locator_url = ?, kind = ?, doc = ?, last_seen = ? "
    "WHERE media_id = ?"
)
_ON_PAGE = (
    "UPDATE {ks}.media_by_page_version USING TIMESTAMP ? SET kind = ?, locator_url = ? "
    "WHERE page_version_id = ? AND media_id = ?"
)
_ON_PAGE_CONTENT = (
    "UPDATE {ks}.media_by_page_version USING TIMESTAMP ? SET content_id = ? "
    "WHERE page_version_id = ? AND media_id = ?"
)
_BY_CONTENT = (
    "UPDATE {ks}.media_by_content USING TIMESTAMP ? SET locator_url = ?, first_seen = ? "
    "WHERE content_id = ? AND shard = ? AND media_id = ?"
)
_VERSION_FIRST = (
    "UPDATE {ks}.media_content_versions USING TIMESTAMP ? SET scheme = ?, digest = ?, "
    "first_seen = ? WHERE media_id = ? AND month = ? AND content_id = ?"
)
_VERSION_LAST = (
    "UPDATE {ks}.media_content_versions USING TIMESTAMP ? SET last_seen = ? "
    "WHERE media_id = ? AND month = ? AND content_id = ?"
)
_MEDIA_GET = "SELECT doc, first_seen, last_seen FROM {ks}.media WHERE media_id = ?"
_OBS_GET = "SELECT doc FROM {ks}.media_observations WHERE page_observation_id = ? AND media_id = ?"
_OBS_ON = "SELECT doc FROM {ks}.media_observations WHERE page_observation_id = ?"
_SIGHTINGS = (
    "SELECT observed_at, page_observation_id, page_url_id, page_url, page_version_id, "
    "probe_status, content_id FROM {ks}.media_observations_by_media "
    "WHERE media_id = ? AND month = ? AND shard = ? AND observed_at >= ? AND observed_at <= ? "
    "LIMIT ?"
)
_ON_PAGE_GET = (
    "SELECT media_id, kind, locator_url, content_id FROM {ks}.media_by_page_version "
    "WHERE page_version_id = ?"
)
_BY_CONTENT_GET = (
    "SELECT media_id, locator_url, first_seen FROM {ks}.media_by_content "
    "WHERE content_id = ? AND shard = ?"
)
_VERSIONS_GET = (
    "SELECT content_id, scheme, digest, first_seen, last_seen FROM {ks}.media_content_versions "
    "WHERE media_id = ? AND month = ?"
)


class ScyllaMediaRepository:
    def __init__(self, session: ScyllaSession, outbox: ScyllaOutbox) -> None:
        self._s = session
        self._outbox = outbox

    def record_observation(
        self, observation: MediaObservation, *, event: EventEnvelope[MediaObserved] | None = None
    ) -> None:
        require_payload(event, lambda p: p.observation == observation, "media observation")
        o = observation
        media_id = o.media.media_id
        auth = [
            self._s.bind(
                _OBS_INSERT,
                AUTH,
                (o.page_observation_id.uuid, media_id.uuid, o.observed_at, o.model_dump_json()),
            ),
            self._s.bind(
                _BY_MEDIA_INSERT,
                AUTH,
                (
                    media_id.uuid,
                    month_bucket(o.observed_at),
                    shard_of(o.page_observation_id, MEDIA_MONTH_SHARDS),
                    o.observed_at,
                    o.page_observation_id.uuid,
                    o.page.url_id.uuid,
                    o.page.url,
                    o.page_version_id.uuid,
                    o.probe_status.value,
                    o.content.content_id.uuid if o.content else None,
                ),
            ),
        ]
        write_authoritative(self._s, self._outbox, auth, event)
        self._s.execute_all(self._derived(o))

    def _derived(self, o: MediaObservation) -> list[BoundStatement]:
        """M1, M4, M5, M6 for one observation (also the rebuild step)."""
        s = self._s
        media = o.media
        latest, earliest = latest_wins(o.observed_at), earliest_wins(o.observed_at)
        page_key = (o.page_version_id.uuid, media.media_id.uuid)
        statements: list[BoundStatement] = [
            s.partition_batch(
                [
                    s.bind(_MEDIA_FIRST, DERIVED, (earliest, o.observed_at, media.media_id.uuid)),
                    s.bind(
                        _MEDIA_LAST,
                        DERIVED,
                        (
                            latest,
                            media.locator.url,
                            media.kind.value,
                            media.model_dump_json(),
                            o.observed_at,
                            media.media_id.uuid,
                        ),
                    ),
                ],
                DERIVED,
            ),
            s.bind(_ON_PAGE, DERIVED, (latest, media.kind.value, media.locator.url, *page_key)),
        ]
        if o.content is not None:
            content = o.content
            version_key = (
                media.media_id.uuid,
                month_bucket(o.observed_at),
                content.content_id.uuid,
            )
            statements += [
                s.bind(_ON_PAGE_CONTENT, DERIVED, (latest, content.content_id.uuid, *page_key)),
                s.bind(
                    _BY_CONTENT,
                    DERIVED,
                    (
                        earliest,
                        media.locator.url,
                        o.observed_at,
                        content.content_id.uuid,
                        shard_of(media.media_id, CONTENT_SHARDS),
                        media.media_id.uuid,
                    ),
                ),
                s.partition_batch(
                    [
                        s.bind(
                            _VERSION_FIRST,
                            DERIVED,
                            (
                                earliest,
                                content.scheme,
                                str(content.digest),
                                o.observed_at,
                                *version_key,
                            ),
                        ),
                        s.bind(_VERSION_LAST, DERIVED, (latest, o.observed_at, *version_key)),
                    ],
                    DERIVED,
                ),
            ]
        return statements

    def rebuild_media(self, media_id: MediaId, *, since: datetime, until: datetime) -> int:
        """Re-derive M1/M4/M5/M6 from the authoritative M3 → M2 rows. Returns rows used."""
        count = 0
        for sighting in self.sightings(media_id, since=since, until=until, limit=2**31 - 1):
            observation = self.observation(sighting.page_observation_id, media_id)
            if observation is not None:
                self._s.execute_all(self._derived(observation))
                count += 1
        return count

    def get(self, media_id: MediaId) -> MediaRecord | None:
        rows = self._s.execute(self._s.bind(_MEDIA_GET, EC, (media_id.uuid,)))
        if not rows or rows[0].doc is None or rows[0].first_seen is None:
            return None
        return MediaRecord(
            media=Media.model_validate_json(rows[0].doc),
            first_seen=utc(rows[0].first_seen),
            last_seen=utc(rows[0].last_seen),
        )

    def observation(
        self, page_observation_id: ObservationId, media_id: MediaId
    ) -> MediaObservation | None:
        rows = self._s.execute(
            self._s.bind(_OBS_GET, EC, (page_observation_id.uuid, media_id.uuid))
        )
        return MediaObservation.model_validate_json(rows[0].doc) if rows else None

    def observations_on(self, page_observation_id: ObservationId) -> list[MediaObservation]:
        rows = self._s.execute(self._s.bind(_OBS_ON, EC, (page_observation_id.uuid,)))
        return [MediaObservation.model_validate_json(r.doc) for r in rows]

    def sightings(
        self, media_id: MediaId, *, since: datetime, until: datetime, limit: int = 100
    ) -> list[MediaSighting]:
        found: list[MediaSighting] = []
        for month in months_back(until, since):
            if len(found) >= limit:
                break
            results = self._s.execute_many(
                self._s.bind(_SIGHTINGS, EC, (media_id.uuid, month, shard, since, until, limit))
                for shard in range(MEDIA_MONTH_SHARDS)
            )
            month_rows = [
                MediaSighting(
                    page_observation_id=ObservationId.from_uuid(r.page_observation_id),
                    observed_at=utc(r.observed_at),
                    page_url_id=UrlId.from_uuid(r.page_url_id),
                    page_url=r.page_url,
                    page_version_id=PageVersionId.from_uuid(r.page_version_id),
                    probe_status=ProbeStatus(r.probe_status),
                    content_id=ContentId.from_uuid(r.content_id) if r.content_id else None,
                )
                for rows in results
                for r in rows
            ]
            month_rows.sort(key=lambda m: m.observed_at, reverse=True)
            found += month_rows[: limit - len(found)]
        return found

    def on_page_version(self, page_version_id: PageVersionId) -> list[MediaOnPage]:
        rows = self._s.execute(self._s.bind(_ON_PAGE_GET, EC, (page_version_id.uuid,)))
        return [
            MediaOnPage(
                media_id=MediaId.from_uuid(r.media_id),
                kind=MediaKind(r.kind),
                locator_url=r.locator_url,
                content_id=ContentId.from_uuid(r.content_id) if r.content_id else None,
            )
            for r in rows
            if r.kind is not None
        ]

    def by_content(self, content_id: ContentId) -> list[MediaWithContent]:
        results = self._s.execute_many(
            self._s.bind(_BY_CONTENT_GET, EC, (content_id.uuid, shard))
            for shard in range(CONTENT_SHARDS)
        )
        found = [
            MediaWithContent(
                media_id=MediaId.from_uuid(r.media_id),
                locator_url=r.locator_url,
                first_seen=utc(r.first_seen),
            )
            for rows in results
            for r in rows
        ]
        return sorted(found, key=lambda m: m.first_seen)

    def content_versions(
        self, media_id: MediaId, *, since: datetime, until: datetime
    ) -> list[ContentVersion]:
        results = self._s.execute_many(
            self._s.bind(_VERSIONS_GET, EC, (media_id.uuid, month))
            for month in months_back(until, since)
        )
        merged: dict[ContentId, ContentVersion] = {}
        for rows in results:
            for r in rows:
                if r.first_seen is None or r.last_seen is None:
                    continue
                version = ContentVersion(
                    content_id=ContentId.from_uuid(r.content_id),
                    scheme=r.scheme,
                    digest=ContentDigest(r.digest),
                    first_seen=utc(r.first_seen),
                    last_seen=utc(r.last_seen),
                )
                known = merged.get(version.content_id)
                if known is not None:
                    version = ContentVersion(
                        content_id=known.content_id,
                        scheme=known.scheme,
                        digest=known.digest,
                        first_seen=min(known.first_seen, version.first_seen),
                        last_seen=max(known.last_seen, version.last_seen),
                    )
                merged[version.content_id] = version
        return sorted(merged.values(), key=lambda v: v.first_seen, reverse=True)
