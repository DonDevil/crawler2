"""Scylla tables owned by P7 (design §9.3; migration V004).

Only P7 reads or writes them. Profiles are derived (``DERIVED_WRITE``) and
rebuildable; the state row is the reader's checkpoint, advanced by LWT.
"""

from __future__ import annotations

import time
from collections.abc import Collection, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

from antipiracy_contracts.ids import DomainId

from crawler2.intelligence.projection import (
    ProjectionState,
    StoredProfile,
    profile_from_json,
    profile_to_json,
)
from crawler2.storage.errors import StorageUnavailableError
from crawler2.storage.scylla.session import Consistency, ScyllaSession, utc

_STATE_COLS = (
    "projection, generation, policy, next_bucket, gaps, rebuilds, last_gap_from, "
    "last_gap_to, generation_started_at"
)
_GET_STATE = f"SELECT {_STATE_COLS} FROM {{ks}}.intel_projection_state WHERE projection = ?"
_CREATE_STATE = (
    f"INSERT INTO {{ks}}.intel_projection_state ({_STATE_COLS}, updated_at, updated_by) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) IF NOT EXISTS"
)
_REPLACE_STATE = (
    "UPDATE {ks}.intel_projection_state SET generation = ?, policy = ?, next_bucket = ?, "
    "gaps = ?, rebuilds = ?, last_gap_from = ?, last_gap_to = ?, generation_started_at = ?, "
    "updated_at = ?, updated_by = ? WHERE projection = ? IF generation = ? AND next_bucket = ?"
)
_PROFILE_COLS = "domain_id, generation, policy, through_bucket, profile"
_GET_PROFILE = f"SELECT {_PROFILE_COLS} FROM {{ks}}.intel_source_profiles WHERE domain_id = ?"
_PUT_PROFILE = (
    f"INSERT INTO {{ks}}.intel_source_profiles ({_PROFILE_COLS}, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
_SCAN = f"SELECT {_PROFILE_COLS} FROM {{ks}}.intel_source_profiles"
_CAS_ATTEMPTS = 5


def _state(row: Any) -> ProjectionState:
    return ProjectionState(
        projection=row.projection,
        generation=row.generation,
        policy=row.policy,
        next_bucket=row.next_bucket,
        gaps=row.gaps or 0,
        rebuilds=row.rebuilds or 0,
        last_gap_from=row.last_gap_from,
        last_gap_to=row.last_gap_to,
        generation_started_at=utc(row.generation_started_at) if row.generation_started_at else None,
    )


def _profile(row: Any) -> StoredProfile:
    return StoredProfile(
        profile=profile_from_json(row.profile),
        generation=row.generation,
        policy=row.policy,
        through_bucket=row.through_bucket,
    )


class ScyllaSourceProfileStore:
    def __init__(self, session: ScyllaSession) -> None:
        self._s = session

    def state(self, projection: str) -> ProjectionState | None:
        rows = self._s.execute(self._s.bind(_GET_STATE, Consistency.STRONG_READ, (projection,)))
        return _state(rows[0]) if rows else None

    def _cas(self, statement: Any) -> bool:
        """A timed-out CAS has an unknown outcome; repeating it is safe because the
        condition fails if our own earlier attempt was applied (the caller re-reads)."""
        for attempt in range(_CAS_ATTEMPTS):
            try:
                return bool(self._s.execute(statement)[0].applied)
            except StorageUnavailableError:
                if attempt == _CAS_ATTEMPTS - 1:
                    raise
                time.sleep(0.05 * 2**attempt)
        raise AssertionError("unreachable")  # pragma: no cover

    def create_state(self, state: ProjectionState, *, by: str) -> bool:
        return self._cas(
            self._s.bind(
                _CREATE_STATE,
                Consistency.SERIAL,
                (*self._values(state), datetime.now(UTC), by),
            )
        )

    def replace_state(self, expected: ProjectionState, new: ProjectionState, *, by: str) -> bool:
        if new.projection != expected.projection:
            raise ValueError("a state is replaced by the same projection's state")
        values = self._values(new)
        return self._cas(
            self._s.bind(
                _REPLACE_STATE,
                Consistency.SERIAL,
                (
                    *values[1:],
                    datetime.now(UTC),
                    by,
                    new.projection,
                    expected.generation,
                    expected.next_bucket,
                ),
            )
        )

    @staticmethod
    def _values(state: ProjectionState) -> tuple[Any, ...]:
        return (
            state.projection,
            state.generation,
            state.policy,
            state.next_bucket,
            state.gaps,
            state.rebuilds,
            state.last_gap_from,
            state.last_gap_to,
            state.generation_started_at,
        )

    def profiles(self, domain_ids: Collection[str]) -> dict[str, StoredProfile]:
        results = self._s.execute_many(
            self._s.bind(_GET_PROFILE, Consistency.EVENTUAL_READ, (DomainId(d).uuid,))
            for d in domain_ids
        )
        return {str(DomainId.from_uuid(r.domain_id)): _profile(r) for rows in results for r in rows}

    def put_profiles(self, rows: Sequence[StoredProfile]) -> None:
        now = datetime.now(UTC)
        self._s.execute_all(
            self._s.bind(
                _PUT_PROFILE,
                Consistency.DERIVED_WRITE,
                (
                    DomainId(row.profile.domain_id).uuid,
                    row.generation,
                    row.policy,
                    row.through_bucket,
                    profile_to_json(row.profile),
                    now,
                ),
            )
            for row in rows
        )

    def scan(self) -> Iterator[StoredProfile]:
        for row in self._s.stream(self._s.bind(_SCAN, Consistency.EVENTUAL_READ, ())):
            yield _profile(row)
