"""Read-only row sources for the exporter (design §4).

``ScyllaRowSource`` scans one table in ``of`` disjoint token ranges over its
partition key (``token(pk) >= lo AND token(pk) <= hi``), paged by the driver at
LOCAL_QUORUM. The ranges cover the whole Murmur3 ring, so a scan returns every
row exactly once *for the instant each page is read*; it is not a snapshot
(Scylla has none across partitions or tables). It issues ``SELECT`` statements
only.

``MemoryRowSource`` is the deterministic stand-in for tests: rows are assigned
to segments by a stable hash of their partition key and yielded in pages, and
faults can be injected per segment.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from crawler2.analytics.datasets import SourceTable

MIN_TOKEN, MAX_TOKEN = -(2**63), 2**63 - 1


def token_ranges(of: int) -> list[tuple[int, int]]:
    """``of`` disjoint, inclusive ranges covering [MIN_TOKEN, MAX_TOKEN]."""
    if of < 1:
        raise ValueError("segments must be >= 1")
    span = MAX_TOKEN - MIN_TOKEN + 1
    bounds = [MIN_TOKEN + span * i // of for i in range(of)] + [MAX_TOKEN + 1]
    return [(bounds[i], bounds[i + 1] - 1) for i in range(of)]


class RowSource(Protocol):
    @property
    def label(self) -> str:
        """Where the rows come from (e.g. ``scylla:crawler2_m1``); part of the export scope."""
        ...

    def available(self, table: str) -> bool: ...

    def scan(self, source: SourceTable, segment: int, of: int) -> Iterator[Mapping[str, Any]]: ...

    def count(self, source: SourceTable, segment: int, of: int) -> int:
        """Rows in the segment now (an independent re-count; same non-snapshot semantics)."""
        ...


class ScyllaRowSource:
    def __init__(self, session: Any, *, keyspace: str | None = None) -> None:
        """``session`` is a ``ScyllaSession``; ``keyspace`` defaults to its own."""
        self._session = session
        self._keyspace = keyspace or session.keyspace

    @property
    def label(self) -> str:
        return f"scylla:{self._keyspace}"

    def available(self, table: str) -> bool:
        from crawler2.storage.scylla.session import Consistency

        rows = self._session.execute(
            self._session.bind(
                "SELECT table_name FROM system_schema.tables "
                "WHERE keyspace_name = ? AND table_name = ?",
                Consistency.EVENTUAL_READ,
                (self._keyspace, table),
            )
        )
        return bool(rows)

    def _ranged(self, source: SourceTable, select: str, segment: int, of: int) -> Any:
        from crawler2.storage.scylla.session import Consistency

        pk = ", ".join(source.partition_key)
        lo, hi = token_ranges(of)[segment]
        cql = (
            f"SELECT {select} FROM {self._keyspace}.{source.table} "
            f"WHERE token({pk}) >= ? AND token({pk}) <= ?"
        )
        return self._session.bind(cql, Consistency.STRONG_READ, (lo, hi))

    def scan(self, source: SourceTable, segment: int, of: int) -> Iterator[Mapping[str, Any]]:
        statement = self._ranged(source, ", ".join(source.columns), segment, of)
        for row in self._session.stream(statement, fetch_size=source.fetch_size):
            yield row._asdict()

    def count(self, source: SourceTable, segment: int, of: int) -> int:
        rows = self._session.execute(self._ranged(source, "count(*)", segment, of))
        return int(rows[0][0])


@dataclass
class MemoryRowSource:
    """Tables as lists of dict rows (tests and fixtures; never a real store)."""

    tables: dict[str, list[dict[str, Any]]]
    page_size: int = 2
    name: str = "memory:fixture"
    faults: dict[tuple[str, int], int] = field(default_factory=dict)
    """(table, segment) → how many more scans of it fail mid-way."""
    pages_served: int = 0

    @property
    def label(self) -> str:
        return self.name

    def available(self, table: str) -> bool:
        return table in self.tables

    def _segment_of(self, source: SourceTable, row: Mapping[str, Any], of: int) -> int:
        key = "|".join(str(row.get(k)) for k in source.partition_key)
        return zlib.crc32(key.encode()) % of

    def _rows(self, source: SourceTable, segment: int, of: int) -> Sequence[dict[str, Any]]:
        return [r for r in self.tables[source.table] if self._segment_of(source, r, of) == segment]

    def scan(self, source: SourceTable, segment: int, of: int) -> Iterator[Mapping[str, Any]]:
        from crawler2.storage.errors import StorageUnavailableError

        rows = self._rows(source, segment, of)
        for start in range(0, len(rows), self.page_size):
            self.pages_served += 1
            if start > 0 and self.faults.get((source.table, segment), 0) > 0:
                self.faults[(source.table, segment)] -= 1
                raise StorageUnavailableError(f"injected fault in {source.table}[{segment}]")
            for row in rows[start : start + self.page_size]:
                yield {c: row.get(c) for c in source.columns}

    def count(self, source: SourceTable, segment: int, of: int) -> int:
        return len(self._rows(source, segment, of))
