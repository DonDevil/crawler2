"""P8 exit gate: the synthetic dedup benchmark (design §3.2 and §10 "exit").

    benchmarks/p8-media/run.sh            # sets the host env for the compose stack

The whole P8 pipeline runs in-process on the real Scylla and Redis, against a
loopback fixture media web (no internet):

  media.discovered (outbox) → resolver → probe pool → hash pool → encode requester

Corpus (generated with ffmpeg from one 6-second synthetic source with temporal
noise, so the 900k encode really reaches its bitrate, plus crafted
bytes; nothing is committed):

- **byte-identical groups:** every content item is served on 3 locators on 3
  different hosts and referenced from 2 pages each (6 sightings per item);
- **different-content set** (pairwise): re-encoded bitrate (H.264 200k vs
  900k), other containers (MP4, Matroska remux, WebM/VP8), other packaging
  (fragmented MP4 of the same stream), HLS vs MP4 (two HLS renditions with
  1-s and 2-s segments: manifests are registered and probed, never hashed,
  P8-D6), and crafted collisions (equal length and equal first/middle/last
  64 KiB, bytes differ elsewhere) for the 900k MP4 and for a random blob.

Gate (plan §P8, design §3.2):
- **duplicate encode tasks** = for each byte-identical group, (encode claims - 1)
  + (distinct ``encode.requested`` event_ids - 1) + (ContentIds - 1); must be 0;
- **false merges** in the different-content set = pairs sharing a ContentId,
  locators sharing a MediaId, and any P8 equivalence record; must be 0.

Results: ``benchmarks/p8-media/results/dedup-bench.json``.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from antipiracy_contracts.events import decode_event_as
from antipiracy_contracts.events.fingerprinting import EncodeRequested
from antipiracy_contracts.ids import ContentId, MediaId
from antipiracy_contracts.models.media import MediaKind
from antipiracy_contracts.models.web import UrlRef
from antipiracy_contracts.urls import CanonicalUrl

from crawler2.core.configuration import Settings
from crawler2.frontier.redis import connect_redis
from crawler2.media.cli import producer_for
from crawler2.media.encode import EncodeRequester
from crawler2.media.hashing import FullContentHasher
from crawler2.media.model import ReaderState
from crawler2.media.probe import (
    HashWorker,
    ProbeWorker,
    guarded,
    hash_limits,
    probe_fetcher,
    run_pool,
)
from crawler2.media.resolver import Resolver, ResolverConfig
from crawler2.media.schedule import ProbeScheduler
from crawler2.media.store import ScyllaRegistryStore
from crawler2.media.work import RedisWork
from crawler2.storage.events.idempotency import idempotency_key
from crawler2.storage.layout import outbox_bucket
from crawler2.storage.scylla import Migrator, ScyllaSession, ScyllaStorage
from tests.fixtures.contracts import T0, page_observation
from tests.fixtures.media import discovered, outbox_rows, reference
from tests.fixtures.mediaweb import (
    RoutingTransport,
    blob,
    crafted_twin,
    media_site,
    public_resolver,
)

KEYSPACE = "crawler2_p8_bench"
COPIES = 3
PAGES_PER_LOCATOR = 2
RESULTS = Path(__file__).with_name("results") / "dedup-bench.json"
FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
EXACT = ["-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1"]


def _ff(*args: str) -> None:
    subprocess.run([*FF, *args], check=True)


def corpus(tmp: Path) -> dict[str, dict[str, Any]]:
    """name → {body, content_type, kind, family, extra files}."""
    src = [
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=6:size=320x240:rate=25",
        "-vf",
        "noise=alls=25:allf=t",
    ]
    x264 = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "25", "-preset", "veryfast"]
    _ff(*src, *x264, "-b:v", "200k", *EXACT, str(tmp / "low.mp4"))
    _ff(*src, *x264, "-b:v", "900k", *EXACT, str(tmp / "high.mp4"))
    _ff("-i", str(tmp / "high.mp4"), "-c", "copy", *EXACT, str(tmp / "high.mkv"))
    _ff(*src, "-c:v", "libvpx", "-b:v", "500k", *EXACT, str(tmp / "vp8.webm"))
    _ff(
        "-i",
        str(tmp / "high.mp4"),
        "-c",
        "copy",
        "-movflags",
        "frag_keyframe+empty_moov",
        *EXACT,
        str(tmp / "frag.mp4"),
    )
    for seconds in (1, 2):
        out = tmp / f"hls{seconds}"
        out.mkdir()
        _ff(
            "-i",
            str(tmp / "high.mp4"),
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            str(seconds),
            "-hls_list_size",
            "0",
            "-hls_segment_filename",
            str(out / "seg%03d.ts"),
            str(out / "index.m3u8"),
        )
    high = (tmp / "high.mp4").read_bytes()
    rand = blob("p8-bench-random", 512 * 1024)
    items: dict[str, dict[str, Any]] = {
        "mp4_h264_200k": {"body": (tmp / "low.mp4").read_bytes(), "type": "video/mp4"},
        "mp4_h264_900k": {"body": high, "type": "video/mp4"},
        "mkv_remux_900k": {"body": (tmp / "high.mkv").read_bytes(), "type": "video/x-matroska"},
        "webm_vp8_500k": {"body": (tmp / "vp8.webm").read_bytes(), "type": "video/webm"},
        "fmp4_repack_900k": {"body": (tmp / "frag.mp4").read_bytes(), "type": "video/mp4"},
        "crafted_twin_of_mp4_900k": {"body": crafted_twin(high), "type": "video/mp4"},
        "random_blob": {"body": rand, "type": "video/mp4"},
        "crafted_twin_of_blob": {"body": crafted_twin(rand), "type": "video/mp4"},
    }
    for name, item in items.items():
        item["kind"] = MediaKind.VIDEO_FILE
        item["path"] = f"/{name}.{'webm' if 'webm' in name else 'mkv' if 'mkv' in name else 'mp4'}"
    for seconds in (1, 2):
        out = tmp / f"hls{seconds}"
        items[f"hls_{seconds}s_segments"] = {
            "body": (out / "index.m3u8").read_bytes(),
            "type": "application/vnd.apple.mpegurl",
            "kind": MediaKind.HLS_MANIFEST,
            "path": f"/hls{seconds}/index.m3u8",
            "segments": {
                f"/hls{seconds}/{p.name}": p.read_bytes() for p in sorted(out.glob("*.ts"))
            },
        }
    return items


def _locators(name: str, item: dict[str, Any]) -> list[CanonicalUrl]:
    return [
        UrlRef.of(f"https://cdn{c}-{name}.bench.example{item['path']}").url for c in range(COPIES)
    ]


async def drain(
    work: RedisWork, settings: Settings, store: ScyllaRegistryStore, requester: Any
) -> dict[str, int]:
    m = settings.media.model_copy(update={"probe_per_host_interval_s": 0.0})
    counts = {"probes": 0, "hashes": 0}
    identity = f"{settings.host_id}:media_probe:1:" + "b" * 32
    idle = {"n": 0}

    def stop() -> bool:
        if work.depth("probe") or work.depth("hash"):
            idle["n"] = 0
            return False
        idle["n"] += 1
        return idle["n"] > 60

    def transport() -> Any:
        return guarded(m, resolver=public_resolver(), inner=RoutingTransport(SITE))

    fetcher = probe_fetcher(settings.workers.fetch, transport(), connections=8)
    hasher = FullContentHasher(transport(), hash_limits(m, settings.workers.fetch))
    probe = ProbeWorker(fetcher, store, work, worker=identity, settings=m)
    hashw = HashWorker(hasher, store, work, worker=identity, settings=m, requester=requester)

    async def p(job: Any) -> None:
        done = await probe.handle(job)  # await first: `+=` would read before awaiting
        counts["probes"] += done is not None

    async def h(job: Any) -> None:
        done = await hashw.handle(job)
        counts["hashes"] += done is not None

    try:
        await asyncio.gather(
            run_pool("probe", work, p, concurrency=4, stop=stop, block_s=0.2),
            run_pool("hash", work, h, concurrency=4, stop=stop, block_s=0.2),
        )
    finally:
        await fetcher.close()
        await hasher.close()
    return counts


SITE: Any = None


def main() -> int:
    global SITE
    started = datetime.now(UTC)
    t0 = time.monotonic()
    settings = Settings()
    with ScyllaSession.connect(settings.scylla, keyspace=KEYSPACE) as session:
        session.execute_raw(f"DROP KEYSPACE IF EXISTS {KEYSPACE}")
        Migrator(session, settings.scylla, applied_by="p8-bench").migrate()
    storage = ScyllaStorage.open(settings.scylla, keyspace=KEYSPACE, instance="p8-bench")
    store = ScyllaRegistryStore(storage.session)
    client = connect_redis(settings.redis)
    namespace = f"p8bench-{int(time.time())}"
    work = RedisWork(client, namespace=namespace)
    tmp = Path(tempfile.mkdtemp(prefix="p8-bench-"))
    try:
        items = corpus(tmp)
        timings: dict[str, float] = {"corpus_s": round(time.monotonic() - t0, 2)}
        with media_site() as site:
            SITE = site
            for item in items.values():
                site.put(item["path"], item["body"], content_type=item["type"])
                for path, body in item.get("segments", {}).items():
                    site.put(path, body, content_type="video/mp2t")

            # 1. P5's media.discovered, as the crawl would emit it (2 pages per locator).
            identity = f"{settings.host_id}:media_probe:1:" + "a" * 32
            now = work.now()
            first_bucket = outbox_bucket(now)
            sightings = 0
            for name, item in items.items():
                for c, url in enumerate(_locators(name, item)):
                    for pg in range(PAGES_PER_LOCATOR):
                        page = page_observation(
                            UrlRef.of(f"https://site{pg}.bench.example/{name}/{c}"),
                            at=T0,
                            seed=f"{namespace}-{name}-{c}-{pg}",
                        )
                        storage.outbox.enqueue(discovered(page, [reference(url, item["kind"])]))
                        sightings += 1
            assert store.create_state(
                ReaderState(
                    reader="bench",
                    epoch=1,
                    owner=None,
                    lease_until=now - timedelta(hours=1),
                    next_bucket=first_bucket,
                    started_bucket=first_bucket,
                ),
                at=now,
                by="bench",
            )
            requester = EncodeRequester(
                store,
                storage.projections,
                storage.outbox,
                producer=producer_for(identity),
                clock=work.now,
            )
            resolver = Resolver(
                storage.outbox,
                store,
                storage.media,
                producer=producer_for(identity),
                config=ResolverConfig(reader="bench", settle=timedelta(0)),
                views=store,
                sink=ProbeScheduler(work, reprobe_after=timedelta(days=7), requester=requester),
                clock=work.now,
            )
            wait = 61 - datetime.now(UTC).second
            time.sleep(wait)  # let the minute bucket of the events close
            t1 = time.monotonic()
            while not resolver.run_once().caught_up:
                pass
            timings["resolve_s"] = round(time.monotonic() - t1, 2)
            t2 = time.monotonic()
            counts = asyncio.run(drain(work, settings, store, requester))
            timings["probe_hash_encode_s"] = round(time.monotonic() - t2, 2)
            sweep = requester.sweep()
            hashed_bytes = sum(site.bytes_sent[item["path"]] for item in items.values())
        return report(
            items, storage, store, started, timings, counts, sightings, sweep, hashed_bytes
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for key in client.scan_iter(match=f"{namespace}:*"):
            client.delete(key)


def report(
    items: dict[str, dict[str, Any]],
    storage: ScyllaStorage,
    store: ScyllaRegistryStore,
    started: datetime,
    timings: dict[str, float],
    counts: dict[str, int],
    sightings: int,
    sweep: Any,
    hashed_bytes: int,
) -> int:
    groups: dict[str, Any] = {}
    media_ids: dict[str, str] = {}
    for name, item in items.items():
        contents: set[str] = set()
        statuses = []
        keyed = 0
        for url in _locators(name, item):
            media = MediaId.of_locator(url)
            media_ids[url] = str(media)
            latest = store.latest(media)
            statuses.append(latest.probe.probe_status.value if latest and latest.probe else None)
            key = latest.current_content() if latest else None
            if key is not None:
                keyed += 1
                contents.add(str(key.content_id))
        claims, event_ids = 0, set()
        for cid in contents:
            claim = store.encode_claim(ContentId(cid), "default")
            claims += claim is not None
            if claim is not None:
                ikey = idempotency_key(decode_event_as(claim.envelope, EncodeRequested))
                event_ids |= {r.event_id for r in outbox_rows(storage, ikey, since=started)}
        hashed = item["kind"] is MediaKind.VIDEO_FILE
        duplicates = (
            max(0, claims - 1) + max(0, len(event_ids) - 1) + max(0, len(contents) - 1)
            if hashed
            else 0
        )
        groups[name] = {
            "kind": item["kind"].value,
            "bytes": len(item["body"]),
            "sha256": hashlib.sha256(item["body"]).hexdigest(),
            "locators": COPIES,
            "sightings": COPIES * PAGES_PER_LOCATOR,
            "probe_status": sorted({str(x) for x in statuses}),
            "locators_with_content_key": keyed,
            "content_ids": sorted(contents),
            "encode_claims": claims,
            "encode_event_ids": len(event_ids),
            "duplicate_encode_tasks": duplicates,
            "expected": "one ContentId and one encode" if hashed else "no ContentId (P8-D6)",
            "ok": (keyed == COPIES and len(contents) == 1 and claims == 1 and len(event_ids) == 1)
            if hashed
            else (keyed == 0 and claims == 0 and statuses == ["probed"] * COPIES),
        }
    hashed_items = {n: g for n, g in groups.items() if g["kind"] == "video_file"}
    pairs = list(itertools.combinations(sorted(hashed_items), 2))
    merged = [
        [a, b]
        for a, b in pairs
        if set(hashed_items[a]["content_ids"]) & set(hashed_items[b]["content_ids"])
    ]
    shared_media = len(media_ids) - len(set(media_ids.values()))
    tables = {
        r.table_name
        for r in storage.session.execute_raw(
            f"SELECT table_name FROM system_schema.tables WHERE keyspace_name = '{KEYSPACE}'"
        )
    }
    equivalence_tables = sorted(t for t in tables if "equival" in t)
    duplicate_tasks = sum(g["duplicate_encode_tasks"] for g in groups.values())
    false_merges = len(merged) + shared_media + len(equivalence_tables)
    passed = duplicate_tasks == 0 and false_merges == 0 and all(g["ok"] for g in groups.values())
    doc = {
        "benchmark": "p8 synthetic dedup (exit gate)",
        "run_at": started.isoformat(),
        "keyspace": KEYSPACE,
        "scheme": "full-bytes/v1",
        "corpus": "ffmpeg testsrc + temporal noise, 6 s 320x240 25 fps (libx264 200k/900k, "
        "Matroska remux, fragmented MP4, libvpx 500k, HLS 1 s / 2 s segments) "
        "+ crafted collisions",
        "sightings": sightings,
        "probes": counts["probes"],
        "hashes": counts["hashes"],
        "hash_bytes_served": hashed_bytes,
        "sweeper": {"scanned": sweep.scanned, "reemitted": sweep.reemitted},
        "timings": timings,
        "groups": groups,
        "different_content_pairs": len(pairs),
        "pairs_sharing_a_content_id": merged,
        "locators_sharing_a_media_id": shared_media,
        "p8_equivalence_tables": equivalence_tables,
        "duplicate_encode_tasks": duplicate_tasks,
        "false_merges": false_merges,
        "gate": "PASS" if passed else "FAIL",
    }
    RESULTS.parent.mkdir(exist_ok=True)
    RESULTS.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: doc[k] for k in ("duplicate_encode_tasks", "false_merges", "gate")}))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
