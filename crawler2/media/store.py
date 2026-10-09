"""Scylla access to the P8-owned tables (migration V005; design §8).

Only P8 reads or writes them. Every LWT here is single-partition
(``Consistency.SERIAL``: Paxos at ``LOCAL_SERIAL``); reads that must observe
completed LWTs are issued at ``LOCAL_SERIAL`` too. A timed-out CAS has an
unknown outcome: every CAS here is safe to repeat, and the callers reconcile
against the stored row (P2 evidence "pattern B").
"""

from __future__ import annotations

import random
import time
from datetime import datetime
from typing import Any

from antipiracy_contracts.ids import EventId, MediaId, ObservationId
from cassandra import ConsistencyLevel
from cassandra.query import BoundStatement

from crawler2.media.model import (
    ClaimResult,
    GapRecord,
    HashRecord,
    MediaLatest,
    ProbeRecord,
    ReaderMode,
    ReaderState,
    StoredClaim,
)
from crawler2.storage.errors import StorageUnavailableError
from crawler2.storage.scylla.session import Consistency, ScyllaSession, utc

SERIAL = Consistency.SERIAL
STRONG = Consistency.STRONG_READ
AUTH = Consistency.AUTHORITATIVE_WRITE
_CAS_ATTEMPTS = 5

_STATE_COLS = (
    "reader, epoch, owner, lease_until, next_bucket, state, gap_from, gap_to, gaps, started_bucket"
)
_STATE_GET = f"SELECT {_STATE_COLS} FROM {{ks}}.media_reader_state WHERE reader = ?"
_STATE_CREATE = (
    f"INSERT INTO {{ks}}.media_reader_state ({_STATE_COLS}, updated_at, updated_by) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) IF NOT EXISTS"
)
_ACQUIRE = (
    "UPDATE {ks}.media_reader_state SET epoch = ?, owner = ?, lease_until = ?, updated_at = ?, "
    "updated_by = ? WHERE reader = ? IF epoch = ? AND lease_until <= ?"
)
_RENEW = (
    "UPDATE {ks}.media_reader_state SET lease_until = ?, updated_at = ?, updated_by = ? "
    "WHERE reader = ? IF epoch = ? AND owner = ?"
)
_ADVANCE = (
    "UPDATE {ks}.media_reader_state SET next_bucket = ?, lease_until = ?, updated_at = ?, "
    "updated_by = ? WHERE reader = ? IF epoch = ? AND next_bucket = ? AND state = ?"
)
_ENTER_GAP = (
    "UPDATE {ks}.media_reader_state SET state = ?, gap_from = ?, gap_to = ?, gaps = ?, "
    "updated_at = ?, updated_by = ? WHERE reader = ? IF epoch = ? AND next_bucket = ? "
    "AND state = ?"
)
_ACCEPT_GAP = (
    "UPDATE {ks}.media_reader_state SET state = ?, next_bucket = ?, epoch = ?, owner = ?, "
    "lease_until = ?, updated_at = ?, updated_by = ? WHERE reader = ? "
    "IF epoch = ? AND state = ? AND next_bucket = ?"
)
_GAP_DETECTED = (
    "UPDATE {ks}.media_reader_gaps SET gap_to = ?, detected_at = ?, detected_by = ? "
    "WHERE reader = ? AND gap_from = ?"
)
_GAP_ACCEPTED = (
    "UPDATE {ks}.media_reader_gaps SET gap_to = ?, accepted_at = ?, accepted_by = ?, note = ? "
    "WHERE reader = ? AND gap_from = ?"
)
_GAPS = (
    "SELECT reader, gap_from, gap_to, detected_at, detected_by, accepted_at, accepted_by, note "
    "FROM {ks}.media_reader_gaps WHERE reader = ?"
)
_CLAIM = (
    "INSERT INTO {ks}.media_observation_claims (page_observation_id, media_id, event_id, "
    "envelope, claimed_at, claimed_epoch, claimed_by) VALUES (?, ?, ?, ?, ?, ?, ?) IF NOT EXISTS"
)
_CLAIM_COLS = "event_id, envelope, claimed_epoch, claimed_by, committed_at"
_CLAIM_GET = (
    f"SELECT {_CLAIM_COLS} FROM {{ks}}.media_observation_claims "
    "WHERE page_observation_id = ? AND media_id = ?"
)
_CLAIMS_ON = (
    f"SELECT media_id, {_CLAIM_COLS} FROM {{ks}}.media_observation_claims "
    "WHERE page_observation_id = ?"
)
_COMMITTED = (
    "UPDATE {ks}.media_observation_claims SET committed_at = ? "
    "WHERE page_observation_id = ? AND media_id = ? IF committed_at = null"
)
_LATEST_GET = (
    "SELECT probe_doc, hash_doc, ok_hash_doc FROM {ks}.media_probe_latest WHERE media_id = ?"
)


