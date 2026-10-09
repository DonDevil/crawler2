"""Stored forms of the P8-owned tables (V005) and the values passed between P8 parts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.ids import ContentId, MediaId
from antipiracy_contracts.models.media import ContentKey, MediaKind, ProbeStatus, TechnicalHints

from crawler2.storage.layout import bucket_start

SCHEME = "full-bytes/v1"
"""Level-3 ContentKey scheme: SHA-256 of the entire byte sequence served (P8-D5)."""


class ReaderMode(StrEnum):
    RUNNING = "running"
    GAP = "gap"


@dataclass(frozen=True, slots=True)
class ReaderState:
    """One row of ``media_reader_state`` (ADR-024 rule 3)."""

    reader: str
    epoch: int
    owner: str | None
    lease_until: datetime
    next_bucket: int
    state: ReaderMode = ReaderMode.RUNNING
    gap_from: int | None = None
    gap_to: int | None = None
    gaps: int = 0
    started_bucket: int | None = None

    def lag(self, now: datetime) -> timedelta:
        """How far behind ``now`` the resolved observations are complete."""
        return now - bucket_start(self.next_bucket)


@dataclass(frozen=True, slots=True)
class GapRecord:
    """One row of ``media_reader_gaps``."""

    reader: str
    gap_from: int
    gap_to: int
    detected_at: datetime | None
    detected_by: str | None
    accepted_at: datetime | None
    accepted_by: str | None
    note: str | None


@dataclass(frozen=True, slots=True)
class StoredClaim:
    """One row of ``media_observation_claims``: the decided envelope (wire bytes)."""

    event_id: str
    envelope: str
    claimed_epoch: int | None
    claimed_by: str | None
    committed: bool


@dataclass(frozen=True, slots=True)
class ClaimResult:
    won: bool
    claim: StoredClaim


# --- probe and hash facts (design §5.4) ---------------------------------------------


class HashOutcome(StrEnum):
    OK = "ok"
    TOO_LARGE = "too_large"
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    FORBIDDEN = "forbidden"
    CHANGED_DURING_READ = "changed_during_read"
    HTTP_ERROR = "http_error"
    """A non-200 final response (other than 401/403, which are ``forbidden``)."""
    CONTENT_ENCODED = "content_encoded"
    """The server applied a Content-Encoding despite ``Accept-Encoding: identity``:
    the served representation is not the identity byte sequence (design §3.4)."""


@dataclass(frozen=True, slots=True)
class Validators:
    """What tells two probes/reads of one locator apart: total length, ETag, Last-Modified."""

    length: int | None = None
    etag: str | None = None
    last_modified: str | None = None

    @property
    def empty(self) -> bool:
        return self.length is None and self.etag is None and self.last_modified is None


@dataclass(frozen=True, slots=True)
class ProbeRecord:
    """One probe of one media (``media_probes``); provenance included."""

    probe_id: str
    media_id: str
    locator_url: str
    kind: MediaKind
    probed_at: datetime
    probe_status: ProbeStatus
    worker: str
    fetch_outcome: str
    fetch_detail: str | None = None
    final_url: str | None = None
    redirect_count: int = 0
    http_status: int | None = None
    content_type: str | None = None
    declared_length: int | None = None
    total_length: int | None = None
    accept_ranges: str | None = None
    range_honoured: bool = False
    etag: str | None = None
    last_modified: str | None = None
    bytes_read: int = 0
    container: str | None = None
    head_sample_sha256: str | None = None
    referer: str | None = None

    @property
    def validators(self) -> Validators:
        length = self.total_length
        if length is None and not self.range_honoured:
            length = self.declared_length  # a full (non-ranged) response states the whole size
        return Validators(length, self.etag, self.last_modified)

    @property
    def is_media(self) -> bool:
        """The probe identified a media content type or container (design §5.1)."""
        ctype = (self.content_type or "").split(";", 1)[0].strip().lower()
        return self.container is not None or ctype.startswith(("video/", "audio/"))

    def hints(self) -> TechnicalHints:
        return TechnicalHints(
            content_length=self.validators.length,
            etag=self.etag[:500] if self.etag else None,
            media_type=self.content_type[:200] if self.content_type else None,
        )

    def to_json(self) -> str:
        doc = asdict(self)
        doc["probed_at"] = self.probed_at.isoformat()
        return json.dumps(doc, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> ProbeRecord:
        doc: dict[str, Any] = json.loads(text)
        doc["probed_at"] = datetime.fromisoformat(doc["probed_at"])
        doc["kind"] = MediaKind(doc["kind"])
        doc["probe_status"] = ProbeStatus(doc["probe_status"])
        return cls(**doc)


@dataclass(frozen=True, slots=True)
class HashRecord:
    """One full-content hash attempt (``media_hashes``); provenance included."""

    hash_id: str
    media_id: str
    locator_url: str
    hashed_at: datetime
    outcome: HashOutcome
    worker: str
    probe_id: str
    validators: Validators
    """Of the response that was read (equal to the probe's for an ``ok`` hash)."""
    scheme: str = SCHEME
    digest: str | None = None
    byte_count: int = 0
    final_url: str | None = None
    redirect_count: int = 0
    http_status: int | None = None
    detail: str | None = None
    elapsed_s: float = 0.0

    @property
    def content(self) -> ContentKey | None:
        if self.outcome is not HashOutcome.OK or self.digest is None:
            return None
        return ContentKey.of(self.scheme, ContentDigest(self.digest))

    @property
    def content_id(self) -> ContentId | None:
        key = self.content
        return key.content_id if key is not None else None

    def to_json(self) -> str:
        doc = asdict(self)
        doc["hashed_at"] = self.hashed_at.isoformat()
        return json.dumps(doc, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> HashRecord:
        doc: dict[str, Any] = json.loads(text)
        doc["hashed_at"] = datetime.fromisoformat(doc["hashed_at"])
        doc["outcome"] = HashOutcome(doc["outcome"])
        doc["validators"] = Validators(**doc["validators"])
        return cls(**doc)


@dataclass(frozen=True, slots=True)
class MediaLatest:
    """``media_probe_latest``: the newest probe, hash attempt and ok hash of one media."""

    media_id: MediaId
    probe: ProbeRecord | None = None
    hash: HashRecord | None = None
    ok_hash: HashRecord | None = None

    def current_content(self) -> ContentKey | None:
        """The level-3 key valid for the *current* version (design §6.2), else None.

        Only an ok hash whose validators equal the latest probe's counts; a
        probe that shows any change (length, ETag, Last-Modified) makes it stale.
        """
        if self.probe is None or self.ok_hash is None:
            return None
        if self.probe.probe_status is not ProbeStatus.PROBED:
            return None
        if self.ok_hash.validators != self.probe.validators:
            return None
        # Without any validator, equality proves nothing; the hash stays current
        # until the rehash_after re-hash (a stated limitation, design §12).
        return self.ok_hash.content
