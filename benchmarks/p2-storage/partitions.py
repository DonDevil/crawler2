"""P2 partition-size benchmark: worst-case partitions of a 1M-page synthetic load.

Runs inside an app container (P0: only containers reach Scylla). Driven by
run.sh, which flushes/compacts and reads sizes from Scylla itself.

Method (docs/phases/p02-storage/benchmarks.md):

1. Every scenario concentrates the *entire* 1M-page load on one key
   (one domain, one hub URL, one media file, one target, ...), which is
   strictly worse than any realistic distribution of 1M pages.
2. Pass 1 computes, for every logical row, its partition via the real
   ``crawler2.storage.layout`` functions and counts rows per shard (exact
   bucket distribution).
3. Pass 2 writes, through the real repositories and contract models, every
   row of the heaviest shard. That partition is therefore byte-for-byte the
   partition the full load would produce; the other shards hold statistically
   identical row counts (reported) and are not written, which keeps the run
   within the dev host's budget without shrinking the measured partition.

    PYTHONPATH=/app python benchmarks/p2-storage/partitions.py write [--scale 0.01] [--only S1,S2]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events import new_event
from antipiracy_contracts.events.web import PageObserved, UrlsDiscovered
from antipiracy_contracts.ids import EventId, EvidenceId, FetchAttemptId, ObservationId, UrlId
from antipiracy_contracts.models.blobs import BlobRef
from antipiracy_contracts.models.evidence import EvidenceSeal
from antipiracy_contracts.models.matching import MatchResult
from antipiracy_contracts.models.media import Media
from antipiracy_contracts.models.web import DiscoveredLink, LinkRelation, UrlRef
from antipiracy_contracts.ownership import Component
from antipiracy_contracts.urls import CanonicalUrl

from crawler2.core.configuration import Settings
from crawler2.storage import layout
from crawler2.storage.scylla import Migrator, ScyllaSession, ScyllaStorage
from tests.fixtures.contracts import (
    content_key,
    evidence_candidate,
    fetch_attempt,
    match,
    media,
    media_observation,
    page_observation,
    producer,
    seeded_uuid7,
    target,
    url,
)

PAGES = 1_000_000
DAY0 = datetime(2026, 9, 1, tzinfo=UTC)
MONTH0 = datetime(2026, 8, 1, tzinfo=UTC)
THREADS = 24


@dataclass(frozen=True)
class Scenario:
    code: str
    table: str
    what: str
    logical_rows: int
    shards: int
    shard_of: Callable[[int], int]
    write: Callable[[ScyllaStorage, Sequence[int]], None]


def _pool_map(fn: Callable[[int], None], items: Iterable[int]) -> None:
    with ThreadPoolExecutor(THREADS) as pool:
        for _ in pool.map(fn, items):
            pass


# --- S1 giant domain: 1M pages of one host, fetched and observed within one UTC day ------

GIANT = "giant.example"


def _giant_url(i: int) -> UrlRef:
    return url(f"catalog/item-{i}", GIANT)


def _attempt_id(i: int) -> FetchAttemptId:
    return FetchAttemptId.from_uuid(seeded_uuid7(f"s1-fat-{i}", DAY0))


def _giant_url_id(i: int) -> UrlId:
    return UrlId.of(CanonicalUrl(f"https://{GIANT}/catalog/item-{i}"))


def write_s1_attempts(storage: ScyllaStorage, idx: Sequence[int]) -> None:
    def one(i: int) -> None:
        at = DAY0 + timedelta(microseconds=i * 86_000)
        storage.fetch_attempts.record(fetch_attempt(_giant_url(i), at=at, seed=f"s1-fat-{i}"))

    _pool_map(one, idx)


def write_s1_observations(storage: ScyllaStorage, idx: Sequence[int]) -> None:
    def one(i: int) -> None:
        at = DAY0 + timedelta(microseconds=i * 86_000)
        body = f"<html>item {i}</html>".encode()
        storage.pages.record(page_observation(_giant_url(i), at=at, body=body, seed=f"s1-o{i}"))

    _pool_map(one, idx)


KNOWN_PER_PAGE = 10  # a 1M-page crawl of the giant domain knows ~10M of its URLs


def _known_url(i: int) -> str:
    return f"https://{GIANT}/known/{i}"


def write_s1_known(storage: ScyllaStorage, idx: Sequence[int]) -> None:
    chunks = [idx[i : i + 250] for i in range(0, len(idx), 250)]

    def one(chunk_no: int) -> None:
        refs = [UrlRef.of(_known_url(i)) for i in chunks[chunk_no]]
        storage.urls.record_discovered(refs, seen_at=DAY0)

    _pool_map(one, range(len(chunks)))


# --- S2 hub URL linked from every page ---------------------------------------------------

HUB = url("hub", "portal.example")


def _source_id(i: int) -> UrlId:
    return UrlId.of(CanonicalUrl(f"https://src{i % 5000}.example/page/{i}"))


def write_s2_inlinks(storage: ScyllaStorage, idx: Sequence[int]) -> None:
    link = (DiscoveredLink(target=HUB, relation=LinkRelation.ANCHOR, anchor_text="home"),)

    def one(i: int) -> None:
        source = UrlRef.of(f"https://src{i % 5000}.example/page/{i}")
        page = page_observation(source, at=DAY0, seed=f"s2-{i}")
        discovered = UrlsDiscovered(
            page_observation_id=page.observation_id,
            page=source,
            page_version_id=page.page_version_id,
            links=link,
        )
        storage.links.record(discovered, observed_at=DAY0)

    _pool_map(one, idx)


# --- S3 popular media: one file embedded on every page; the same bytes at 1M locators ---

POPULAR = media("intro/trailer.mp4", "cdn.example")
SAME_BYTES = content_key(b"s3-popular-content")


def _page_obs_id(i: int) -> ObservationId:
    return ObservationId.from_uuid(seeded_uuid7(f"s3-p{i}", MONTH0))


def write_s3_popular(storage: ScyllaStorage, idx: Sequence[int]) -> None:
    def one(i: int) -> None:
        at = MONTH0 + timedelta(seconds=i * 2)  # 1M sightings within the month
        page = page_observation(
            url(f"page/{i}", f"site{i % 20000}.example"), at=at, seed=f"s3-p{i}"
        )
        storage.media.record_observation(media_observation(POPULAR, page, content=SAME_BYTES))

    _pool_map(one, idx)


def _mirror(i: int) -> Media:
    return media(f"mirror/{i}/trailer.mp4", f"mirror{i % 1000}.example")


def write_s3_mirrors(storage: ScyllaStorage, idx: Sequence[int]) -> None:
    # Each mirror is sighted on its own page: a page observation carries at most 1 000
    # media references (P1 media.discovered), so M2/M4 stay bounded by contract (S7).
    def one(i: int) -> None:
        page = page_observation(
            url(f"mirrors/{i}", f"site{i % 20000}.example"), at=MONTH0, seed=f"s3b-p{i}"
        )
        storage.media.record_observation(media_observation(_mirror(i), page, content=SAME_BYTES))

    _pool_map(one, idx)


# --- S4 long URL history: observed every minute for a 31-day month, new version each ---

HOT = url("live/scores", "hot.example")
MINUTES = 31 * 24 * 60


def write_s4_history(storage: ScyllaStorage, idx: Sequence[int]) -> None:
    def one(i: int) -> None:
        at = MONTH0 + timedelta(minutes=i)
        body = f"<html>scores at minute {i}</html>".encode()
        storage.pages.record(page_observation(HOT, at=at, body=body, seed=f"s4-{i}"))

    _pool_map(one, idx)


def write_s4_attempts(storage: ScyllaStorage, idx: Sequence[int]) -> None:
    def one(i: int) -> None:
        at = MONTH0 + timedelta(minutes=i)
        storage.fetch_attempts.record(fetch_attempt(HOT, at=at, seed=f"s4-fat-{i}"))

    _pool_map(one, idx)


# --- S5 link-heavy page: the maximum batch of 10 000 links -------------------------------


def write_s5_links(storage: ScyllaStorage, idx: Sequence[int]) -> None:
    source = url("sitemap", "links.example")
    page = page_observation(source, at=DAY0, seed="s5")
    links = tuple(
        DiscoveredLink(
            target=UrlRef.of(f"https://links.example/article/{i}/some-descriptive-slug"),
            relation=LinkRelation.ANCHOR,
            anchor_text=f"Article number {i} with a realistic anchor text",
        )
        for i in idx
    )
    storage.links.record(
        UrlsDiscovered(
            page_observation_id=page.observation_id,
            page=source,
            page_version_id=page.page_version_id,
            links=links,
        ),
        observed_at=DAY0,
    )


# --- S6 hot target: every page matched in one month; evidence for every match ------------

WORK = target("s6-hot-target")


def _match(i: int) -> MatchResult:
    return match(
        content_key(f"s6-{i}".encode()).content_id, WORK.ref, at=MONTH0 + timedelta(seconds=i * 2)
    )


def write_s6_matches(storage: ScyllaStorage, idx: Sequence[int]) -> None:
    source = url("matched", "pirate.example")

    def one(i: int) -> None:
        storage.projections.record_match(
            _match(i), media_ids=[_mirror(i).media_id], source_domains=[source.domain_id]
        )

    _pool_map(one, idx)


def _evidence_id(i: int) -> EvidenceId:
    return EvidenceId.from_uuid(seeded_uuid7(f"s6-e{i}", MONTH0))


def write_s6_evidence(storage: ScyllaStorage, idx: Sequence[int]) -> None:
    page = page_observation(url("evidence-page", "pirate.example"), at=MONTH0, seed="s6-page")
    manifest = BlobRef(
        uri="s3://crawler2-evidence/manifest", digest=ContentDigest.of_bytes(b"m"), size_bytes=1
    )

    def one(i: int) -> None:
        result = _match(i)
        evidence = storage.evidence.open_candidate(result.match_id, _evidence_id(i), at=MONTH0)
        candidate = evidence_candidate(
            result, evidence, _mirror(i), page, at=MONTH0 + timedelta(seconds=i)
        )
        storage.evidence.create_candidate(candidate)
        storage.evidence.seal(
            EvidenceSeal(
                evidence_id=evidence, manifest=manifest, finalized_at=candidate.collected_at
            )
        )

    _pool_map(one, idx)


# --- S7/S8/S9: bounded-by-contract partitions ----------------------------------------------


def write_s7_media_on_page(storage: ScyllaStorage, idx: Sequence[int]) -> None:
    page = page_observation(url("gallery", "media-heavy.example"), at=DAY0, seed="s7")

    def one(i: int) -> None:
        storage.media.record_observation(media_observation(_mirror(10_000_000 + i), page))

    _pool_map(one, idx)


def write_s8_targets(storage: ScyllaStorage, idx: Sequence[int]) -> None:
    _pool_map(lambda i: storage.projections.apply_target_registered(target(f"s8-{i}")), idx)


EVENTS_PER_S = 10_000  # design ceiling of the cluster-wide event rate (benchmarks.md)
OUTBOX_MINUTE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _event_id(i: int) -> EventId:
    return EventId.from_uuid(seeded_uuid7(f"s9-{i}", OUTBOX_MINUTE))


def write_s9_outbox(storage: ScyllaStorage, idx: Sequence[int]) -> None:
    worker = producer(Component.CRAWLER_WORKER)

    def one(i: int) -> None:
        observation = page_observation(
            url(f"o/{i}", "outbox.example"), at=OUTBOX_MINUTE, seed=f"s9-o{i}"
        )
        envelope = new_event(
            PageObserved(observation=observation),
            producer=worker,
            occurred_at=OUTBOX_MINUTE,
            event_id=_event_id(i),
        )
        storage.session.execute(
            storage.outbox.statement(envelope, now=OUTBOX_MINUTE + timedelta(seconds=i % 60))
        )

    _pool_map(one, idx)


SCENARIOS = [
    Scenario(
        "S1a",
        "fetch_attempts_by_domain_day",
        "1M fetches of one domain in one day",
        PAGES,
        layout.DOMAIN_DAY_SHARDS,
        lambda i: layout.shard_of(_attempt_id(i), layout.DOMAIN_DAY_SHARDS),
        write_s1_attempts,
    ),
    Scenario(
        "S1b",
        "observations_by_domain_day",
        "1M observations of one domain in one day",
        PAGES,
        layout.DOMAIN_DAY_SHARDS,
        lambda i: layout.shard_of(_giant_url_id(i), layout.DOMAIN_DAY_SHARDS),
        write_s1_observations,
    ),
    Scenario(
        "S1c",
        "urls_by_domain",
        "10M known URLs of one domain (10 per crawled page)",
        PAGES * KNOWN_PER_PAGE,
        layout.URLS_BY_DOMAIN_SHARDS,
        lambda i: layout.shard_of(
            UrlId.of(CanonicalUrl(_known_url(i))), layout.URLS_BY_DOMAIN_SHARDS
        ),
        write_s1_known,
    ),
    Scenario(
        "S2",
        "inlinks_by_url",
        "one hub URL linked from all 1M pages",
        PAGES,
        layout.INLINK_SHARDS,
        lambda i: layout.shard_of(_source_id(i), layout.INLINK_SHARDS),
        write_s2_inlinks,
    ),
    Scenario(
        "S3a",
        "media_observations_by_media",
        "one media file on all 1M pages in one month",
        PAGES,
        layout.MEDIA_MONTH_SHARDS,
        lambda i: layout.shard_of(_page_obs_id(i), layout.MEDIA_MONTH_SHARDS),
        write_s3_popular,
    ),
    Scenario(
        "S3b",
        "media_by_content",
        "the same bytes at 1M distinct locators",
        PAGES,
        layout.CONTENT_SHARDS,
        lambda i: layout.shard_of(_mirror(i).media_id, layout.CONTENT_SHARDS),
        write_s3_mirrors,
    ),
    Scenario(
        "S4a",
        "page_observations_by_url",
        "one URL observed every minute for 31 days, new version each time",
        MINUTES,
        1,
        lambda i: 0,
        write_s4_history,
    ),
    Scenario(
        "S4b",
        "fetch_attempts_by_url",
        "one URL fetched every minute for 31 days (30-day TTL window)",
        MINUTES,
        1,
        lambda i: 0,
        write_s4_attempts,
    ),
    Scenario(
        "S5",
        "links_by_page_version",
        "one page version with the maximum 10 000 links",
        10_000,
        1,
        lambda i: 0,
        write_s5_links,
    ),
    Scenario(
        "S6a",
        "matches_by_target",
        "1M matches of one target in one month",
        PAGES,
        layout.MATCH_MONTH_SHARDS,
        lambda i: layout.shard_of(_match(i).match_id, layout.MATCH_MONTH_SHARDS),
        write_s6_matches,
    ),
    Scenario(
        "S6b",
        "evidence_by_target",
        "1M evidence items of one target in one month",
        PAGES,
        layout.MATCH_MONTH_SHARDS,
        lambda i: layout.shard_of(_evidence_id(i), layout.MATCH_MONTH_SHARDS),
        write_s6_evidence,
    ),
    Scenario(
        "S7",
        "media_observations",
        "one page observation with the maximum 1 000 media",
        1_000,
        1,
        lambda i: 0,
        write_s7_media_on_page,
    ),
    Scenario(
        "S8",
        "targets",
        "10 000 targets in the listing partition",
        10_000,
        1,
        lambda i: 0,
        write_s8_targets,
    ),
    Scenario(
        "S9",
        "outbox",
        f"one minute at {EVENTS_PER_S} events/s cluster-wide",
        EVENTS_PER_S * 60,
        layout.OUTBOX_SHARDS,
        lambda i: layout.shard_of(_event_id(i), layout.OUTBOX_SHARDS),
        write_s9_outbox,
    ),
]
"""S1c/S3b/S6a/S6b additionally fill secondary tables of the same write path (e.g. S4a
also fills page_versions_by_url, S3a media_content_versions); run.sh reports all tables."""


def cmd_write(args: argparse.Namespace) -> None:
    settings = Settings()
    session = ScyllaSession.connect(settings.scylla, keyspace=args.keyspace)
    if args.reset:
        session.execute_raw(f"DROP KEYSPACE IF EXISTS {args.keyspace}")
        Migrator(session, settings.scylla, applied_by=f"{settings.host_id}:bench").migrate()
    storage = ScyllaStorage.over(session)
    selected = [s for s in SCENARIOS if not args.only or s.code in args.only.split(",")]
    plan = []
    for scenario in selected:
        n = max(1, int(scenario.logical_rows * args.scale))
        started = time.perf_counter()
        shard_by_row = bytearray(scenario.shard_of(i) for i in range(n))  # shards < 256
        counts = Counter(shard_by_row)
        heaviest, rows = counts.most_common(1)[0]
        idx = [i for i, shard in enumerate(shard_by_row) if shard == heaviest]
        planned = time.perf_counter()
        scenario.write(storage, idx)
        written = time.perf_counter()
        values = [counts.get(s, 0) for s in range(scenario.shards)]
        entry = {
            "code": scenario.code,
            "table": scenario.table,
            "what": scenario.what,
            "logical_rows": n,
            "shards": scenario.shards,
            "heaviest_shard": heaviest,
            "rows_written": rows,
            "shard_rows": {
                "min": min(values),
                "max": max(values),
                "mean": round(statistics.fmean(values), 1),
                "stdev": round(statistics.pstdev(values), 1),
            },
            "plan_s": round(planned - started, 1),
            "write_s": round(written - planned, 1),
            "write_rows_per_s": round(rows / max(written - planned, 1e-9)),
        }
        plan.append(entry)
        print(json.dumps(entry), file=sys.stderr, flush=True)
    session.close()
    print(json.dumps({"scale": args.scale, "pages": PAGES, "scenarios": plan}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["write"])
    parser.add_argument("--keyspace", default="crawler2_bench")
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--only", default="")
    parser.add_argument("--reset", action="store_true")
    cmd_write(parser.parse_args())


if __name__ == "__main__":
    main()
