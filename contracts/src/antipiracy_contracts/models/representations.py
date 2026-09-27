"""Representations: what the fingerprinter derives from media content.

Deliberately opaque to crawler2: a spec is a name, a version and a digest
of its configuration. Model architecture, tensor layout and index details
stay inside the fingerprinter and can change without a contract change.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Self

from pydantic import Field, model_validator

from antipiracy_contracts.base import ContractKind, ContractModel, UtcTimestamp
from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.ids import ContentId, RepresentationId

SpecName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")]
SpecVersion = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")]


class RepresentationSpec(ContractModel):
    """How a representation is computed. Different spec => different representation."""

    KIND: ClassVar[ContractKind] = ContractKind.VALUE

    name: SpecName
    version: SpecVersion
    config_digest: ContentDigest
    """Digest of the full encoder configuration (sampling, preprocessing, precision, ...)."""


class Representation(ContractModel):
    """Metadata of a stored representation of one content identity. Rebuildable.

    Vectors are not part of the contract; they live in fingerprinter storage.
    """

    KIND: ClassVar[ContractKind] = ContractKind.REPRESENTATION

    representation_id: RepresentationId
    content_id: ContentId
    spec: RepresentationSpec
    created_at: UtcTimestamp
    segment_count: Annotated[int, Field(ge=0)] | None = None
    media_duration_seconds: Annotated[float, Field(ge=0)] | None = None

    @classmethod
    def expected_id(cls, content_id: ContentId, spec: RepresentationSpec) -> RepresentationId:
        return RepresentationId.for_content(content_id, spec.name, spec.version, spec.config_digest)

    @model_validator(mode="after")
    def _id_matches_inputs(self) -> Self:
        if self.representation_id != self.expected_id(self.content_id, self.spec):
            raise ValueError("representation_id is not derived from (content_id, spec)")
        return self
