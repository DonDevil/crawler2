"""P8 fixture media web (loopback only): GET and HEAD, Range, chunked framing, gzip,
mutable content, redirects, stalls. Plus the test seams that let the SSRF guard see
public-looking hostnames while the bytes come from this loopback server.

- ``MediaSite.put(path, body, …)`` serves bytes with ETag/Last-Modified;
  ``honour_range``, ``chunked`` (no Content-Length), ``gzip`` (Content-Encoding
  despite ``identity``), ``status``, ``redirect``, ``stall_s``;
- ``site.replace(path, body)`` changes the bytes (and validators) of a path;
- ``RoutingTransport`` sends every request to the loopback server, keeping the
  original Host header; ``public_resolver`` maps names to public addresses
  (and others to private ones) for the guard.
"""

from __future__ import annotations

import gzip as gzip_lib
import hashlib
import ipaddress
import socket
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

_CHUNK = 64 * 1024


def blob(seed: str, size: int) -> bytes:
    """Deterministic pseudo-random bytes."""
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(f"{seed}:{counter}".encode()).digest() * 64
        counter += 1
    return bytes(out[:size])


def crafted_twin(original: bytes, *, sample: int = 64 * 1024) -> bytes:
    """Same length and identical first/middle/last ``sample`` bytes; different elsewhere."""
    size = len(original)
    assert size >= 5 * sample, "too small to keep three samples and change bytes between them"
    middle = size // 2 - sample // 2
    flip_at = sample + (middle - sample) // 2  # between the head and the middle sample
    twin = bytearray(original)
    for i in range(flip_at, flip_at + 1024):
        twin[i] ^= 0xFF
    return bytes(twin)


@dataclass(frozen=True)
class Resource:
    body: bytes = b""
    content_type: str = "video/mp4"
    status: int = 200
    honour_range: bool = True
    chunked: bool = False
    gzip: bool = False
    redirect: str | None = None
    stall_s: float = 0.0
    version: int = 1
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def etag(self) -> str:
        return '"' + hashlib.sha256(self.body).hexdigest()[:16] + f'-{self.version}"'

    @property
    def last_modified(self) -> str:
        return f"Wed, 0{min(self.version, 9)} Sep 2026 10:00:00 GMT"


class MediaSite:
    def __init__(self, server: ThreadingHTTPServer) -> None:
        self._server = server
        self.resources: dict[str, Resource] = {}
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.bytes_sent: defaultdict[str, int] = defaultdict(int)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def put(self, path: str, body: bytes = b"", **kw: Any) -> None:
        with self.lock:
            self.resources[path] = Resource(body=body, **kw)

    def replace(self, path: str, body: bytes) -> None:
        with self.lock:
            old = self.resources[path]
            self.resources[path] = replace(old, body=body, version=old.version + 1)

    def get(self, path: str) -> Resource | None:
        with self.lock:
            return self.resources.get(path)

    def seen(self, path: str, method: str = "GET") -> list[dict[str, str]]:
        with self.lock:
            return [h for m, p, h in self.requests if p == path and m == method]


class _Handler(BaseHTTPRequestHandler):
    site: MediaSite
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        """Quiet."""

    def do_HEAD(self) -> None:
        self._serve(head=True)

    def do_GET(self) -> None:
        self._serve(head=False)

    def _serve(self, *, head: bool) -> None:
        path = self.path.split("?", 1)[0]
        with self.site.lock:
            self.site.requests.append(
                ("HEAD" if head else "GET", path, {k.lower(): v for k, v in self.headers.items()})
            )
        res = self.site.get(path)
        if res is None:
            self._plain(404, b"not found", head)
            return
        if res.redirect is not None:
            self.send_response(302)
            self.send_header("Location", res.redirect)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if res.status != 200:
            self._plain(res.status, b"no", head)
            return
        body = gzip_lib.compress(res.body, mtime=0) if res.gzip else res.body
        start, end, status = 0, len(body) - 1, 200
        wanted = self.headers.get("Range")
        if wanted and res.honour_range and wanted.startswith("bytes=") and not res.gzip:
            first, _, last = wanted[6:].partition("-")
            start = int(first or 0)
            end = min(int(last) if last else len(body) - 1, len(body) - 1)
            status = 206
        part = body[start : end + 1]
        self.send_response(status)
        self.send_header("Content-Type", res.content_type)
        self.send_header("ETag", res.etag)
        self.send_header("Last-Modified", res.last_modified)
        self.send_header("Accept-Ranges", "bytes" if res.honour_range else "none")
        for name, value in res.extra_headers.items():
            self.send_header(name, value)
        if res.gzip:
            self.send_header("Content-Encoding", "gzip")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
        if res.chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(len(part)))
        self.end_headers()
        if head:
            return
        try:
            for offset in range(0, len(part), _CHUNK):
                piece = part[offset : offset + _CHUNK]
                if res.stall_s:
                    time.sleep(res.stall_s)
                if res.chunked:
                    self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                else:
                    self.wfile.write(piece)
                with self.site.lock:
                    self.site.bytes_sent[path] += len(piece)
            if res.chunked:
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            return

    def _plain(self, status: int, body: bytes, head: bool) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


@contextmanager
def media_site(host: str = "127.0.0.1", port: int = 0) -> Iterator[MediaSite]:
    handler: Any = type("H", (_Handler,), {})
    server = _Server((host, port), handler)
    site = MediaSite(server)
    handler.site = site
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


PUBLIC = "93.184.216.34"
PRIVATE = "10.20.30.40"


def public_resolver(private: tuple[str, ...] = ("internal.example",)) -> Any:
    """Every name resolves to a public address, except ``private`` ones (10.20.30.40)
    and literal IPs (themselves). No real DNS."""

    def resolve(hostname: str, port: int) -> list[tuple[Any, ...]]:
        if hostname == "nxdomain.example":
            raise socket.gaierror("unmapped test hostname")
        try:
            ip = str(ipaddress.ip_address(hostname))
        except ValueError:
            ip = PRIVATE if hostname in private else PUBLIC
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sockaddr = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
        return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]

    return resolve


class RoutingTransport(httpx.AsyncBaseTransport):
    """Delivers every request to the loopback site (the Host header keeps the name).
    Requests to ``dead.example`` go to a closed port (connection refused)."""

    def __init__(self, site: MediaSite) -> None:
        self._site = site
        self._inner = httpx.AsyncHTTPTransport()
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        port = 9 if request.url.host == "dead.example" else self._site.port
        routed = request.url.copy_with(scheme="http", host="127.0.0.1", port=port)
        request.url = routed
        request.headers["Host"] = request.headers.get("Host", "")
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class NeverCalled(httpx.AsyncBaseTransport):
    """Fails the test if a request gets past the guard."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"request past the SSRF guard: {request.url}")
