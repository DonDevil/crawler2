"""Evidence storage (E1-E4): the only compare-and-set state in crawler2 (LWT).

- E1 ``evidence_by_match``: INSERT IF NOT EXISTS — one evidence item per match,
  even when two hosts process the same ``match.found`` concurrently.
- E2 ``evidence``: the candidate is write-once (INSERT IF NOT EXISTS); sealing is
  ``UPDATE ... IF state = 'collecting'`` and therefore happens exactly once.
  Identical replays succeed; a *different* value raises ``ConflictError``.
- Events use outbox pattern B (after the LWT): conditional statements cannot
  join a multi-partition batch. A crash before the outbox row is completed by
  the redelivery of the triggering event; the replay re-appends it.

Legal content and retention of evidence are open (ADR-005); this stores only
the P1 provenance contracts and never deletes.
"""

from __future__ import annotations

import random
import time
from datetime import datetime
from typing import Any

from antipiracy_contracts.events import EventEnvelope
from antipiracy_contracts.events.evidence import EvidenceCandidateCreated, EvidenceFinalized
from antipiracy_contracts.ids import EvidenceId, MatchId, TargetId
from antipiracy_contracts.models.evidence import EvidenceCandidate, EvidenceSeal

from crawler2.storage.errors import ConflictError, StorageUnavailableError
from crawler2.storage.layout import MATCH_MONTH_SHARDS, month_bucket, shard_of
from crawler2.storage.repositories import (
    EvidenceProvenance,
    EvidenceRecord,
    EvidenceState,
    EvidenceSummary,
)
from crawler2.storage.scylla.outbox import ScyllaOutbox
from crawler2.storage.scylla.session import Consistency, ScyllaSession, utc
from crawler2.storage.scylla.web import ScyllaPageObservationRepository, require_payload

SERIAL = Consistency.SERIAL
_CAS_ATTEMPTS = 5
DERIVED = Consistency.DERIVED_WRITE
EC = Consistency.EVENTUAL_READ

_OPEN = (
    "INSERT INTO {ks}.evidence_by_match (match_id, evidence_id, opened_at) VALUES (?, ?, ?) "
    "IF NOT EXISTS"
)
_OPENED = "SELECT evidence_id FROM {ks}.evidence_by_match WHERE match_id = ?"
_CREATE = (
    "INSERT INTO {ks}.evidence (evidence_id, match_id, target_id, state, collected_at, "
    "candidate_doc) VALUES (?, ?, ?, ?, ?, ?) IF NOT EXISTS"
)
_SEAL = (
    "UPDATE {ks}.evidence SET state = ?, finalized_at = ?, seal_doc = ? WHERE evidence_id = ? "
    "IF state = ?"
)
_GET = "SELECT state, candidate_doc, seal_doc FROM {ks}.evidence WHERE evidence_id = ?"
_BY_TARGET_COLLECTED = (
    "UPDATE {ks}.evidence_by_target SET match_id = ?, collected_at = ? "
    "WHERE target_id = ? AND month = ? AND shard = ? AND evidence_id = ?"
)
_BY_TARGET_SEALED = (
    "UPDATE {ks}.evidence_by_target SET finalized_at = ? "
    "WHERE target_id = ? AND month = ? AND shard = ? AND evidence_id = ?"
)
_BY_TARGET_GET = (
    "SELECT evidence_id, match_id, collected_at, finalized_at FROM {ks}.evidence_by_target "
    "WHERE target_id = ? AND month = ? AND shard = ?"
)


def _record(r: Any) -> EvidenceRecord:
    return EvidenceRecord(
        candidate=EvidenceCandidate.model_validate_json(r.candidate_doc),
        state=EvidenceState(r.state),
        seal=EvidenceSeal.model_validate_json(r.seal_doc) if r.seal_doc else None,
    )


