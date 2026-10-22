"""Scylla tables owned by P11 (design §4.3; migration V006).

Only P11 reads or writes them. Rows are derived (``DERIVED_WRITE``) and
rebuildable; every write is an idempotent upsert keyed by
(host, match, page observation), so replays and concurrent consumers write
identical cells and cannot double-count.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any, Protocol

from antipiracy_contracts.ids import DomainId, MatchId, ObservationId, TargetId

from crawler2.intelligence.feedback import MatchEvidence
from crawler2.storage.scylla.session import Consistency, ScyllaSession, utc

BUCKET = 0
MAX_ROWS_PER_SOURCE = 500
"""Evidence rows read per host (bounded partition read on the snapshot path)."""

_COLS = (
    "domain_id, match_id, page_observation_id, target_id, target_version, decided_at, "
    "confidence, verdict, page_url, template, page_kind, capability"
)
_PUT = f"INSERT INTO {{ks}}.feedback_evidence ({_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
_PUT_SOURCE = "INSERT INTO {ks}.feedback_sources (bucket, domain_id) VALUES (?, ?)"
_SOURCES = "SELECT domain_id FROM {ks}.feedback_sources WHERE bucket = ?"
_OF = f"SELECT {_COLS} FROM {{ks}}.feedback_evidence WHERE domain_id = ? LIMIT ?"
_DROP = "DELETE FROM {ks}.feedback_evidence WHERE domain_id = ?"
_DROP_SOURCE = "DELETE FROM {ks}.feedback_sources WHERE bucket = ? AND domain_id = ?"


class FeedbackStore(Protocol):
    def put_evidence(self, rows: Sequence[MatchEvidence]) -> None: ...

    def sources(self) -> list[str]: ...

    def evidence(self, domain_ids: Collection[str]) -> dict[str, list[MatchEvidence]]: ...

    def clear(self) -> int:
        """Drop all derived feedback (before a rebuild); the number of hosts dropped."""
        ...


def _row(r: Any) -> MatchEvidence:
    return MatchEvidence(
        domain_id=str(DomainId.from_uuid(r.domain_id)),
        match_id=str(MatchId.from_uuid(r.match_id)),
        page_observation_id=str(ObservationId.from_uuid(r.page_observation_id)),
        target_id=str(TargetId.from_uuid(r.target_id)),
        target_version=r.target_version,
        decided_at=utc(r.decided_at),
        confidence=r.confidence,
        verdict=r.verdict,
        page_url=r.page_url,
        template=r.template,
        page_kind=r.page_kind,
        capability=r.capability,
    )


class ScyllaFeedbackStore:
    def __init__(self, session: ScyllaSession) -> None:
        self._s = session

    def put_evidence(self, rows: Sequence[MatchEvidence]) -> None:
        statements = [
            self._s.bind(
                _PUT,
                Consistency.DERIVED_WRITE,
                (
                    DomainId(e.domain_id).uuid,
                    MatchId(e.match_id).uuid,
                    ObservationId(e.page_observation_id).uuid,
                    TargetId(e.target_id).uuid,
                    e.target_version,
                    e.decided_at,
                    e.confidence,
                    e.verdict,
                    e.page_url,
                    e.template,
                    e.page_kind,
                    e.capability,
                ),
            )
            for e in rows
        ]
        statements += [
            self._s.bind(_PUT_SOURCE, Consistency.DERIVED_WRITE, (BUCKET, DomainId(d).uuid))
            for d in sorted({e.domain_id for e in rows})
        ]
        self._s.execute_all(statements)

    def sources(self) -> list[str]:
        rows = self._s.execute(self._s.bind(_SOURCES, Consistency.EVENTUAL_READ, (BUCKET,)))
        return [str(DomainId.from_uuid(r.domain_id)) for r in rows]

    def evidence(self, domain_ids: Collection[str]) -> dict[str, list[MatchEvidence]]:
        results = self._s.execute_many(
            self._s.bind(_OF, Consistency.EVENTUAL_READ, (DomainId(d).uuid, MAX_ROWS_PER_SOURCE))
            for d in domain_ids
        )
        found: dict[str, list[MatchEvidence]] = {}
        for rows in results:
            for r in rows:
                e = _row(r)
                found.setdefault(e.domain_id, []).append(e)
        return found

    def clear(self) -> int:
        domains = self.sources()
        self._s.execute_all(
            stmt
            for d in domains
            for stmt in (
                self._s.bind(_DROP, Consistency.DERIVED_WRITE, (DomainId(d).uuid,)),
                self._s.bind(_DROP_SOURCE, Consistency.DERIVED_WRITE, (BUCKET, DomainId(d).uuid)),
            )
        )
        return len(domains)
