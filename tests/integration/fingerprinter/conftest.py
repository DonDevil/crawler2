"""Cross-service fixtures (P9-D19): crawler2's own storage fixtures; the
fingerprinter runs as a separate process from its own repository."""

from tests.integration.storage.conftest import (  # noqa: F401 - re-exported fixtures
    event_settings,
    keyspace,
    redis_client,
    settings,
    storage,
)
