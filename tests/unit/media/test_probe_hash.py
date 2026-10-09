"""Probe (P4 media probe through the guard) and the full-content hash reader against the
loopback fixture media web: Range honoured/ignored, redirects, 403, non-media, too large,
timeout, unreachable, content encoding, transfer framing, changing content, manifests,
and full SHA-256 / crafted-collision checks (design §3, §5)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from antipiracy_contracts.ids import MediaId
from antipiracy_contracts.models.media import MediaKind, ProbeStatus
from antipiracy_contracts.models.web import UrlRef

from crawler2.core.configuration import MediaSettings
from crawler2.core.configuration.settings import HttpFetchSettings
from crawler2.crawlers.model import Expectation, FetchRequest
from crawler2.media.hashing import FullContentHasher, HashLimits, same_version
from crawler2.media.model import HashOutcome, HashRecord, ProbeRecord, Validators
from crawler2.media.probe import guarded, probe_fetcher, probe_record
from crawler2.media.schedule import hashable
from crawler2.media.work import Job
from tests.fixtures.mediaweb import (
    MediaSite,
    RoutingTransport,
    blob,
    crafted_twin,
    media_site,
    public_resolver,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)
KIB = 1024
BODY = blob("movie-a", 400 * KIB)


@pytest.fixture
def site() -> Iterator[MediaSite]:
    with media_site() as s:
        yield s


def _job(path: str, kind: MediaKind = MediaKind.VIDEO_FILE) -> Job:
    url = UrlRef.of(f"https://cdn.example{path}").url
    return Job(str(MediaId.of_locator(url)), url, kind.value, referer="https://site.example/p")


async def _probe_async(site: MediaSite, job: Job) -> ProbeRecord:
    transport = guarded(MediaSettings(), resolver=public_resolver(), inner=RoutingTransport(site))
    fetcher = probe_fetcher(HttpFetchSettings(), transport, connections=4)
    try:
        result = await fetcher.fetch(
            FetchRequest(
                url=job.locator_url,
                url_id=UrlRef.of(job.locator_url).url_id,
                expect=Expectation.MEDIA_PROBE,
            )
        )
    finally:
        await fetcher.close()
    return probe_record(job, result, worker="t", at=NOW)


def probe(site: MediaSite, path: str, kind: MediaKind = MediaKind.VIDEO_FILE) -> ProbeRecord:
    return asyncio.run(_probe_async(site, _job(path, kind)))


async def _hash_async(site: MediaSite, record: ProbeRecord, limits: HashLimits) -> HashRecord:
    transport = guarded(MediaSettings(), resolver=public_resolver(), inner=RoutingTransport(site))
    hasher = FullContentHasher(transport, limits)
    try:
        return await hasher.hash(record, worker="t", now=NOW, referer="https://site.example/p")
    finally:
        await hasher.close()


def full_hash(site: MediaSite, record: ProbeRecord, **limits: object) -> HashRecord:
    return asyncio.run(_hash_async(site, record, HashLimits(**limits)))  # type: ignore[arg-type]


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# --- probe ---------------------------------------------------------------------------


def test_probe_with_range_honoured_reads_at_most_64_kib(site: MediaSite) -> None:
    site.put("/v.mp4", BODY)
    p = probe(site, "/v.mp4")
    assert p.probe_status is ProbeStatus.PROBED
    assert p.range_honoured
    assert p.total_length == len(BODY)  # from Content-Range
    assert p.validators == Validators(len(BODY), p.etag, p.last_modified)
    assert p.bytes_read <= 64 * KIB + 1024  # headers + 64 KiB (P4 D2 invariant)
    assert site.seen("/v.mp4")[0]["range"] == "bytes=0-65535"
    assert site.bytes_sent["/v.mp4"] <= 64 * KIB


def test_probe_with_range_ignored_still_stops_at_64_kib(site: MediaSite) -> None:
    site.put("/n.mp4", BODY, honour_range=False)
    p = probe(site, "/n.mp4")
    assert p.probe_status is ProbeStatus.PROBED
    assert not p.range_honoured
    assert p.total_length is None
    assert p.declared_length == len(BODY)
    assert p.validators.length == len(BODY)  # a full response's length is the size
    assert p.bytes_read < len(BODY)


def test_probe_redirect_is_provenance_not_identity(site: MediaSite) -> None:
    site.put("/final.mp4", BODY)
    site.put("/go.mp4", redirect="https://mirror.example/final.mp4")
    p = probe(site, "/go.mp4")
    assert p.probe_status is ProbeStatus.PROBED
    assert p.redirect_count == 1
    assert p.final_url == "https://mirror.example/final.mp4"
    assert p.locator_url == "https://cdn.example/go.mp4"  # the MediaId stays the locator's
    assert p.media_id == str(MediaId.of_locator(UrlRef.of("https://cdn.example/go.mp4").url))


@pytest.mark.parametrize(
    ("status", "expected"),
    [(403, ProbeStatus.FORBIDDEN), (401, ProbeStatus.FORBIDDEN), (404, ProbeStatus.UNREACHABLE)],
)
def test_probe_http_errors(site: MediaSite, status: int, expected: ProbeStatus) -> None:
    site.put("/e.mp4", status=status)
    assert probe(site, "/e.mp4").probe_status is expected


def test_probe_non_media_and_unreachable(site: MediaSite) -> None:
    site.put("/page.mp4", b"<html><body>a player page</body></html>", content_type="text/html")
    assert probe(site, "/page.mp4").probe_status is ProbeStatus.NOT_MEDIA
    job = Job("x", "https://dead.example/v.mp4", "video_file")
    job = replace(job, media_id=str(MediaId.of_locator(UrlRef.of(job.locator_url).url)))
    dead = asyncio.run(_probe_async(site, job))
    assert dead.probe_status is ProbeStatus.UNREACHABLE


def test_manifest_is_probed_for_hints_but_never_hashed(site: MediaSite) -> None:
    site.put(
        "/hls/master.m3u8",
        b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nlow/index.m3u8\n",
        content_type="application/vnd.apple.mpegurl",
    )
    p = probe(site, "/hls/master.m3u8", MediaKind.HLS_MANIFEST)
    assert p.probe_status is ProbeStatus.PROBED
    assert p.fetch_detail == "manifest"
    assert not hashable(p)


def test_unknown_kind_hashed_only_when_the_probe_shows_media(site: MediaSite) -> None:
    site.put("/blob", BODY[: 200 * KIB], content_type="application/octet-stream")
    site.put("/clip", b"\x00\x00\x00\x20ftypisom" + BODY[: 100 * KIB], content_type="video/mp4")
    assert not hashable(probe(site, "/blob", MediaKind.UNKNOWN))
    p = probe(site, "/clip", MediaKind.UNKNOWN)
    assert p.container == "mp4"
    assert hashable(p)


# --- full-content hash (full-bytes/v1) -------------------------------------------------


def test_full_sha256_of_known_bytes_with_and_without_range_support(site: MediaSite) -> None:
    site.put("/a.mp4", BODY)
    site.put("/b.mp4", BODY, honour_range=False)
    for path in ("/a.mp4", "/b.mp4"):
        record = full_hash(site, probe(site, path))
        assert record.outcome is HashOutcome.OK, record.detail
        assert record.digest == sha(BODY)
        assert record.byte_count == len(BODY)
        assert record.content is not None
        assert record.content.scheme == "full-bytes/v1"
        headers = site.seen(path)[-1]
        assert headers["accept-encoding"] == "identity"
        assert "range" not in headers
        assert headers["referer"] == "https://site.example/p"
    assert site.seen("/a.mp4", "HEAD")  # validators re-checked after the read


def test_transfer_framing_is_removed_and_bytes_hashed_exactly(site: MediaSite) -> None:
    site.put("/chunked.mp4", BODY, chunked=True)
    record = full_hash(site, probe(site, "/chunked.mp4"))
    assert record.outcome is HashOutcome.OK
    assert record.digest == sha(BODY)  # chunk-size lines and CRLFs are not content


def test_content_encoding_is_refused_not_decoded(site: MediaSite) -> None:
    site.put("/gz.mp4", BODY, gzip=True)
    record = full_hash(site, probe(site, "/gz.mp4"))
    assert record.outcome is HashOutcome.CONTENT_ENCODED
    assert record.digest is None
    assert record.content_id is None


def test_crafted_collision_gets_a_distinct_content_id(site: MediaSite) -> None:
    twin = crafted_twin(BODY)
    sample = 64 * KIB
    middle = len(BODY) // 2 - sample // 2
    assert len(twin) == len(BODY)
    assert twin[:sample] == BODY[:sample]
    assert twin[middle : middle + sample] == BODY[middle : middle + sample]
    assert twin[-sample:] == BODY[-sample:]
    assert twin != BODY
    site.put("/orig.mp4", BODY)
    site.put("/twin.mp4", twin)
    a = full_hash(site, probe(site, "/orig.mp4"))
    b = full_hash(site, probe(site, "/twin.mp4"))
    assert a.outcome is b.outcome is HashOutcome.OK
    assert a.content_id != b.content_id
    assert a.digest == sha(BODY)
    assert b.digest == sha(twin)


def test_byte_identical_copies_share_one_content_id(site: MediaSite) -> None:
    for host_path in ("/x1.mp4", "/x2.mp4", "/x3.mp4"):
        site.put(host_path, BODY)
    ids = {full_hash(site, probe(site, p)).content_id for p in ("/x1.mp4", "/x2.mp4", "/x3.mp4")}
    assert len(ids) == 1


def test_too_large_by_declared_length_and_while_streaming(site: MediaSite) -> None:
    site.put("/big.mp4", BODY)
    declared = full_hash(site, probe(site, "/big.mp4"), max_bytes=100 * KIB)
    assert declared.outcome is HashOutcome.TOO_LARGE
    assert declared.byte_count == 0  # refused before reading
    site.put("/bigc.mp4", BODY, chunked=True)
    streamed = full_hash(site, probe(site, "/bigc.mp4"), max_bytes=100 * KIB)
    assert streamed.outcome is HashOutcome.TOO_LARGE
    assert 100 * KIB < streamed.byte_count <= 100 * KIB + 64 * KIB + 16
    assert streamed.content_id is None


def test_timeout_forbidden_unreachable_http_error(site: MediaSite) -> None:
    site.put("/slow.mp4", BODY, stall_s=0.2)
    slow = full_hash(site, probe(site, "/slow.mp4"), timeout_s=0.5)
    assert slow.outcome is HashOutcome.TIMEOUT
    assert slow.content_id is None
    p = probe(site, "/slow.mp4")
    site.put("/slow.mp4", status=403)
    assert full_hash(site, p).outcome is HashOutcome.FORBIDDEN
    site.put("/slow.mp4", status=404)
    assert full_hash(site, p).outcome is HashOutcome.UNREACHABLE
    site.put("/slow.mp4", status=500)
    assert full_hash(site, p).outcome is HashOutcome.HTTP_ERROR
    dead = replace(p, locator_url="https://dead.example/v.mp4")
    assert full_hash(site, dead).outcome is HashOutcome.UNREACHABLE


def test_content_changed_since_the_probe_or_during_the_read(site: MediaSite) -> None:
    site.put("/c.mp4", BODY)
    p = probe(site, "/c.mp4")
    site.replace("/c.mp4", blob("movie-a-v2", len(BODY)))
    record = full_hash(site, p)
    assert record.outcome is HashOutcome.CHANGED_DURING_READ
    assert record.content_id is None

    site.put("/d.mp4", BODY, stall_s=0.02)
    p = probe(site, "/d.mp4")

    async def change_mid_read() -> HashRecord:
        task = asyncio.create_task(_hash_async(site, p, HashLimits()))
        while site.bytes_sent["/d.mp4"] <= 64 * KIB:  # the GET is streaming
            await asyncio.sleep(0.005)
        site.replace("/d.mp4", blob("movie-a-v3", len(BODY)))
        return await task

    during = asyncio.run(change_mid_read())
    assert during.outcome is HashOutcome.CHANGED_DURING_READ
    assert during.content_id is None


def test_same_version_compares_only_stated_validators() -> None:
    assert same_version(Validators(10, '"a"', None), Validators(None, '"a"', "x"))
    assert not same_version(Validators(10, None, None), Validators(11, None, None))
    assert same_version(Validators(), Validators(5, '"z"', "y"))
