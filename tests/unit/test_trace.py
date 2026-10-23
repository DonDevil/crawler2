"""crawler2-trace: a chain is found by any id in it and widened by correlation id."""

from __future__ import annotations

import json
from typing import Any

import pytest

from crawler2.diagnostics import trace as t


class FakeRedis:
    def __init__(self, streams: dict[str, list[dict[str, Any]]]) -> None:
        self.streams = streams

    def xrange(self, key: str, min: str, count: int) -> list[Any]:
        if key not in self.streams:
            raise KeyError(key)
        entries = [
            (f"{i + 1}-0", {b"envelope": json.dumps(e).encode()})
            for i, e in enumerate(self.streams[key])
        ]
        start = 0
        if min != "-":
            start = next((i for i, (eid, _) in enumerate(entries) if eid == min), -1)
        return entries[start : start + count]


def env(etype: str, eid: str, corr: str | None, at: str, **payload: Any) -> dict[str, Any]:
    return {
        "event_id": eid,
        "event_type": etype,
        "occurred_at": at,
        "correlation_id": corr,
        "causation_id": None,
        "producer": {"service": "x"},
        "payload": payload,
    }


def test_widens_a_hit_to_the_whole_correlation_chain_in_time_order() -> None:
    streams = {
        "s:fetch": [
            env("fetch.completed", "evt_f1", "cor_A", "2026-10-22T10:00:00Z"),
            env("fetch.completed", "evt_f2", "cor_B", "2026-10-22T10:00:01Z"),
        ],
        "s:page": [
            env(
                "page.observed",
                "evt_p1",
                "cor_A",
                "2026-10-22T10:00:02Z",
                url="https://x/needle-url",
            )
        ],
        "s:media": [
            env("media.discovered", "evt_m1", "cor_A", "2026-10-22T10:00:03Z"),
            env("media.discovered", "evt_m2", "cor_B", "2026-10-22T10:00:04Z"),
        ],
    }
    hops, scanned = t.trace(
        FakeRedis(streams),
        {"fetch.completed": "s:fetch", "page.observed": "s:page", "media.discovered": "s:media"},
        "needle-url",
    )
    assert [h.event_id for h in hops] == ["evt_f1", "evt_p1", "evt_m1"]
    assert [h.matched_directly for h in hops] == [False, True, False]
    assert scanned == {"fetch.completed": 2, "page.observed": 1, "media.discovered": 2}


def test_an_event_without_correlation_is_found_directly_only() -> None:
    streams = {
        "s:m": [env("match.found", "evt_x", None, "2026-10-22T10:00:00Z", match_id="mat_123456789")]
    }
    hops, _ = t.trace(FakeRedis(streams), {"match.found": "s:m"}, "mat_123456789")
    assert [h.event_id for h in hops] == ["evt_x"]


def test_scan_is_bounded_and_missing_streams_are_empty() -> None:
    streams = {
        "s:a": [
            env("fetch.completed", f"e{i}", None, f"2026-10-22T10:00:{i:02d}Z") for i in range(30)
        ]
    }
    hops, scanned = t.trace(
        FakeRedis(streams),
        {"fetch.completed": "s:a", "page.observed": "s:none"},
        "e29",
        max_scan=10,
    )
    assert hops == []
    assert scanned == {"fetch.completed": 10, "page.observed": 0}


def test_short_needles_are_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert t.main(["abc"]) == 2
    assert "too short" in capsys.readouterr().err
