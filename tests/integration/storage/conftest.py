"""Real Scylla/MinIO/Redis fixtures. Run inside an app container (P0: host Python
cannot reach Scylla); each host gets its own throwaway keyspace and stream prefix."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import redis

from crawler2.core.configuration import EventSettings, Settings
from crawler2.storage.objectstore import S3ObjectStore
from crawler2.storage.scylla import Migrator, ScyllaSession, ScyllaStorage


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
def keyspace(settings: Settings) -> str:
    return f"crawler2_it_{settings.host_id.replace('-', '_')}"


@pytest.fixture(scope="session")
def storage(settings: Settings, keyspace: str) -> Iterator[ScyllaStorage]:
    session = ScyllaSession.connect(settings.scylla, keyspace=keyspace)
    # A test-owned keyspace: recreated per session so every run starts fresh.
    session.execute_raw(f"DROP KEYSPACE IF EXISTS {keyspace}")
    Migrator(session, settings.scylla, applied_by=f"{settings.host_id}:it").migrate()
    yield ScyllaStorage.over(session)
    session.close()


@pytest.fixture(scope="session")
def object_store(
    settings: Settings, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[S3ObjectStore]:
    store = S3ObjectStore(
        settings.minio,
        scratch_dir=Path(tmp_path_factory.mktemp("spool")),
        bucket=f"crawler2-it-{settings.host_id}",
    )
    store.ensure_bucket()
    yield store
    store.close()


@pytest.fixture
def redis_client(settings: Settings) -> Iterator[redis.Redis]:
    client = redis.Redis(host=settings.redis.host, port=settings.redis.port, db=settings.redis.db)
    yield client
    client.close()


@pytest.fixture
def event_settings(redis_client: redis.Redis) -> Iterator[EventSettings]:
    """A private stream namespace per test; its streams are deleted afterwards."""
    prefix = f"it-{uuid.uuid4().hex[:12]}:"
    yield EventSettings(stream_prefix=prefix)
    for key in redis_client.scan_iter(match=f"{prefix}*"):
        redis_client.delete(key)
