"""Redis frontier: every state transition is one Lua script (ADR-015, ADR-016).

Data model, invariants and failure semantics:
docs/phases/p03-frontier-scheduling/p3-frontier-scheduling.md.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Sequence
from importlib import resources
from typing import Any, cast

import redis
from antipiracy_contracts.ids import DomainId, UrlId

from crawler2.core.configuration import ExecutionQueue, FrontierSettings, RedisSettings
from crawler2.frontier.errors import FrontierError, FrontierUnavailableError
from crawler2.frontier.model import (
    Admission,
    AdmitOutcome,
    AdmitResult,
    Claim,
    CompleteOutcome,
    DeferOutcome,
    DeferResult,
    FailOutcome,
    FailResult,
    FrontierStats,
    RecoveryResult,
)

_SCRIPTS = ("admit", "claim", "heartbeat", "complete", "fail", "defer", "recover")
_ACTIVE_STATES = frozenset({"scheduled", "ready", "leased"})
# Server replies that mean "cannot serve now", not "the script is wrong".
_TRANSIENT_REPLIES = ("OOM", "BUSY", "LOADING", "MASTERDOWN", "READONLY", "TRYAGAIN")


def _lua(name: str) -> str:
    return (resources.files(__package__) / "lua" / f"{name}.lua").read_text(encoding="utf-8")


def connect_redis(settings: RedisSettings) -> redis.Redis:
    """A client suitable for the frontier (string replies, bounded waits)."""
    return redis.Redis(
        host=settings.host,
        port=settings.port,
        db=settings.db,
        password=settings.password.get_secret_value() if settings.password else None,
        socket_timeout=settings.socket_timeout_s,
        socket_connect_timeout=settings.socket_timeout_s,
        decode_responses=True,
    )


class RedisFrontier:
    """The P3 frontier. Thread-safe as far as the redis client is; one per process is enough.

    ``clock`` exists for deterministic tests only: when set, its value is
    passed to every script instead of Redis ``TIME``. Production never sets it
    (ADR-006: Redis ``TIME`` is the distributed clock).
    """

    def __init__(
        self,
        client: redis.Redis,
        settings: FrontierSettings,
        *,
        namespace: str,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not client.get_encoder().decode_responses:
            raise ValueError("the frontier needs a client with decode_responses=True")
        self._r = client
        self._cfg = settings
        self._clock = clock
        self._prefix = f"{namespace}:fr:"
        self._queues = tuple(ExecutionQueue)
        self._queues_arg = ",".join(q.value for q in self._queues)
        prelude = _lua("prelude")
        self._scripts = {
            name: client.register_script(prelude + "\n" + _lua(name)) for name in _SCRIPTS
        }

    @property
    def lease_ttl_s(self) -> float:
        return self._cfg.lease_ttl_s

    @property
    def settings(self) -> FrontierSettings:
        return self._cfg

    # -- script plumbing ---------------------------------------------------

    def _head(self) -> list[str]:
        now = "" if self._clock is None else f"{self._clock():.6f}"
        return [self._prefix, now, self._queues_arg]

    def _run(self, name: str, *args: object, client: Any = None) -> Any:
        try:
            argv = cast(list[str | int | float], [*self._head(), *args])
            return self._scripts[name](args=argv, client=client if client is not None else self._r)
        except redis.ResponseError as exc:
            if str(exc).startswith(_TRANSIENT_REPLIES):
                raise FrontierUnavailableError(f"{name}: {exc}") from exc
            raise FrontierError(f"{name}: {exc}") from exc
        except redis.RedisError as exc:
            raise FrontierUnavailableError(f"{name}: {exc}") from exc

    def _admit_args(self, a: Admission) -> list[object]:
        due = "" if a.not_before is None else f"{a.not_before.timestamp():.6f}"
        return [
            a.url.url_id,
            a.url.url,
            a.url.domain_id,
            a.queue.value,
            a.priority,
            due,
            a.reason,
            self._cfg.max_depth[a.queue],
        ]

    @staticmethod
    def _admit_result(reply: list[str]) -> AdmitResult:
        return AdmitResult(AdmitOutcome(reply[0]), reply[1] or None)

    # -- producer API ------------------------------------------------------

    def admit(self, admission: Admission) -> AdmitResult:
        return self._admit_result(self._run("admit", *self._admit_args(admission)))

    def admit_many(self, admissions: Iterable[Admission]) -> list[AdmitResult]:
        """Pipelined ``admit`` (one round trip per batch, each admission atomic)."""
        pipe = self._r.pipeline(transaction=False)
        count = 0
        for admission in admissions:
            self._run("admit", *self._admit_args(admission), client=pipe)
            count += 1
        if count == 0:
            return []
        try:
            replies = pipe.execute()
        except redis.RedisError as exc:
            raise FrontierUnavailableError(f"admit_many: {exc}") from exc
        return [self._admit_result(reply) for reply in replies]

    def set_domain_interval(self, domain_id: DomainId, interval_s: float | None) -> None:
        """Override (or with ``None`` reset) one domain's politeness interval."""
        key = self._prefix + "interval"
        try:
            if interval_s is None:
                self._r.hdel(key, domain_id)
            else:
                if interval_s < 0:
                    raise ValueError("interval must be >= 0")
                self._r.hset(key, domain_id, f"{interval_s:.6f}")
        except redis.RedisError as exc:
            raise FrontierUnavailableError(f"set_domain_interval: {exc}") from exc

    # -- worker API --------------------------------------------------------

    def claim(self, queue: ExecutionQueue, *, lease_ttl_s: float | None = None) -> Claim | None:
        """Lease the best eligible task of ``queue``, or ``None`` if none is eligible now."""
        token = uuid.uuid4().hex
        reply = self._run(
            "claim",
            queue.value,
            token,
            lease_ttl_s if lease_ttl_s is not None else self._cfg.lease_ttl_s,
            self._cfg.default_interval_s,
            self._cfg.promote_batch,
        )
        if not reply:
            return None
        url_id, url, domain_id, priority, attempt, lex, claimed_at, reason = reply
        return Claim(
            url_id=UrlId(url_id),
            url=url,
            domain_id=DomainId(domain_id),
            queue=queue,
            priority=int(priority),
            attempt=int(attempt),
            token=token,
            lease_expires_at=float(lex),
            claimed_at=float(claimed_at),
            reason=reason,
        )

    def heartbeat(self, claim: Claim, *, lease_ttl_s: float | None = None) -> Claim | None:
        """Extend the lease; ``None`` = the claim is no longer this worker's."""
        ttl = lease_ttl_s if lease_ttl_s is not None else self._cfg.lease_ttl_s
        reply = self._run("heartbeat", claim.url_id, claim.token, ttl)
        if reply is None:
            return None
        return Claim(
            url_id=claim.url_id,
            url=claim.url,
            domain_id=claim.domain_id,
            queue=claim.queue,
            priority=claim.priority,
            attempt=claim.attempt,
            token=claim.token,
            lease_expires_at=float(reply),
            claimed_at=claim.claimed_at,
            reason=claim.reason,
        )

    def complete(self, claim: Claim) -> CompleteOutcome:
        return CompleteOutcome(self._run("complete", claim.url_id, claim.token))

    def fail(
        self, claim: Claim, reason: str, *, next_queue: ExecutionQueue | None = None
    ) -> FailResult:
        cfg = self._cfg
        outcome, retry_at = self._run(
            "fail",
            claim.url_id,
            claim.token,
            reason[:200],
            next_queue.value if next_queue is not None else "",
            cfg.max_attempts,
            cfg.base_backoff_s,
            cfg.max_backoff_s,
        )
        return FailResult(FailOutcome(outcome), float(retry_at) if retry_at else None)

    def defer(self, claim: Claim, *, delay_s: float | None = None) -> DeferResult:
        delay = delay_s if delay_s is not None else self._cfg.defer_delay_s
        outcome, retry_at = self._run("defer", claim.url_id, claim.token, delay)
        return DeferResult(DeferOutcome(outcome), float(retry_at) if retry_at else None)

    # -- maintenance -------------------------------------------------------

    def recover(self, *, batch: int | None = None) -> RecoveryResult:
        """Expired leases -> retry or dead letter; due scheduled -> ready. Safe to run anywhere."""
        cfg = self._cfg
        recovered, dead, promoted = self._run(
            "recover",
            batch if batch is not None else cfg.recover_batch,
            cfg.max_attempts,
            cfg.base_backoff_s,
            cfg.max_backoff_s,
            cfg.dead_ttl_s,
            cfg.dead_max,
            cfg.promote_batch,
        )
        return RecoveryResult(int(recovered), int(dead), int(promoted))

    def stats(self) -> FrontierStats:
        p = self._prefix
        pipe = self._r.pipeline(transaction=True)
        pipe.hgetall(p + "depth")
        pipe.zcard(p + "scheduled")
        pipe.zcard(p + "leases")
        pipe.zcard(p + "gate")
        pipe.zcard(p + "dead")
        pipe.hgetall(p + "stats")
        for q in self._queues:
            pipe.zcard(p + f"ready:{q.value}")
        try:
            depth, scheduled, leased, gated, dead, counters, *eligible = pipe.execute()
        except redis.RedisError as exc:
            raise FrontierUnavailableError(f"stats: {exc}") from exc
        return FrontierStats(
            depth={q: int(depth.get(q.value, 0)) for q in self._queues},
            eligible_domains=dict(zip(self._queues, map(int, eligible), strict=True)),
            scheduled=scheduled,
            leased=leased,
            gated_domains=gated,
            dead_letters=dead,
            counters={k: int(v) for k, v in counters.items()},
        )

    def dead_letters(self, limit: int = 100) -> list[dict[str, str]]:
        """Most recent recovery-exhausted tasks (task fields incl. ``url``, ``err``)."""
        try:
            ids = cast(list[str], self._r.zrevrange(self._prefix + "dead", 0, limit - 1))
            pipe = self._r.pipeline(transaction=False)
            for url_id in ids:
                pipe.hgetall(self._prefix + f"task:{url_id}")
            hashes = cast(list[dict[str, str]], pipe.execute())
            return [{"url_id": i, **h} for i, h in zip(ids, hashes, strict=True) if h]
        except redis.RedisError as exc:
            raise FrontierUnavailableError(f"dead_letters: {exc}") from exc

    def clear(self) -> None:
        """Delete every frontier key of this namespace (tests/benchmarks only)."""
        for key in self._r.scan_iter(match=self._prefix + "*", count=1000):
            self._r.unlink(key)

    def audit(self) -> list[str]:
        """Check every structural invariant (§6, §17); empty list = consistent.

        O(total state) with SCAN: an operations/test tool, never on a hot path.
        """
        return _Auditor(self._r, self._prefix, self._queues).run()


