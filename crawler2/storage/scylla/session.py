"""Connection, prepared statements and consistency levels (ADR-012 §6).

One ``ScyllaSession`` per process. Statements are prepared once with an
explicit consistency level; nothing in a repository chooses a level ad hoc.
Driver errors that mean "backend unavailable" become
``StorageUnavailableError``; everything else propagates.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from cassandra import (
    ConsistencyLevel,
    OperationTimedOut,
    ReadTimeout,
    Unavailable,
    WriteTimeout,
)
from cassandra.auth import PlainTextAuthProvider
from cassandra.cluster import (
    EXEC_PROFILE_DEFAULT,
    Cluster,
    ExecutionProfile,
    NoHostAvailable,
    Session,
)
from cassandra.concurrent import execute_concurrent
from cassandra.policies import DCAwareRoundRobinPolicy, TokenAwarePolicy
from cassandra.query import BatchStatement, BatchType, BoundStatement, PreparedStatement

from crawler2.core.configuration import ScyllaSettings
from crawler2.storage.errors import StorageUnavailableError

_DDL_TIMEOUT_S = 120.0
_CONCURRENCY = 32
"""In-flight statements per ``execute_many`` call."""
_CONCURRENT_CALLS = 8
"""``execute_many`` calls in flight per session: caps client in-flight requests at
8 x 32 = 256, well below the driver's per-connection limit (ConnectionBusy)."""

UNAVAILABLE_ERRORS: tuple[type[Exception], ...] = (
    NoHostAvailable,
    OperationTimedOut,
    Unavailable,
    ReadTimeout,
    WriteTimeout,
)


class Consistency(Enum):
    """Named consistency choices; identical meaning at RF=1 and RF=3 (ADR-002)."""

    AUTHORITATIVE_WRITE = ConsistencyLevel.LOCAL_QUORUM
    """Authoritative rows and the outbox: survive the loss of one replica once acked."""
    DERIVED_WRITE = ConsistencyLevel.LOCAL_ONE
    """Rebuildable rows (projections, read models); a lost write is repaired by replay."""
    EVENTUAL_READ = ConsistencyLevel.LOCAL_ONE
    """Access patterns marked 'EC ok' in the P1 catalog."""
    STRONG_READ = ConsistencyLevel.LOCAL_QUORUM
    """Read-after-write (W7, E3, outbox relay, schema history)."""
    SERIAL = ConsistencyLevel.LOCAL_SERIAL
    """Reads that must observe completed LWT (E1, E2)."""


