"""Match decisions produced by the fingerprinter's matcher."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar, Self

from pydantic import Field, model_validator

from antipiracy_contracts.base import ContractKind, ContractModel, UtcTimestamp
from antipiracy_contracts.ids import ContentId, MatchId, RepresentationId
from antipiracy_contracts.models.representations import SpecName, SpecVersion
from antipiracy_contracts.models.targets import TargetRef

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class Technique(ContractModel):
    """The comparison method, versioned so a result can be reproduced or re-evaluated."""

    KIND: ClassVar[ContractKind] = ContractKind.VALUE

    name: SpecName
    version: SpecVersion


class MatchVerdict(StrEnum):
    CONFIRMED = "confirmed"
    """Passed temporal verification above the technique's confirmation threshold."""
    PROBABLE = "probable"
    """Above the reporting floor but below confirmation; needs review before evidence."""


class AlignedSegment(ContractModel):
    """Where in the media and in the target the verified overlap lies."""

    KIND: ClassVar[ContractKind] = ContractKind.VALUE

    media_offset_seconds: Annotated[float, Field(ge=0)]
    target_offset_seconds: Annotated[float, Field(ge=0)]
    duration_seconds: Annotated[float, Field(gt=0)]


class MatchResult(ContractModel):
    """A positive comparison of one content identity against one target version.

    Keyed by content identity, not by URL: crawler2 maps it back to media,
    pages and observations through its own registry. Non-matches are not
    events — with ANN candidate reduction there is no per-pair job whose
    negative outcome would be meaningful.
    """

    KIND: ClassVar[ContractKind] = ContractKind.DECISION

    match_id: MatchId
    content_id: ContentId
    representation_id: RepresentationId
    target: TargetRef
    target_representation_id: RepresentationId
    technique: Technique
    verdict: MatchVerdict
    confidence: Confidence
    segments: tuple[AlignedSegment, ...] = ()
    decided_at: UtcTimestamp

    @classmethod
    def expected_id(
        cls,
        representation_id: RepresentationId,
        target_representation_id: RepresentationId,
        technique: Technique,
    ) -> MatchId:
        return MatchId.of(
            representation_id, target_representation_id, technique.name, technique.version
        )

    @model_validator(mode="after")
    def _id_matches_inputs(self) -> Self:
        expected = self.expected_id(
            self.representation_id, self.target_representation_id, self.technique
        )
        if self.match_id != expected:
            raise ValueError("match_id is not derived from (representations, technique)")
        return self
