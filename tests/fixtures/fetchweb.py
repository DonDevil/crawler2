"""The P4 fixture web: every behaviour the fetcher contract suite needs (design §23).

All routes are deterministic and loopback-only. Large bodies are generated
while streaming (never materialised), and ``FixtureState.bytes_sent``
records how much of them a client actually pulled.
"""

from __future__ import annotations

import contextlib
import gzip
import select
import socket
import struct
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import brotli

from tests.fixtures.web import Dynamic, FixtureHandler, Route

HUGE_MEDIA_BYTES = 1024**3
"""1 GiB: a full download would be unmistakable."""
ETAG = '"fixture-v1"'
LAST_MODIFIED = "Wed, 01 Jul 2026 10:00:00 GMT"
_CHUNK = 64 * 1024


def _html(title: str, links: int = 5, extra: str = "") -> bytes:
    anchors = "".join(f'<a href="/p/{i}">link {i}</a>\n' for i in range(links))
    return (
        f"<!doctype html><html><head><title>{title}</title></head>"
        f"<body><h1>{title}</h1>{anchors}{extra}</body></html>"
    ).encode()


OK_BODY = _html("fixture ok")
JS_ONLY = (
    b'<!doctype html><html><head><title>app</title></head><body><div id="root"></div>'
    b"<noscript>Please enable JavaScript to view this site.</noscript>"
    b"<script>document.getElementById('root').innerHTML="
    b"'<h1>rendered-by-js</h1>' + [0,1,2,3].map(function(i){"
    b"return '<a href=\"/js/'+i+'\">js '+i+'</a>'}).join('');</script></body></html>"
)
CAPTCHA_BODY = (
    b"<html><head><title>Are you not a robot?</title></head><body>"
    b'<form action="/verify"><div class="g-recaptcha" data-sitekey="x"></div></form>'
    b"</body></html>"
)
CHALLENGE_BODY = (
    b"<html><head><title>Just a moment...</title></head><body>"
    b'<div id="cf-browser-verification">Checking your browser</div></body></html>'
)
MANIFEST = (
    b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\nlow/index.m3u8\n"
    b"#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720\nhigh/index.m3u8\n"
)
MP4_HEAD = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41"


def _send(h: FixtureHandler, status: int, headers: dict[str, str], body: bytes) -> None:
    h.send_response(status)
    for name, value in headers.items():
        h.send_header(name, value)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.write_body(body)


def _redirect(to: str, status: int = 302) -> Route:
    return Route(status=status, headers={"Location": to})


def _conditional(h: FixtureHandler) -> None:
    if h.headers.get("If-None-Match") == ETAG or h.headers.get("If-Modified-Since") == (
        LAST_MODIFIED
    ):
        h.send_response(304)
        h.send_header("ETag", ETAG)
        h.end_headers()
        return
    _send(
        h,
        200,
        {"Content-Type": "text/html", "ETag": ETAG, "Last-Modified": LAST_MODIFIED},
        _html("versioned page"),
    )


def _stream_media(h: FixtureHandler, *, honour_range: bool, content_type: str) -> None:
    """A 1 GiB video, generated on the fly; stops when the client goes away."""
    start, end = 0, HUGE_MEDIA_BYTES - 1
    requested = h.headers.get("Range")
    status = 200
    if honour_range and requested and requested.startswith("bytes="):
        first, _, last = requested[6:].partition("-")
        start = int(first or 0)
        end = min(int(last), end) if last else end
        status = 206
    h.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
    h.send_response(status)
    h.send_header("Content-Type", content_type)
    h.send_header("Accept-Ranges", "bytes" if honour_range else "none")
    h.send_header("Content-Length", str(end - start + 1))
    if status == 206:
        h.send_header("Content-Range", f"bytes {start}-{end}/{HUGE_MEDIA_BYTES}")
    h.send_header("ETag", '"media-1"')
    h.end_headers()
    remaining = end - start + 1
    first_chunk = True
    while remaining > 0:
        size = min(_CHUNK, remaining)
        chunk = (MP4_HEAD + b"\x00" * (size - len(MP4_HEAD))) if first_chunk else b"\x00" * size
        first_chunk = False
        h.write_body(chunk)
        remaining -= size


def _slowloris(h: FixtureHandler) -> None:
    h.send_response(200)
    h.send_header("Content-Type", "text/html")
    h.send_header("Content-Length", "100000")
    h.end_headers()
    for _ in range(600):  # one byte per 0.5 s: never idle long enough for a read timeout
        h.write_body(b"x")
        h.wfile.flush()
        time.sleep(0.5)


def _stall(h: FixtureHandler) -> None:
    time.sleep(60)


def _slow_page(h: FixtureHandler) -> None:
    """Headers and the start of a page, then nothing for a long time (browser chaos)."""
    h.send_response(200)
    h.send_header("Content-Type", "text/html")
    h.end_headers()
    h.write_body(b"<html><head><title>slow</title></head><body><p>loading")
    h.wfile.flush()
    time.sleep(60)


def _malformed(h: FixtureHandler) -> None:
    h.wfile.write(b"HTTP/1.1 twohundred OK\r\nContent-Type text/html\r\n\r\n<html>")


def _truncated(h: FixtureHandler) -> None:
    h.send_response(200)
    h.send_header("Content-Type", "text/html")
    h.send_header("Content-Length", "5000")
    h.end_headers()
    h.write_body(b"<html><body>only a little")
    h.close_connection = True


