"""Target lifecycle event payloads (produced by the fingerprinter's target manager)."""

from __future__ import annotations

from typing import ClassVar

from antipiracy_contracts.base import ShortText, UtcTimestamp
from antipiracy_contracts.events.envelope import EventPayload
from antipiracy_contracts.ids import TargetId
from antipiracy_contracts.models.targets import Target


class TargetRegistered(EventPayload):
    """A target, or a new version of one, is ready for matching."""

    EVENT_TYPE: ClassVar[str] = "target.registered"
    SCHEMA_MAJOR: ClassVar[int] = 1

    target: Target


class TargetRetired(EventPayload):
    """A target is no longer searched for. Past matches and evidence remain valid."""

    EVENT_TYPE: ClassVar[str] = "target.retired"
    SCHEMA_MAJOR: ClassVar[int] = 1

    target_id: TargetId
    retired_at: UtcTimestamp
    reason: ShortText | None = None
