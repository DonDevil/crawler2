"""Evidence lifecycle event payloads (inside crawler2)."""

from __future__ import annotations

from typing import ClassVar

from antipiracy_contracts.events.envelope import EventPayload
from antipiracy_contracts.models.evidence import EvidenceCandidate, EvidenceSeal


class EvidenceCandidateCreated(EventPayload):
    EVENT_TYPE: ClassVar[str] = "evidence.candidate_created"
    SCHEMA_MAJOR: ClassVar[int] = 1

    candidate: EvidenceCandidate


class EvidenceFinalized(EventPayload):
    EVENT_TYPE: ClassVar[str] = "evidence.finalized"
    SCHEMA_MAJOR: ClassVar[int] = 1

    seal: EvidenceSeal
