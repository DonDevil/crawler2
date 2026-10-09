"""P8 resolver under a real Scylla outage (opt-in: it pauses the shared Scylla container).

    P8_SCYLLA_OUTAGE=1 scripts/test-media.sh tests/integration/storage/test_media_outage.py

The container is frozen (``docker pause``) between the claim and the commit of one
reference. ``run_forever`` must retry with backoff, never advance past the uncommitted
claim, and commit the stored envelope once Scylla is back.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from typing import Any

import pytest
from antipiracy_contracts.ids import MediaId

from crawler2.storage.errors import StorageError
from crawler2.storage.scylla import ScyllaStorage
from tests.fixtures.media import Background, Bench, Pause, discovered, reference

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("P8_SCYLLA_OUTAGE") != "1", reason="pauses Scylla; set P8_SCYLLA_OUTAGE=1"
    ),
]
CONTAINER = os.environ.get("P8_SCYLLA_CONTAINER", "crawler2-scylla-1")


def _docker(action: str) -> None:
    subprocess.run(  # noqa: S603 -- fixed arguments, our own container
        ["sg", "docker", "-c", f"docker {action} {CONTAINER}"],  # noqa: S607
        check=True,
        timeout=60,
    )


def test_scylla_outage_is_retried_and_nothing_is_skipped(storage: ScyllaStorage) -> None:
    bench = Bench.create(storage)
    page = bench.page("outage")
    refs = [reference(f"https://cdn.example/{bench.reader}/{i}.mp4") for i in range(3)]
    bench.put(discovered(page, refs))
    pause = Pause("after_claim")
    resolver = bench.resolver("A", hook=pause)
    failures: list[str] = []
    done = {"advanced": False}

    def stop() -> bool:
        return done["advanced"]

    def sleep(seconds: float) -> None:
        failures.append(f"retry after {seconds}s")
        time.sleep(seconds)
        with contextlib.suppress(StorageError):
            done["advanced"] = bench.state().next_bucket > bench.b0

    def run() -> Any:
        resolver.run_forever(poll_interval_s=0.5, retry_max_s=4.0, stop=stop, sleep=sleep)

    loop = Background(run)
    assert pause.reached.wait(30)
    _docker("pause")
    try:
        pause.resume.set()
        time.sleep(25)  # record_observation times out; the loop retries meanwhile
    finally:
        _docker("unpause")
    deadline = time.monotonic() + 600
    while not done["advanced"] and time.monotonic() < deadline:
        time.sleep(2)
    assert done["advanced"], "resolver never recovered"
    loop.join()
    assert failures  # it retried at least once
    print(f"outage: {len(failures)} sleeps/retries before recovery")
    for ref in refs:
        bench.invariant(page.observation_id, MediaId.of_locator(ref.locator.url))
