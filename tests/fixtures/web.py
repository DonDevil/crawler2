"""Deterministic local fixture web server (plan B.2: no live internet in CI).

A threaded HTTP server on 127.0.0.1 with an ephemeral port, serving a
route table. P0 routes are canned responses (``Route``); P4 adds dynamic
routes (``Dynamic``) that own the socket, for redirects with state,
conditional requests, streaming, slow and malformed responses. The site
logs every request (path + headers) and counts body bytes written per
path, so tests can assert on what a client actually asked for and pulled.
"""

from __future__ import annotations

import contextlib
import threading
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass(frozen=True, slots=True)
class Route:
    status: int = 200
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Dynamic:
    """A route that writes its own response through the handler."""

    handle: Callable[[FixtureHandler], None]


@dataclass(frozen=True, slots=True)
class SeenRequest:
    path: str
    headers: Mapping[str, str]


class FixtureState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[SeenRequest] = []
        self.bytes_sent: defaultdict[str, int] = defaultdict(int)

    def requests_to(self, path: str) -> list[SeenRequest]:
        with self.lock:
            return [r for r in self.requests if r.path == path]


class FixtureHandler(BaseHTTPRequestHandler):
    routes: Mapping[str, Route | Dynamic]
    state: FixtureState

    def do_GET(self) -> None:
        with self.state.lock:
            self.state.requests.append(
                SeenRequest(self.path, {k.lower(): v for k, v in self.headers.items()})
            )
        route = self.routes.get(self.path) or self.routes.get(self.path.split("?", 1)[0])
        if isinstance(route, Dynamic):
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                route.handle(self)
            return
        route = route or Route(status=404, body=b"not found")
        self.send_response(route.status)
        for name, value in route.headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(route.body)))
        self.end_headers()
        self.write_body(route.body)

    def write_body(self, data: bytes) -> None:
        """Write and account body bytes against this request's path."""
        self.wfile.write(data)
        with self.state.lock:
            self.state.bytes_sent[self.path] += len(data)

    def log_message(self, format: str, *args: object) -> None:
        """Keep test output clean; requests are asserted on, not logged."""


class FixtureSite:
    def __init__(self, server: ThreadingHTTPServer, state: FixtureState) -> None:
        self._server = server
        self.state = state

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"


def _handler_for(
    routes: Mapping[str, Route | Dynamic], state: FixtureState
) -> type[BaseHTTPRequestHandler]:
    return type("Handler", (FixtureHandler,), {"routes": dict(routes), "state": state})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


@contextmanager
def serve(routes: Mapping[str, Route | Dynamic]) -> Iterator[FixtureSite]:
    state = FixtureState()
    server = _Server(("127.0.0.1", 0), _handler_for(routes, state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield FixtureSite(server, state)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
