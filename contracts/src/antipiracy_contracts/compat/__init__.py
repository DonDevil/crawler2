"""Golden compatibility fixtures shipped with the contract package (ADR-009).

Both crawler2 and the fingerprinter run these against the contract version
they pin: every fixture must decode, and forward fixtures (a newer minor
version with unknown fields) must decode to the same payload as their base.

Released fixtures are immutable. ``MANIFEST.json`` pins each file's
SHA-256; changing a released fixture means changing released semantics,
which the evolution policy forbids. New behaviour gets new fixtures.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from antipiracy_contracts.events.catalog import CATALOG, EventSpec
from antipiracy_contracts.events.envelope import EnvelopeHeader
from antipiracy_contracts.ownership import ServiceName

FIXTURE_DIR = Path(__file__).parent / "fixtures"
MANIFEST = FIXTURE_DIR / "MANIFEST.json"


@dataclass(frozen=True, slots=True)
class EventFixture:
    name: str
    """File stem, e.g. ``encode.requested.v1`` or ``encode.requested.v1.forward``."""
    spec: EventSpec
    raw: bytes
    base_name: str | None
    """For a forward fixture, the fixture it must decode identically to."""

    @property
    def producer_service(self) -> ServiceName:
        return self.spec.producer.service

    @property
    def consumer_services(self) -> frozenset[ServiceName]:
        return frozenset(consumer.service for consumer in self.spec.consumers)


def event_fixtures() -> list[EventFixture]:
    fixtures = []
    for path in sorted((FIXTURE_DIR / "events").glob("*.json")):
        raw = path.read_bytes()
        header = EnvelopeHeader.model_validate_json(raw)
        spec = CATALOG.resolve(header.event_type, header.schema_major)
        name = path.stem
        base = name.removesuffix(".forward") if name.endswith(".forward") else None
        fixtures.append(EventFixture(name, spec, raw, base))
    return fixtures


def model_fixture(name: str) -> bytes:
    return (FIXTURE_DIR / "models" / f"{name}.json").read_bytes()


def fixture_digests() -> dict[str, str]:
    """Current SHA-256 of every fixture file, keyed by path relative to the fixture dir."""
    return {
        path.relative_to(FIXTURE_DIR).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(FIXTURE_DIR.rglob("*.json"))
        if path != MANIFEST
    }


def released_digests() -> dict[str, str]:
    digests: dict[str, str] = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return digests
