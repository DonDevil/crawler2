"""Evidence provenance contracts.

The legal content of a case package is still open (ADR-005, blocks P12
design). These contracts fix only what any outcome needs: stable evidence
identity, references to the immutable provenance it rests on, and a
digest-pinned manifest once finalized. Additions will be additive fields.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar

from pydantic import Field

from antipiracy_contracts.base import ContractKind, ContractModel, UtcTimestamp
from antipiracy_contracts.ids import ContentId, EvidenceId, MatchId, MediaId, ObservationId
from antipiracy_contracts.models.blobs import BlobRef
from antipiracy_contracts.models.targets import TargetRef


class ArtifactRole(StrEnum):
    PAGE_SNAPSHOT = "page_snapshot"
    MEDIA_SAMPLE = "media_sample"
    SCREENSHOT = "screenshot"
    OTHER = "other"


class EvidenceArtifact(ContractModel):
    KIND: ClassVar[ContractKind] = ContractKind.VALUE

    role: ArtifactRole
    blob: BlobRef


class EvidenceCandidate(ContractModel):
    """Everything collected for one match, before completeness checks and freezing."""

    KIND: ClassVar[ContractKind] = ContractKind.EVIDENCE

    evidence_id: EvidenceId
    match_id: MatchId
    target: TargetRef
    content_id: ContentId
    media_ids: Annotated[tuple[MediaId, ...], Field(min_length=1)]
    page_observation_ids: Annotated[tuple[ObservationId, ...], Field(min_length=1)]
    artifacts: tuple[EvidenceArtifact, ...] = ()
    collected_at: UtcTimestamp


class EvidenceSeal(ContractModel):
    """The finalized, write-once form of an evidence item: its manifest, pinned by digest."""

    KIND: ClassVar[ContractKind] = ContractKind.EVIDENCE

    evidence_id: EvidenceId
    manifest: BlobRef
    finalized_at: UtcTimestamp
