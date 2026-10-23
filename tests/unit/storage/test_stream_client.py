"""Stream consumers need byte replies (M1 found crawler2-extract using the frontier client)."""

from __future__ import annotations

from crawler2.core.configuration import RedisSettings
from crawler2.storage.events.consumer import RedisStreamReader, connect_stream_client


def test_stream_client_returns_bytes_that_the_reader_understands() -> None:
    client = connect_stream_client(RedisSettings(host="127.0.0.1", port=1))
    assert client.get_encoder().decode_responses is False
    entry = RedisStreamReader._entry(b"1-0", {b"envelope": b"{}"})
    assert (entry.entry_id, entry.envelope) == ("1-0", b"{}")


def test_every_reader_call_maps_redis_errors_to_a_typed_dependency_failure() -> None:
    """P6 TC-033: a Redis error must reach the consumer loops as StorageError (which they
    retry in-process), never as a raw redis exception that ends the process."""
    import pytest
    import redis

    from crawler2.storage.errors import StorageUnavailableError
    from crawler2.storage.events.consumer import StreamEntry

    class Down:
        def xreadgroup(self, *a: object, **k: object) -> None:
            raise redis.ConnectionError("down")

        xautoclaim = xack = xreadgroup

    reader = RedisStreamReader(Down(), "s", "g", "c")  # type: ignore[arg-type]
    with pytest.raises(StorageUnavailableError):
        reader.read()
    with pytest.raises(StorageUnavailableError):
        reader.claim_stale(min_idle_ms=1)
    with pytest.raises(StorageUnavailableError):
        reader.ack([StreamEntry("1-0", b"{}")])
    reader.ack([])  # nothing to acknowledge: no call, no error