def _reset(h: FixtureHandler) -> None:
    h.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    h.connection.close()


def _large_html(h: FixtureHandler) -> None:
    size = 6 * 1024 * 1024
    h.send_response(200)
    h.send_header("Content-Type", "text/html")
    h.send_header("Content-Length", str(size))
    h.end_headers()
    sent = 0
    while sent < size:
        n = min(_CHUNK, size - sent)
        h.write_body(b"a" * n)
        sent += n


def _cookie_set(h: FixtureHandler) -> None:
    h.send_response(302)
    h.send_header("Location", "/cookie/check")
    h.send_header("Set-Cookie", "session=abc; Path=/")
    h.end_headers()


def _cookie_check(h: FixtureHandler) -> None:
    _send(h, 200, {"Content-Type": "text/html"}, _html("cookie " + h.headers.get("Cookie", "")))


def pages(count: int) -> dict[str, Route]:
    """Distinct small pages for pool/leak tests."""
    return {
        f"/page/{i}": Route(headers={"Content-Type": "text/html"}, body=_html(f"page {i}", 4))
        for i in range(count)
    }


def fetch_routes() -> dict[str, Route | Dynamic]:
    html = {"Content-Type": "text/html; charset=utf-8"}
    bomb = gzip.compress(b"\0" * (64 * 1024 * 1024), compresslevel=9)
    return {
        "/ok": Route(headers=html, body=OK_BODY),
        "/redirect": _redirect("/ok"),
        "/chain/1": _redirect("/chain/2", 301),
        "/chain/2": _redirect("/chain/3", 302),
        "/chain/3": _redirect("/ok", 307),
        "/loop/a": _redirect("/loop/b"),
        "/loop/b": _redirect("/loop/a"),
        "/redirect-ftp": _redirect("ftp://127.0.0.1/file"),
        "/etag": Dynamic(_conditional),
        "/gzip": Route(
            headers={**html, "Content-Encoding": "gzip"}, body=gzip.compress(_html("gzip page"))
        ),
        "/brotli": Route(
            headers={**html, "Content-Encoding": "br"}, body=brotli.compress(_html("brotli page"))
        ),
        "/bomb": Route(headers={**html, "Content-Encoding": "gzip"}, body=bomb),
        "/js-only": Route(headers=html, body=JS_ONLY),
        "/captcha": Route(headers=html, body=CAPTCHA_BODY),
        "/blocked": Route(status=403, headers=html, body=CHALLENGE_BODY),
        "/ratelimited": Route(status=429, headers={**html, "Retry-After": "30"}, body=b"slow down"),
        "/server-error": Route(status=503, headers=html, body=_html("unavailable", 0)),
        "/media/huge.mp4": Dynamic(
            lambda h: _stream_media(h, honour_range=True, content_type="video/mp4")
        ),
        "/media/norange.mp4": Dynamic(
            lambda h: _stream_media(h, honour_range=False, content_type="video/mp4")
        ),
        "/stream/video": Dynamic(
            lambda h: _stream_media(h, honour_range=False, content_type="video/mp4")
        ),
        "/master.m3u8": Route(
            headers={"Content-Type": "application/vnd.apple.mpegurl"}, body=MANIFEST
        ),
        "/slowloris": Dynamic(_slowloris),
        "/stall": Dynamic(_stall),
        "/slow-page": Dynamic(_slow_page),
        "/malformed": Dynamic(_malformed),
        "/truncated": Dynamic(_truncated),
        "/reset": Dynamic(_reset),
        "/large": Dynamic(_large_html),
        "/cookie/set": Dynamic(_cookie_set),
        "/cookie/check": Dynamic(_cookie_check),
    }


@dataclass
class SocksProxy:
    """Minimal SOCKS5 (no auth, CONNECT, IPv4/domain) in front of the fixture web."""

    port: int
    connections: int = 0

    @property
    def url(self) -> str:
        return f"socks5h://127.0.0.1:{self.port}"


def _relay(a: socket.socket, b: socket.socket) -> None:
    sockets = [a, b]
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [], 30)
            if not readable:
                return
            for s in readable:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return
    finally:
        a.close()
        b.close()


def _socks_session(client: socket.socket, proxy: SocksProxy) -> None:
    try:
        greeting = client.recv(262)
        if len(greeting) < 2 or greeting[0] != 5:
            client.close()
            return
        client.sendall(b"\x05\x00")
        head = client.recv(4)
        atyp = head[3]
        if atyp == 1:
            host = socket.inet_ntoa(client.recv(4))
        elif atyp == 3:
            host = client.recv(client.recv(1)[0]).decode()
        else:
            client.sendall(b"\x05\x08\x00\x01" + b"\0" * 6)
            client.close()
            return
        port = struct.unpack("!H", client.recv(2))[0]
        proxy.connections += 1
        upstream = socket.create_connection((host, port), timeout=10)
        client.sendall(b"\x05\x00\x00\x01" + b"\0" * 6)
        _relay(client, upstream)
    except OSError:
        client.close()


@contextlib.contextmanager
def socks_proxy() -> Iterator[SocksProxy]:
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(32)
    proxy = SocksProxy(port=server.getsockname()[1])
    stop = threading.Event()

    def accept() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                client, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=_socks_session, args=(client, proxy), daemon=True).start()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield proxy
    finally:
        stop.set()
        server.close()
        thread.join(timeout=2)