def utc(value: datetime) -> datetime:
    """The driver returns naive UTC datetimes; contracts require aware ones."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ScyllaSession:
    def __init__(self, cluster: Cluster, session: Session, keyspace: str) -> None:
        self._cluster = cluster
        self._session = session
        self.keyspace = keyspace
        self._prepared: dict[tuple[str, Consistency], PreparedStatement] = {}
        self._fanout = threading.BoundedSemaphore(_CONCURRENT_CALLS)

    @classmethod
    def connect(cls, settings: ScyllaSettings, *, keyspace: str | None = None) -> ScyllaSession:
        auth = None
        if settings.username is not None:
            password = settings.password.get_secret_value() if settings.password else ""
            auth = PlainTextAuthProvider(username=settings.username, password=password)
        profile = ExecutionProfile(
            load_balancing_policy=TokenAwarePolicy(
                DCAwareRoundRobinPolicy(local_dc=settings.local_dc)
            ),
            request_timeout=settings.request_timeout_s,
        )
        cluster = Cluster(
            contact_points=settings.contact_points,
            port=settings.port,
            execution_profiles={EXEC_PROFILE_DEFAULT: profile},
            connect_timeout=settings.connect_timeout_s,
            protocol_version=4,
            auth_provider=auth,
        )
        try:
            session = cluster.connect()
        except UNAVAILABLE_ERRORS as exc:
            cluster.shutdown()
            raise StorageUnavailableError(f"cannot connect to Scylla: {exc}") from exc
        return cls(cluster, session, keyspace or settings.keyspace)

    def close(self) -> None:
        self._cluster.shutdown()

    def __enter__(self) -> ScyllaSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- statements ---------------------------------------------------------

    def prepare(self, cql: str, consistency: Consistency) -> PreparedStatement:
        """Prepare ``cql`` (``{ks}`` = this session's keyspace) once per consistency."""
        key = (cql, consistency)
        statement = self._prepared.get(key)
        if statement is None:
            statement = self._run(lambda: self._session.prepare(cql.format(ks=self.keyspace)))
            if consistency is Consistency.SERIAL:
                statement.consistency_level = ConsistencyLevel.LOCAL_QUORUM
                statement.serial_consistency_level = ConsistencyLevel.LOCAL_SERIAL
            else:
                statement.consistency_level = consistency.value
                statement.serial_consistency_level = ConsistencyLevel.LOCAL_SERIAL
            self._prepared[key] = statement
        return statement

    def bind(self, cql: str, consistency: Consistency, values: Sequence[Any]) -> BoundStatement:
        return self.prepare(cql, consistency).bind(values)

    def execute(self, statement: BoundStatement | BatchStatement) -> list[Any]:
        return list(self._run(lambda: self._session.execute(statement)))

    def execute_raw(self, cql: str) -> list[Any]:
        """Unprepared statement (DDL and schema tooling only)."""
        # DDL waits for schema agreement (and a snapshot on DROP): allow longer.
        return list(self._run(lambda: self._session.execute(cql, timeout=_DDL_TIMEOUT_S)))

    def execute_all(self, statements: Iterable[BoundStatement | BatchStatement]) -> None:
        """Run independent writes concurrently (bounded in-flight); raise on any failure."""
        self.execute_many(statements)

    def execute_many(
        self, statements: Iterable[BoundStatement | BatchStatement]
    ) -> list[list[Any]]:
        """Run independent statements concurrently; results in input order."""
        pairs = [(statement, None) for statement in statements]
        if not pairs:
            return []
        with self._fanout:
            results = self._run(
                lambda: execute_concurrent(
                    self._session,
                    pairs,
                    concurrency=min(_CONCURRENCY, len(pairs)),
                    raise_on_first_error=True,
                )
            )
        return [list(result.result_or_exc) for result in results]

    def stream(self, statement: BoundStatement, *, fetch_size: int = 1000) -> Iterator[Any]:
        """Iterate a large result with driver paging (never materialized at once)."""
        statement.fetch_size = fetch_size
        result = self._run(lambda: self._session.execute(statement))
        iterator = iter(result)
        while True:
            try:
                row = next(iterator)
            except StopIteration:
                return
            except UNAVAILABLE_ERRORS as exc:
                raise StorageUnavailableError(f"{type(exc).__name__}: {exc}") from exc
            yield row

    def logged_batch(
        self, statements: Iterable[BoundStatement], consistency: Consistency
    ) -> BatchStatement:
        """All-or-nothing (eventually) across partitions via the batchlog. Not isolated."""
        batch = BatchStatement(batch_type=BatchType.LOGGED, consistency_level=consistency.value)
        for statement in statements:
            batch.add(statement)
        return batch

    def partition_batch(
        self, statements: Iterable[BoundStatement], consistency: Consistency
    ) -> BatchStatement:
        """Unlogged batch of rows of ONE partition: applied as one atomic mutation."""
        batch = BatchStatement(batch_type=BatchType.UNLOGGED, consistency_level=consistency.value)
        for statement in statements:
            batch.add(statement)
        return batch

    @staticmethod
    def _run[T](call: Callable[[], T]) -> T:
        try:
            result = call()
        except UNAVAILABLE_ERRORS as exc:
            raise StorageUnavailableError(f"{type(exc).__name__}: {exc}") from exc
        return result
