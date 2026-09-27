"""Event envelope, payloads, catalog (ownership) and codec."""

from antipiracy_contracts.events.catalog import (
    CATALOG,
    ContractError,
    EventCatalog,
    EventSpec,
    OwnershipViolationError,
    UnexpectedEventTypeError,
    UnknownEventTypeError,
    UnsupportedSchemaVersionError,
)
from antipiracy_contracts.events.codec import decode_event, decode_event_as, encode_event, new_event
from antipiracy_contracts.events.envelope import (
    ENVELOPE_VERSION,
    EnvelopeHeader,
    EventEnvelope,
    EventPayload,
)

__all__ = [
    "CATALOG",
    "ENVELOPE_VERSION",
    "ContractError",
    "EnvelopeHeader",
    "EventCatalog",
    "EventEnvelope",
    "EventPayload",
    "EventSpec",
    "OwnershipViolationError",
    "UnexpectedEventTypeError",
    "UnknownEventTypeError",
    "UnsupportedSchemaVersionError",
    "decode_event",
    "decode_event_as",
    "encode_event",
    "new_event",
]
