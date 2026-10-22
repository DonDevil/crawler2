"""P12 campaign statistics against the hand-counted fixture campaign (validation group E)."""

from __future__ import annotations

from datetime import UTC, datetime

from crawler2.evidence.stats import campaign_stats, months
from tests.unit.evidence import campaign as fixture
from tests.unit.evidence.fakes import World


def test_fixture_campaign_matches_hand_counted_values() -> None:
    w = World()
    case, _ = fixture.build(w)
    stats = campaign_stats(fixture.data(w, case))
    for section, expected in fixture.EXPECTED.items():
        got = stats[section]
        if isinstance(expected, dict):
            assert {k: got[k] for k in expected} == expected, section
        else:
            assert got == expected, section
    assert stats["window"] == {"from_month": "2026-06", "to_month": "2026-06"}
    assert set(stats["unavailable"]) == {"pages_examined", "media_discovered"}


def test_duplicate_delivery_and_replays_do_not_inflate_counts() -> None:
    w = World()
    case, ids = fixture.build(w)
    before = campaign_stats(fixture.data(w, case))
    collector = w.collector()
    for _ in range(3):
        for eid in ids.values():
            cand = w.store.candidate(eid)
            assert cand is not None
            collector.collect(cand.dossier.match)
    assert campaign_stats(fixture.data(w, case)) == before


def test_targets_outside_the_campaign_are_not_counted() -> None:
    w = World()
    case, ids = fixture.build(w)
    data = fixture.data(w, case)
    assert str(ids["m4"]) not in {str(e.evidence_id) for e in data.evidence}
    assert "other.example" not in campaign_stats(data)["sources"]["pages_per_domain"]


def test_month_window_is_bounded_and_ordered() -> None:
    got = months(datetime(2025, 11, 15, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC))
    assert [m.strftime("%Y-%m") for m in got] == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert len(months(datetime(2000, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))) == 36