class _Auditor:
    def __init__(self, r: redis.Redis, prefix: str, queues: Sequence[ExecutionQueue]) -> None:
        self.r: Any = r  # decode_responses=True: every reply is str
        self.p, self.queues = prefix, [q.value for q in queues]
        self.problems: list[str] = []

    def _zset(self, key: str) -> dict[str, float]:
        return {m: float(s) for m, s in self.r.zrange(self.p + key, 0, -1, withscores=True)}

    def run(self) -> list[str]:
        p, bad = self.p, self.problems.append
        tasks: dict[str, dict[str, str]] = {}
        for key in self.r.scan_iter(match=p + "task:*", count=1000):
            tasks[key[len(p) + 5 :]] = self.r.hgetall(key)
        scheduled, leases, dead, gate, yields = (
            self._zset(k) for k in ("scheduled", "leases", "dead", "gate", "yield")
        )
        queues: dict[tuple[str, str], dict[str, float]] = {}
        for key in self.r.scan_iter(match=p + "q:*", count=1000):
            _, q, dom = key[len(p) :].split(":")
            queues[(q, dom)] = self._zset(key[len(p) :])
        member_of = {i: (q, d) for (q, d), members in queues.items() for i in members}
        depth = {q: 0 for q in self.queues}

        for i, t in tasks.items():
            st, q, dom = t.get("st"), t.get("q", ""), t.get("dom", "")
            if q not in depth:
                bad(f"{i}: unknown queue {q!r}")
                continue
            if st in _ACTIVE_STATES:
                depth[q] += 1
            where = {
                "scheduled": i in scheduled,
                "leased": i in leases,
                "dead": i in dead,
                "ready": member_of.get(i) == (q, dom),
            }
            if st not in where:
                bad(f"{i}: unknown state {st!r}")
                continue
            if not where[st] or sum(where.values()) != 1 or (i in member_of and st != "ready"):
                bad(f"{i}: state {st} but placed in {sorted(k for k, v in where.items() if v)}")
            if st == "leased" and ("tok" not in t or abs(float(t["lex"]) - leases[i]) > 1e-3):
                bad(f"{i}: lease record mismatch")
        for name, zset in (("scheduled", scheduled), ("leases", leases)):
            for i in zset:
                if i not in tasks:
                    bad(f"{name} member {i} has no task")
        for i, (q, dom) in member_of.items():
            owner = tasks.get(i)
            if owner is None or owner.get("q") != q or owner.get("dom") != dom:
                bad(f"queue {q}:{dom} member {i} does not match its task")
        stored = {k: int(v) for k, v in self.r.hgetall(p + "depth").items()}
        for q in self.queues:
            if stored.get(q, 0) != depth[q]:
                bad(f"depth[{q}] = {stored.get(q, 0)}, counted {depth[q]}")
        for q in self.queues:
            ready = self._zset(f"ready:{q}")
            for dom, score in ready.items():
                members = queues.get((q, dom))
                if dom in gate:
                    bad(f"ready:{q} holds gated domain {dom}")
                if f"{q}|{dom}" in yields:
                    bad(f"ready:{q} holds domain {dom} that is yielding")
                if not members:
                    bad(f"ready:{q} holds domain {dom} without work")
                elif min(members.values()) != score:
                    bad(f"ready:{q} score of {dom} is not its head rank")
            for (qq, dom), members in queues.items():
                held = f"{q}|{dom}" in yields
                if qq == q and members and dom not in ready and dom not in gate and not held:
                    bad(f"stranded: {q}:{dom} has work but is neither eligible nor gated")
        return self.problems