class ScyllaEvidenceRepository:
    def __init__(
        self,
        session: ScyllaSession,
        outbox: ScyllaOutbox,
        pages: ScyllaPageObservationRepository,
    ) -> None:
        self._s = session
        self._outbox = outbox
        self._pages = pages

    def _serial(self, statement: Any) -> list[Any]:
        """Run an LWT or SERIAL read, retrying Paxos contention timeouts.

        A timed-out CAS has an unknown outcome; every CAS here is safe to repeat
        (IF NOT EXISTS returns the existing row — possibly our own earlier write —
        and the seal CAS is reconciled against the stored seal by the caller).
        """
        for attempt in range(_CAS_ATTEMPTS):
            try:
                return self._s.execute(statement)
            except StorageUnavailableError:
                if attempt == _CAS_ATTEMPTS - 1:
                    raise
                time.sleep(random.uniform(0.05, 0.2) * 2**attempt)  # noqa: S311 — jitter
        raise AssertionError("unreachable")  # pragma: no cover

    def open_candidate(
        self, match_id: MatchId, proposed: EvidenceId, *, at: datetime
    ) -> EvidenceId:
        rows = self._serial(self._s.bind(_OPEN, SERIAL, (match_id.uuid, proposed.uuid, at)))
        if rows[0].applied:
            return proposed
        return EvidenceId.from_uuid(rows[0].evidence_id)

    def create_candidate(
        self,
        candidate: EvidenceCandidate,
        *,
        event: EventEnvelope[EvidenceCandidateCreated] | None = None,
    ) -> EvidenceRecord:
        require_payload(event, lambda p: p.candidate == candidate, "evidence candidate")
        c = candidate
        opened = self._serial(self._s.bind(_OPENED, SERIAL, (c.match_id.uuid,)))
        if not opened or EvidenceId.from_uuid(opened[0].evidence_id) != c.evidence_id:
            raise ConflictError(f"{c.evidence_id} is not the evidence item opened for {c.match_id}")
        rows = self._serial(
            self._s.bind(
                _CREATE,
                SERIAL,
                (
                    c.evidence_id.uuid,
                    c.match_id.uuid,
                    c.target.target_id.uuid,
                    EvidenceState.COLLECTING.value,
                    c.collected_at,
                    c.model_dump_json(),
                ),
            )
        )
        if not rows[0].applied:
            existing = self.get(c.evidence_id)
            if existing is None or existing.candidate != c:
                raise ConflictError(f"{c.evidence_id} already holds a different candidate")
        self._s.execute(
            self._s.bind(
                _BY_TARGET_COLLECTED,
                DERIVED,
                (
                    c.match_id.uuid,
                    c.collected_at,
                    c.target.target_id.uuid,
                    month_bucket(c.collected_at),
                    shard_of(c.evidence_id, MATCH_MONTH_SHARDS),
                    c.evidence_id.uuid,
                ),
            )
        )
        if event is not None:
            self._outbox.enqueue(event)
        record = self.get(c.evidence_id)
        if record is None:  # pragma: no cover - LWT guarantees visibility at SERIAL
            raise ConflictError(f"{c.evidence_id} vanished after creation")
        return record

    def seal(
        self, seal: EvidenceSeal, *, event: EventEnvelope[EvidenceFinalized] | None = None
    ) -> EvidenceRecord:
        require_payload(event, lambda p: p.seal == seal, "evidence seal")
        rows = self._serial(
            self._s.bind(
                _SEAL,
                SERIAL,
                (
                    EvidenceState.SEALED.value,
                    seal.finalized_at,
                    seal.model_dump_json(),
                    seal.evidence_id.uuid,
                    EvidenceState.COLLECTING.value,
                ),
            )
        )
        record = self.get(seal.evidence_id)
        if record is None:
            raise ConflictError(f"cannot seal {seal.evidence_id}: no candidate")
        if not rows[0].applied and record.seal != seal:
            raise ConflictError(f"{seal.evidence_id} is already sealed with another manifest")
        candidate = record.candidate
        self._s.execute(
            self._s.bind(
                _BY_TARGET_SEALED,
                DERIVED,
                (
                    seal.finalized_at,
                    candidate.target.target_id.uuid,
                    month_bucket(candidate.collected_at),
                    shard_of(seal.evidence_id, MATCH_MONTH_SHARDS),
                    seal.evidence_id.uuid,
                ),
            )
        )
        if event is not None:
            self._outbox.enqueue(event)
        return record

    def get(self, evidence_id: EvidenceId) -> EvidenceRecord | None:
        rows = self._serial(self._s.bind(_GET, SERIAL, (evidence_id.uuid,)))
        if not rows or rows[0].candidate_doc is None:
            return None
        return _record(rows[0])

    def for_target(self, target_id: TargetId, month: datetime) -> list[EvidenceSummary]:
        results = self._s.execute_many(
            self._s.bind(_BY_TARGET_GET, EC, (target_id.uuid, month_bucket(month), shard))
            for shard in range(MATCH_MONTH_SHARDS)
        )
        found = [
            EvidenceSummary(
                evidence_id=EvidenceId.from_uuid(r.evidence_id),
                match_id=MatchId.from_uuid(r.match_id),
                collected_at=utc(r.collected_at),
                finalized_at=utc(r.finalized_at) if r.finalized_at else None,
            )
            for rows in results
            for r in rows
            if r.collected_at is not None
        ]
        return sorted(found, key=lambda e: e.finalized_at or e.collected_at, reverse=True)

    def provenance(self, evidence_id: EvidenceId) -> EvidenceProvenance | None:
        """E3: E2 then W7 point reads (LOCAL_QUORUM); snapshots are the BlobRefs inside."""
        record = self.get(evidence_id)
        if record is None:
            return None
        observations = []
        missing = []
        for observation_id in record.candidate.page_observation_ids:
            observation = self._pages.get(observation_id)
            if observation is None:
                missing.append(observation_id)
            else:
                observations.append(observation)
        return EvidenceProvenance(record, tuple(observations), tuple(missing))
