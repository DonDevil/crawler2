"""P5 page intelligence on Scylla: extracts, revisions, archival decisions (V002).

Rules as in ``web.py``: idempotent upserts keyed by contract IDs, convergent
cell timestamps for first/last sightings, events in the same logged batch
as the row they describe (ADR-013 pattern A).
"""

from __future__ import annotations

from typing import Any

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events import EventEnvelope
from antipiracy_contracts.events.media import MediaDiscovered
from antipiracy_contracts.events.web import PageChanged
from antipiracy_contracts.ids import ObservationId, PageRevisionId, PageVersionId, UrlId

from crawler2.storage.layout import earliest_wins, latest_wins
from crawler2.storage.repositories import (
    ExtractRecord,
    PageRevision,
    RetentionDecision,
    RevisionSighting,
    SnapshotDecision,
)
from crawler2.storage.scylla.outbox import ScyllaOutbox
from crawler2.storage.scylla.session import Consistency, ScyllaSession, utc

AUTH = Consistency.AUTHORITATIVE_WRITE
EC = Consistency.EVENTUAL_READ

_EXTRACT_INSERT = (
    "INSERT INTO {ks}.page_extracts (page_version_id, url_id, extractor, normalization, "
    "revision_id, normalized_digest, observed_at, doc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
_EXTRACT_GET = (
    "SELECT url_id, extractor, normalization, revision_id, normalized_digest, observed_at, doc "
    "FROM {ks}.page_extracts WHERE page_version_id = ?"
)
_REVISION_FIRST = (
    "UPDATE {ks}.page_revisions_by_url USING TIMESTAMP ? SET normalization = ?, "
    "normalized_digest = ?, first_seen = ?, first_observation_id = ?, first_page_version_id = ? "
    "WHERE url_id = ? AND revision_id = ?"
)
_REVISION_LAST = (
    "UPDATE {ks}.page_revisions_by_url USING TIMESTAMP ? SET last_seen = ?, "
    "last_observation_id = ?, last_page_version_id = ? WHERE url_id = ? AND revision_id = ?"
)
_REVISION_COLUMNS = (
    "revision_id, normalization, normalized_digest, first_seen, first_observation_id, "
    "first_page_version_id, last_seen, last_observation_id, last_page_version_id"
)
_REVISION_GET = (
    f"SELECT {_REVISION_COLUMNS} FROM {{ks}}.page_revisions_by_url "
    "WHERE url_id = ? AND revision_id = ?"
)
_REVISIONS_GET = f"SELECT {_REVISION_COLUMNS} FROM {{ks}}.page_revisions_by_url WHERE url_id = ?"
_RETENTION_INSERT = (
    "INSERT INTO {ks}.snapshot_retention (digest, observation_id, decision, reason, decided_at) "
    "VALUES (?, ?, ?, ?, ?)"
)
_RETENTION_GET = (
    "SELECT observation_id, decision, reason, decided_at FROM {ks}.snapshot_retention "
    "WHERE digest = ?"
)


def _revision(row: Any) -> PageRevision | None:
    # A row whose first_* cells are missing was only partially replayed; treat as absent.
    if row.first_seen is None or row.last_seen is None:
        return None
    return PageRevision(
        revision_id=PageRevisionId.from_uuid(row.revision_id),
        normalization=row.normalization,
        normalized_digest=ContentDigest(row.normalized_digest),
        first_seen=utc(row.first_seen),
        first_observation_id=ObservationId.from_uuid(row.first_observation_id),
        first_page_version_id=PageVersionId.from_uuid(row.first_page_version_id),
        last_seen=utc(row.last_seen),
        last_observation_id=ObservationId.from_uuid(row.last_observation_id),
        last_page_version_id=PageVersionId.from_uuid(row.last_page_version_id),
    )


class ScyllaPageIntelligenceRepository:
    def __init__(self, session: ScyllaSession, outbox: ScyllaOutbox) -> None:
        self._s = session
        self._outbox = outbox

    def record_extract(self, record: ExtractRecord) -> None:
        self._s.execute(
            self._s.bind(
                _EXTRACT_INSERT,
                AUTH,
                (
                    record.page_version_id.uuid,
                    record.url_id.uuid,
                    record.extractor,
                    record.normalization,
                    record.revision_id.uuid,
                    str(record.normalized_digest),
                    record.observed_at,
                    record.doc,
                ),
            )
        )

    def extract(self, page_version_id: PageVersionId) -> ExtractRecord | None:
        rows = self._s.execute(self._s.bind(_EXTRACT_GET, EC, (page_version_id.uuid,)))
        if not rows:
            return None
        r = rows[0]
        return ExtractRecord(
            page_version_id=page_version_id,
            url_id=UrlId.from_uuid(r.url_id),
            extractor=r.extractor,
            normalization=r.normalization,
            revision_id=PageRevisionId.from_uuid(r.revision_id),
            normalized_digest=ContentDigest(r.normalized_digest),
            observed_at=utc(r.observed_at),
            doc=r.doc,
        )

    def record_sighting(
        self,
        sighting: RevisionSighting,
        *,
        changed: EventEnvelope[PageChanged] | None = None,
        media: EventEnvelope[MediaDiscovered] | None = None,
    ) -> None:
        o = sighting
        if changed is not None and not (
            changed.payload.revision_id == o.revision_id
            and changed.payload.page_observation_id == o.observation_id
            and changed.payload.page.url_id == o.url_id
        ):
            raise ValueError("page.changed payload does not describe this sighting")
        if media is not None and media.payload.page_observation_id != o.observation_id:
            raise ValueError("media.discovered payload does not describe this sighting")
        at = o.observed_at
        key = (o.url_id.uuid, o.revision_id.uuid)
        statements = [
            self._s.bind(
                _REVISION_FIRST,
                AUTH,
                (
                    earliest_wins(at),
                    o.normalization,
                    str(o.normalized_digest),
                    at,
                    o.observation_id.uuid,
                    o.page_version_id.uuid,
                    *key,
                ),
            ),
            self._s.bind(
                _REVISION_LAST,
                AUTH,
                (latest_wins(at), at, o.observation_id.uuid, o.page_version_id.uuid, *key),
            ),
        ]
        events = []
        if changed is not None:
            events.append(self._outbox.statement(changed))
        if media is not None:
            events.append(self._outbox.statement(media))
        if events:
            self._s.execute(self._s.logged_batch([*statements, *events], AUTH))
        else:
            self._s.execute(self._s.partition_batch(statements, AUTH))

    def revision(self, url_id: UrlId, revision_id: PageRevisionId) -> PageRevision | None:
        rows = self._s.execute(self._s.bind(_REVISION_GET, EC, (url_id.uuid, revision_id.uuid)))
        return _revision(rows[0]) if rows else None

    def revisions(self, url_id: UrlId) -> list[PageRevision]:
        rows = self._s.execute(self._s.bind(_REVISIONS_GET, EC, (url_id.uuid,)))
        found = [r for r in map(_revision, rows) if r is not None]
        return sorted(found, key=lambda r: r.first_seen, reverse=True)

    def record_retention(self, decision: RetentionDecision) -> None:
        self._s.execute(
            self._s.bind(
                _RETENTION_INSERT,
                AUTH,
                (
                    str(decision.digest),
                    decision.observation_id.uuid,
                    decision.decision.value,
                    decision.reason,
                    decision.decided_at,
                ),
            )
        )

    def retention(self, digest: ContentDigest) -> list[RetentionDecision]:
        rows = self._s.execute(self._s.bind(_RETENTION_GET, EC, (str(digest),)))
        return [
            RetentionDecision(
                digest=digest,
                observation_id=ObservationId.from_uuid(r.observation_id),
                decision=SnapshotDecision(r.decision),
                reason=r.reason,
                decided_at=utc(r.decided_at),
            )
            for r in rows
        ]
