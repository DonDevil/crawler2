import json
from datetime import UTC, datetime

import pytest
from antipiracy_contracts.compat import event_fixtures
from antipiracy_contracts.events import (
    EventEnvelope,
    OwnershipViolationError,
    UnexpectedEventTypeError,
    UnknownEventTypeError,
    UnsupportedSchemaVersionError,
    decode_event,
    decode_event_as,
    encode_event,
    new_event,
)
from antipiracy_contracts.events.fingerprinting import EncodeRequested, RepresentationReady
from antipiracy_contracts.events.web import CrawlReason, CrawlRequested
from antipiracy_contracts.ids import CorrelationId, EventId
from antipiracy_contracts.models.web import UrlRef
from antipiracy_contracts.ownership import Component, Producer, ServiceName
from pydantic import ValidationError

NOW = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
INTELLIGENCE = Producer(
    service=ServiceName.CRAWLER2, component=Component.CRAWL_INTELLIGENCE, instance="h1:intel:1:a"
)


def _crawl_request() -> CrawlRequested:
    return CrawlRequested(url=UrlRef.of("https://example.com/"), reason=CrawlReason.SEED)


def _raw(name: str) -> dict[str, object]:
    fixture = next(f for f in event_fixtures() if f.name == name)
    data: dict[str, object] = json.loads(fixture.raw)
    return data


def test_new_event_fills_envelope_from_catalog() -> None:
    correlation = CorrelationId.new()
    cause = EventId.new()
    envelope = new_event(
        _crawl_request(),
        producer=INTELLIGENCE,
        occurred_at=NOW,
        correlation_id=correlation,
        causation_id=cause,
        metadata={"trace.id": "abc"},
    )
    assert envelope.event_type == "crawl.requested"
    assert envelope.schema_version == "1.0"
    assert (envelope.schema_major, envelope.schema_minor) == (1, 0)
    assert envelope.envelope_version == 1
    assert envelope.correlation_id == correlation
    assert envelope.causation_id == cause
    assert decode_event(encode_event(envelope)) == envelope


def test_non_owner_cannot_produce_an_event() -> None:
    worker = Producer(
        service=ServiceName.CRAWLER2, component=Component.CRAWLER_WORKER, instance="h1:http:1:a"
    )
    with pytest.raises(OwnershipViolationError, match="crawl_intelligence"):
        new_event(_crawl_request(), producer=worker, occurred_at=NOW)


def test_consumer_rejects_event_from_non_owner() -> None:
    data = _raw("encode.requested.v1")
    data["producer"] = {"service": "crawler2", "component": "extraction", "instance": "h:x:1:a"}
    with pytest.raises(OwnershipViolationError):
        decode_event(json.dumps(data))


def test_producer_component_must_belong_to_service() -> None:
    with pytest.raises(ValidationError, match="belongs to service"):
        Producer(service=ServiceName.CRAWLER2, component=Component.ENCODER, instance="x")


def test_envelope_rejects_payload_of_another_type() -> None:
    envelope = new_event(_crawl_request(), producer=INTELLIGENCE, occurred_at=NOW)
    data = envelope.model_dump(mode="json")
    data["event_type"] = "fetch.completed"
    with pytest.raises(ValidationError, match="does not match payload"):
        EventEnvelope[CrawlRequested].model_validate_json(json.dumps(data))


def test_unknown_event_type_and_unknown_major() -> None:
    data = _raw("encode.requested.v1")
    data["event_type"] = "encode.cancelled"
    with pytest.raises(UnknownEventTypeError):
        decode_event(json.dumps(data))
    data = _raw("encode.requested.v1")
    data["schema_version"] = "2.0"
    with pytest.raises(UnsupportedSchemaVersionError):
        decode_event(json.dumps(data))


@pytest.mark.parametrize("version", ["1", "01.0", "1.0.0", "v1.0", ""])
def test_schema_version_format(version: str) -> None:
    data = _raw("encode.requested.v1")
    data["schema_version"] = version
    with pytest.raises(ValidationError):
        decode_event(json.dumps(data))


def test_newer_minor_with_unknown_fields_is_accepted() -> None:
    base = decode_event(json.dumps(_raw("encode.requested.v1")))
    data = _raw("encode.requested.v1")
    data["schema_version"] = "1.42"
    data["future_envelope_field"] = True
    payload = data["payload"]
    assert isinstance(payload, dict)
    payload["future_payload_field"] = [1, 2]
    newer = decode_event(json.dumps(data))
    assert newer.payload == base.payload
    assert newer.schema_minor == 42


def test_missing_required_payload_field_is_rejected() -> None:
    data = _raw("encode.requested.v1")
    payload = data["payload"]
    assert isinstance(payload, dict)
    del payload["content"]
    with pytest.raises(ValidationError, match="content"):
        decode_event(json.dumps(data))


def test_absent_optional_payload_fields_take_defaults() -> None:
    data = _raw("encode.requested.v1")
    payload = data["payload"]
    assert isinstance(payload, dict)
    for optional in ("referer", "required_spec", "priority"):
        del payload[optional]
    decoded = decode_event_as(json.dumps(data), EncodeRequested)
    assert decoded.payload.referer is None
    assert decoded.payload.required_spec is None
    assert decoded.payload.priority == 50


def test_decode_event_as_checks_the_type() -> None:
    with pytest.raises(UnexpectedEventTypeError):
        decode_event_as(json.dumps(_raw("encode.requested.v1")), RepresentationReady)


def test_occurred_at_must_be_timezone_aware() -> None:
    data = _raw("encode.requested.v1")
    data["occurred_at"] = "2026-09-28T10:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        decode_event(json.dumps(data))


@pytest.mark.parametrize(
    "metadata",
    [{"Bad Key": "x"}, {"k": "v" * 501}, {f"k{i}": "v" for i in range(33)}],
)
def test_metadata_is_bounded(metadata: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        new_event(_crawl_request(), producer=INTELLIGENCE, occurred_at=NOW, metadata=metadata)


def test_priority_bounds() -> None:
    with pytest.raises(ValidationError):
        CrawlRequested(url=UrlRef.of("https://e.x/"), reason=CrawlReason.SEED, priority=101)
