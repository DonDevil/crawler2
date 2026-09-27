"""Golden fixtures: the proof that producers and consumers agree on the wire format.

The crawler-side and fingerprinter-side tests use only the public contract
API, exactly as each service's consumer code will.
"""

import json

import pytest
from antipiracy_contracts.compat import (
    EventFixture,
    event_fixtures,
    fixture_digests,
    released_digests,
)
from antipiracy_contracts.events import CATALOG, decode_event, encode_event
from antipiracy_contracts.events.fingerprinting import (
    EncodeFailed,
    EncodeRequested,
    MatchFound,
    RepresentationReady,
)
from antipiracy_contracts.events.targets import TargetRegistered
from antipiracy_contracts.ownership import ServiceName

FIXTURES = event_fixtures()
BY_NAME = {f.name: f for f in FIXTURES}


def test_released_fixtures_are_unchanged() -> None:
    assert fixture_digests() == released_digests(), (
        "released fixtures are immutable; add a new fixture instead of editing one"
    )


def test_every_event_type_has_a_base_fixture() -> None:
    covered = {f.spec.event_type for f in FIXTURES if f.base_name is None}
    assert covered == {spec.event_type for spec in CATALOG}


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.name)
def test_fixture_decodes_and_round_trips(fixture: EventFixture) -> None:
    envelope = decode_event(fixture.raw)
    assert decode_event(encode_event(envelope)) == envelope
    if fixture.base_name is None:
        # Base fixtures are exactly what the current producer code emits.
        assert json.loads(encode_event(envelope)) == json.loads(fixture.raw)


@pytest.mark.parametrize(
    "fixture", [f for f in FIXTURES if f.base_name is not None], ids=lambda f: f.name
)
def test_forward_fixture_matches_its_base(fixture: EventFixture) -> None:
    newer = decode_event(fixture.raw)
    base = decode_event(BY_NAME[str(fixture.base_name)].raw)
    assert newer.schema_major == base.schema_major
    assert newer.schema_minor > base.schema_minor
    assert newer.payload == base.payload
    assert newer.model_copy(update={"schema_version": base.schema_version}) == base


def test_fingerprinter_consumes_what_crawler2_produces() -> None:
    inbound = [
        f
        for f in FIXTURES
        if f.producer_service is ServiceName.CRAWLER2
        and ServiceName.FINGERPRINTER in f.consumer_services
    ]
    assert {f.spec.event_type for f in inbound} == {"encode.requested"}
    for fixture in inbound:
        request = decode_event(fixture.raw).payload
        assert isinstance(request, EncodeRequested)
        # Everything needed to fetch and encode, without crawler internals:
        assert request.content.content_id.startswith("cnt_")
        assert request.source.locator.url.startswith("https://")
        assert request.required_spec is None or request.required_spec.name


def test_crawler2_consumes_what_the_fingerprinter_produces() -> None:
    inbound = [
        f
        for f in FIXTURES
        if f.producer_service is ServiceName.FINGERPRINTER
        and ServiceName.CRAWLER2 in f.consumer_services
    ]
    seen = {type(decode_event(f.raw).payload) for f in inbound}
    assert {RepresentationReady, EncodeFailed, MatchFound, TargetRegistered} <= seen


def test_match_result_joins_back_to_the_encode_request() -> None:
    request = decode_event(BY_NAME["encode.requested.v1"].raw).payload
    ready = decode_event(BY_NAME["representation.ready.v1"].raw).payload
    match = decode_event(BY_NAME["match.found.v1"].raw).payload
    target = decode_event(BY_NAME["target.registered.v1"].raw).payload
    assert isinstance(request, EncodeRequested)
    assert isinstance(ready, RepresentationReady)
    assert isinstance(match, MatchFound)
    assert isinstance(target, TargetRegistered)
    assert ready.representation.content_id == request.content.content_id
    assert ready.representation.spec == request.required_spec
    assert match.match.content_id == request.content.content_id
    assert match.match.representation_id == ready.representation.representation_id
    assert match.match.target == target.target.ref


def test_encode_failure_answers_its_request() -> None:
    request = decode_event(BY_NAME["encode.requested.v1"].raw)
    failed = decode_event(BY_NAME["encode.failed.v1"].raw)
    assert isinstance(failed.payload, EncodeFailed)
    assert failed.payload.request_id == request.event_id
    assert failed.causation_id == request.event_id
    assert failed.correlation_id == request.correlation_id
