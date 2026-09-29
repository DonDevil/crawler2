"""The one worker runtime (P4 design §10, D3): claim → one attempt → record → report.

Every pool (http, browser, tor) is this loop with a different ``Fetcher``.
The runtime owns the claim lifecycle, heartbeat, the hard attempt
deadline, health gating, recording and the frontier report; fetchers own
only the attempt. The frontier stays the single retry authority (D4): the
runtime never repeats an attempt, it reports ``complete``/``fail``/``defer``
and the frontier decides.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

import psutil
from antipiracy_contracts.models.web import FetchCapability, UrlRef

from crawler2.core.configuration import ExecutionQueue
from crawler2.core.configuration.settings import PoolSettings
from crawler2.core.observability import Metrics, get_logger
from crawler2.crawlers.classify import error_info
from crawler2.crawlers.decide import Decision, Operation, decide
from crawler2.crawlers.health import NetworkHealth
from crawler2.crawlers.model import (
    Fetcher,
    FetcherState,
    FetcherUnavailableError,
    FetchRequest,
    FetchResult,
    Outcome,
)
from crawler2.crawlers.recorder import Recorder
from crawler2.frontier import (
    Claim,
    ClaimLostError,
    Frontier,
    FrontierUnavailableError,
    run_with_heartbeat,
)
from crawler2.storage.errors import StorageError

_log = get_logger("crawlers.runtime")
_RSS_CHECK_EVERY = 25


class WorkerState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    OFFLINE = "offline"
    OVERLOADED = "overloaded"
    DRAINING = "draining"
    STOPPED = "stopped"


class FetchMetrics:
    """Bounded label sets only: pool, outcome, operation — never URLs or hosts."""

    def __init__(self, metrics: Metrics) -> None:
        self.attempts = metrics.counter(
            "fetch_attempts_total", "Fetch attempts by pool and outcome", ["pool", "outcome"]
        )
        self.operations = metrics.counter(
            "fetch_frontier_ops_total", "Frontier reports by pool and operation", ["pool", "op"]
        )
        self.escalations = metrics.counter(
            "fetch_escalations_total", "Capability changes requested", ["pool", "to"]
        )
        self.bytes = metrics.counter(
            "fetch_bytes_total", "Network bytes read by attempts", ["pool"]
        )
        self.media_probe_bytes = metrics.counter(
            "fetch_media_probe_bytes_total", "Body bytes read from media responses", ["pool"]
        )
        self.redirects = metrics.counter("fetch_redirects_total", "Redirect hops", ["pool"])
        self.duration = metrics.histogram(
            "fetch_duration_seconds", "Attempt wall time", ["pool", "outcome"]
        )
        self.lost_claims = metrics.counter("fetch_lost_claims_total", "Claims lost", ["pool"])
        self.record_errors = metrics.counter(
            "fetch_record_errors_total", "Attempts deferred because storage failed", ["pool"]
        )
        self.active = metrics.gauge("worker_active_attempts", "Attempts in flight", ["pool"])
        self.state = metrics.gauge(
            "worker_state", "1 for the current worker state", ["pool", "state"]
        )


@dataclass
class RuntimeStats:
    """In-process counters for tests and the health line (not authoritative state)."""

    claims: int = 0
    attempts: dict[str, int] = field(default_factory=dict)
    operations: dict[str, int] = field(default_factory=dict)
    reports: dict[str, int] = field(default_factory=dict)
    """``<op>:<frontier result>``, e.g. ``fail:retry_scheduled``."""
    lost: int = 0
    report_failures: int = 0


class WorkerRuntime:
    def __init__(
        self,
        *,
        frontier: Frontier,
        queue: ExecutionQueue,
        fetcher: Fetcher,
        recorder: Recorder,
        settings: PoolSettings,
        health: NetworkHealth,
        metrics: Metrics,
        max_memory_mb: int,
        recover_every_s: float = 30.0,
    ) -> None:
        self._frontier = frontier
        self._queue = queue
        self._fetcher = fetcher
        self._recorder = recorder
        self._s = settings
        self._health = health
        self._metrics = FetchMetrics(metrics)
        self._max_memory_mb = max_memory_mb
        self._recover_every_s = recover_every_s
        self._pool = queue.value
        self._slots = asyncio.Semaphore(settings.concurrency)
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = asyncio.Event()
        self._state = WorkerState.STARTING
        self._overloaded = False
        self._done = 0
        self.stats = RuntimeStats()

    # --- state -----------------------------------------------------------
    @property
    def state(self) -> WorkerState:
        return self._state

    def _set_state(self, state: WorkerState) -> None:
        if state is not self._state:
            _log.info(
                "worker_state", pool=self._pool, previous=self._state.value, state=state.value
            )
            self._metrics.state.labels(self._pool, self._state.value).set(0)
            self._state = state
        self._metrics.state.labels(self._pool, state.value).set(1)

    def _refresh_state(self) -> WorkerState:
        if self._stopping.is_set():
            state = WorkerState.DRAINING
        elif self._fetcher.health().state is FetcherState.UNAVAILABLE:
            state = WorkerState.UNAVAILABLE
        elif self._health.offline:
            state = WorkerState.OFFLINE
        elif self._overloaded:
            state = WorkerState.OVERLOADED
        elif self._fetcher.health().state is FetcherState.DEGRADED or self._health.state.value == (
            "suspect"
        ):
            state = WorkerState.DEGRADED
        else:
            state = WorkerState.READY
        self._set_state(state)
        return state

    def can_claim(self) -> bool:
        return self._refresh_state() in (WorkerState.READY, WorkerState.DEGRADED)

    # --- lifecycle ---------------------------------------------------------
    def stop(self) -> None:
        self._stopping.set()

    async def run(self, *, max_claims: int | None = None) -> None:
        """Run until ``stop()``; ``max_claims`` bounds a run (tests, benchmarks)."""
        await self._start_fetcher()
        recover = asyncio.ensure_future(self._recover_loop())
        idle = self._s.idle_poll_min_s
        try:
            while not self._stopping.is_set():
                if max_claims is not None and self.stats.claims >= max_claims:
                    break
                if not self.can_claim():
                    if self._state is WorkerState.UNAVAILABLE:
                        await self._restart_fetcher()
                    await self._pause(self._s.idle_poll_max_s)
                    continue
                await self._slots.acquire()
                try:
                    claim = await asyncio.to_thread(self._frontier.claim, self._queue)
                except FrontierUnavailableError as exc:
                    self._slots.release()
                    _log.warning("claim_unavailable", pool=self._pool, error=str(exc)[:200])
                    await self._pause(self._s.idle_poll_max_s)
                    continue
                if claim is None:
                    self._slots.release()
                    await self._pause(idle)
                    idle = min(idle * 2, self._s.idle_poll_max_s)
                    continue
                idle = self._s.idle_poll_min_s
                self.stats.claims += 1
                task = asyncio.ensure_future(self._attempt(claim))
                self._tasks.add(task)
                task.add_done_callback(self._finished)
        finally:
            recover.cancel()
            await self._drain()
            with contextlib.suppress(asyncio.CancelledError):
                await recover
            await self._fetcher.close()
            await self._health.close()
            self._set_state(WorkerState.STOPPED)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        self._slots.release()
        if not task.cancelled() and task.exception() is not None:
            _log.error("attempt_crashed", pool=self._pool, error=repr(task.exception())[:300])

    async def _pause(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), seconds)

    async def _drain(self) -> None:
        self._set_state(WorkerState.DRAINING)
        if not self._tasks:
            return
        _, pending = await asyncio.wait(set(self._tasks), timeout=self._s.shutdown_grace_s)
        for task in pending:
            task.cancel()  # each cancelled attempt defers its claim (see _attempt)
        if pending:
            await asyncio.wait(pending, timeout=self._s.shutdown_grace_s)

    async def _start_fetcher(self) -> None:
        try:
            await self._fetcher.start()
        except FetcherUnavailableError as exc:
            _log.error("fetcher_unavailable", pool=self._pool, error=str(exc)[:200])
        self._refresh_state()

    async def _restart_fetcher(self) -> None:
        refresh = getattr(self._fetcher, "refresh_health", None)
        if refresh is not None:
            await refresh()
        if self._fetcher.health().state is FetcherState.UNAVAILABLE:
            with contextlib.suppress(FetcherUnavailableError):
                await self._fetcher.start()

    async def _recover_loop(self) -> None:
        while True:
            await asyncio.sleep(self._recover_every_s)
            try:
                await asyncio.to_thread(self._frontier.recover)
            except FrontierUnavailableError as exc:
                _log.warning("recover_unavailable", error=str(exc)[:200])

    # --- one attempt --------------------------------------------------------
    async def _attempt(self, claim: Claim) -> None:
        self._metrics.active.labels(self._pool).inc()
        started_wall = datetime.now(UTC)
        started = time.monotonic()
        try:
            result, claim = await self._execute(claim)
        except ClaimLostError:
            self.stats.lost += 1
            self._metrics.lost_claims.labels(self._pool).inc()
            return
        except asyncio.CancelledError:
            # shutdown: not the target's fault; give the task back without using its budget
            with contextlib.suppress(FrontierUnavailableError):
                await asyncio.shield(asyncio.to_thread(self._frontier.defer, claim))
            raise
        finally:
            self._metrics.active.labels(self._pool).dec()
        finished_wall = datetime.now(UTC)
        self._health.observe(result.outcome)
        decision = decide(result, self._queue, network_offline=self._health.offline)
        if decision.consumes_attempt:
            decision = await self._record(claim, result, decision, started_wall, finished_wall)
        self._observe(claim, result, decision, time.monotonic() - started)
        await self._report(claim, decision)
        self._done += 1
        if self._done % _RSS_CHECK_EVERY == 0:
            rss_mb = psutil.Process().memory_info().rss / 2**20
            self._overloaded = rss_mb > self._max_memory_mb
            if self._overloaded:
                _log.warning("worker_overloaded", pool=self._pool, rss_mb=round(rss_mb))

    async def _execute(self, claim: Claim) -> tuple[FetchResult, Claim]:
        validators = None
        if self._fetcher.capability is not FetchCapability.BROWSER:
            validators = await asyncio.to_thread(self._recorder.latest_validators, claim.url_id)
        request = FetchRequest(
            url=claim.url, url_id=claim.url_id, attempt=claim.attempt, validators=validators
        )
        hard_cap = self._fetcher.total_timeout_s + self._s.attempt_grace_s

        async def bounded() -> FetchResult:
            async with asyncio.timeout(hard_cap):
                return await self._fetcher.fetch(request)

        try:
            return await run_with_heartbeat(self._frontier, claim, bounded())
        except TimeoutError as exc:
            return self._synthetic(claim, Outcome.TIMEOUT, exc), claim
        except FetcherUnavailableError as exc:
            return self._synthetic(claim, Outcome.FETCHER_UNAVAILABLE, exc), claim

    def _synthetic(self, claim: Claim, outcome: Outcome, exc: BaseException) -> FetchResult:
        return FetchResult(
            outcome=outcome,
            capability=self._fetcher.capability,
            requested_url=claim.url,
            error=error_info(exc),
        )

    async def _record(
        self,
        claim: Claim,
        result: FetchResult,
        decision: Decision,
        started: datetime,
        finished: datetime,
    ) -> Decision:
        try:
            await asyncio.to_thread(
                self._recorder.record,
                UrlRef.of(claim.url),
                result,
                started_at=started,
                finished_at=finished,
            )
        except StorageError as exc:
            # Nothing durable came of this attempt: retry later without charging the target.
            self._metrics.record_errors.labels(self._pool).inc()
            _log.warning("record_failed", pool=self._pool, error=str(exc)[:200])
            return Decision(Operation.DEFER, "storage_unavailable", consumes_attempt=False)
        return decision

    def _observe(
        self, claim: Claim, result: FetchResult, decision: Decision, elapsed: float
    ) -> None:
        outcome = result.outcome.value
        self.stats.attempts[outcome] = self.stats.attempts.get(outcome, 0) + 1
        self.stats.operations[decision.operation.value] = (
            self.stats.operations.get(decision.operation.value, 0) + 1
        )
        self._metrics.attempts.labels(self._pool, outcome).inc()
        self._metrics.duration.labels(self._pool, outcome).observe(elapsed)
        self._metrics.bytes.labels(self._pool).inc(result.bytes_read)
        self._metrics.redirects.labels(self._pool).inc(len(result.redirects))
        if result.media is not None:
            self._metrics.media_probe_bytes.labels(self._pool).inc(result.media.bytes_read)
        if decision.next_queue is not None:
            self._metrics.escalations.labels(self._pool, decision.next_queue.value).inc()
        _log.info(
            "attempt",
            pool=self._pool,
            url_id=str(claim.url_id),
            attempt=claim.attempt,
            outcome=outcome,
            op=decision.operation.value,
            reason=decision.reason,
            status=result.status,
            bytes=result.bytes_read,
            seconds=round(elapsed, 3),
        )

    async def _report(self, claim: Claim, decision: Decision) -> None:
        deadline = time.monotonic() + self._frontier.lease_ttl_s
        while True:
            try:
                if decision.operation is Operation.COMPLETE:
                    outcome: object = await asyncio.to_thread(self._frontier.complete, claim)
                elif decision.operation is Operation.FAIL:
                    outcome = await asyncio.to_thread(
                        self._frontier.fail,
                        claim,
                        decision.reason,
                        next_queue=decision.next_queue,
                    )
                else:
                    outcome = await asyncio.to_thread(self._frontier.defer, claim)
            except FrontierUnavailableError as exc:
                if time.monotonic() >= deadline:
                    # The lease lapses; recovery reschedules the task (P3 §19).
                    self.stats.report_failures += 1
                    _log.error("report_abandoned", pool=self._pool, error=str(exc)[:200])
                    return
                await asyncio.sleep(self._s.report_retry_s)
                continue
            result = getattr(outcome, "outcome", outcome)  # FailResult/DeferResult or enum
            key = f"{decision.operation.value}:{getattr(result, 'value', result)}"
            self.stats.reports[key] = self.stats.reports.get(key, 0) + 1
            self._metrics.operations.labels(self._pool, decision.operation.value).inc()
            return
