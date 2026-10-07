"""Event transport behind a small interface; Redis Streams implementation (ADR-004).

The relay hands over the exact envelope bytes stored in the outbox, so what
consumers decode is byte-for-byte what the producer committed. Nothing here
knows about Scylla; replacing the transport (e.g. Kafka) replaces this module.

Retention (ADR-022) is bounded by bytes and never drops an entry a consumer
group has not acknowledged:

- a stream **with** consumer groups is trimmed (exact ``XTRIM MINID``) to the
  oldest entry some group still needs: its oldest pending entry, or the entry
  after its last-delivered ID. Its byte budget is enforced at the *write*:
  when the next event would push the stream past ``stream_max_bytes``, the
  publisher stops and raises ``StreamBackpressureError``. The undelivered rows stay
  in the outbox (the system of record) until consumers catch up;
- a stream **without** groups has no reader that could miss an entry; it keeps
  at most ``stream_maxlen`` entries and ``stream_max_bytes`` bytes.

A consumer group must exist before the entries it needs are published: a group
created later starts from whatever is still retained (history is replayed from
the outbox, ``crawler2-storage relay --replay-from``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import redis

from crawler2.core.configuration import EventSettings
from crawler2.storage.errors import StorageUnavailableError

ENTRY_OVERHEAD_BYTES = 1024
"""Conservative per-entry cost beyond the envelope. Measured on Redis 7.4: an entry
of ≥ 4 KiB fills a stream node of its own and costs ~1.2 KiB more than its fields;
smaller entries share nodes and cost less. The stream's real size is re-measured
(``MEMORY USAGE``) before every batch, so an estimation error is bounded by one
batch and never accumulates."""


@dataclass(frozen=True, slots=True)
class OutboundEvent:
    event_id: str
    event_type: str
    schema_major: int
    envelope: bytes


class StreamBackpressureError(StorageUnavailableError):
    """A stream's unacknowledged entries fill its byte budget.

    The first ``published`` events of the batch were delivered; the rest were
    not and must be offered again later. Not a failure of Redis: consumers are
    behind.
    """

    def __init__(self, stream: str, published: int, unacked_bytes: int) -> None:
        super().__init__(
            f"stream {stream} is at its byte budget ({unacked_bytes} bytes unacknowledged); "
            f"{published} event(s) delivered"
        )
        self.stream = stream
        self.published = published
        self.unacked_bytes = unacked_bytes


class EventPublisher(Protocol):
    def publish(self, events: Sequence[OutboundEvent]) -> None:
        """Deliver every event or raise. A raise may follow a partial delivery; the
        caller republishes, so delivery is at-least-once (duplicates are expected).
        ``StreamBackpressureError`` reports how many leading events were delivered."""
        ...


def stream_name(settings: EventSettings, event_type: str, schema_major: int) -> str:
    """One stream per (event type, major), shared by every producer instance."""
    return f"{settings.stream_prefix}{event_type}.v{schema_major}"


@dataclass(frozen=True, slots=True)
class GroupState:
    """What retention needs from ``XINFO GROUPS`` / ``XPENDING`` for one group."""

    name: str
    last_delivered_id: str
    pending: int
    oldest_pending_id: str | None


def _id(entry_id: str) -> tuple[int, int]:
    ms, _, seq = entry_id.partition("-")
    return int(ms), int(seq or 0)


def next_id(entry_id: str) -> str:
    """The smallest stream ID greater than ``entry_id``."""
    ms, seq = _id(entry_id)
    return f"{ms}-{seq + 1}"


def trim_floor(groups: Sequence[GroupState]) -> str | None:
    """Oldest stream ID any group still needs (``XTRIM MINID`` keeps IDs ≥ it).

    ``None`` when there is no group (nothing to protect).
    """
    if not groups:
        return None
    needed = [
        g.oldest_pending_id if g.pending and g.oldest_pending_id else next_id(g.last_delivered_id)
        for g in groups
    ]
    return min(needed, key=_id)


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class RedisStreamPublisher:
    def __init__(self, client: redis.Redis, settings: EventSettings) -> None:
        self._client = client
        self._settings = settings

    def publish(self, events: Sequence[OutboundEvent]) -> None:
        if not events:
            return
        try:
            self._publish(events)
        except StreamBackpressureError:
            raise
        except redis.RedisError as exc:
            raise StorageUnavailableError(f"event publication failed: {exc}") from exc

    def _publish(self, events: Sequence[OutboundEvent]) -> None:
        budget = self._settings.stream_max_bytes
        streams = [stream_name(self._settings, e.event_type, e.schema_major) for e in events]
        state = {s: self._retain(s) for s in dict.fromkeys(streams)}
        used = {s: st[1] for s, st in state.items()}
        count = 0
        blocked: str | None = None
        for stream, event in zip(streams, events, strict=True):
            grouped, current = state[stream][0], used[stream]
            size = len(event.envelope) + ENTRY_OVERHEAD_BYTES
            # Only a stream with readers holds back: an event larger than the whole
            # budget still passes once the stream has nothing unacknowledged left.
            if grouped and current > 0 and current + size > budget:
                blocked = stream
                break
            used[stream] = current + size
            count += 1
        if count:
            pipe = self._client.pipeline(transaction=False)
            for stream, event in zip(streams[:count], events[:count], strict=True):
                fields: Any = {
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "envelope": event.envelope,
                }
                if state[stream][0]:
                    pipe.xadd(stream, fields)
                else:
                    pipe.xadd(stream, fields, maxlen=self._settings.stream_maxlen, approximate=True)
            pipe.execute()
            for stream in dict.fromkeys(streams[:count]):
                if not state[stream][0]:
                    self._cap_bytes(stream)
        if blocked is not None:
            raise StreamBackpressureError(blocked, count, used[blocked])

    def groups(self, stream: str) -> list[GroupState]:
        try:
            infos: Any = self._client.xinfo_groups(stream)
        except redis.ResponseError as exc:  # no such key: no groups yet
            if "no such key" in str(exc).lower():
                return []
            raise
        out = []
        for info in infos:
            name = _text(info["name"])
            pending = int(info["pending"])
            oldest = None
            if pending:
                summary: Any = self._client.xpending(stream, name)
                oldest = _text(summary["min"]) if summary.get("min") else None
            out.append(GroupState(name, _text(info["last-delivered-id"]), pending, oldest))
        return out

    def _retain(self, stream: str) -> tuple[bool, int]:
        """Trim what every group has acknowledged; return (has groups, bytes held)."""
        groups = self.groups(stream)
        floor = trim_floor(groups)
        if floor is not None:
            self._client.xtrim(stream, minid=floor, approximate=False)
            if not self._client.xlen(stream):
                return True, 0
        return bool(groups), self._bytes(stream)

    def _bytes(self, stream: str) -> int:
        usage: Any = self._client.memory_usage(stream, samples=0)
        return int(usage or 0)

    def _cap_bytes(self, stream: str) -> None:
        """Byte cap for a stream nobody reads through a group (oldest entries go)."""
        size = self._bytes(stream)
        budget = self._settings.stream_max_bytes
        if size <= budget:
            return
        length = int(self._client.xlen(stream))
        keep = int(length * budget / size * 0.9)
        self._client.xtrim(stream, maxlen=keep, approximate=False)