def _reader_state(row: Any) -> ReaderState:
    return ReaderState(
        reader=row.reader,
        epoch=row.epoch,
        owner=row.owner,
        lease_until=utc(row.lease_until),
        next_bucket=row.next_bucket,
        state=ReaderMode(row.state),
        gap_from=row.gap_from,
        gap_to=row.gap_to,
        gaps=row.gaps or 0,
        started_bucket=row.started_bucket,
    )


def _claim(row: Any) -> StoredClaim:
    return StoredClaim(
        event_id=str(EventId.from_uuid(row.event_id)),
        envelope=row.envelope,
        claimed_epoch=row.claimed_epoch,
        claimed_by=row.claimed_by,
        committed=row.committed_at is not None,
    )


class ScyllaRegistryStore:
    def __init__(self, session: ScyllaSession) -> None:
        self._s = session

    # --- helpers --------------------------------------------------------------

    def _serial_read(self, cql: str, values: tuple[Any, ...]) -> list[Any]:
        """A read that completes in-flight Paxos rounds (sees every applied LWT)."""
        statement: BoundStatement = self._s.bind(cql, STRONG, values)
        statement.consistency_level = ConsistencyLevel.LOCAL_SERIAL
        return self._cas_rows(statement)

    def _cas_rows(self, statement: Any) -> list[Any]:
        for attempt in range(_CAS_ATTEMPTS):
            try:
                return self._s.execute(statement)
            except StorageUnavailableError:
                if attempt == _CAS_ATTEMPTS - 1:
                    raise
                time.sleep(random.uniform(0.05, 0.2) * 2**attempt)  # noqa: S311 -- jitter
        raise AssertionError("unreachable")  # pragma: no cover

    def _cas(self, cql: str, values: tuple[Any, ...]) -> bool:
        return bool(self._cas_rows(self._s.bind(cql, SERIAL, values))[0].applied)

    # --- reader state (ADR-024 rule 3) -----------------------------------------------

    def state(self, reader: str) -> ReaderState | None:
        rows = self._serial_read(_STATE_GET, (reader,))
        return _reader_state(rows[0]) if rows else None

    def create_state(self, state: ReaderState, *, at: datetime, by: str) -> bool:
        return self._cas(
            _STATE_CREATE,
            (
                state.reader,
                state.epoch,
                state.owner,
                state.lease_until,
                state.next_bucket,
                state.state.value,
                state.gap_from,
                state.gap_to,
                state.gaps,
                state.started_bucket,
                at,
                by,
            ),
        )

    def acquire(
        self, reader: str, *, expected_epoch: int, owner: str, lease_until: datetime, now: datetime
    ) -> bool:
        """Take an expired lease: epoch + 1. Fails while any lease is still valid."""
        return self._cas(
            _ACQUIRE,
            (expected_epoch + 1, owner, lease_until, now, owner, reader, expected_epoch, now),
        )

    def renew(
        self, reader: str, *, epoch: int, owner: str, lease_until: datetime, now: datetime
    ) -> bool:
        return self._cas(_RENEW, (lease_until, now, owner, reader, epoch, owner))

    def advance(
        self,
        reader: str,
        *,
        epoch: int,
        from_bucket: int,
        lease_until: datetime,
        now: datetime,
        by: str,
    ) -> bool:
        """``next_bucket`` b → b + 1, only for the epoch's owner and only from b."""
        return self._cas(
            _ADVANCE,
            (
                from_bucket + 1,
                lease_until,
                now,
                by,
                reader,
                epoch,
                from_bucket,
                ReaderMode.RUNNING.value,
            ),
        )

    def enter_gap(
        self, state: ReaderState, *, epoch: int, gap_to: int, now: datetime, by: str
    ) -> bool:
        """Record the gap, then halt the reader (``state = gap``)."""
        self._s.execute(
            self._s.bind(_GAP_DETECTED, AUTH, (gap_to, now, by, state.reader, state.next_bucket))
        )
        return self._cas(
            _ENTER_GAP,
            (
                ReaderMode.GAP.value,
                state.next_bucket,
                gap_to,
                state.gaps + 1,
                now,
                by,
                state.reader,
                epoch,
                state.next_bucket,
                ReaderMode.RUNNING.value,
            ),
        )

    def accept_gap(
        self, state: ReaderState, *, resume_at: int, now: datetime, by: str, note: str
    ) -> bool:
        """Operator: record the accepted loss first, then resume at ``resume_at``.

        The epoch is bumped, so the halted owner must acquire again.
        """
        if state.state is not ReaderMode.GAP or state.gap_from is None:
            raise ValueError("the reader is not in a gap")
        self._s.execute(
            self._s.bind(
                _GAP_ACCEPTED, AUTH, (resume_at, now, by, note, state.reader, state.gap_from)
            )
        )
        return self._cas(
            _ACCEPT_GAP,
            (
                ReaderMode.RUNNING.value,
                resume_at,
                state.epoch + 1,
                None,
                now,
                now,
                by,
                state.reader,
                state.epoch,
                ReaderMode.GAP.value,
                state.next_bucket,
            ),
        )

    def gaps(self, reader: str) -> list[GapRecord]:
        rows = self._s.execute(self._s.bind(_GAPS, STRONG, (reader,)))
        return [
            GapRecord(
                reader=r.reader,
                gap_from=r.gap_from,
                gap_to=r.gap_to,
                detected_at=utc(r.detected_at) if r.detected_at else None,
                detected_by=r.detected_by,
                accepted_at=utc(r.accepted_at) if r.accepted_at else None,
                accepted_by=r.accepted_by,
                note=r.note,
            )
            for r in rows
        ]

    # --- observation claims (design §4.4) ----------------------------------------------

    def claim_observation(
        self,
        page_observation_id: ObservationId,
        media_id: MediaId,
        *,
        event_id: EventId,
        envelope: str,
        at: datetime,
        epoch: int,
        by: str,
    ) -> ClaimResult:
        """Write-once decision. On loss, the stored claim is returned and must be used."""
        rows = self._cas_rows(
            self._s.bind(
                _CLAIM,
                SERIAL,
                (page_observation_id.uuid, media_id.uuid, event_id.uuid, envelope, at, epoch, by),
            )
        )
        if rows[0].applied:
            return ClaimResult(
                won=True,
                claim=StoredClaim(str(event_id), envelope, epoch, by, committed=False),
            )
        stored = _claim(rows[0])
        # A repeated (timed-out) CAS finds our own earlier write: still ours.
        return ClaimResult(won=stored.event_id == str(event_id), claim=stored)

    def claim(self, page_observation_id: ObservationId, media_id: MediaId) -> StoredClaim | None:
        rows = self._serial_read(_CLAIM_GET, (page_observation_id.uuid, media_id.uuid))
        return _claim(rows[0]) if rows else None

    def claims_on(self, page_observation_id: ObservationId) -> dict[str, StoredClaim]:
        rows = self._serial_read(_CLAIMS_ON, (page_observation_id.uuid,))
        return {str(MediaId.from_uuid(r.media_id)): _claim(r) for r in rows}

    def mark_committed(
        self, page_observation_id: ObservationId, media_id: MediaId, *, at: datetime
    ) -> None:
        """Idempotent: a second call finds ``committed_at`` set and changes nothing."""
        self._cas(_COMMITTED, (at, page_observation_id.uuid, media_id.uuid))

    # --- probe/hash view (design §6.2) ---------------------------------------------------

    def latest(self, media_id: MediaId) -> MediaLatest | None:
        rows = self._s.execute(self._s.bind(_LATEST_GET, STRONG, (media_id.uuid,)))
        if not rows:
            return None
        r = rows[0]
        return MediaLatest(
            media_id=media_id,
            probe=ProbeRecord.from_json(r.probe_doc) if r.probe_doc else None,
            hash=HashRecord.from_json(r.hash_doc) if r.hash_doc else None,
            ok_hash=HashRecord.from_json(r.ok_hash_doc) if r.ok_hash_doc else None,
        )
