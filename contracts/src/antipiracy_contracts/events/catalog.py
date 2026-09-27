"""The event catalog: every event type, its owner, its consumers and its guarantees.

This is the single source for event ownership. Each (event type, major)
has exactly one producing component; consumers subscribe but never
co-own the schema. ``new_event``/``decode_event`` enforce ownership, so a
component publishing an event it does not own fails in tests and at runtime.

Delivery guarantees shared by every event (ADR-004, ADR-009):
at-least-once, duplicates allowed, no ordering across events. Every
consumer deduplicates on the event's idempotency key.
"""

from __future__ import annotations

import types
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Union, get_args, get_origin

from pydantic import BaseModel

from antipiracy_contracts.events.envelope import EventPayload
from antipiracy_contracts.events.evidence import EvidenceCandidateCreated, EvidenceFinalized
from antipiracy_contracts.events.fingerprinting import (
    EncodeFailed,
    EncodeRequested,
    MatchFound,
    RepresentationReady,
)
from antipiracy_contracts.events.media import MediaDiscovered, MediaObserved
from antipiracy_contracts.events.targets import TargetRegistered, TargetRetired
from antipiracy_contracts.events.web import (
    CrawlRequested,
    FetchCompleted,
    PageObserved,
    UrlsDiscovered,
)
from antipiracy_contracts.ownership import Component


class ContractError(ValueError):
    """Base class for contract violations detected while encoding/decoding events."""


class UnknownEventTypeError(ContractError):
    """No catalog entry for this event type. Consumers skip it (and log)."""


class UnsupportedSchemaVersionError(ContractError):
    """The event type is known but not at this major version."""


class OwnershipViolationError(ContractError):
    """A component emitted an event type it does not own."""


class UnexpectedEventTypeError(ContractError):
    """A valid event arrived where a different event type was required."""


@dataclass(frozen=True, slots=True)
class EventSpec:
    payload: type[EventPayload]
    minor: int
    """Current minor version produced. Bumped for each additive change."""
    producer: Component
    consumers: tuple[Component, ...]
    meaning: str
    idempotency_key: tuple[str, ...]
    """Dotted payload paths whose values identify a logical duplicate."""
    ordering: str
    """What a consumer may and may not assume about order."""

    @property
    def event_type(self) -> str:
        return self.payload.EVENT_TYPE

    @property
    def major(self) -> int:
        return self.payload.SCHEMA_MAJOR

    @property
    def schema_version(self) -> str:
        return f"{self.major}.{self.minor}"


class EventCatalog:
    def __init__(self, specs: tuple[EventSpec, ...]) -> None:
        by_key: dict[tuple[str, int], EventSpec] = {}
        by_payload: dict[type[EventPayload], EventSpec] = {}
        for spec in specs:
            key = (spec.event_type, spec.major)
            if key in by_key:
                raise ContractError(f"duplicate catalog entry for {key}")
            if spec.producer in spec.consumers:
                raise ContractError(f"{spec.event_type}: producer listed as its own consumer")
            if not spec.consumers:
                raise ContractError(f"{spec.event_type}: an event without consumers is not needed")
            for path in spec.idempotency_key:
                _check_path(spec.payload, path)
            by_key[key] = spec
            by_payload[spec.payload] = spec
        self._specs = specs
        self._by_key = by_key
        self._by_payload = by_payload

    def __iter__(self) -> Iterator[EventSpec]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def resolve(self, event_type: str, major: int) -> EventSpec:
        spec = self._by_key.get((event_type, major))
        if spec is not None:
            return spec
        if any(known_type == event_type for known_type, _ in self._by_key):
            raise UnsupportedSchemaVersionError(f"{event_type} has no major version {major}")
        raise UnknownEventTypeError(f"unknown event type {event_type!r}")

    def spec_of(self, payload_type: type[EventPayload]) -> EventSpec:
        try:
            return self._by_payload[payload_type]
        except KeyError:
            raise UnknownEventTypeError(f"{payload_type.__name__} is not in the catalog") from None


def _check_path(model: type[BaseModel], path: str) -> None:
    current: type[BaseModel] | None = model
    for segment in path.split("."):
        if current is None or segment not in current.model_fields:
            raise ContractError(f"{model.__name__}: idempotency path {path!r} does not exist")
        current = _model_type(current.model_fields[segment].annotation)


def _model_type(annotation: object) -> type[BaseModel] | None:
    """The model type behind ``M`` or ``M | None``; None for leaf values."""
    if get_origin(annotation) in (Union, types.UnionType):
        members = [arg for arg in get_args(annotation) if arg is not type(None)]
        annotation = members[0] if len(members) == 1 else None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


_ANY_ORDER = "none: consumers must tolerate any order and redelivery"

