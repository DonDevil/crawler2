"""Partition layout: time buckets, hash shards and convergent cell timestamps.

These functions *are* the physical data model (ADR-012): the repositories
and the partition benchmark both call them, so the benchmark measures the
layout that production uses. Changing a shard count or bucket width changes
where existing rows live and therefore requires a migration, not an edit.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Final

from antipiracy_contracts.ids import TypedId

# Fixed shard counts per table family (sized by the P2 partition benchmark).
DOMAIN_DAY_SHARDS: Final = 16
"""fetch_attempts_by_domain_day (W2), observations_by_domain_day (W14)."""
URLS_BY_DOMAIN_SHARDS: Final = 64
"""urls_by_domain (W11): a large site can have 10^7 known URLs."""
INLINK_SHARDS: Final = 32
"""inlinks_by_url (W10): hub pages have 10^6+ linking pages."""
MEDIA_MONTH_SHARDS: Final = 16
"""media_observations_by_media (M3): a popular file is embedded on many pages."""
CONTENT_SHARDS: Final = 8
"""media_by_content (M5): the same bytes at many locators."""
MATCH_MONTH_SHARDS: Final = 16
"""matches_by_target (P4), matches_by_domain (P5), evidence_by_target (E4)."""
OUTBOX_SHARDS: Final = 32
"""outbox (X1): one minute of events over 32 partitions (≤ ~30k events/s cluster-wide
keeps every partition under 100 MB; see p02-storage/benchmarks.md)."""
OUTBOX_BUCKET_SECONDS: Final = 60

TARGETS_BUCKET: Final = 0
"""targets (P2): 10^2-10^4 rows in total fit one listing partition."""

_MIRROR_ANCHOR: Final = datetime(2020, 1, 1, tzinfo=UTC)
"""earliest_wins mirrors instants around this anchor (valid for instants before 2070)."""


def shard_of(identifier: TypedId, shards: int) -> int:
    """Stable shard of an ID. Every ID's low 62 bits are hash or random output (ADR-008)."""
    return identifier.uuid.int % shards


def day_bucket(at: datetime) -> int:
    """``YYYYMMDD`` of the UTC day, readable in cqlsh."""
    utc = at.astimezone(UTC)
    return utc.year * 10_000 + utc.month * 100 + utc.day


def month_bucket(at: datetime) -> int:
    """``YYYYMM`` of the UTC month."""
    utc = at.astimezone(UTC)
    return utc.year * 100 + utc.month


def days_back(newest: datetime, days: int) -> Iterator[int]:
    """Day buckets from ``newest`` backwards, ``days`` of them."""
    start = newest.astimezone(UTC).date()
    for offset in range(days):
        day: date = start - timedelta(days=offset)
        yield day.year * 10_000 + day.month * 100 + day.day


def months_back(newest: datetime, oldest: datetime) -> Iterator[int]:
    """Month buckets from ``newest`` back to ``oldest`` inclusive (newest first)."""
    year, month = newest.astimezone(UTC).year, newest.astimezone(UTC).month
    stop = month_bucket(oldest)
    while year * 100 + month >= stop:
        yield year * 100 + month
        month -= 1
        if month == 0:
            year, month = year - 1, 12


def outbox_bucket(at: datetime) -> int:
    """Minutes since the Unix epoch."""
    return int(at.timestamp()) // OUTBOX_BUCKET_SECONDS


def bucket_start(bucket: int) -> datetime:
    return datetime.fromtimestamp(bucket * OUTBOX_BUCKET_SECONDS, tz=UTC)


# --- Convergent cell timestamps ---------------------------------------------
#
# Scylla resolves concurrent writes to one cell by write timestamp (highest
# wins; equal timestamps by value). Setting the timestamp from the *fact*
# instead of the arrival time makes out-of-order, duplicated and concurrent
# deliveries converge to the same state without LWT (ADR-012 §4).


def _micros(at: datetime) -> int:
    utc = at.astimezone(UTC)
    return int((utc - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1))


def latest_wins(at: datetime) -> int:
    """Write timestamp so that the cell keeps the value with the greatest ``at``."""
    return _micros(at)


def earliest_wins(at: datetime) -> int:
    """Write timestamp so that the cell keeps the value with the smallest ``at``.

    ``2*anchor - at``: strictly decreasing in ``at`` and always in the past.
    (Scylla rejects timestamps more than 3 days in the future —
    ``restrict_future_timestamp`` — so the classic ``MAX - at`` is unusable.)
    Only earliest_wins writes may touch such a cell.
    """
    mirrored = 2 * _micros(_MIRROR_ANCHOR) - _micros(at)
    if mirrored <= 0:
        raise ValueError(f"instant outside the earliest_wins range (before 2070): {at}")
    return mirrored


def version_wins(version: int) -> int:
    """Write timestamp so that the cell keeps the value of the highest version."""
    if version < 1:
        raise ValueError(f"version must be >= 1: {version}")
    return version
