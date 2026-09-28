"""Schema bootstrap, idempotency, versioning and verification against real Scylla."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from crawler2.core.configuration import Settings
from crawler2.storage.errors import SchemaError
from crawler2.storage.scylla import Migrator, ScyllaSession, ScyllaStorage
from tests.unit.storage.test_migrations import EXPECTED_TABLES

pytestmark = pytest.mark.integration

TTL_30D, TTL_14D = 2_592_000, 1_209_600
EXPECTED_TTL = {
    "fetch_attempts_by_domain_day": TTL_30D,
    "fetch_attempts_by_url": TTL_30D,
    "observations_by_domain_day": TTL_30D,
    "outbox": TTL_14D,
    "processed_events": TTL_14D,
}


@pytest.fixture
def fresh(settings: Settings) -> Iterator[tuple[ScyllaSession, Migrator]]:
    keyspace = f"crawler2_schema_{uuid.uuid4().hex[:8]}"
    session = ScyllaSession.connect(settings.scylla, keyspace=keyspace)
    yield session, Migrator(session, settings.scylla, applied_by="it:schema")
    session.execute_raw(f"DROP KEYSPACE IF EXISTS {keyspace}")
    session.close()


def test_fresh_init_is_complete_and_rerun_is_a_noop(
    fresh: tuple[ScyllaSession, Migrator],
) -> None:
    session, migrator = fresh
    before = migrator.status()
    assert not before.keyspace_exists
    assert [m.version for m in before.pending] == [1]

    assert migrator.migrate() == (1,)
    status = migrator.status()
    assert status.up_to_date
    assert status.current_version == 1
    assert status.applied[0].checksum == migrator.migrations[0].checksum

    assert migrator.migrate() == ()  # idempotent
    assert migrator.status().applied == status.applied  # history not rewritten

    tables = {
        r.table_name: r
        for r in session.execute_raw(
            "SELECT table_name, default_time_to_live, compaction FROM system_schema.tables "
            f"WHERE keyspace_name = '{session.keyspace}'"
        )
    }
    assert set(EXPECTED_TABLES) | {"schema_migrations", "schema_lock"} == set(tables)
    for name, row in tables.items():
        assert row.default_time_to_live == EXPECTED_TTL.get(name, 0), name
        strategy = row.compaction["class"]
        expected = "TimeWindow" if name in EXPECTED_TTL else "SizeTiered"
        assert expected in strategy, (name, strategy)
    ScyllaStorage.over(session)  # repositories prepare against the migrated schema
    Migrator(session, Settings().scylla, applied_by="x").require_current()


def test_repositories_refuse_an_unmigrated_keyspace(settings: Settings) -> None:
    with pytest.raises(SchemaError, match="not at schema"):
        ScyllaStorage.open(settings.scylla, keyspace="crawler2_absent", instance="it")


def test_tampered_history_is_detected_and_blocks_migrate(
    fresh: tuple[ScyllaSession, Migrator],
) -> None:
    session, migrator = fresh
    migrator.migrate()
    session.execute_raw(
        f"UPDATE {session.keyspace}.schema_migrations SET checksum = 'edited' WHERE version = 1"
    )
    status = migrator.status()
    assert not status.up_to_date
    assert any("changed after it was applied" in p for p in status.problems)
    with pytest.raises(SchemaError, match="changed after it was applied"):
        migrator.migrate()


def test_tablet_keyspaces_are_flagged(settings: Settings) -> None:
    keyspace = f"crawler2_tablets_{uuid.uuid4().hex[:8]}"
    with ScyllaSession.connect(settings.scylla, keyspace=keyspace) as session:
        session.execute_raw(
            f"CREATE KEYSPACE {keyspace} WITH replication = "
            f"{{'class': 'NetworkTopologyStrategy', '{settings.scylla.local_dc}': 1}}"
        )
        try:
            status = Migrator(session, settings.scylla, applied_by="it").status()
            assert any("tablets" in p for p in status.problems)
        finally:
            session.execute_raw(f"DROP KEYSPACE {keyspace}")


def test_concurrent_migrators_are_serialized(fresh: tuple[ScyllaSession, Migrator]) -> None:
    session, _ = fresh
    settings = Settings().scylla
    first = Migrator(session, settings, applied_by="host-a")
    second = Migrator(session, settings, applied_by="host-b")
    first._bootstrap()
    first._acquire_lock(1)
    with pytest.raises(SchemaError, match="in progress by host-a"):
        second.migrate(lock_wait_s=1)
    first._release_lock()
    assert second.migrate(lock_wait_s=1) == (1,)
