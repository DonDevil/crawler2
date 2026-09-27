"""Services, their components, and the producer identity every event carries."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar

from pydantic import Field, model_validator

from antipiracy_contracts.base import ContractKind, ContractModel


class ServiceName(StrEnum):
    """Deployable services. Each owns its own keyspace in the shared Scylla cluster."""

    CRAWLER2 = "crawler2"
    FINGERPRINTER = "fingerprinter"


class Component(StrEnum):
    """Logical producers/consumers of events. Each belongs to exactly one service."""

    # crawler2
    CRAWL_INTELLIGENCE = "crawl_intelligence"
    FRONTIER = "frontier"
    CRAWLER_WORKER = "crawler_worker"
    EXTRACTION = "extraction"
    MEDIA_REGISTRY = "media_registry"
    FEEDBACK = "feedback"
    EVIDENCE_COLLECTOR = "evidence_collector"
    EVIDENCE_FINALIZER = "evidence_finalizer"
    EVIDENCE_EXPORTER = "evidence_exporter"
    # fingerprinter
    TARGET_MANAGER = "target_manager"
    ENCODER = "encoder"
    MATCHER = "matcher"

    @property
    def service(self) -> ServiceName:
        return _FINGERPRINTER_COMPONENTS.get(self, ServiceName.CRAWLER2)


_FINGERPRINTER_COMPONENTS = {
    Component.TARGET_MANAGER: ServiceName.FINGERPRINTER,
    Component.ENCODER: ServiceName.FINGERPRINTER,
    Component.MATCHER: ServiceName.FINGERPRINTER,
}

InstanceName = Annotated[str, Field(pattern=r"^[A-Za-z0-9._:-]{1,200}$")]
"""Opaque process identity, e.g. crawler2's ``{host_id}:{role}:{pid}:{uuid}``."""


class Producer(ContractModel):
    """Who emitted an event: service, component and the concrete process instance."""

    KIND: ClassVar[ContractKind] = ContractKind.VALUE

    service: ServiceName
    component: Component
    instance: InstanceName

    @model_validator(mode="after")
    def _component_belongs_to_service(self) -> Producer:
        if self.component.service is not self.service:
            raise ValueError(
                f"component {self.component.value!r} belongs to service "
                f"{self.component.service.value!r}, not {self.service.value!r}"
            )
        return self
