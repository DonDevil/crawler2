"""The transport-independent event envelope (ADR-009).

The envelope says *what happened, when, by whom, and in which causal
chain*; nothing about streams, partitions or acknowledgement. P2 decides
how envelopes are stored (outbox) and carried (Redis Streams, ADR-004).
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal, Self, cast

from pydantic import Field, model_validator

from antipiracy_contracts.base import ContractKind, ContractModel, UtcTimestamp
from antipiracy_contracts.ids import CorrelationId, EventId
from antipiracy_contracts.ownership import Producer

ENVELOPE_VERSION: Literal[1] = 1

EventType = Annotated[str, Field(pattern=r"^[a-z][a-z_]*\.[a-z][a-z_]*$", max_length=64)]
SchemaVersion = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,3}\.(0|[1-9][0-9]{0,3})$")]
"""``MAJOR.MINOR``. Same major = compatible; minor counts additive changes."""

MetadataKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]
MetadataValue = Annotated[str, Field(max_length=500)]


class EventPayload(ContractModel):
    """Base of every event payload. Each subclass is one (event type, major version)."""

    KIND: ClassVar[ContractKind] = ContractKind.EVENT
    EVENT_TYPE: ClassVar[str]
    SCHEMA_MAJOR: ClassVar[int]


class EnvelopeHeader(ContractModel):
    """Everything in an envelope except the payload.

    Consumers parse this first to route a message, then parse the full
    envelope with the payload model the catalog names.
    """

    KIND: ClassVar[ContractKind] = ContractKind.VALUE

    envelope_version: Literal[1]
    event_id: EventId
    event_type: EventType
    schema_version: SchemaVersion
    occurred_at: UtcTimestamp
    """When the described occurrence happened (not when it was published)."""
    producer: Producer
    correlation_id: CorrelationId | None = None
    """Shared by every event in one causal chain, e.g. crawl request -> ... -> evidence."""
    causation_id: EventId | None = None
    """The event that directly caused this one, if any."""
    metadata: Annotated[dict[MetadataKey, MetadataValue], Field(max_length=32)] = Field(
        default_factory=dict
    )
    """Extension/diagnostic boundary (e.g. trace IDs). No consumer may depend on it for
    correctness; anything with contract meaning becomes a typed payload field instead."""

    @property
    def schema_major(self) -> int:
        return int(self.schema_version.partition(".")[0])

    @property
    def schema_minor(self) -> int:
        return int(self.schema_version.partition(".")[2])


class EventEnvelope[PayloadT: EventPayload](EnvelopeHeader):
    KIND: ClassVar[ContractKind] = ContractKind.EVENT

    payload: PayloadT

    @model_validator(mode="after")
    def _payload_matches_header(self) -> Self:
        payload_type = type(self.payload)
        if self.event_type != payload_type.EVENT_TYPE:
            raise ValueError(
                f"event_type {self.event_type!r} does not match payload "
                f"{payload_type.__name__} ({payload_type.EVENT_TYPE!r})"
            )
        if self.schema_major != payload_type.SCHEMA_MAJOR:
            raise ValueError(
                f"schema major {self.schema_major} does not match payload "
                f"{payload_type.__name__} (major {payload_type.SCHEMA_MAJOR})"
            )
        return self


def envelope_model[P: EventPayload](payload_type: type[P]) -> type[EventEnvelope[P]]:
    """The concrete envelope model for a payload type (pydantic caches each parametrization)."""
    # Parametrizing with a runtime type value is valid pydantic but not expressible to mypy.
    return cast("type[EventEnvelope[P]]", EventEnvelope.__class_getitem__(payload_type))
