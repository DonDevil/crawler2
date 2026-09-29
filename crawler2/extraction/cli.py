"""``crawler2-extract``: the P5 extraction consumer of ``page.observed``.

Any number of processes on any hosts share one consumer group; Redis hands
each entry to one of them, a crashed consumer's pending entries are
reclaimed after ``extraction.claim_idle_ms``, and the processed-event
markers plus idempotent repositories make redelivery harmless (B.4 #5).
No intelligence service is needed (B.5 #1).
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from antipiracy_contracts.events import ContractError
from antipiracy_contracts.events.web import PageObserved
from pydantic import ValidationError

from crawler2.core.configuration import Settings, WorkerRole
from crawler2.core.identity import WorkerIdentity
from crawler2.core.observability import Metrics, configure_logging, get_logger
from crawler2.extraction.archival import ArchivalProfile
from crawler2.extraction.service import PageIntelligenceService
from crawler2.storage.errors import StorageError
from crawler2.storage.events.consumer import IdempotentConsumer, RedisStreamReader, StreamEntry
from crawler2.storage.events.publisher import stream_name

_log = get_logger("extraction.cli")


class ExtractionLoop:
    def __init__(
        self,
        reader: RedisStreamReader,
        consumer: IdempotentConsumer[PageObserved],
        settings: Settings,
    ) -> None:
        self._reader = reader
        self._consumer = consumer
        self._settings = settings.extraction
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    def run_once(self) -> int:
        """Handle one batch (stale entries first); returns the number acknowledged."""
        s = self._settings
        entries = self._reader.claim_stale(min_idle_ms=s.claim_idle_ms, count=s.batch_size)
        if not entries:
            entries = self._reader.read(count=s.batch_size, block_ms=s.block_ms)
        done: list[StreamEntry] = []
        for entry in entries:
            try:
                self._consumer.handle(entry.envelope)
            except (ContractError, ValidationError):
                _log.exception("invalid_event_skipped", entry_id=entry.entry_id)
            except StorageError:
                # Left pending: redelivered by claim_stale once idle (maybe to another host).
                _log.exception("storage_unavailable", entry_id=entry.entry_id)
                break
            done.append(entry)
        self._reader.ack(done)
        return len(done)

    def run(self, *, max_idle_polls: int | None = None) -> None:
        idle = 0
        while not self._stopping:
            try:
                handled = self.run_once()
            except StorageError:
                _log.exception("poll_failed")
                time.sleep(1.0)
                continue
            idle = 0 if handled else idle + 1
            if max_idle_polls is not None and idle >= max_idle_polls:
                return


def build(settings: Settings) -> ExtractionLoop:
    from crawler2.frontier.redis import connect_redis
    from crawler2.storage.objectstore.s3 import S3ObjectStore
    from crawler2.storage.scylla import ScyllaStorage

    identity = str(WorkerIdentity.create(settings.host_id, WorkerRole.EXTRACTION))
    storage = ScyllaStorage.open(settings.scylla, instance=identity)
    metrics = Metrics(settings)
    service = PageIntelligenceService(
        objects=S3ObjectStore(settings.minio, scratch_dir=settings.scratch_dir),
        links=storage.links,
        urls=storage.urls,
        pages=storage.page_intel,
        instance=identity,
        profile=ArchivalProfile(sample_rate=settings.extraction.archival_sample_rate),
        metrics=metrics,
    )
    reader = RedisStreamReader(
        connect_redis(settings.redis),
        stream_name(settings.events, PageObserved.EVENT_TYPE, PageObserved.SCHEMA_MAJOR),
        settings.extraction.consumer_group,
        identity,
    )
    reader.ensure_group()
    consumer = IdempotentConsumer(
        settings.extraction.consumer_group,
        PageObserved,
        service.handle,
        storage.processed_events,
        metrics=metrics,
    )
    _log.info("extraction_start", worker=identity, stream=reader.stream)
    return ExtractionLoop(reader, consumer, settings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crawler2-extract", description=__doc__)
    parser.add_argument(
        "--exit-when-idle", type=int, default=None, help="stop after N empty polls (tests)"
    )
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings)
    loop = build(settings)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: loop.stop())
    loop.run(max_idle_polls=args.exit_when_idle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
