"""Probe pool (level 2) and hash pool (level 3) workers (design §5).

- **Probe:** P4's ``HttpFetcher`` with ``Expectation.MEDIA_PROBE`` (at most
  ``media_probe_bytes`` = 64 KiB read, D2 unchanged), through the SSRF-guarded
  transport. One probe per ``MediaId`` when due, never per sighting.
- **Hash:** ``FullContentHasher`` (``hashing.py``), through the same guard.
- **Coordination (Redis, never facts):** a per-media lease per stage, per-host
  probe spacing (``probe_per_host_interval_s``) and per-host hash slots
  (``hash_per_host_concurrency``), all on Redis ``TIME``. Limits apply to the
  locator's host; redirect targets are not separately throttled.
- **Facts (Scylla):** every probe and every hash attempt is an authoritative
  row with provenance (worker, final URL, hops, status, time).

Probes bypass P3's per-domain politeness (media hosts are third parties); the
per-host interval and slots are their politeness.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

from antipiracy_contracts.ids import MediaId
from antipiracy_contracts.models.media import MediaKind, ProbeStatus
from antipiracy_contracts.models.web import UrlRef

from crawler2.core.configuration import MediaSettings
from crawler2.core.configuration.settings import HttpFetchSettings
from crawler2.core.observability import Metrics, get_logger
from crawler2.crawlers.http import HttpFetcher
from crawler2.crawlers.model import BodyKind, Expectation, FetchRequest, FetchResult, Outcome
from crawler2.media.hashing import FullContentHasher, HashLimits
from crawler2.media.model import HashOutcome, HashRecord, MediaLatest, ProbeRecord
from crawler2.media.schedule import hash_due, probe_due
from crawler2.media.ssrf import GuardedTransport, Resolver, default_resolver, parse_networks
from crawler2.media.work import Job, RedisWork
from crawler2.storage.errors import StorageError

_log = get_logger("media.probe")

UNSAFE = "UnsafeDestinationError"
_UNREACHABLE = frozenset(
    {
        Outcome.TIMEOUT,
        Outcome.DNS_ERROR,
        Outcome.NETWORK_ERROR,
        Outcome.TLS_ERROR,
        Outcome.REDIRECT_ERROR,
        Outcome.INVALID_RESPONSE,
        Outcome.PROXY_UNAVAILABLE,
        Outcome.FETCHER_UNAVAILABLE,
        Outcome.FETCHER_CRASH,
        Outcome.CANCELLED,
        Outcome.NOT_MODIFIED,
    }
)


class ProbeStore(Protocol):
    def latest(self, media_id: MediaId) -> MediaLatest | None: ...

    def record_probe(self, probe: ProbeRecord) -> None: ...

    def record_hash(self, record: HashRecord) -> None: ...


class EncodeRequester(Protocol):
    """Increment 3: called after every ``ok`` hash."""

    def request(self, record: HashRecord, probe: ProbeRecord, referer: str | None) -> Any: ...


# --- probe result → probe fact -------------------------------------------------------


def probe_status(result: FetchResult, kind: MediaKind) -> tuple[ProbeStatus, str | None]:
    """How a P4 media-probe attempt maps onto the P1 ``ProbeStatus`` (+ a detail code)."""
    if result.error is not None and result.error.kind == UNSAFE:
        return ProbeStatus.FORBIDDEN, "unsafe_destination"
    if result.outcome is Outcome.MEDIA and result.media is not None:
        return ProbeStatus.PROBED, None
    if result.status in (401, 403) or result.outcome in (Outcome.BLOCKED, Outcome.CAPTCHA):
        return ProbeStatus.FORBIDDEN, f"http_{result.status}" if result.status else None
    if result.outcome is Outcome.HTTP_ERROR or result.outcome in _UNREACHABLE:
        detail = f"http_{result.status}" if result.status else result.outcome.value
        return ProbeStatus.UNREACHABLE, detail
    if result.outcome is Outcome.OK and result.body_kind is BodyKind.MANIFEST:
        return ProbeStatus.PROBED, "manifest"  # registered for hints only (P8-D6)
    return ProbeStatus.NOT_MEDIA, result.outcome.value


def probe_record(job: Job, result: FetchResult, *, worker: str, at: datetime) -> ProbeRecord:
    kind = MediaKind(job.kind)
    status, detail = probe_status(result, kind)
    media = result.media
    validators = result.validators
    return ProbeRecord(
        probe_id=str(uuid.uuid4()),
        media_id=job.media_id,
        locator_url=job.locator_url,
        kind=kind,
        probed_at=at,
        probe_status=status,
        worker=worker,
        fetch_outcome=result.outcome.value,
        fetch_detail=detail,
        final_url=result.final_url,
        redirect_count=len(result.redirects),
        http_status=result.status,
        content_type=(media.content_type if media else result.content_type),
        declared_length=(media.declared_length if media else result.content_length),
        total_length=media.total_length if media else None,
        accept_ranges=media.accept_ranges if media else None,
        range_honoured=media.range_honoured if media else False,
        etag=(media.etag if media else (validators.etag if validators else None)),
        last_modified=(
            media.last_modified if media else (validators.last_modified if validators else None)
        ),
        bytes_read=result.bytes_read,
        container=media.container if media else None,
        referer=job.referer,
    )


# --- metrics -------------------------------------------------------------------------


class ProbeMetrics:
    def __init__(self, metrics: Metrics | None) -> None:
        self.enabled = metrics is not None
        if metrics is None:
            return
        self.probes = metrics.counter("media_probes_total", "Probe facts by status", ["status"])
        self.hashes = metrics.counter(
            "media_hashes_total", "Full-content hash attempts by outcome", ["outcome"]
        )
        self.hash_bytes = metrics.counter(
            "media_hash_bytes_total", "Bytes streamed through the hash reader (discarded)"
        )
        self.ssrf = metrics.counter(
            "media_ssrf_rejections_total", "Requests refused by the SSRF guard", ["path"]
        )
        self.skipped = metrics.counter(
            "media_work_skipped_total", "Jobs not worked, by stage and reason", ["stage", "reason"]
        )
        self.seconds = metrics.histogram(
            "media_hash_seconds",
            "Duration of one full-content hash read",
            buckets=(0.1, 1, 5, 15, 60, 300, 900, 1800, 3600),
        )

    def rejected(self, path: str) -> Callable[[str], None]:
        def count(_host: str) -> None:
            if self.enabled:
                self.ssrf.labels(path=path).inc()

        return count

    def skip(self, stage: str, reason: str) -> None:
        if self.enabled:
            self.skipped.labels(stage=stage, reason=reason).inc()


def guarded(
    settings: MediaSettings,
    *,
    resolver: Resolver = default_resolver,
    inner: Any = None,
    on_reject: Callable[[str], None] | None = None,
) -> GuardedTransport:
    return GuardedTransport(
        inner,
        resolver=resolver,
        allowed=parse_networks(settings.ssrf_allowed_networks),
        on_reject=on_reject,
    )


def probe_fetcher(
    fetch: HttpFetchSettings, transport: GuardedTransport, *, connections: int
) -> HttpFetcher:
    """P4's fetcher, unchanged, with the guard as its transport and no proxy (a proxy
    would bypass the transport)."""
    return HttpFetcher(
        fetch.model_copy(update={"proxy": None}), transport=transport, max_connections=connections
    )


def hash_limits(settings: MediaSettings, fetch: HttpFetchSettings) -> HashLimits:
    return HashLimits(
        max_bytes=settings.hash_max_bytes,
        timeout_s=settings.hash_timeout_s,
        connect_timeout_s=fetch.connect_timeout_s,
        read_timeout_s=settings.hash_read_timeout_s,
        max_redirects=fetch.max_redirects,
        user_agent=fetch.user_agent,
    )


# --- workers --------------------------------------------------------------------------


class ProbeWorker:
    def __init__(
        self,
        fetcher: HttpFetcher,
        store: ProbeStore,
        work: RedisWork,
        *,
        worker: str,
        settings: MediaSettings,
        metrics: ProbeMetrics | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._store = store
        self._work = work
        self._worker = worker
        self._s = settings
        self._m = metrics or ProbeMetrics(None)

    async def handle(self, job: Job) -> ProbeRecord | None:
        work = self._work
        if not await asyncio.to_thread(
            work.lease, "probe", job.media_id, self._worker, ttl_s=self._s.probe_lease_s
        ):
            self._m.skip("probe", "leased")
            return None
        try:
            now = await asyncio.to_thread(work.now)
            latest = await asyncio.to_thread(self._store.latest, MediaId(job.media_id))
            if not probe_due(latest, now, timedelta(seconds=self._s.reprobe_after_s)):
                self._m.skip("probe", "fresh")
                return None
            host = urlsplit(job.locator_url).hostname or ""
            while (
                wait := await asyncio.to_thread(
                    work.throttle, host, interval_s=self._s.probe_per_host_interval_s
                )
            ) > 0:
                await asyncio.sleep(wait)
            result = await self._fetcher.fetch(
                FetchRequest(
                    url=job.locator_url,
                    url_id=UrlRef.of(job.locator_url).url_id,
                    expect=Expectation.MEDIA_PROBE,
                )
            )
            record = probe_record(
                job, result, worker=self._worker, at=await asyncio.to_thread(work.now)
            )
            await asyncio.to_thread(self._store.record_probe, record)
            if self._m.enabled:
                self._m.probes.labels(status=record.probe_status.value).inc()
            _log.info(
                "media_probed",
                media_id=job.media_id,
                status=record.probe_status.value,
                detail=record.fetch_detail,
                http_status=record.http_status,
                hops=record.redirect_count,
            )
            if hash_due(
                record, latest, record.probed_at, timedelta(seconds=self._s.rehash_after_s)
            ):
                await asyncio.to_thread(work.want, "hash", replace(job, probe_id=record.probe_id))
            return record
        finally:
            await asyncio.to_thread(work.release, "probe", job.media_id, self._worker)


class HashWorker:
    def __init__(
        self,
        hasher: FullContentHasher,
        store: ProbeStore,
        work: RedisWork,
        *,
        worker: str,
        settings: MediaSettings,
        requester: EncodeRequester | None = None,
        metrics: ProbeMetrics | None = None,
        requeue_delay_s: float = 1.0,
    ) -> None:
        self._hasher = hasher
        self._store = store
        self._work = work
        self._worker = worker
        self._s = settings
        self._requester = requester
        self._m = metrics or ProbeMetrics(None)
        self._requeue_delay_s = requeue_delay_s

    async def handle(self, job: Job) -> HashRecord | None:
        work = self._work
        ttl = self._s.hash_timeout_s + 120
        if not await asyncio.to_thread(work.lease, "hash", job.media_id, self._worker, ttl_s=ttl):
            self._m.skip("hash", "leased")
            return None
        slot: str | None = None
        try:
            now = await asyncio.to_thread(work.now)
            latest = await asyncio.to_thread(self._store.latest, MediaId(job.media_id))
            probe = latest.probe if latest is not None else None
            if probe is None or not hash_due(
                probe, latest, now, timedelta(seconds=self._s.rehash_after_s)
            ):
                self._m.skip("hash", "not_due")
                return None
            host = urlsplit(probe.locator_url).hostname or ""
            slot = await asyncio.to_thread(
                work.host_slot,
                host,
                self._worker,
                slots=self._s.hash_per_host_concurrency,
                ttl_s=ttl,
            )
            if slot is None:  # the host is busy: put the job back for later
                self._m.skip("hash", "host_busy")
                await asyncio.sleep(self._requeue_delay_s)
                await asyncio.to_thread(work.want, "hash", job)
                return None
            record = await self._hasher.hash(
                probe, worker=self._worker, now=now, referer=job.referer
            )
            record = replace(record, hashed_at=await asyncio.to_thread(work.now))
            await asyncio.to_thread(self._store.record_hash, record)
            if self._m.enabled:
                self._m.hashes.labels(outcome=record.outcome.value).inc()
                self._m.hash_bytes.inc(record.byte_count)
                self._m.seconds.observe(record.elapsed_s)
            _log.info(
                "media_hashed",
                media_id=job.media_id,
                outcome=record.outcome.value,
                detail=record.detail,
                bytes=record.byte_count,
                content_id=str(record.content_id) if record.content_id else None,
                elapsed_s=record.elapsed_s,
            )
            if record.outcome is HashOutcome.OK and self._requester is not None:
                await asyncio.to_thread(self._requester.request, record, probe, job.referer)
            return record
        finally:
            if slot is not None:
                await asyncio.to_thread(work.release_slot, slot, self._worker)
            await asyncio.to_thread(work.release, "hash", job.media_id, self._worker)


async def run_pool(
    stage: str,
    work: RedisWork,
    handle: Callable[[Job], Awaitable[Any]],
    *,
    concurrency: int,
    stop: Callable[[], bool],
    block_s: float = 2.0,
    retry_max_s: float = 30.0,
) -> None:
    """``concurrency`` loops pop ``stage`` jobs until ``stop()``. A failed job is
    logged and wanted again; it never stops the pool."""

    async def loop(slot: int) -> None:
        pause = 0.5
        while not stop():
            try:
                job = await asyncio.to_thread(work.pop, stage, timeout_s=block_s)
            except StorageError as exc:
                _log.warning("media_queue_unavailable", stage=stage, error=str(exc)[:200])
                await asyncio.sleep(pause)
                pause = min(pause * 2, retry_max_s)
                continue
            pause = 0.5
            if job is None:
                continue
            try:
                await handle(job)
            except StorageError as exc:
                _log.warning(
                    "media_job_retry", stage=stage, media_id=job.media_id, error=str(exc)[:200]
                )
                await asyncio.sleep(pause)
                # On failure the next sighting or re-probe wants it again.
                with contextlib.suppress(StorageError):
                    await asyncio.to_thread(work.want, stage, job)
            except Exception:  # noqa: BLE001 -- one bad job must not stop the pool
                _log.exception("media_job_failed", stage=stage, media_id=job.media_id)

    await asyncio.gather(*(loop(i) for i in range(concurrency)))
