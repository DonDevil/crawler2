"""Creating, encoding and decoding events (JSON wire form).

Decoding is two-step: the header routes to a catalog entry, then the full
envelope is parsed with that entry's payload model. Unknown fields at any
level are ignored (forward compatibility); a newer minor version from a
producer is accepted by an older consumer of the same major.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from antipiracy_contracts.events.catalog import (
    CATALOG,
    EventSpec,
    OwnershipViolationError,
    UnexpectedEventTypeError,
)
from antipiracy_contracts.events.envelope import (
    ENVELOPE_VERSION,
    EnvelopeHeader,
    EventEnvelope,
    EventPayload,
    envelope_model,
)
from antipiracy_contracts.ids import CorrelationId, EventId
from antipiracy_contracts.ownership import Producer


def new_event[P: EventPayload](
    payload: P,
    *,
    producer: Producer,
    occurred_at: datetime,
    correlation_id: CorrelationId | None = None,
    causation_id: EventId | None = None,
    metadata: dict[str, str] | None = None,
    event_id: EventId | None = None,
) -> EventEnvelope[P]:
    """Wrap a payload in an envelope, enforcing catalog ownership."""
    spec = CATALOG.spec_of(type(payload))
    _check_owner(spec, producer)
    return envelope_model(type(payload))(
        envelope_version=ENVELOPE_VERSION,
        event_id=event_id or EventId.new(),
        event_type=spec.event_type,
        schema_version=spec.schema_version,
        occurred_at=occurred_at,
        producer=producer,
        correlation_id=correlation_id,
        causation_id=causation_id,
        metadata=metadata or {},
        payload=payload,
    )


def encode_event[P: EventPayload](envelope: EventEnvelope[P]) -> bytes:
    return envelope.model_dump_json().encode("utf-8")


def decode_event(data: str | bytes) -> EventEnvelope[EventPayload]:
    """Decode any cataloged event. Raises ``ContractError`` subclasses or ``ValidationError``."""
    header = EnvelopeHeader.model_validate_json(data)
    spec = CATALOG.resolve(header.event_type, header.schema_major)
    _check_owner(spec, header.producer)
    return envelope_model(spec.payload).model_validate_json(data)


def decode_event_as[P: EventPayload](data: str | bytes, payload_type: type[P]) -> EventEnvelope[P]:
    """Decode an event that must be of ``payload_type`` (a consumer's single subscription)."""
    envelope = decode_event(data)
    if not isinstance(envelope.payload, payload_type):
        raise UnexpectedEventTypeError(
            f"expected {payload_type.EVENT_TYPE}, got {envelope.event_type}"
        )
    return cast(EventEnvelope[P], envelope)


def _check_owner(spec: EventSpec, producer: Producer) -> None:
    if producer.component is not spec.producer:
        raise OwnershipViolationError(
            f"{spec.event_type} is owned by {spec.producer.value!r}; "
            f"{producer.component.value!r} may not produce it"
        )
