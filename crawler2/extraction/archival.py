"""Raw snapshot archival decision (P5 design §13).

P4 stores every body content-addressed; P5 deletes nothing. It records, per
(snapshot, observation), whether the snapshot must be kept, so a future
garbage collector (P13/P14, after ADR-005) keeps a blob if any decision
retains it. The inputs that need intelligence — evidence candidacy (P12)
and page value (P7) — arrive in an explicit ``ArchivalProfile``; P5 has no
value model of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from antipiracy_contracts.ids import ObservationId

from crawler2.storage.repositories import SnapshotDecision

_SAMPLE_SCALE: Final = 10_000


@dataclass(frozen=True, slots=True)
class ArchivalProfile:
    """Supplied by the caller; until P7/P12 exist it is the configured default."""

    evidence_candidate: bool = False
    high_value: bool = False
    sample_rate: float = 0.05

    def __post_init__(self) -> None:
        if not 0.0 <= self.sample_rate <= 1.0:
            raise ValueError("sample_rate must be within [0, 1]")


DEFAULT_PROFILE: Final = ArchivalProfile()


def decide(
    profile: ArchivalProfile, *, observation_id: ObservationId, new_revision: bool
) -> tuple[SnapshotDecision, str]:
    """Deterministic on every host: sampling hashes the observation ID, never a clock."""
    if profile.evidence_candidate:
        return SnapshotDecision.RETAIN, "evidence_candidate"
    if new_revision and profile.high_value:
        return SnapshotDecision.RETAIN, "high_value_change"
    if new_revision:
        return SnapshotDecision.RETAIN, "new_revision"
    if observation_id.uuid.int % _SAMPLE_SCALE < round(profile.sample_rate * _SAMPLE_SCALE):
        return SnapshotDecision.RETAIN, "sampled"
    return SnapshotDecision.SAMPLED_OUT, "sampled_out"
