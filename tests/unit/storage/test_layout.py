from collections import Counter
from datetime import UTC, datetime, timedelta

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

instants = st.datetimes(
    min_value=datetime(2020, 1, 1), max_value=datetime(2069, 12, 31), timezones=st.just(UTC)
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
