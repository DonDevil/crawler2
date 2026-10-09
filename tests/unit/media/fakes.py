"""In-memory doubles of the P8 stores with the same compare-and-set semantics."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from antipiracy_contracts.events import EventEnvelope
from antipiracy_contracts.events.media import MediaObserved
from antipiracy_contracts.ids import MediaId, ObservationId
from antipiracy_contracts.models.media import MediaObservation

from crawler2.media.model import ClaimResult, MediaLatest, ReaderMode, ReaderState, StoredClaim
from crawler2.storage.events.relay import OutboxRecord


class FakeOutbox:
    def __init__(self, earliest: int = 0) -> None:
        self.by_bucket: dict[tuple[int, int], list[OutboxRecord]] = {}
        self.earliest = earliest

    def add(self, record: OutboxRecord) -> None:
        self.by_bucket.setdefault((record.shard, record.bucket), []).append(record)

    def rows(self, shard: int, bucket: int) -> list[OutboxRecord]:
        return list(self.by_bucket.get((shard, bucket), []))

    def earliest_bucket(self) -> int:
        return self.earliest


class FakeStore:
    def __init__(self) -> None:
        self.states: dict[str, ReaderState] = {}
        self.claims: dict[tuple[str, str], StoredClaim] = {}
        self.latest_rows: dict[str, MediaLatest] = {}
        self.refuse_create = False
        self.refuse_renew = False

    def state(self, reader: str) -> ReaderState | None:
        return self.states.get(reader)

    def create_state(self, state: ReaderState, *, at: datetime, by: str) -> bool:
        if self.refuse_create or state.reader in self.states:
            return False
        self.states[state.reader] = state
        return True

    def acquire(
        self, reader: str, *, expected_epoch: int, owner: str, lease_until: datetime, now: datetime
    ) -> bool:
        s = self.states[reader]
        if s.epoch != expected_epoch or s.lease_until > now:
            return False
        self.states[reader] = replace(
            s, epoch=expected_epoch + 1, owner=owner, lease_until=lease_until
        )
        return True

    def renew(
        self, reader: str, *, epoch: int, owner: str, lease_until: datetime, now: datetime
    ) -> bool:
        s = self.states[reader]
        if self.refuse_renew or s.epoch != epoch or s.owner != owner:
            return False
        self.states[reader] = replace(s, lease_until=lease_until)
        return True

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
        s = self.states[reader]
        if s.epoch != epoch or s.next_bucket != from_bucket or s.state is not ReaderMode.RUNNING:
            return False
        self.states[reader] = replace(s, next_bucket=from_bucket + 1, lease_until=lease_until)
        return True

    def enter_gap(
        self, state: ReaderState, *, epoch: int, gap_to: int, now: datetime, by: str
    ) -> bool:
        s = self.states[state.reader]
        if s.epoch != epoch or s.next_bucket != state.next_bucket:
            return False
        self.states[state.reader] = replace(
            s, state=ReaderMode.GAP, gap_from=s.next_bucket, gap_to=gap_to, gaps=s.gaps + 1
        )
        return True

    def accept_gap(
        self, state: ReaderState, *, resume_at: int, now: datetime, by: str, note: str
    ) -> bool:
        s = self.states[state.reader]
        if s.epoch != state.epoch or s.state is not ReaderMode.GAP:
            return False
        self.states[state.reader] = replace(
            s,
            state=ReaderMode.RUNNING,
            next_bucket=resume_at,
            epoch=s.epoch + 1,
            owner=None,
            lease_until=now,
        )
        return True

    def claim_observation(
        self,
        page_observation_id: ObservationId,
        media_id: MediaId,
        *,
        event_id: Any,
        envelope: str,
        at: datetime,
        epoch: int,
        by: str,
    ) -> ClaimResult:
        key = (str(page_observation_id), str(media_id))
        if key in self.claims:
            return ClaimResult(False, self.claims[key])
        self.claims[key] = StoredClaim(str(event_id), envelope, epoch, by, committed=False)
        return ClaimResult(True, self.claims[key])

    def claim(self, page_observation_id: ObservationId, media_id: MediaId) -> StoredClaim | None:
        return self.claims.get((str(page_observation_id), str(media_id)))

    def mark_committed(
        self, page_observation_id: ObservationId, media_id: MediaId, *, at: datetime
    ) -> None:
        key = (str(page_observation_id), str(media_id))
        self.claims[key] = replace(self.claims[key], committed=True)

    def latest(self, media_id: MediaId) -> MediaLatest | None:
        return self.latest_rows.get(str(media_id))


class FakeObservations:
    def __init__(self) -> None:
        self.written: list[tuple[MediaObservation, str]] = []

    def record_observation(
        self, observation: MediaObservation, *, event: EventEnvelope[MediaObserved] | None = None
    ) -> None:
        assert event is not None
        self.written.append((observation, str(event.event_id)))
