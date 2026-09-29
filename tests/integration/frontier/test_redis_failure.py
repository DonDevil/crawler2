"""Redis-down semantics (§19): fail closed, never report a result Redis did not give.

A TCP proxy between the frontier and the real Redis is cut and restored,
so the client sees genuine connection failures, then the same Redis state.
"""

from __future__ import annotations

import contextlib
import socket
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import redis

from crawler2.core.configuration import ExecutionQueue as Q
from crawler2.core.configuration import FrontierSettings, Settings
from crawler2.frontier import (
    AdmitOutcome,
    CompleteOutcome,
    FailOutcome,
    FrontierUnavailableError,
)
from crawler2.frontier.redis import RedisFrontier
from tests.integration.frontier.conftest import TEST_DB, Clock, admission

pytestmark = pytest.mark.integration


class CuttableProxy:
    def __init__(self, host: str, port: int) -> None:
        self._target = (host, port)
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self._up = True
        self._conns: list[socket.socket] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            if not self._up:
                client.close()
                continue
            upstream = socket.create_connection(self._target)
            with self._lock:
                self._conns += [client, upstream]
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    @staticmethod
    def _pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                with contextlib.suppress(OSError):
                    s.shutdown(socket.SHUT_RDWR)

    def cut(self) -> None:
        self._up = False
        with self._lock:
            conns, self._conns = self._conns, []
        for s in conns:
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)

    def restore(self) -> None:
        self._up = True

    def close(self) -> None:
        self.cut()
        self._server.close()


@pytest.fixture
def outage(
    make_frontier: Callable[..., RedisFrontier], clock: Clock
) -> Iterator[tuple[RedisFrontier, RedisFrontier, CuttableProxy]]:
    """(frontier through the proxy, direct frontier on the same namespace, proxy)."""
    direct = make_frontier()
    cfg = Settings().redis
    proxy = CuttableProxy(cfg.host, cfg.port)
    client = redis.Redis(
        host="127.0.0.1", port=proxy.port, db=TEST_DB, decode_responses=True, socket_timeout=2
    )
    via_proxy = RedisFrontier(
        client,
        FrontierSettings(default_interval_s=0.0),
        namespace=direct._prefix.removesuffix(":fr:"),
        clock=clock,
    )
    yield via_proxy, direct, proxy
    proxy.close()


def down(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    with pytest.raises(FrontierUnavailableError):
        fn(*args, **kwargs)


def test_every_operation_fails_closed_and_state_survives(
    outage: tuple[RedisFrontier, RedisFrontier, CuttableProxy], clock: Clock
) -> None:
    f, direct, proxy = outage
    f.admit(admission("a", 1))
    f.admit(admission("b", 1))
    held = f.claim(Q.HTTP)
    assert held is not None
    proxy.cut()

    down(f.admit, admission("c", 1))
    down(f.claim, Q.HTTP)  # never a claim, never "no work"
    down(f.heartbeat, held)  # an outage is not a lost claim (not None)
    down(f.complete, held)
    down(f.fail, held, "x")
    down(f.defer, held)
    down(f.recover)
    down(f.stats)
    # Nothing changed behind the outage.
    stats = direct.stats()
    assert stats.counters == {"admitted": 2, "claimed": 1}
    assert (stats.leased, stats.depth[Q.HTTP]) == (1, 2)

    proxy.restore()
    assert f.heartbeat(held) is not None  # same token still owns it
    assert f.complete(held) is CompleteOutcome.COMPLETED
    other = f.claim(Q.HTTP)
    assert other is not None
    assert other.url == "https://b.test/1"
    assert f.fail(other, "503").outcome is FailOutcome.RETRY_SCHEDULED
    assert f.admit(admission("c", 1)).outcome is AdmitOutcome.READY


def test_outage_longer_than_lease_is_recovered_after_return(
    outage: tuple[RedisFrontier, RedisFrontier, CuttableProxy], clock: Clock
) -> None:
    f, _direct, proxy = outage
    f.admit(admission("a", 1))
    held = f.claim(Q.HTTP)
    assert held is not None
    proxy.cut()
    clock.advance(f.lease_ttl_s + 1)
    down(f.complete, held)
    proxy.restore()
    assert f.recover().recovered == 1
    # The worker that was cut off learns it lost the claim; nothing is lost.
    assert f.heartbeat(held) is None
    assert f.complete(held) is CompleteOutcome.STALE
    clock.advance(f.settings.base_backoff_s)
    again = f.claim(Q.HTTP)
    assert again is not None
    assert again.attempt == 2
    f.complete(again)


def test_ambiguous_admit_is_safe_to_repeat(
    outage: tuple[RedisFrontier, RedisFrontier, CuttableProxy],
) -> None:
    f, direct, _ = outage
    # Whether or not a lost reply's admission landed, repeating it is idempotent.
    assert direct.admit(admission("a", 1)).outcome is AdmitOutcome.READY
    assert f.admit(admission("a", 1)).outcome is AdmitOutcome.DUPLICATE
    assert direct.stats().depth[Q.HTTP] == 1
