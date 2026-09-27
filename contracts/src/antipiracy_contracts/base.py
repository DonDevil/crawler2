"""Primitives every contract model is built from.

Evolution policy (ADR-009) is encoded here, once:

- ``frozen=True``: a contract value never changes after construction.
- ``extra="ignore"``: a consumer built against minor version N parses
  messages from a producer at minor N+k by dropping fields it does not know.
  Producers cannot smuggle unknown fields in by accident because the mypy
  pydantic plugin runs with ``init_forbid_extra`` (see pyproject.toml).
- ``strict=True``: no silent coercion (``"1"`` is not an int). JSON input
  still accepts the JSON spelling of datetimes, enums and tuples.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, ClassVar

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field


class ContractKind(StrEnum):
    """What a contract model *is*; the kinds are deliberately not interchangeable."""

    ENTITY = "entity"
    """A persistent thing known to the system, addressed by a stable ID."""
    OBSERVATION = "observation"
    """What was seen about an entity/resource at one point in time. Append-only."""
    ATTEMPT = "attempt"
    """One execution of work (e.g. a fetch), successful or not."""
    REPRESENTATION = "representation"
    """A derived computational artifact (e.g. an embedding set). Rebuildable."""
    DECISION = "decision"
    """A judgement derived from representations; reproducible from its inputs."""
    EVIDENCE = "evidence"
    """An immutable provenance record supporting a later case/report."""
    VALUE = "value"
    """A value object embedded in other contracts; no identity of its own."""
    EVENT = "event"
    """An event payload: an occurrence or request addressed to other components."""


class ContractModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    KIND: ClassVar[ContractKind]


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcTimestamp = Annotated[AwareDatetime, AfterValidator(_to_utc)]
"""A timezone-aware instant, normalized to UTC. Naive datetimes are rejected."""

Priority = Annotated[int, Field(ge=0, le=100)]
"""Relative urgency, 0 (lowest) .. 100 (highest), default ``DEFAULT_PRIORITY``.

One number with one meaning for every consumer: higher is served sooner
within that consumer's own queue. It is never mapped onto separate streams
or queues (V1 defect D12).
"""

DEFAULT_PRIORITY = 50

ShortText = Annotated[str, Field(min_length=1, max_length=500)]
"""Human-readable diagnostic text. Never parsed by consumers."""
