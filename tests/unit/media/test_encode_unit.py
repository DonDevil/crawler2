"""Encode requester and representation loop branches with in-memory doubles."""

from __future__ import annotations

from typing import Any

import pytest

from crawler2.media.encode import RepresentationLoop
from crawler2.storage.errors import StorageUnavailableError
from crawler2.storage.events.consumer import StreamEntry


class Reader:
    def __init__(self, entries: list[StreamEntry], *, stale: bool = False) -> None:
        self.entries = entries
        self.stale = stale
        self.acked: list[str] = []

    def claim_stale(self, *, min_idle_ms: int, count: int) -> list[StreamEntry]:
        return self.entries if self.stale else []

    def read(self, *, count: int, block_ms: int) -> list[StreamEntry]:
        out, self.entries = self.entries, []
        return out

    def ack(self, entries: list[StreamEntry]) -> None:
        self.acked += [e.entry_id for e in entries]
        if self.stale:
            self.entries = []


class Consumer:
    def __init__(self, fail_on: dict[bytes, Exception]) -> None:
        self.fail_on = fail_on
        self.seen: list[bytes] = []

    def handle(self, wire: bytes) -> None:
        if wire in self.fail_on:
            raise self.fail_on[wire]
        self.seen.append(wire)


def test_loop_acks_invalid_and_leaves_storage_failures_pending() -> None:
    from antipiracy_contracts.events import ContractError

    reader = Reader(
        [StreamEntry("1-0", b"ok"), StreamEntry("2-0", b"bad"), StreamEntry("3-0", b"down")]
    )
    consumer = Consumer({b"bad": ContractError("x"), b"down": StorageUnavailableError("db")})
    loop = RepresentationLoop([(reader, consumer)], batch_size=10, block_ms=1, claim_idle_ms=1)  # type: ignore[list-item]
    assert loop.run_once() == 2
    assert reader.acked == ["1-0", "2-0"]  # invalid acked (skipped), storage failure left pending
    stale = Reader([StreamEntry("9-0", b"ok")], stale=True)
    loop2 = RepresentationLoop([(stale, Consumer({}))], batch_size=10, block_ms=1, claim_idle_ms=1)  # type: ignore[list-item]
    loop2.run(max_idle_polls=1)
    assert stale.acked == ["9-0"]
    loop2.stop()
    loop2.run()  # stopped: returns at once


def test_sweep_forever_retries_and_stops() -> None:
    from crawler2.media.encode import EncodeRequester

    calls = {"n": 0}

    class Store:
        def encode_claims_on(self, day: Any) -> list[Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise StorageUnavailableError("down")
            return []

    requester = EncodeRequester(Store(), None, None, producer=None)  # type: ignore[arg-type]
    sleeps: list[float] = []
    requester.sweep_forever(interval_s=5, stop=lambda: len(sleeps) >= 2, sleep=sleeps.append)
    assert sleeps == [5, 5]
    with pytest.raises(ValueError, match="ok hash"):
        requester.request(type("H", (), {"content": None})(), None, None)  # type: ignore[arg-type]
