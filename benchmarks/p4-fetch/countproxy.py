"""Byte-counting HTTP forward proxy for the P4 engine evaluation (stdlib only).

Every engine under test (V1 and V2, HTTP clients and browsers) is pointed
at this proxy, so "bytes" has one definition for all of them: bytes read
from upstream sockets (response direction, on the wire, TLS included for
CONNECT tunnels). V1 cannot report sizes itself (P0 baseline limitation 3).

Plain-HTTP requests are forwarded with ``Connection: close`` so each proxy
connection carries exactly one exchange; CONNECT tunnels are piped.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_HEAD_LIMIT = 64 * 1024


@dataclass
class Counters:
    upstream_bytes: int = 0
    connections: int = 0
    errors: int = 0
    hosts: set[str] = field(default_factory=set)

    def snapshot(self) -> tuple[int, int]:
        return self.upstream_bytes, self.connections


async def _pipe(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, counters: Counters | None
) -> None:
    try:
        while chunk := await reader.read(65536):
            if counters is not None:
                counters.upstream_bytes += len(chunk)
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


class CountingProxy:
    def __init__(self, connect_timeout_s: float = 20.0) -> None:
        self.counters = Counters()
        self._connect_timeout = connect_timeout_s
        self._server: asyncio.Server | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("proxy not started")
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 5)

    async def _open(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        self.counters.connections += 1
        self.counters.hosts.add(host)
        return await asyncio.wait_for(
            asyncio.open_connection(host, port, happy_eyeballs_delay=0.25), self._connect_timeout
        )

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, OSError):
            writer.close()
            return
        if len(head) > _HEAD_LIMIT:
            writer.close()
            return
        request_line, _, rest = head.partition(b"\r\n")
        try:
            method, target, version = request_line.decode("latin-1").split(" ", 2)
        except ValueError:
            writer.close()
            return
        try:
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                up_r, up_w = await self._open(host.strip("[]"), int(port))
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:
                parts = urlsplit(target)
                host = parts.hostname or ""
                port = parts.port or 80
                path = parts.path or "/"
                if parts.query:
                    path += "?" + parts.query
                headers = [
                    line
                    for line in rest.split(b"\r\n")
                    if line and not line.lower().startswith((b"proxy-connection:", b"connection:"))
                ]
                up_r, up_w = await self._open(host, port)
                out = f"{method} {path} {version}\r\n".encode("latin-1")
                up_w.write(out + b"\r\n".join([*headers, b"Connection: close"]) + b"\r\n\r\n")
                await up_w.drain()
        except Exception:  # noqa: BLE001 -- any upstream failure is a 502 to the client
            self.counters.errors += 1
            with contextlib.suppress(Exception):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
            writer.close()
            return
        await asyncio.gather(_pipe(reader, up_w, None), _pipe(up_r, writer, self.counters))
