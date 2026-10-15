"""crawler2 ↔ fingerprinter cross-service test (P9 design §16, P9-D19).

crawler2's P8 ``EncodeRequester`` claims and emits ``encode.requested`` (outbox,
stored envelope); the envelope is appended to the shared stream by crawler2's
own ``RedisStreamPublisher`` (what the relay does for that row). The P9
fingerprinter runs as **separate processes** (its CLI, its own keyspace and
buckets) — no fingerprinter code is imported here and its keyspace is never
read. crawler2's ``RepresentationLoop`` (group ``media-registry``) then applies
``representation.ready`` / ``encode.failed`` to crawler2's own projection.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import redis
from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events.fingerprinting import (
    EncodeFailed,
    EncodeFailure,
    RepresentationReady,
)
from antipiracy_contracts.models.media import ContentKey, Media, MediaKind
from antipiracy_contracts.models.web import UrlRef

from crawler2.core.configuration import EventSettings, Settings
from crawler2.media.encode import (
    EncodeRequester,
    RepresentationLoop,
    RepresentationProjection,
    RequestResult,
)
from crawler2.media.model import SCHEME
from crawler2.media.store import ScyllaRegistryStore
from crawler2.storage.events.consumer import IdempotentConsumer, RedisStreamReader
from crawler2.storage.events.publisher import OutboundEvent, RedisStreamPublisher, stream_name
from crawler2.storage.repositories import RepresentationState
from crawler2.storage.scylla import ScyllaStorage
from tests.fixtures.media import Clock, registry

FP_DIR = Path(os.environ.get("P9_FINGERPRINTER_DIR", Path.home() / "anti_piracy" / "fingerprinter"))
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (FP_DIR / "fingerprinter" / "cli.py").exists(), reason="fingerprinter repo missing"
    ),
]


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def web(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    root = tmp_path / "web"
    root.mkdir()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(root)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _video(path: Path, seed: int) -> bytes:
    subprocess.run(  # noqa: S603 - fixed argument list
        [  # noqa: S607 - ffmpeg from PATH, as in the P8 benchmark
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x120:rate=24:duration=11",
            "-vf",
            f"hue=h={seed * 40},drawtext=text={uuid.uuid4().hex}:fontsize=10:x=2:y=2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def _plain(value: Any) -> str:
    return str(value.get_secret_value() if hasattr(value, "get_secret_value") else value)


class Fingerprinter:
    """The fingerprinter as an external service: CLI subprocesses only."""

    def __init__(self, settings: Settings, events: EventSettings, tmp: Path) -> None:
        tag = uuid.uuid4().hex[:8]
        env = {k: v for k, v in os.environ.items() if not k.startswith("FINGERPRINTER_")}
        env.update(
            {
                "FINGERPRINTER_HOST_ID": "xs-host",
                "FINGERPRINTER_SCYLLA__CONTACT_POINTS": ",".join(settings.scylla.contact_points),
                "FINGERPRINTER_SCYLLA__KEYSPACE": "fingerprinter_it_xs",
                "FINGERPRINTER_REDIS__HOST": settings.redis.host,
                "FINGERPRINTER_REDIS__PORT": str(settings.redis.port),
                "FINGERPRINTER_REDIS__PREFIX": f"fpxs:{tag}:",
                "FINGERPRINTER_REDIS__STREAM_PREFIX": events.stream_prefix,  # the shared streams
                "FINGERPRINTER_MINIO__ENDPOINT": settings.minio.endpoint,
                "FINGERPRINTER_MINIO__ACCESS_KEY": _plain(settings.minio.access_key),
                "FINGERPRINTER_MINIO__SECRET_KEY": _plain(settings.minio.secret_key),
                "FINGERPRINTER_MINIO__REPRESENTATIONS_BUCKET": f"fp-xs-{tag}-reps",
                "FINGERPRINTER_MINIO__TARGETS_BUCKET": f"fp-xs-{tag}-targets",
                "FINGERPRINTER_INTAKE__READER": f"xs-{tag}",
                # loopback fixture web only
                "FINGERPRINTER_ACQUIRE__SSRF_ALLOW_CIDRS": "127.0.0.0/8",
                "FINGERPRINTER_SCRATCH_DIR": str(tmp / "fp-scratch"),
                "FINGERPRINTER_ROLES": "encoder,scheduler",
                "FINGERPRINTER_ENCODER__POLL_INTERVAL_S": "0.2",
            }
        )
        self.env = env

    def __call__(self, *args: str, check: bool = True) -> Any:
        out = subprocess.run(  # noqa: S603
            [str(FP_DIR / "env" / "bin" / "python"), "-m", "fingerprinter", "--json", *args],
            cwd=FP_DIR,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=900,
        )
        if check and out.returncode != 0:
            raise AssertionError(
                f"fingerprinter {args} exited {out.returncode}: {out.stderr[-2000:]}"
            )
        try:
            return json.loads(out.stdout)
        except ValueError:
            return out.stdout


def _loop(
    redis_client: redis.Redis, events: EventSettings, storage: ScyllaStorage, group: str
) -> RepresentationLoop:
    projection = RepresentationProjection(storage.projections)
    ready = RedisStreamReader(
        redis_client, stream_name(events, "representation.ready", 1), group, "c1"
    )
    failed = RedisStreamReader(redis_client, stream_name(events, "encode.failed", 1), group, "c1")
    for r in (ready, failed):
        r.ensure_group()  # crawler2 creates its group at ID 0 before anything is published
    return RepresentationLoop(
        [
            (
                ready,
                IdempotentConsumer(
                    group, RepresentationReady, projection.ready, storage.processed_events
                ),
            ),
            (
                failed,
                IdempotentConsumer(
                    group, EncodeFailed, projection.failed, storage.processed_events
                ),
            ),
        ],
        batch_size=50,
        block_ms=100,
        claim_idle_ms=60_000,
    )


def test_p9_fingerprinter_serves_crawler2_requests_end_to_end(
    storage: ScyllaStorage,
    redis_client: redis.Redis,
    event_settings: EventSettings,
    settings: Settings,
    web: Any,
    tmp_path: Path,
) -> None:
    root, base = web
    store = ScyllaRegistryStore(storage.session)
    requester = EncodeRequester(
        store,
        storage.projections,
        storage.outbox,
        producer=registry("xs"),
        clock=Clock(datetime.now(UTC).replace(microsecond=0)),
        grace=timedelta(seconds=300),
    )
    publisher = RedisStreamPublisher(redis_client, event_settings)
    loop = _loop(redis_client, event_settings, storage, "media-registry")
    contents: dict[str, ContentKey] = {}
    for i, name in enumerate(["a", "b"]):
        data = _video(root / f"{name}.mp4", i + 1)
        contents[name] = ContentKey.of(
            SCHEME, ContentDigest("sha256:" + hashlib.sha256(data).hexdigest())
        )
    contents["gone"] = ContentKey.of(
        SCHEME, ContentDigest.of_bytes(b"never served " + uuid.uuid4().bytes)
    )
    for name, key in contents.items():
        media = Media.of(UrlRef.of(f"{base}/{name}.mp4"), MediaKind.VIDEO_FILE)
        assert requester.request_content(key, media, f"{base}/page/{name}") is RequestResult.EMITTED
        claim = store.encode_claim(key.content_id, "default")
        assert claim is not None
        publisher.publish(
            [OutboundEvent(claim.event_id, "encode.requested", 1, claim.envelope.encode())]
        )

    fp = Fingerprinter(settings, event_settings, tmp_path)
    fp("migrate")
    fp("runtime", "init-profile", check=False)  # idempotent across runs of the keyspace
    added = fp("spec", "add", "--builtin", "dinov2-seg/2", check=False)
    digest = added["spec_digest"] if isinstance(added, dict) else None
    if digest is None:
        digest = next(
            s["spec_digest"]
            for s in fp("spec", "list")
            if s["compute_dtype"] == "float32" and s["version"] == "2"
        )
    fp("spec", "set-default", digest)
    fp("intake", "init", "--from-start")
    status = fp("intake", "run", "--until-idle")
    assert status["consumed_ordinal"] == 3
    assert status["open_gaps"] == []
    report = fp("encoder", "run", "--device", "cpu", "--idle-exit-s", "5")
    assert report["exit_code"] == 0
    assert report["stats"]["won"] == 2
    fp("reconcile", "run", "--once")

    loop.run(max_idle_polls=3)
    by_name = {
        n: {s.spec_key: s for s in storage.projections.representation_status(k.content_id)}
        for n, k in contents.items()
    }
    for n in ("a", "b"):
        ready = [s for s in by_name[n].values() if s.state is RepresentationState.READY]
        assert len(ready) == 1, by_name[n]
        rep = ready[0].representation
        assert rep is not None
        assert (rep.spec.name, rep.spec.version) == ("dinov2-seg", "2")
        assert str(rep.spec.config_digest) == digest
        assert rep.segment_count == 3
    gone = by_name["gone"]["default"]
    assert gone.state is RepresentationState.FAILED  # encode.failed.spec echoes 'default'
    # crawler2 never re-requests (P8-D7), and a ready content is suppressed
    again = Media.of(UrlRef.of(f"{base}/a-copy.mp4"), MediaKind.VIDEO_FILE)
    assert requester.request_content(contents["a"], again, None) is RequestResult.SUPPRESSED
    assert (
        requester.request_content(contents["gone"], again, None) is RequestResult.CLAIMED_ELSEWHERE
    )
    for stream in ("representation.ready", "encode.failed"):
        assert (
            redis_client.xpending(stream_name(event_settings, stream, 1), "media-registry")[
                "pending"
            ]
            == 0
        )
    entries: Any = redis_client.xrange(stream_name(event_settings, "encode.failed", 1))
    (failed,) = [fields for _, fields in entries]
    envelope = json.loads(failed[b"envelope"])
    assert envelope["payload"]["failure"] == EncodeFailure.SOURCE_UNREACHABLE.value
    gone_claim = store.encode_claim(contents["gone"].content_id, "default")
    assert gone_claim is not None
    assert envelope["payload"]["request_id"] == gone_claim.event_id
