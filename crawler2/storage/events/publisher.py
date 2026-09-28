"""Event transport behind a small interface; Redis Streams implementation (ADR-004).

The relay hands over the exact envelope bytes stored in the outbox, so what
consumers decode is byte-for-byte what the producer committed. Nothing here
knows about Scylla; replacing the transport (e.g. Kafka) replaces this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import redis

from crawler2.core.configuration import EventSettings
from crawler2.storage.errors import StorageUnavailableError


@dataclass(frozen=True, slots=True)
class OutboundEvent:
    event_id: str
    event_type: str
    schema_major: int
    envelope: bytes


class EventPublisher(Protocol):
    def publish(self, events: Sequence[OutboundEvent]) -> None:
        """Deliver every event or raise. A raise may follow a partial delivery; the
        caller republishes, so delivery is at-least-once (duplicates are expected)."""
        ...


def stream_name(settings: EventSettings, event_type: str, schema_major: int) -> str:
    """One stream per (event type, major), shared by every producer instance."""
    return f"{settings.stream_prefix}{event_type}.v{schema_major}"


class RedisStreamPublisher:
    def __init__(self, client: redis.Redis, settings: EventSettings) -> None:
        self._client = client
        self._settings = settings

    def publish(self, events: Sequence[OutboundEvent]) -> None:
        if not events:
            return
        pipe = self._client.pipeline(transaction=False)
        for event in events:
            pipe.xadd(
                stream_name(self._settings, event.event_type, event.schema_major),
                {
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "envelope": event.envelope,
                },
                maxlen=self._settings.stream_maxlen,
                approximate=True,
            )
        try:
            pipe.execute()
        except redis.RedisError as exc:
            raise StorageUnavailableError(f"event publication failed: {exc}") from exc
