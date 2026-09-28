"""Cross-service projections kept by crawler2 (P1-P5), built from fingerprinter events.

Order independence without LWT (ADR-012 §4): each fact has its own cells
with a timestamp rule, so any delivery order converges —
ready (latest created_at) supersedes failures (highest attempt) by the
read rule, the highest target version wins, and retirement is terminal.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Any

from antipiracy_contracts.events import EventEnvelope
from antipiracy_contracts.events.fingerprinting import EncodeFailed, EncodeRequested
from antipiracy_contracts.events.targets import TargetRetired
from antipiracy_contracts.ids import ContentId, DomainId, EventId, MatchId, MediaId, TargetId
from antipiracy_contracts.models.matching import MatchResult, MatchVerdict
from antipiracy_contracts.models.representations import Representation, RepresentationSpec
from antipiracy_contracts.models.targets import Target

from crawler2.storage.layout import (
    MATCH_MONTH_SHARDS,
    TARGETS_BUCKET,
    earliest_wins,
    latest_wins,
    month_bucket,
    shard_of,
    version_wins,
)
from crawler2.storage.repositories import (
    MatchSummary,
    RepresentationState,
    RepresentationStatus,
    TargetState,
)
from crawler2.storage.scylla.session import Consistency, ScyllaSession, utc

DERIVED = Consistency.DERIVED_WRITE
EC = Consistency.EVENTUAL_READ

DEFAULT_SPEC_KEY = "default"


def spec_key(spec: RepresentationSpec | None) -> str:
    """Absent spec = the fingerprinter's default spec (P1 ``encode.requested``)."""
    if spec is None:
        return DEFAULT_SPEC_KEY
    return f"{spec.name}/{spec.version}/{spec.config_digest}"


_REQUESTED = (
    "UPDATE {ks}.representation_status USING TIMESTAMP ? SET requested_at = ?, "
    "request_event_id = ? WHERE content_id = ? AND spec_key = ?"
)
_READY = (
    "UPDATE {ks}.representation_status USING TIMESTAMP ? SET ready_at = ?, "
    "representation_id = ?, representation_doc = ? WHERE content_id = ? AND spec_key = ?"
)
_FAILED = (
    "UPDATE {ks}.representation_status USING TIMESTAMP ? SET failed_at = ?, failure = ?, "
    "retryable = ?, failed_attempt = ? WHERE content_id = ? AND spec_key = ?"
)
_STATUS = (
    "SELECT spec_key, requested_at, request_event_id, representation_doc, failed_at, failure, "
    "retryable, failed_attempt FROM {ks}.representation_status WHERE content_id = ?"
)
_TARGET_VERSION = (
    "UPDATE {ks}.targets USING TIMESTAMP ? SET version = ?, doc = ? "
    "WHERE bucket = ? AND target_id = ?"
)
_TARGET_RETIRED = (
    "UPDATE {ks}.targets USING TIMESTAMP ? SET retired_at = ?, retire_reason = ? "
    "WHERE bucket = ? AND target_id = ?"
)
_TARGET_COLS = "target_id, doc, retired_at, retire_reason"
_TARGET_GET = f"SELECT {_TARGET_COLS} FROM {{ks}}.targets WHERE bucket = ? AND target_id = ?"
_TARGETS = f"SELECT {_TARGET_COLS} FROM {{ks}}.targets WHERE bucket = ?"
_MATCH_BY_CONTENT = (
    "UPDATE {ks}.matches_by_content USING TIMESTAMP ? SET decided_at = ?, doc = ? "
    "WHERE content_id = ? AND match_id = ?"
)
_MATCH_BY_TARGET = (
    "UPDATE {ks}.matches_by_target USING TIMESTAMP ? SET target_version = ?, content_id = ?, "
    "verdict = ?, confidence = ?, decided_at = ?, media_ids = media_ids + ? "
    "WHERE target_id = ? AND month = ? AND shard = ? AND match_id = ?"
)
_MATCH_BY_DOMAIN = (
    "UPDATE {ks}.matches_by_domain USING TIMESTAMP ? SET target_id = ?, target_version = ?, "
    "content_id = ?, verdict = ?, confidence = ?, decided_at = ? "
    "WHERE domain_id = ? AND month = ? AND shard = ? AND match_id = ?"
)
_MATCHES_OF_CONTENT = "SELECT doc FROM {ks}.matches_by_content WHERE content_id = ?"
_MATCH_COLS = "match_id, target_version, content_id, verdict, confidence, decided_at"
_MATCHES_OF_TARGET = (
    f"SELECT {_MATCH_COLS}, media_ids FROM {{ks}}.matches_by_target "
    "WHERE target_id = ? AND month = ? AND shard = ?"
)
_MATCHES_OF_DOMAIN = (
    f"SELECT {_MATCH_COLS}, target_id FROM {{ks}}.matches_by_domain "
    "WHERE domain_id = ? AND month = ? AND shard = ?"
)


def _status(content_id: ContentId, r: Any) -> RepresentationStatus:
    representation = (
        Representation.model_validate_json(r.representation_doc) if r.representation_doc else None
    )
    if representation is not None:
        state = RepresentationState.READY
    elif r.failed_attempt is not None:
        state = RepresentationState.FAILED
    else:
        state = RepresentationState.REQUESTED
    return RepresentationStatus(
        content_id=content_id,
        spec_key=r.spec_key,
        state=state,
        requested_at=utc(r.requested_at) if r.requested_at else None,
        request_event_id=EventId.from_uuid(r.request_event_id) if r.request_event_id else None,
        representation=representation,
        failure=r.failure,
        retryable=r.retryable,
        failed_attempt=r.failed_attempt,
        failed_at=utc(r.failed_at) if r.failed_at else None,
    )


def _target_state(r: Any) -> TargetState | None:
    if r.doc is None:
        return None  # retired before any registration arrived; kept for when it does
    return TargetState(
        target=Target.model_validate_json(r.doc),
        retired_at=utc(r.retired_at) if r.retired_at else None,
        retire_reason=r.retire_reason,
    )


def _summary(r: Any, target_id: TargetId) -> MatchSummary:
    return MatchSummary(
        match_id=MatchId.from_uuid(r.match_id),
        target_id=target_id,
        target_version=r.target_version,
        content_id=ContentId.from_uuid(r.content_id),
        verdict=MatchVerdict(r.verdict),
        confidence=r.confidence,
        decided_at=utc(r.decided_at),
        media_ids=frozenset(MediaId.from_uuid(m) for m in getattr(r, "media_ids", None) or ()),
    )


class ScyllaProjectionRepository:
    def __init__(self, session: ScyllaSession) -> None:
        self._s = session

    # --- P1 -----------------------------------------------------------------

    def mark_encode_requested(self, event: EventEnvelope[EncodeRequested]) -> None:
        request = event.payload
        self._s.execute(
            self._s.bind(
                _REQUESTED,
                DERIVED,
                (
                    latest_wins(event.occurred_at),
                    event.occurred_at,
                    event.event_id.uuid,
                    request.content.content_id.uuid,
                    spec_key(request.required_spec),
                ),
            )
        )

    def apply_representation_ready(self, representation: Representation) -> None:
        r = representation
        self._s.execute(
            self._s.bind(
                _READY,
                DERIVED,
                (
                    latest_wins(r.created_at),
                    r.created_at,
                    r.representation_id.uuid,
                    r.model_dump_json(),
                    r.content_id.uuid,
                    spec_key(r.spec),
                ),
            )
        )

    def apply_encode_failed(self, event: EventEnvelope[EncodeFailed]) -> None:
        f = event.payload
        self._s.execute(
            self._s.bind(
                _FAILED,
                DERIVED,
                (
                    version_wins(f.attempt),
                    event.occurred_at,
                    f.failure.value,
                    f.retryable,
                    f.attempt,
                    f.content_id.uuid,
                    spec_key(f.spec),
                ),
            )
        )

    def representation_status(self, content_id: ContentId) -> list[RepresentationStatus]:
        rows = self._s.execute(self._s.bind(_STATUS, EC, (content_id.uuid,)))
        return [_status(content_id, r) for r in rows]

    # --- P2 -----------------------------------------------------------------

    def apply_target_registered(self, target: Target) -> None:
        self._s.execute(
            self._s.bind(
                _TARGET_VERSION,
                DERIVED,
                (
                    version_wins(target.ref.version),
                    target.ref.version,
                    target.model_dump_json(),
                    TARGETS_BUCKET,
                    target.ref.target_id.uuid,
                ),
            )
        )

    def apply_target_retired(self, retired: TargetRetired) -> None:
        self._s.execute(
            self._s.bind(
                _TARGET_RETIRED,
                DERIVED,
                (
                    earliest_wins(retired.retired_at),
                    retired.retired_at,
                    retired.reason,
                    TARGETS_BUCKET,
                    retired.target_id.uuid,
                ),
            )
        )

    def target(self, target_id: TargetId) -> TargetState | None:
        rows = self._s.execute(self._s.bind(_TARGET_GET, EC, (TARGETS_BUCKET, target_id.uuid)))
        return _target_state(rows[0]) if rows else None

    def targets(self) -> list[TargetState]:
        rows = self._s.execute(self._s.bind(_TARGETS, EC, (TARGETS_BUCKET,)))
        return [state for r in rows if (state := _target_state(r)) is not None]

    # --- P3-P5 --------------------------------------------------------------

    def record_match(
        self,
        match: MatchResult,
        *,
        media_ids: Collection[MediaId],
        source_domains: Collection[DomainId],
    ) -> None:
        m = match
        ts = latest_wins(m.decided_at)
        month = month_bucket(m.decided_at)
        shard = shard_of(m.match_id, MATCH_MONTH_SHARDS)
        facts = (m.verdict.value, m.confidence, m.decided_at)
        statements = [
            self._s.bind(
                _MATCH_BY_CONTENT,
                DERIVED,
                (ts, m.decided_at, m.model_dump_json(), m.content_id.uuid, m.match_id.uuid),
            ),
            self._s.bind(
                _MATCH_BY_TARGET,
                DERIVED,
                (
                    ts,
                    m.target.version,
                    m.content_id.uuid,
                    *facts,
                    {media.uuid for media in media_ids},
                    m.target.target_id.uuid,
                    month,
                    shard,
                    m.match_id.uuid,
                ),
            ),
        ]
        statements += [
            self._s.bind(
                _MATCH_BY_DOMAIN,
                DERIVED,
                (
                    ts,
                    m.target.target_id.uuid,
                    m.target.version,
                    m.content_id.uuid,
                    *facts,
                    domain.uuid,
                    month,
                    shard,
                    m.match_id.uuid,
                ),
            )
            for domain in set(source_domains)
        ]
        self._s.execute_all(statements)

    def matches_for_content(self, content_id: ContentId) -> list[MatchResult]:
        rows = self._s.execute(self._s.bind(_MATCHES_OF_CONTENT, EC, (content_id.uuid,)))
        matches = [MatchResult.model_validate_json(r.doc) for r in rows]
        return sorted(matches, key=lambda m: m.decided_at, reverse=True)

    def matches_for_target(self, target_id: TargetId, month: datetime) -> list[MatchSummary]:
        results = self._s.execute_many(
            self._s.bind(_MATCHES_OF_TARGET, EC, (target_id.uuid, month_bucket(month), shard))
            for shard in range(MATCH_MONTH_SHARDS)
        )
        found = [_summary(r, target_id) for rows in results for r in rows]
        return sorted(found, key=lambda m: m.decided_at, reverse=True)

    def matches_for_domain(self, domain_id: DomainId, month: datetime) -> list[MatchSummary]:
        results = self._s.execute_many(
            self._s.bind(_MATCHES_OF_DOMAIN, EC, (domain_id.uuid, month_bucket(month), shard))
            for shard in range(MATCH_MONTH_SHARDS)
        )
        found = [_summary(r, TargetId.from_uuid(r.target_id)) for rows in results for r in rows]
        return sorted(found, key=lambda m: m.decided_at, reverse=True)
