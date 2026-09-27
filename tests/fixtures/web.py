"""Deterministic local fixture web server (plan B.2: no live internet in CI).

P0 provides the mechanism only: a threaded HTTP server on 127.0.0.1 with an
ephemeral port, serving a route table of canned responses. Later phases add
routes (redirects, 304, gzip/brotli, JS pages, HLS, ad iframes, slow and
large responses) without changing how tests obtain the server.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass(frozen=True, slots=True)
class Route:
    status: int = 200
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


class FixtureSite:
    def __init__(self, server: ThreadingHTTPServer) -> None:
        self._server = server

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"


def _handler_for(routes: Mapping[str, Route]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            route = routes.get(self.path, Route(status=404, body=b"not found"))
            self.send_response(route.status)
            for name, value in route.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(route.body)))
            self.end_headers()
            self.wfile.write(route.body)

        def log_message(self, format: str, *args: object) -> None:
            """Keep test output clean; requests are asserted on, not logged."""

    return Handler


@contextmanager
def serve(routes: Mapping[str, Route]) -> Iterator[FixtureSite]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(dict(routes)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield FixtureSite(server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
