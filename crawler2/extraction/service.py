"""The P5 consumer: one ``page.observed`` → extraction facts, revision, events (design §1, §14).

Pure extraction runs in ``extract``; this module only reads the snapshot,
decides what to (re)write and calls the P2 repositories in the order that
makes redelivery safe:

1. first extraction of a raw version: links (+ ``urls.discovered``),
   ``url_state``, then the extract row — the commit marker;
2. every eligible observation: the revision sighting in one logged batch
   with ``page.changed`` (only when the revision is new) and
   ``media.discovered``; then the archival decision.

A crash anywhere is repaired by redelivery: steps before the marker are
idempotent and are redone; after it, only step 2 runs again.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum

from antipiracy_contracts.events import EventEnvelope, new_event
from antipiracy_contracts.events.media import MediaDiscovered
from antipiracy_contracts.events.web import PageChanged, PageObserved, UrlsDiscovered
from antipiracy_contracts.models.web import PageObservation
from antipiracy_contracts.ownership import Component, Producer

from crawler2.core.observability import Metrics, get_logger
from crawler2.extraction.archival import DEFAULT_PROFILE, ArchivalProfile, decide
from crawler2.extraction.extract import extract
from crawler2.extraction.model import (
    BASELINE_RULES,
    EXTRACTOR_VERSION,
    Limits,
    NormalizationRules,
    PageExtract,
)
from crawler2.extraction.parse import is_html, media_type_of
from crawler2.extraction.urls import ALLOW_ALL, LinkPolicy
from crawler2.storage.objectstore.base import ObjectStore
from crawler2.storage.repositories import (
    ExtractRecord,
    LinkRepository,
    PageIntelligenceRepository,
    RetentionDecision,
    RevisionSighting,
    UrlRepository,
)

_log = get_logger("extraction.service")


class Result(StrEnum):
    EXTRACTED = "extracted"
    REUSED = "reused"
    SKIPPED_STATUS = "skipped_status"
    SKIPPED_TYPE = "skipped_type"
    NO_SNAPSHOT = "no_snapshot"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Outcome:
    result: Result
    new_revision: bool = False


class _Metrics:
    def __init__(self, metrics: Metrics | None) -> None:
        self.enabled = metrics is not None
        if metrics is None:
            return
        self.pages = metrics.counter("p5_pages_total", "Observations handled", ["result"])
        self.extract_seconds = metrics.histogram(
            "p5_extract_seconds", "CPU-bound parse + extraction time per page"
        )
        self.links = metrics.histogram(
            "p5_links_per_page", "Links per extracted page", buckets=(0, 10, 50, 100, 500, 1e4)
        )
        self.media = metrics.histogram(
            "p5_media_per_page", "Media references per extracted page", buckets=(0, 1, 5, 20, 1e3)
        )
        self.metadata = metrics.counter(
            "p5_metadata_fields_total", "Metadata fields present", ["field"]
        )
        self.dropped = metrics.counter("p5_dropped_refs_total", "Discarded references", ["reason"])
        self.truncated = metrics.counter("p5_truncated_total", "Limits hit", ["what"])
        self.revisions = metrics.counter("p5_revisions_total", "Revision sightings", ["kind"])
        self.archival = metrics.counter(
            "p5_archival_decisions_total", "Snapshot decisions", ["decision", "reason"]
        )

    def extracted(self, facts: PageExtract, seconds: float) -> None:
        if not self.enabled:
            return
        self.extract_seconds.observe(seconds)
        self.links.observe(len(facts.links))
        self.media.observe(len(facts.media))
        for field, value in facts.metadata:
            if value is not None:
                self.metadata.labels(field=field).inc()
        for reason, count in facts.stats.dropped.items():
            self.dropped.labels(reason=reason).inc(count)
        for what in facts.stats.truncated:
            self.truncated.labels(what=what).inc()


class PageIntelligenceService:
    def __init__(
        self,
        *,
        objects: ObjectStore,
        links: LinkRepository,
        urls: UrlRepository,
        pages: PageIntelligenceRepository,
        instance: str,
        profile: ArchivalProfile = DEFAULT_PROFILE,
        rules: NormalizationRules = BASELINE_RULES,
        policy: LinkPolicy = ALLOW_ALL,
        limits: Limits | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self._objects = objects
        self._links = links
        self._urls = urls
        self._pages = pages
        self._profile = profile
        self._rules = rules
        self._policy = policy
        self._limits = limits or Limits()
        self._metrics = _Metrics(metrics)
        self._producer = Producer(
            service=Component.EXTRACTION.service, component=Component.EXTRACTION, instance=instance
        )

    def handle(self, envelope: EventEnvelope[PageObserved]) -> None:
        """``IdempotentConsumer`` handler. Storage errors propagate (event is redelivered)."""
        self.process(envelope)

    def process(self, envelope: EventEnvelope[PageObserved]) -> Outcome:
        outcome = self._process(envelope)
        if self._metrics.enabled:
            self._metrics.pages.labels(result=outcome.result.value).inc()
        return outcome

    def _process(self, envelope: EventEnvelope[PageObserved]) -> Outcome:
        o = envelope.payload.observation
        if not 200 <= o.http_status < 300:
            return Outcome(Result.SKIPPED_STATUS)
        if o.snapshot is None:
            return Outcome(Result.NO_SNAPSHOT)
        media_type = media_type_of(o.content_type)
        if media_type is not None and not is_html(media_type, b""):
            return Outcome(Result.SKIPPED_TYPE)
        stored = self._pages.extract(o.page_version_id)
        if (
            stored is not None
            and stored.extractor == EXTRACTOR_VERSION
            and stored.normalization == self._rules.scheme
        ):
            facts = PageExtract.model_validate_json(stored.doc)
            result = Result.REUSED
        else:
            body = self._objects.get(o.snapshot)
            if media_type is None and not is_html(None, body):
                return Outcome(Result.SKIPPED_TYPE)
            try:
                started = time.process_time()
                facts = extract(
                    body,
                    page=o.final,
                    content_type=o.content_type,
                    rules=self._rules,
                    policy=self._policy,
                    limits=self._limits,
                )
                self._metrics.extracted(facts, time.process_time() - started)
            except Exception:  # noqa: BLE001 — a poison page must not block the stream
                _log.exception("extract_failed", observation_id=str(o.observation_id))
                return Outcome(Result.FAILED)
            self._record_version(envelope, facts)
            result = Result.EXTRACTED
        new_revision = self._record_sighting(envelope, facts)
        return Outcome(result, new_revision)

    def _record_version(self, envelope: EventEnvelope[PageObserved], facts: PageExtract) -> None:
        o = envelope.payload.observation
        if facts.links:
            discovered = UrlsDiscovered(
                page_observation_id=o.observation_id,
                page=o.final,
                page_version_id=o.page_version_id,
                links=facts.links,
            )
            self._links.record(
                discovered, observed_at=o.observed_at, event=self._event(discovered, envelope)
            )
            self._urls.record_discovered(
                [link.target for link in facts.links], seen_at=o.observed_at
            )
        self._pages.record_extract(
            ExtractRecord(
                page_version_id=o.page_version_id,
                url_id=o.final.url_id,
                extractor=facts.extractor,
                normalization=facts.normalization,
                revision_id=facts.revision_id,
                normalized_digest=facts.hashes.normalized,
                observed_at=o.observed_at,
                doc=facts.model_dump_json(),
            )
        )

    def _record_sighting(self, envelope: EventEnvelope[PageObserved], facts: PageExtract) -> bool:
        o = envelope.payload.observation
        known = self._pages.revision(o.final.url_id, facts.revision_id) is not None
        changed = None
        if not known:
            changed = self._event(
                PageChanged(
                    page_observation_id=o.observation_id,
                    page=o.final,
                    page_version_id=o.page_version_id,
                    revision_id=facts.revision_id,
                    normalization=facts.normalization,
                    hashes=facts.hashes,
                    observed_at=o.observed_at,
                ),
                envelope,
            )
        media = None
        if facts.media:
            media = self._event(
                MediaDiscovered(
                    page_observation_id=o.observation_id,
                    page=o.final,
                    page_version_id=o.page_version_id,
                    references=facts.media,
                ),
                envelope,
            )
        self._pages.record_sighting(
            RevisionSighting(
                url_id=o.final.url_id,
                revision_id=facts.revision_id,
                normalization=facts.normalization,
                normalized_digest=facts.hashes.normalized,
                observation_id=o.observation_id,
                page_version_id=o.page_version_id,
                observed_at=o.observed_at,
            ),
            changed=changed,
            media=media,
        )
        self._record_retention(o, new_revision=not known)
        if self._metrics.enabled:
            self._metrics.revisions.labels(kind="resighted" if known else "new").inc()
        return not known

    def _record_retention(self, o: PageObservation, *, new_revision: bool) -> None:
        assert o.snapshot is not None  # noqa: S101 — checked by _process
        decision, reason = decide(
            self._profile, observation_id=o.observation_id, new_revision=new_revision
        )
        self._pages.record_retention(
            RetentionDecision(
                digest=o.snapshot.digest,
                observation_id=o.observation_id,
                decision=decision,
                reason=reason,
                decided_at=o.observed_at,
            )
        )
        if self._metrics.enabled:
            self._metrics.archival.labels(decision=decision.value, reason=reason).inc()

    def _event[P: UrlsDiscovered | MediaDiscovered | PageChanged](
        self, payload: P, cause: EventEnvelope[PageObserved]
    ) -> EventEnvelope[P]:
        return new_event(
            payload,
            producer=self._producer,
            occurred_at=cause.payload.observation.observed_at,
            correlation_id=cause.correlation_id,
            causation_id=cause.event_id,
        )
