"""Collectors never turn a missing value into zero; reconciliation finds lost/duplicate work."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from crawler2.soak.collectors import (
    HostCollector,
    ProcessCollector,
    SyntheticCollector,
    parse_prometheus,
    select,
)
from crawler2.soak.config import Edge
from crawler2.soak.reconcile import Ledger, StreamTailer, extract
from tests.unit.soak.helpers import ENCODE

# --- collectors -----------------------------------------------------------------------------

PROM = """# HELP x_total x
# TYPE x_total counter
x_total{kind="a"} 3
x_total{kind="b"} 4
# TYPE depth gauge
depth 12
process_start_time_seconds 1.7e9
"""


def test_prometheus_selector_sums_matching_series_and_reports_absence() -> None:
    samples = parse_prometheus(PROM)
    assert select(samples, "x_total") == 7
    assert select(samples, 'x_total{kind="a"}') == 3
    assert select(samples, "depth") == 12
    assert select(samples, "nope") is None, "absent is None, never 0"
    assert select(samples, 'x_total{kind="a"} / x_total') == 3 / 7
    assert select(samples, "x_total / nope") is None
    assert select(samples, "x_total / depth") == 7 / 12
    with pytest.raises(ValueError, match="bad selector"):
        select(samples, "x_total{")


def test_host_collector_reports_real_values_and_a_failed_disk_as_missing(tmp_path: Path) -> None:
    r = HostCollector(
        {"disk_paths": {"data": str(tmp_path), "gone": str(tmp_path / "missing")}}
    ).collect()
    assert r.values["cpu_percent"] is not None
    assert 0 < (r.values["disk_data_free_fraction"] or 0) <= 1
    assert r.values["disk_gone_free_fraction"] is None
    assert "disk_gone_free_fraction" in r.errors


def test_process_collector_measures_a_labelled_process_and_notes_a_missing_one() -> None:
    import subprocess
    import sys
    import time

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "soak-marker-7f3a"]
    )
    try:
        collector = ProcessCollector(
            {"match": {"me": "soak-marker-7f3a", "ghost": "no-such-process-xyz"}}
        )
        deadline = time.monotonic() + 10
        r = collector.collect()
        while r.values["me_up"] != 1.0 and time.monotonic() < deadline:  # exec may not be done yet
            time.sleep(0.05)
            r = collector.collect()
        assert r.values["me_up"] == 1.0
        assert (r.values["me_rss_mb"] or 0) > 0
        assert (r.values["me_fds"] or 0) > 0
        assert len(r.identities["me"]) == 1
        assert r.values["ghost_up"] == 0.0
        assert r.values["ghost_rss_mb"] is None
        assert r.identities["ghost"] == []
    finally:
        proc.kill()
        proc.wait()


def test_synthetic_collector_withholds_dropped_series_and_down_samples() -> None:
    c = SyntheticCollector(
        {"series": {"a": {"kind": "linear", "slope": 2}, "b": {}}, "drop": ["b"], "down_at": [1]}
    )
    first, second = c.collect(), c.collect()
    assert first.values["a"] == 0
    assert first.values["b"] is None
    assert second.values["up"] == 0.0
    assert second.values["a"] is None


# --- extraction & ledger ---------------------------------------------------------------------


def test_extract_follows_paths_and_fans_out_over_lists() -> None:
    doc = {"payload": {"refs": [{"id": "a"}, {"id": "b"}, {"x": 1}], "one": {"two": 5}}}
    assert extract(doc, "payload.one.two") == ["5"]
    assert extract(doc, "payload.refs[].id") == ["a", "b"]
    assert extract(doc, "payload.missing.deep") == []


def _req(n: int, t: float = 0) -> dict[str, Any]:
    return {
        "event_id": f"q{n}",
        "event_type": "encode.requested",
        "occurred_at": "2026-10-23T00:00:00Z",
        "payload": {"content": {"content_id": f"c{n}"}},
    }


def _rdy(n: int, rep: str | None = None, eid: str | None = None) -> dict[str, Any]:
    return {
        "event_id": eid or f"r{n}",
        "event_type": "representation.ready",
        "occurred_at": "2026-10-23T00:00:01Z",
        "payload": {
            "representation": {"content_id": f"c{n}", "representation_id": rep or f"rep{n}"}
        },
    }


def _ledger(tmp_path: Path, edges: list[Edge] | None = None) -> Ledger:
    return Ledger(tmp_path / "l.sqlite3", edges or [ENCODE])


def test_lost_pending_and_satisfied_work_are_told_apart(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.ingest(_req(1), observed=100)
    led.ingest(_req(2), observed=100)
    led.ingest(_req(3), observed=900)
    led.ingest(_rdy(1), observed=110)
    (res,) = led.edge_results(now=1000, run_started=0)
    assert (res.sources, res.satisfied) == (3, 1)
    assert res.unaccounted == 1, "c2: 900 s old, grace 300 s"
    assert res.pending == 1, "c3 is 100 s old"
    assert res.examples == ["c2"]
    assert res.oldest_unaccounted_s == 900


def test_redelivery_is_counted_but_is_not_a_duplicate_effect(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.ingest(_req(1), observed=1)
    assert led.ingest(_rdy(1), observed=2) == "new"
    assert led.ingest(_rdy(1), observed=3) == "redelivery"
    (res,) = led.edge_results(now=10, run_started=0)
    assert res.duplicate_effects == 0
    assert led.redeliveries() == {"representation.ready": 1}


def test_two_different_effects_for_one_key_is_a_duplicate_business_effect(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.ingest(_req(1), observed=1)
    led.ingest(_rdy(1, rep="repA", eid="r1"), observed=2)
    led.ingest(_rdy(1, rep="repB", eid="r1b"), observed=3)
    (res,) = led.edge_results(now=10, run_started=0)
    assert res.duplicate_effects == 1


def test_targets_without_a_source_are_orphans_only_if_after_the_run_started(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.ingest(_rdy(9), observed=5)
    (res,) = led.edge_results(now=10, run_started=0)
    assert res.orphans == 1
    (late,) = led.edge_results(now=10, run_started=1e12)
    assert late.orphans == 0


def test_filters_and_per_type_keys_make_a_failure_terminal_only_when_not_retryable(
    tmp_path: Path,
) -> None:
    edge = Edge(
        name="encode",
        from_type="encode.requested",
        to_types=["representation.ready", "encode.failed"],
        from_key="payload.content.content_id",
        to_key="payload.representation.content_id",
        to_key_by_type={"encode.failed": "payload.content_id"},
        to_filters={"encode.failed": {"payload.retryable": "False"}},
        grace_s=10,
    )
    led = _ledger(tmp_path, [edge])
    for n in (1, 2):
        led.ingest(_req(n), observed=1)
    led.ingest(
        {
            "event_id": "f1",
            "event_type": "encode.failed",
            "payload": {"content_id": "c1", "retryable": True},
        },
        observed=2,
    )
    led.ingest(
        {
            "event_id": "f2",
            "event_type": "encode.failed",
            "payload": {"content_id": "c2", "retryable": False},
        },
        observed=2,
    )
    (res,) = led.edge_results(now=100, run_started=0)
    assert res.satisfied == 1
    assert res.unaccounted == 1
    assert res.examples == ["c1"], "a retryable failure is not an outcome"


def test_envelopes_without_ids_are_invalid_not_a_crash(tmp_path: Path) -> None:
    assert _ledger(tmp_path).ingest({"event_type": "x"}) == "invalid"


class FakeRedis:
    """Just enough of a Redis stream to drive the tailer."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, str]]] = []
        self.added = 0

    def add(self, env: dict[str, Any], *, count: bool = True) -> None:
        import json

        self.added += 1
        self.entries.append((f"{self.added}-0", {"envelope": json.dumps(env)}))

    def trim(self, n: int) -> None:
        self.entries = self.entries[n:]

    def xrange(self, key: str, min: str, count: int) -> list[Any]:
        after = int(min.lstrip("(").split("-")[0])
        return [e for e in self.entries if int(e[0].split("-")[0]) > after][:count]

    def xinfo_stream(self, key: str) -> dict[str, Any]:
        last = self.entries[-1][0] if self.entries else "0-0"
        return {
            "last-generated-id": f"{self.added}-0" if self.added else last,
            "entries-added": self.added,
        }


