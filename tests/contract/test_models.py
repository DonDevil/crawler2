import importlib
import inspect
import pkgutil
from datetime import datetime, timedelta, timezone

import antipiracy_contracts
import pytest
from antipiracy_contracts.base import ContractKind, ContractModel
from antipiracy_contracts.compat import model_fixture
from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events.fingerprinting import EncodeRequested, MatchFound
from antipiracy_contracts.events.media import MediaObserved
from antipiracy_contracts.events.web import FetchCompleted, PageObserved
from antipiracy_contracts.ids import ContentId, UrlId
from antipiracy_contracts.models.matching import MatchResult
from antipiracy_contracts.models.media import ContentKey, Media, MediaKind, ProbeStatus
from antipiracy_contracts.models.targets import Target
from antipiracy_contracts.models.web import Domain, FetchOutcome, UrlRef
from pydantic import ValidationError

from tests.contract.conftest import payload_of, revalidate


def _all_contract_models() -> list[type[ContractModel]]:
    for module in pkgutil.walk_packages(antipiracy_contracts.__path__, "antipiracy_contracts."):
        importlib.import_module(module.name)
    found: list[type[ContractModel]] = []
    pending: list[type[ContractModel]] = [ContractModel]
    while pending:
        cls = pending.pop()
        for sub in cls.__subclasses__():
            pending.append(sub)
            if sub.__module__.startswith("antipiracy_contracts.") and "[" not in sub.__name__:
                found.append(sub)
    return found


def test_every_contract_model_declares_its_kind_and_is_frozen() -> None:
    models = _all_contract_models()
    assert len(models) > 20
    for model in models:
        assert isinstance(model.KIND, ContractKind), model
        assert model.model_config.get("frozen") is True, model


def test_entities_observations_attempts_are_distinct_types() -> None:
    kinds = {model.__name__: model.KIND for model in _all_contract_models()}
    assert kinds["Target"] is ContractKind.ENTITY
    assert kinds["Media"] is ContractKind.ENTITY
    assert kinds["Domain"] is ContractKind.ENTITY
    assert kinds["PageObservation"] is ContractKind.OBSERVATION
    assert kinds["MediaObservation"] is ContractKind.OBSERVATION
    assert kinds["FetchAttempt"] is ContractKind.ATTEMPT
    assert kinds["Representation"] is ContractKind.REPRESENTATION
    assert kinds["MatchResult"] is ContractKind.DECISION
    assert kinds["EvidenceCandidate"] is ContractKind.EVIDENCE


def test_models_are_immutable() -> None:
    ref = UrlRef.of("https://example.com/")
    with pytest.raises(ValidationError, match="frozen"):
        ref.url_id = UrlId.of(ref.url)  # type: ignore[misc]


def test_url_ref_rejects_mismatched_ids() -> None:
    ref = UrlRef.of("https://example.com/a")
    other = UrlRef.of("https://example.com/b")
    with pytest.raises(ValidationError, match="url_id"):
        revalidate(ref, url_id=other.url_id)
    with pytest.raises(ValidationError, match="not canonical"):
        revalidate(ref, url="https://EXAMPLE.com/a")


def test_domain_model_fixture_and_consistency() -> None:
    domain = Domain.model_validate_json(model_fixture("domain"))
    assert domain.host == "watch-free.example"
    with pytest.raises(ValidationError, match="not canonical"):
        revalidate(domain, host="Watch-Free.example")


def test_target_model_fixture() -> None:
    target = Target.model_validate_json(model_fixture("target"))
    assert target.ref.version == 2
    with pytest.raises(ValidationError):
        revalidate(target, title="")


def test_timestamps_must_be_aware_and_are_normalized_to_utc() -> None:
    attempt = payload_of("fetch.completed.v1")
    assert isinstance(attempt, FetchCompleted)
    plus_two = timezone(timedelta(hours=2))
    shifted = attempt.attempt.model_copy()
    data = shifted.model_dump()
    data["started_at"] = datetime(2026, 9, 28, 12, 0, 0, tzinfo=plus_two)
    data["finished_at"] = datetime(2026, 9, 28, 12, 0, 1, tzinfo=plus_two)
    normalized = type(shifted).model_validate(data)
    assert normalized.started_at.utcoffset() == timedelta(0)
    assert normalized.started_at.hour == 10
    data["started_at"] = datetime(2026, 9, 28, 10, 0, 0)  # naive
    with pytest.raises(ValidationError, match="timezone"):
        type(shifted).model_validate(data)


