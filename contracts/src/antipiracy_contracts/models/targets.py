"""Targets: the protected works the system searches for.

Targets are owned by the fingerprinter's target manager. crawler2 learns
about them only through ``target.registered`` / ``target.retired``; the
reference media and target representations never leave the fingerprinter.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar

from pydantic import Field

from antipiracy_contracts.base import ContractKind, ContractModel, UtcTimestamp
from antipiracy_contracts.ids import TargetId

TargetVersion = Annotated[int, Field(ge=1)]
"""Increments whenever the target's reference material changes."""


class TargetKind(StrEnum):
    MOVIE = "movie"
    SERIES_EPISODE = "series_episode"
    LIVE_EVENT = "live_event"
    OTHER = "other"


class TargetRef(ContractModel):
    """A specific version of a target. Matches are always against a version."""

    KIND: ClassVar[ContractKind] = ContractKind.VALUE

    target_id: TargetId
    version: TargetVersion


class Target(ContractModel):
    """One version of a protected work, as published by the target manager."""

    KIND: ClassVar[ContractKind] = ContractKind.ENTITY

    ref: TargetRef
    title: Annotated[str, Field(min_length=1, max_length=300)]
    kind: TargetKind
    aliases: tuple[Annotated[str, Field(min_length=1, max_length=300)], ...] = ()
    """Alternative titles/search terms used by crawl intelligence for discovery."""
    release_year: Annotated[int, Field(ge=1870, le=2200)] | None = None
    registered_at: UtcTimestamp