def test_tailer_reads_in_bounded_batches_and_detects_trimmed_entries(tmp_path: Path) -> None:
    redis = FakeRedis()
    led = _ledger(tmp_path)
    tailer = StreamTailer(redis, {"encode.requested": "s"}, led, batch=2)
    pos = tailer.start_positions()
    for n in range(5):
        redis.add(_req(n))
    res = tailer.poll(pos)
    assert res.ingested == 5
    assert res.gaps == []
    for n in range(5, 9):
        redis.add(_req(n))
    redis.entries = redis.entries[-1:]  # 4 new entries added, 3 trimmed unread -> 1 readable
    res = tailer.poll(pos)
    assert res.ingested == 1
    assert res.gaps == [{"stream": "s", "event_type": "encode.requested", "entries_lost": 3}]


def test_tailer_reports_read_errors_instead_of_raising(tmp_path: Path) -> None:
    class Broken(FakeRedis):
        def xrange(self, key: str, min: str, count: int) -> list[Any]:
            raise ConnectionError("down")

    tailer = StreamTailer(Broken(), {"encode.requested": "s"}, _ledger(tmp_path))
    res = tailer.poll({})
    assert res.errors
    assert "down" in res.errors[0]


def test_a_stream_that_does_not_exist_yet_is_empty_not_an_error(tmp_path: Path) -> None:
    class NoStream(FakeRedis):
        def xinfo_stream(self, key: str) -> dict[str, Any]:
            if not self.added:
                raise RuntimeError("ERR no such key")
            return super().xinfo_stream(key)

    redis = NoStream()
    tailer = StreamTailer(redis, {"encode.requested": "s"}, _ledger(tmp_path))
    pos = tailer.start_positions()
    assert pos["encode.requested"] == {"id": "0-0", "added": 0}
    res = tailer.poll(pos)
    assert (res.errors, res.gaps, res.ingested) == ([], [], 0)
    redis.add(_req(1))
    assert tailer.poll(pos).ingested == 1