def test_no_silent_type_coercion() -> None:
    attempt = payload_of("fetch.completed.v1")
    assert isinstance(attempt, FetchCompleted)
    with pytest.raises(ValidationError):
        revalidate(attempt.attempt, http_status="200")


def test_fetch_attempt_invariants() -> None:
    fetch = payload_of("fetch.completed.v1")
    assert isinstance(fetch, FetchCompleted)
    attempt = fetch.attempt
    with pytest.raises(ValidationError, match="http_status"):
        revalidate(attempt, http_status=None)
    with pytest.raises(ValidationError, match="finished_at"):
        revalidate(attempt, finished_at="2026-09-28T09:00:00Z")
    failed = revalidate(
        attempt, outcome=FetchOutcome.TIMEOUT.value, http_status=None, final=None, detail="30s"
    )
    assert failed.outcome is FetchOutcome.TIMEOUT
    with pytest.raises(ValidationError):
        revalidate(attempt, outcome="exploded")


def test_page_version_must_derive_from_final_url_and_body() -> None:
    observed = payload_of("page.observed.v1")
    assert isinstance(observed, PageObserved)
    with pytest.raises(ValidationError, match="page_version_id"):
        revalidate(observed.observation, body_digest=ContentDigest.of_bytes(b"x"), snapshot=None)


def test_content_identity_is_distinct_from_media_identity() -> None:
    request = payload_of("encode.requested.v1")
    assert isinstance(request, EncodeRequested)
    # Same bytes at another address: new media identity, same content identity.
    mirror = Media.of(UrlRef.of("https://mirror.example/bbb.mp4"), MediaKind.VIDEO_FILE)
    assert mirror.media_id != request.source.media_id
    same_bytes = ContentKey.of(request.content.scheme, request.content.digest)
    assert same_bytes.content_id == request.content.content_id
    # Same digest under another sampling scheme is a different candidate identity.
    assert ContentId.of("sampled-bytes/v2", request.content.digest) != same_bytes.content_id
    with pytest.raises(ValidationError, match="content_id"):
        revalidate(request.content, scheme="sampled-bytes/v2")


def test_content_key_requires_successful_probe() -> None:
    observed = payload_of("media.observed.v1")
    assert isinstance(observed, MediaObserved)
    with pytest.raises(ValidationError, match="probe"):
        revalidate(observed.observation, probe_status=ProbeStatus.UNREACHABLE.value)


def test_match_id_is_derived_from_compared_representations() -> None:
    found = payload_of("match.found.v1")
    assert isinstance(found, MatchFound)
    match = found.match
    assert match.match_id == MatchResult.expected_id(
        match.representation_id, match.target_representation_id, match.technique
    )
    with pytest.raises(ValidationError, match="match_id"):
        revalidate(match, technique={"name": "temporal-alignment", "version": "3.0"})
    with pytest.raises(ValidationError):
        revalidate(match, confidence=1.5)


def test_optional_fields_default_and_required_fields_are_required() -> None:
    ref = UrlRef.of("https://example.com/")
    minimal = Media.model_validate(
        {
            "media_id": Media.of(ref, MediaKind.HLS_MANIFEST).media_id,
            "locator": ref,
            "kind": MediaKind.HLS_MANIFEST,
        }
    )
    assert minimal.kind is MediaKind.HLS_MANIFEST
    with pytest.raises(ValidationError, match="kind"):
        Media.model_validate({"media_id": minimal.media_id, "locator": ref})


def test_contract_classes_do_not_expose_mutable_defaults() -> None:
    for model in _all_contract_models():
        for name, field in model.model_fields.items():
            assert not isinstance(field.default, list | set), (model, name)
            if inspect.isclass(field.annotation):
                assert field.annotation is not list, (model, name)
