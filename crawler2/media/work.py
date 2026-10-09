"""Redis coordination for P8: the clock, probe/hash work queues, leases and host limits.

Redis holds no fact (B.4 #1): a lost queue loses only *wants*; the next
observation or re-probe wants the media again (design §5.1). Every time
decision uses Redis ``TIME`` (B.4 #3).

Keys (under ``{namespace}:media:``):

- ``q:{stage}``: list of JSON jobs, ``stage`` = probe | hash;
- ``queued:{stage}:{media_id}``: set NX when a job is enqueued, deleted when a
  worker pops it, so one media is queued at most once per stage;
- ``lease:{stage}:{media_id}``: a worker's lease while it works on the media;
- ``host:next:{host}``: earliest next probe start per host (per-host interval);
- ``host:slot:{host}:{i}``: per-host hash slots (per-host concurrency).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import redis

from crawler2.storage.errors import StorageUnavailableError

_QUEUED_TTL_S = 24 * 3600
"""A queued flag outlives any sane queue wait; it only expires if the queue was lost."""

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""
_THROTTLE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
if nxt > now then return nxt - now end
redis.call('SET', KEYS[1], now + tonumber(ARGV[1]), 'PX', tonumber(ARGV[1]) + 60000)
return 0
"""


@dataclass(frozen=True, slots=True)
class Job:
    """One probe or hash want. ``probe_id`` is set for hash jobs (the probe that wanted it)."""

    media_id: str
    locator_url: str
    kind: str
    referer: str | None = None
    probe_id: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str | bytes) -> Job:
        doc: dict[str, Any] = json.loads(text)
        return cls(**doc)


class RedisWork:
    def __init__(self, client: redis.Redis, *, namespace: str) -> None:
        self._r = client
        self._ns = f"{namespace}:media"
        self._release = client.register_script(_RELEASE)
        self._throttle = client.register_script(_THROTTLE)

    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except redis.RedisError as exc:
            raise StorageUnavailableError(f"redis: {exc}") from exc

    def key(self, *parts: str) -> str:
        return ":".join((self._ns, *parts))

    def now(self) -> datetime:
        """Redis ``TIME``: the one clock every host agrees on (B.4 #3), truncated to
        milliseconds, the precision of a Scylla ``timestamp`` (stored facts and their
        JSON documents then carry the same instant)."""
        seconds, micros = self._call(self._r.time)
        return datetime.fromtimestamp(int(seconds), tz=UTC) + timedelta(
            milliseconds=int(micros) // 1000
        )

    # --- queues ---------------------------------------------------------------------

    def want(self, stage: str, job: Job) -> bool:
        """Enqueue unless this media is already queued for ``stage``."""
        queued = self.key("queued", stage, job.media_id)
        if not self._call(self._r.set, queued, "1", nx=True, ex=_QUEUED_TTL_S):
            return False
        self._call(self._r.lpush, self.key("q", stage), job.to_json())
        return True

    def pop(self, stage: str, *, timeout_s: float) -> Job | None:
        reply = self._call(self._r.brpop, [self.key("q", stage)], timeout=timeout_s)
        if reply is None:
            return None
        job = Job.from_json(reply[1])
        self._call(self._r.delete, self.key("queued", stage, job.media_id))
        return job

    def depth(self, stage: str) -> int:
        return int(self._call(self._r.llen, self.key("q", stage)))

    # --- leases and host limits -------------------------------------------------------

    def lease(self, stage: str, media_id: str, worker: str, *, ttl_s: float) -> bool:
        key = self.key("lease", stage, media_id)
        return bool(self._call(self._r.set, key, worker, nx=True, px=int(ttl_s * 1000)))

    def release(self, stage: str, media_id: str, worker: str) -> None:
        self._call(self._release, keys=[self.key("lease", stage, media_id)], args=[worker])

    def throttle(self, host: str, *, interval_s: float) -> float:
        """0 = the caller may start a request to ``host`` now (and reserves the slot);
        otherwise the seconds to wait. Spacing is global across hosts and processes."""
        if interval_s <= 0:
            return 0.0
        wait_ms = self._call(
            self._throttle, keys=[self.key("host", "next", host)], args=[int(interval_s * 1000)]
        )
        return int(wait_ms) / 1000

    def host_slot(self, host: str, worker: str, *, slots: int, ttl_s: float) -> str | None:
        """One of ``slots`` concurrent transfers to ``host``; None when all are taken."""
        for i in range(slots):
            key = self.key("host", "slot", host, str(i))
            if self._call(self._r.set, key, worker, nx=True, px=int(ttl_s * 1000)):
                return key
        return None

    def release_slot(self, key: str, worker: str) -> None:
        self._call(self._release, keys=[key], args=[worker])
