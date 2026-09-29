"""Stream consumers need byte replies (M1 found crawler2-extract using the frontier client)."""

from __future__ import annotations

from crawler2.core.configuration import RedisSettings
from crawler2.storage.events.consumer import RedisStreamReader, connect_stream_client


def test_stream_client_returns_bytes_that_the_reader_understands() -> None:
    client = connect_stream_client(RedisSettings(host="127.0.0.1", port=1))
    assert client.get_encoder().decode_responses is False
    entry = RedisStreamReader._entry(b"1-0", {b"envelope": b"{}"})
    assert (entry.entry_id, entry.envelope) == ("1-0", b"{}")