CATALOG = EventCatalog(
    (
        EventSpec(
            payload=CrawlRequested,
            minor=0,
            producer=Component.CRAWL_INTELLIGENCE,
            consumers=(Component.FRONTIER,),
            meaning="A URL should be crawled (seed, recrawl, discovery or feedback decision).",
            idempotency_key=("url.url_id", "reason", "not_before"),
            ordering="none: the frontier merges requests per url_id (highest priority wins)",
        ),
        EventSpec(
            payload=FetchCompleted,
            minor=0,
            producer=Component.CRAWLER_WORKER,
            consumers=(Component.CRAWL_INTELLIGENCE,),
            meaning="A fetch attempt ended; input to fetch-profile and source learning.",
            idempotency_key=("attempt.fetch_attempt_id",),
            ordering=_ANY_ORDER,
        ),
        EventSpec(
            payload=PageObserved,
            minor=0,
            producer=Component.CRAWLER_WORKER,
            consumers=(Component.EXTRACTION, Component.CRAWL_INTELLIGENCE),
            meaning="A page observation was durably recorded (raw snapshot referenced).",
            idempotency_key=("observation.observation_id",),
            ordering="none: compare observed_at, never arrival order, to find the latest",
        ),
        EventSpec(
            payload=UrlsDiscovered,
            minor=0,
            producer=Component.EXTRACTION,
            consumers=(Component.CRAWL_INTELLIGENCE, Component.FRONTIER),
            meaning="Outgoing links of one page observation (frontier applies default "
            "admission so crawling works with intelligence stopped, plan B.5 #1).",
            idempotency_key=("page_observation_id",),
            ordering=_ANY_ORDER,
        ),
        EventSpec(
            payload=MediaDiscovered,
            minor=0,
            producer=Component.EXTRACTION,
            consumers=(Component.MEDIA_REGISTRY,),
            meaning="Media references found on one page observation.",
            idempotency_key=("page_observation_id",),
            ordering=_ANY_ORDER,
        ),
        EventSpec(
            payload=MediaObserved,
            minor=0,
            producer=Component.MEDIA_REGISTRY,
            consumers=(Component.CRAWL_INTELLIGENCE, Component.EVIDENCE_COLLECTOR),
            meaning="A media reference was resolved to a media entity (and possibly probed).",
            idempotency_key=("observation.page_observation_id", "observation.media.media_id"),
            ordering=_ANY_ORDER,
        ),
        EventSpec(
            payload=EncodeRequested,
            minor=0,
            producer=Component.MEDIA_REGISTRY,
            consumers=(Component.ENCODER,),
            meaning="A content identity lacks a required representation.",
            idempotency_key=("content.content_id", "required_spec"),
            ordering="none: priority orders work inside the encoder's queue, not delivery",
        ),
        EventSpec(
            payload=RepresentationReady,
            minor=0,
            producer=Component.ENCODER,
            consumers=(Component.MEDIA_REGISTRY, Component.MATCHER),
            meaning="A representation of a content identity is stored and usable.",
            idempotency_key=("representation.representation_id",),
            ordering=_ANY_ORDER,
        ),
        EventSpec(
            payload=EncodeFailed,
            minor=0,
            producer=Component.ENCODER,
            consumers=(Component.MEDIA_REGISTRY,),
            meaning="An encode request failed; retryable says whether to try again.",
            idempotency_key=("request_id", "attempt"),
            ordering="none: a later representation.ready supersedes any failure",
        ),
        EventSpec(
            payload=TargetRegistered,
            minor=0,
            producer=Component.TARGET_MANAGER,
            consumers=(Component.CRAWL_INTELLIGENCE, Component.MATCHER),
            meaning="A target (version) is active; triggers discovery and backfill matching.",
            idempotency_key=("target.ref.target_id", "target.ref.version"),
            ordering="none: consumers keep the highest version per target_id",
        ),
        EventSpec(
            payload=TargetRetired,
            minor=0,
            producer=Component.TARGET_MANAGER,
            consumers=(Component.CRAWL_INTELLIGENCE, Component.MATCHER),
            meaning="A target is no longer searched for.",
            idempotency_key=("target_id",),
            ordering="none: retirement is terminal and wins over any registration",
        ),
        EventSpec(
            payload=MatchFound,
            minor=0,
            producer=Component.MATCHER,
            consumers=(Component.FEEDBACK, Component.EVIDENCE_COLLECTOR),
            meaning="Content verified against a target version, with confidence/technique.",
            idempotency_key=("match.match_id",),
            ordering=_ANY_ORDER,
        ),
        EventSpec(
            payload=EvidenceCandidateCreated,
            minor=0,
            producer=Component.EVIDENCE_COLLECTOR,
            consumers=(Component.EVIDENCE_FINALIZER,),
            meaning="Provenance for a match has been collected and awaits finalization.",
            idempotency_key=("candidate.evidence_id",),
            ordering=_ANY_ORDER,
        ),
        EventSpec(
            payload=EvidenceFinalized,
            minor=0,
            producer=Component.EVIDENCE_FINALIZER,
            consumers=(Component.EVIDENCE_EXPORTER,),
            meaning="An evidence item is sealed (write-once manifest pinned by digest).",
            idempotency_key=("seal.evidence_id",),
            ordering=_ANY_ORDER,
        ),
    )
)
