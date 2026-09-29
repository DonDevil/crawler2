import importlib
import pkgutil

import antipiracy_contracts.events as events_package
import pytest
from antipiracy_contracts.events import (
    CATALOG,
    ContractError,
    EventCatalog,
    EventPayload,
    EventSpec,
)
from antipiracy_contracts.events.web import CrawlRequested
from antipiracy_contracts.ownership import Component, ServiceName

# The ownership table agreed in P1 (docs/phases/p01-contracts/events.md);
# page.changed added in contract 1.1 (P5, ADR-020).
EXPECTED_OWNERS = {
    "crawl.requested": Component.CRAWL_INTELLIGENCE,
    "fetch.completed": Component.CRAWLER_WORKER,
    "page.observed": Component.CRAWLER_WORKER,
    "urls.discovered": Component.EXTRACTION,
    "media.discovered": Component.EXTRACTION,
    "page.changed": Component.EXTRACTION,
    "media.observed": Component.MEDIA_REGISTRY,
    "encode.requested": Component.MEDIA_REGISTRY,
    "representation.ready": Component.ENCODER,
    "encode.failed": Component.ENCODER,
    "target.registered": Component.TARGET_MANAGER,
    "target.retired": Component.TARGET_MANAGER,
    "match.found": Component.MATCHER,
    "evidence.candidate_created": Component.EVIDENCE_COLLECTOR,
    "evidence.finalized": Component.EVIDENCE_FINALIZER,
}


def test_catalog_matches_agreed_ownership() -> None:
    assert {spec.event_type: spec.producer for spec in CATALOG} == EXPECTED_OWNERS


def test_every_payload_class_is_cataloged() -> None:
    for module in pkgutil.iter_modules(events_package.__path__, "antipiracy_contracts.events."):
        importlib.import_module(module.name)
    payload_classes = set(EventPayload.__subclasses__())
    assert payload_classes == {spec.payload for spec in CATALOG}


def test_cross_service_events_are_exactly_the_boundary() -> None:
    crossing = {
        spec.event_type
        for spec in CATALOG
        if any(consumer.service is not spec.producer.service for consumer in spec.consumers)
    }
    assert crossing == {
        "encode.requested",
        "representation.ready",
        "encode.failed",
        "target.registered",
        "target.retired",
        "match.found",
    }


def test_specs_document_semantics() -> None:
    for spec in CATALOG:
        assert spec.meaning
        assert spec.ordering
        assert spec.idempotency_key
        assert spec.schema_version == f"{spec.payload.SCHEMA_MAJOR}.{spec.minor}"


def _spec(**overrides: object) -> EventSpec:
    base: dict[str, object] = {
        "payload": CrawlRequested,
        "minor": 0,
        "producer": Component.CRAWL_INTELLIGENCE,
        "consumers": (Component.FRONTIER,),
        "meaning": "m",
        "idempotency_key": ("url.url_id",),
        "ordering": "none",
    }
    base.update(overrides)
    return EventSpec(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("specs", "message"),
    [
        ((_spec(), _spec()), "duplicate"),
        ((_spec(consumers=()),), "without consumers"),
        ((_spec(consumers=(Component.CRAWL_INTELLIGENCE,)),), "own consumer"),
        ((_spec(idempotency_key=("url.nope",)),), "does not exist"),
        ((_spec(idempotency_key=("reason.value",)),), "does not exist"),
    ],
)
def test_catalog_rejects_invalid_specs(specs: tuple[EventSpec, ...], message: str) -> None:
    with pytest.raises(ContractError, match=message):
        EventCatalog(specs)


def test_components_map_to_one_service() -> None:
    fingerprinter = {c for c in Component if c.service is ServiceName.FINGERPRINTER}
    assert fingerprinter == {Component.ENCODER, Component.MATCHER, Component.TARGET_MANAGER}
