"""In-memory stand-ins for the P2 repositories P5 uses, with the same convergence rules
(first_* earliest-wins, last_* latest-wins, events stored with the row they describe)."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events import EventEnvelope
from antipiracy_contracts.events.media import MediaDiscovered
from antipiracy_contracts.events.web import PageChanged, UrlsDiscovered
from antipiracy_contracts.ids import PageRevisionId, PageVersionId, UrlId
from antipiracy_contracts.models.blobs import BlobRef
from antipiracy_contracts.models.web import UrlRef

from crawler2.storage.repositories import (
    ExtractRecord,
    PageRevision,
    RetentionDecision,
    RevisionSighting,
)


@dataclass
class FakeObjects:
    blobs: dict[str, bytes] = field(default_factory=dict)
    gets: int = 0

    def put_bytes(self, data: bytes) -> BlobRef:
        digest = ContentDigest.of_bytes(data)
        self.blobs[str(digest)] = data
        return BlobRef(uri=f"s3://test/{digest.hex}", digest=digest, size_bytes=len(data))

    def get(self, ref: BlobRef) -> bytes:
        self.gets += 1
        return self.blobs[str(ref.digest)]


@dataclass
class FakeLinks:
    recorded: list[tuple[UrlsDiscovered, EventEnvelope[UrlsDiscovered] | None]] = field(
        default_factory=list
    )

    def record(
        self,
        discovered: UrlsDiscovered,
        *,
        observed_at: datetime,
        event: EventEnvelope[UrlsDiscovered] | None = None,
    ) -> None:
        self.recorded.append((discovered, event))


@dataclass
class FakeUrls:
    seen: set[UrlId] = field(default_factory=set)

    def record_discovered(self, urls: Sequence[UrlRef], *, seen_at: datetime) -> None:
        self.seen.update(u.url_id for u in urls)


class FakePages:
    def __init__(self) -> None:
        self.extracts: dict[PageVersionId, ExtractRecord] = {}
        self.rows: dict[tuple[UrlId, PageRevisionId], PageRevision] = {}
        self.outbox: list[EventEnvelope[PageChanged] | EventEnvelope[MediaDiscovered]] = []
        self.decisions: list[RetentionDecision] = []
        self.lock = threading.Lock()
        self.before_write: threading.Barrier | None = None

    def record_extract(self, record: ExtractRecord) -> None:
        self.extracts[record.page_version_id] = record

    def extract(self, page_version_id: PageVersionId) -> ExtractRecord | None:
        return self.extracts.get(page_version_id)

    def record_sighting(
        self,
        sighting: RevisionSighting,
        *,
        changed: EventEnvelope[PageChanged] | None = None,
        media: EventEnvelope[MediaDiscovered] | None = None,
    ) -> None:
        if self.before_write is not None:
            self.before_write.wait(timeout=5)
        s = sighting
        with self.lock:
            key = (s.url_id, s.revision_id)
            row = self.rows.get(key)
            fresh = PageRevision(
                revision_id=s.revision_id,
                normalization=s.normalization,
                normalized_digest=s.normalized_digest,
                first_seen=s.observed_at,
                first_observation_id=s.observation_id,
                first_page_version_id=s.page_version_id,
                last_seen=s.observed_at,
                last_observation_id=s.observation_id,
                last_page_version_id=s.page_version_id,
            )
            if row is None:
                row = fresh
            else:
                if s.observed_at < row.first_seen:
                    row = replace(
                        row,
                        first_seen=s.observed_at,
                        first_observation_id=s.observation_id,
                        first_page_version_id=s.page_version_id,
                    )
                if s.observed_at > row.last_seen:
                    row = replace(
                        row,
                        last_seen=s.observed_at,
                        last_observation_id=s.observation_id,
                        last_page_version_id=s.page_version_id,
                    )
            self.rows[key] = row
            self.outbox.extend(e for e in (changed, media) if e is not None)

    def revision(self, url_id: UrlId, revision_id: PageRevisionId) -> PageRevision | None:
        return self.rows.get((url_id, revision_id))

    def revisions(self, url_id: UrlId) -> list[PageRevision]:
        found = [r for (u, _), r in self.rows.items() if u == url_id]
        return sorted(found, key=lambda r: r.first_seen, reverse=True)

    def record_retention(self, decision: RetentionDecision) -> None:
        self.decisions.append(decision)

    def retention(self, digest: ContentDigest) -> list[RetentionDecision]:
        return [d for d in self.decisions if d.digest == digest]

    def changed(self) -> list[PageChanged]:
        return [e.payload for e in self.outbox if isinstance(e.payload, PageChanged)]
