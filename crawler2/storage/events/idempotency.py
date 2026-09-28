"""Event-specific idempotency keys, computed from the P1 catalog.

The key is exactly the catalog's ``idempotency_key`` paths of the event's
(type, major), rendered deterministically. It is *not* the event id: a
retried producer write allocates a new event id for the same logical fact,
and consumers must still see it as a duplicate.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any

from antipiracy_contracts.events import CATALOG, EventEnvelope, EventPayload
from pydantic import BaseModel


def _render(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, str):  # includes typed IDs and digests
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"cannot render {type(value).__name__} in an idempotency key")


def _value_at(payload: EventPayload, path: str) -> Any:
    current: Any = payload
    for segment in path.split("."):
        current = getattr(current, segment)
        if current is None:
            return None
    return current


def idempotency_key[P: EventPayload](envelope: EventEnvelope[P]) -> str:
    """``[event_type, major, [values...]]`` as compact, key-sorted JSON."""
    spec = CATALOG.resolve(envelope.event_type, envelope.schema_major)
    values = [_render(_value_at(envelope.payload, path)) for path in spec.idempotency_key]
    return json.dumps(
        [spec.event_type, spec.major, values],
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )
