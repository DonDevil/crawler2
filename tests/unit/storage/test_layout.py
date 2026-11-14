from collections import Counter
from datetime import UTC, datetime, timedelta, timezone

import pytest
from antipiracy_contracts.ids import ObservationId, UrlId
from antipiracy_contracts.urls import canonicalize_url
from hypothesis import given
from hypothesis import strategies as st

from crawler2.storage.layout import (
    INLINK_SHARDS,
    bucket_start,
    day_bucket,
    days_back,
    earliest_wins,
    latest_wins,
    month_bucket,
    months_back,
    outbox_bucket,
    shard_of,
    version_wins,
)

# earliest_wins() = 2*anchor - at must stay > 0: valid strictly before 2069-12-31T00:00:00Z.
EW_ANCHOR = datetime(2020, 1, 1, tzinfo=UTC)
EW_LIMIT = datetime(2069, 12, 31, tzinfo=UTC)  # = 2 * anchor, first rejected instant
EW_LAST_VALID = EW_LIMIT - timedelta(microseconds=1)

instants = st.datetimes(
    min_value=EW_ANCHOR.replace(tzinfo=None),
    max_value=EW_LAST_VALID.replace(tzinfo=None),
    timezones=st.just(UTC),
)


def test_buckets_are_readable_utc_calendar_units() -> None:
    at = datetime(2026, 9, 30, 23, 30, tzinfo=UTC)
    assert day_bucket(at) == 20260930
    assert month_bucket(at) == 202609
    # the same instant in another zone lands in the same UTC bucket
    ist = at.astimezone(datetime(2026, 1, 1, tzinfo=UTC).astimezone().tzinfo)
    assert day_bucket(ist) == 20260930


def test_months_back_crosses_year_boundaries_newest_first() -> None:
    months = list(months_back(datetime(2026, 2, 3, tzinfo=UTC), datetime(2025, 11, 20, tzinfo=UTC)))
    assert months == [202602, 202601, 202512, 202511]
    assert list(days_back(datetime(2026, 3, 1, tzinfo=UTC), 3)) == [20260301, 20260228, 20260227]


def test_outbox_bucket_is_a_minute() -> None:
    at = datetime(2026, 9, 1, 12, 0, 59, tzinfo=UTC)
    bucket = outbox_bucket(at)
    assert bucket_start(bucket) == datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    assert outbox_bucket(at + timedelta(seconds=1)) == bucket + 1


def test_shards_are_stable_and_spread() -> None:
    ids = [UrlId.of(canonicalize_url(f"https://h.example/{i}")) for i in range(32_000)]
    assert shard_of(ids[0], INLINK_SHARDS) == shard_of(UrlId(str(ids[0])), INLINK_SHARDS)
    counts = Counter(shard_of(i, INLINK_SHARDS) for i in ids)
    assert set(counts) == set(range(INLINK_SHARDS))
    mean = len(ids) / INLINK_SHARDS  # ~1000 per shard; binomial sd ~31
    assert all(0.85 * mean < c < 1.15 * mean for c in counts.values())
    allocated = Counter(shard_of(ObservationId.new(), 16) for _ in range(16_000))
    assert all(850 < c < 1150 for c in allocated.values())


@given(instants, instants)
def test_convergent_timestamps_order(a: datetime, b: datetime) -> None:
    if a < b:
        assert latest_wins(a) < latest_wins(b)
        assert earliest_wins(a) > earliest_wins(b)
    for t in (latest_wins(a), earliest_wins(a)):
        assert 0 < t < 2**63
    # never a future timestamp (Scylla refuses > 3 days ahead) for any instant since 2020
    assert earliest_wins(a) <= latest_wins(datetime(2020, 1, 1, tzinfo=UTC))


def test_version_wins_orders_versions() -> None:
    assert version_wins(1) < version_wins(2)


def test_earliest_wins_lower_boundary_is_the_anchor() -> None:
    # the mirror's fixed point: at == anchor maps to itself
    assert earliest_wins(EW_ANCHOR) == latest_wins(EW_ANCHOR)


def test_earliest_wins_upper_boundary_last_valid_instant() -> None:
    assert earliest_wins(EW_LAST_VALID) == 1
    assert earliest_wins(EW_LAST_VALID - timedelta(microseconds=1)) == 2
    assert earliest_wins(EW_LAST_VALID) < earliest_wins(EW_LAST_VALID - timedelta(seconds=1))


def test_earliest_wins_rejects_the_first_instant_at_and_after_the_limit() -> None:
    # regression: 2069-12-31T00:00:00Z maps to timestamp 0, which the contract excludes (0 < ts)
    for at in (EW_LIMIT, EW_LIMIT + timedelta(microseconds=1), datetime(2070, 1, 1, tzinfo=UTC)):
        with pytest.raises(ValueError, match="earliest_wins range"):
            earliest_wins(at)


def test_earliest_wins_uses_the_utc_instant_not_the_offset() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    at = datetime(2026, 9, 30, 23, 30, tzinfo=UTC)
    assert earliest_wins(at.astimezone(ist)) == earliest_wins(at)
    # an offset-aware value whose UTC instant is exactly the limit is rejected too
    with pytest.raises(ValueError, match="earliest_wins range"):
        earliest_wins(EW_LIMIT.astimezone(ist))
    assert earliest_wins(EW_LAST_VALID.astimezone(ist)) == 1
