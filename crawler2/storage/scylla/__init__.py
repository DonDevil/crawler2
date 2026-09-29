"""Scylla implementations of the repository interfaces (crawler2 keyspace only)."""

from __future__ import annotations

from dataclasses import dataclass

from crawler2.core.configuration import ScyllaSettings
from crawler2.storage.scylla.evidence import ScyllaEvidenceRepository
from crawler2.storage.scylla.filtering import (
    ScyllaDiscoveryRepository,
    ScyllaFilterRuleRepository,
)
from crawler2.storage.scylla.media import ScyllaMediaRepository
from crawler2.storage.scylla.migrations import Migrator
from crawler2.storage.scylla.outbox import ScyllaOutbox, ScyllaProcessedEventStore
from crawler2.storage.scylla.pages import ScyllaPageIntelligenceRepository
from crawler2.storage.scylla.projections import ScyllaProjectionRepository
from crawler2.storage.scylla.session import ScyllaSession
from crawler2.storage.scylla.web import (
    ScyllaFetchAttemptRepository,
    ScyllaLinkRepository,
    ScyllaPageObservationRepository,
    ScyllaUrlRepository,
)


@dataclass(frozen=True, slots=True)
class ScyllaStorage:
    """Every crawler2 repository over one session. Refuses an outdated schema."""

    session: ScyllaSession
    outbox: ScyllaOutbox
    processed_events: ScyllaProcessedEventStore
    fetch_attempts: ScyllaFetchAttemptRepository
    pages: ScyllaPageObservationRepository
    links: ScyllaLinkRepository
    urls: ScyllaUrlRepository
    media: ScyllaMediaRepository
    projections: ScyllaProjectionRepository
    evidence: ScyllaEvidenceRepository
    page_intel: ScyllaPageIntelligenceRepository
    filter_rules: ScyllaFilterRuleRepository
    discovery: ScyllaDiscoveryRepository

    @classmethod
    def open(
        cls, settings: ScyllaSettings, *, keyspace: str | None = None, instance: str
    ) -> ScyllaStorage:
        session = ScyllaSession.connect(settings, keyspace=keyspace)
        try:
            Migrator(session, settings, applied_by=instance).require_current()
        except Exception:
            session.close()
            raise
        return cls.over(session)

    @classmethod
    def over(cls, session: ScyllaSession) -> ScyllaStorage:
        outbox = ScyllaOutbox(session)
        pages = ScyllaPageObservationRepository(session, outbox)
        return cls(
            session=session,
            outbox=outbox,
            processed_events=ScyllaProcessedEventStore(session),
            fetch_attempts=ScyllaFetchAttemptRepository(session, outbox),
            pages=pages,
            links=ScyllaLinkRepository(session, outbox),
            urls=ScyllaUrlRepository(session),
            media=ScyllaMediaRepository(session, outbox),
            projections=ScyllaProjectionRepository(session),
            evidence=ScyllaEvidenceRepository(session, outbox, pages),
            page_intel=ScyllaPageIntelligenceRepository(session, outbox),
            filter_rules=ScyllaFilterRuleRepository(session),
            discovery=ScyllaDiscoveryRepository(session),
        )

    def close(self) -> None:
        self.session.close()


__all__ = ["Migrator", "ScyllaSession", "ScyllaStorage"]
