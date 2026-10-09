"""P8 increment 2 on the real Scylla and Redis: probe and hash workers write
authoritative facts with provenance, the latest view, M5/M6 from hash facts (P2 write
rules, commuting with the observation path), the stale-hash rule, one probe/hash per
media under concurrency, and rebuild (design §5, §6)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from antipiracy_contracts.ids import MediaId
from antipiracy_contracts.models.media import Media, MediaKind, ProbeStatus
from antipiracy_contracts.models.web import UrlRef

from crawler2.core.configuration import MediaSettings, Settings
from crawler2.core.configuration.settings import HttpFetchSettings
from crawler2.frontier.redis import connect_redis
from crawler2.media import store as store_module
from crawler2.media.hashing import FullContentHasher, HashLimits
from crawler2.media.model import HashOutcome, HashRecord, ProbeRecord
from crawler2.media.probe import HashWorker, ProbeWorker, guarded, probe_fetcher
from crawler2.media.store import ScyllaRegistryStore
from crawler2.media.work import Job, RedisWork
from crawler2.storage.layout import month_bucket
from crawler2.storage.scylla import ScyllaStorage
from crawler2.storage.scylla.session import Consistency
from tests.fixtures.contracts import media_observation, page_observation
from tests.fixtures.mediaweb import MediaSite, RoutingTransport, blob, media_site, public_resolver

pytestmark = pytest.mark.integration
KIB = 1024
EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture
def site() -> Iterator[MediaSite]:
    with media_site() as s:
        yield s


@pytest.fixture
def work(settings: Settings) -> Iterator[RedisWork]:
    client = connect_redis(settings.redis)
    namespace = f"it-p8w-{uuid.uuid4().hex[:10]}"
    yield RedisWork(client, namespace=namespace)
    for key in client.scan_iter(match=f"{namespace}:*"):
        client.delete(key)
    client.close()


class Rig:
    """Probe and hash workers over the real stores, talking to the fixture media web."""

    def __init__(
        self, storage: ScyllaStorage, work: RedisWork, site: MediaSite, **media: object
    ) -> None:
        self.storage = storage
        self.store = ScyllaRegistryStore(storage.session)
        self.work = work
        self.site = site
        self.settings = MediaSettings(probe_per_host_interval_s=0, **media)  # type: ignore[arg-type]
        self.run = uuid.uuid4().hex[:8]

    def transport(self) -> object:
        return guarded(self.settings, resolver=public_resolver(), inner=RoutingTransport(self.site))

    def job(self, path: str, kind: MediaKind = MediaKind.VIDEO_FILE) -> Job:
        url = UrlRef.of(f"https://cdn-{self.run}.example{path}").url
        return Job(str(MediaId.of_locator(url)), url, kind.value, referer="https://site.example/")

    async def probe(self, *jobs: Job, worker: str = "w1") -> list[ProbeRecord | None]:
        fetcher = probe_fetcher(HttpFetchSettings(), self.transport(), connections=8)  # type: ignore[arg-type]
        w = ProbeWorker(fetcher, self.store, self.work, worker=worker, settings=self.settings)
        try:
            return list(await asyncio.gather(*(w.handle(j) for j in jobs)))
        finally:
            await fetcher.close()

    async def hash(self, *jobs: Job, worker: str = "w1") -> list[HashRecord | None]:
        hasher = FullContentHasher(self.transport(), HashLimits())  # type: ignore[arg-type]
        w = HashWorker(
            hasher, self.store, self.work, worker=worker, settings=self.settings, requeue_delay_s=0
        )
        try:
            return list(await asyncio.gather(*(w.handle(j) for j in jobs)))
        finally:
            await hasher.close()

    def queued_hash(self) -> Job:
        job = self.work.pop("hash", timeout_s=1)
        assert job is not None
        return job


def test_probe_then_hash_writes_facts_latest_and_m5_m6(
    storage: ScyllaStorage, work: RedisWork, site: MediaSite
) -> None:
    rig = Rig(storage, work, site)
    body = blob(f"m-{rig.run}", 300 * KIB)
    site.put("/a.mp4", body)
    job = rig.job("/a.mp4")
    (p,) = asyncio.run(rig.probe(job))
    assert p is not None
    assert p.probe_status is ProbeStatus.PROBED
    hash_job = rig.queued_hash()  # the probe wanted a hash
    assert hash_job.probe_id == p.probe_id
    (h,) = asyncio.run(rig.hash(hash_job))
    assert h is not None
    assert h.outcome is HashOutcome.OK
    media = MediaId(job.media_id)
    window = {"since": EPOCH, "until": datetime.now(UTC) + timedelta(days=1)}
    assert rig.store.probes(media, **window) == [p]  # authoritative probe fact
    (stored,) = rig.store.hashes(media, **window)
    assert stored == h
    assert stored.worker == "w1"
    assert stored.final_url == job.locator_url
    latest = rig.store.latest(media)
    assert latest is not None
    assert latest.probe == p
    assert latest.ok_hash == h
    assert latest.current_content() == h.content
    assert h.content_id is not None
    m5 = storage.media.by_content(h.content_id)
    assert [m.media_id for m in m5] == [media]
    (m6,) = storage.media.content_versions(media, **window)
    assert m6.content_id == h.content_id
    assert m6.first_seen == m6.last_seen == h.hashed_at
    assert not rig.work.depth("hash")  # nothing more wanted


def test_same_url_changed_bytes_new_content_id_and_version(
    storage: ScyllaStorage, work: RedisWork, site: MediaSite
) -> None:
    rig = Rig(storage, work, site, reprobe_after_s=1)
    site.put("/c.mp4", blob(f"v1-{rig.run}", 200 * KIB))
    job = rig.job("/c.mp4")
    asyncio.run(rig.probe(job))
    (first,) = asyncio.run(rig.hash(rig.queued_hash()))
    assert first is not None
    media = MediaId(job.media_id)

    site.replace("/c.mp4", blob(f"v2-{rig.run}", 200 * KIB))
    asyncio.run(asyncio.sleep(1.1))  # past reprobe_after (1 s, Redis TIME)
    (p2,) = asyncio.run(rig.probe(job))
    assert p2 is not None
    latest = rig.store.latest(media)
    assert latest is not None
    # Stale-hash rule: the old ok hash no longer matches the latest probe's validators.
    assert latest.ok_hash == first
    assert latest.current_content() is None
    (second,) = asyncio.run(rig.hash(rig.queued_hash()))
    assert second is not None
    assert second.outcome is HashOutcome.OK
    assert second.content_id != first.content_id
    window = {"since": EPOCH, "until": datetime.now(UTC) + timedelta(days=1)}
    versions = {v.content_id for v in storage.media.content_versions(media, **window)}
    assert versions == {first.content_id, second.content_id}  # a new M6 version row
    assert rig.store.latest(media).current_content() == second.content  # type: ignore[union-attr]


def test_stale_hash_is_not_attached_to_new_observations(
    storage: ScyllaStorage, work: RedisWork, site: MediaSite
) -> None:
    """The resolver's view (``media_probe_latest``) drops a hash whose validators no
    longer match the latest probe, so the next observation carries hints only."""
    from tests.fixtures.media import Bench, discovered, reference

    rig = Rig(storage, work, site)
    site.put("/s.mp4", blob(f"s-{rig.run}", 150 * KIB))
    job = rig.job("/s.mp4")
    asyncio.run(rig.probe(job))
    asyncio.run(rig.hash(rig.queued_hash()))
    media = MediaId(job.media_id)
    latest = rig.store.latest(media)
    assert latest is not None
    assert latest.current_content() is not None
    assert latest.probe is not None
    changed = replace(
        latest.probe, probe_id=str(uuid.uuid4()), etag='"other"', probed_at=datetime.now(UTC)
    )
    rig.store.record_probe(changed)  # a newer probe with other validators

    bench = Bench.create(storage)
    page = bench.page("stale")
    bench.put(discovered(page, [reference(job.locator_url)]))
    assert bench.resolver("A", views=rig.store).run_once().committed == 1
    observation = storage.media.observation(page.observation_id, media)
    assert observation is not None
    assert observation.probe_status is ProbeStatus.PROBED
    assert observation.content is None  # never a key P8 has not verified for this version
    assert observation.hints is not None
    assert observation.hints.etag == '"other"'


def test_one_probe_and_one_hash_per_media_under_concurrency(
    storage: ScyllaStorage, work: RedisWork, site: MediaSite
) -> None:
    rig = Rig(storage, work, site)
    site.put("/k.mp4", blob(f"k-{rig.run}", 250 * KIB), stall_s=0.01)
    job = rig.job("/k.mp4")

    async def probes() -> list[ProbeRecord | None]:
        a, b = rig.probe(job, job, worker="host-1"), rig.probe(job, job, worker="host-2")
        return [x for xs in await asyncio.gather(a, b) for x in xs]

    done = [p for p in asyncio.run(probes()) if p is not None]
    media = MediaId(job.media_id)
    window = {"since": EPOCH, "until": datetime.now(UTC) + timedelta(days=1)}
    assert len(done) == 1
    assert len(rig.store.probes(media, **window)) == 1
    hash_job = rig.queued_hash()

    async def hashes() -> list[HashRecord | None]:
        a = rig.hash(hash_job, hash_job, worker="host-1")
        b = rig.hash(hash_job, hash_job, worker="host-2")
        return [x for xs in await asyncio.gather(a, b) for x in xs]

    hashed = [h for h in asyncio.run(hashes()) if h is not None]
    assert len(hashed) == 1
    assert len(rig.store.hashes(media, **window)) == 1


def test_hash_and_observation_paths_commute_and_rebuild_restores(
    storage: ScyllaStorage, work: RedisWork, site: MediaSite
) -> None:
    rig = Rig(storage, work, site)
    site.put("/r.mp4", blob(f"r-{rig.run}", 120 * KIB))
    job = rig.job("/r.mp4")
    asyncio.run(rig.probe(job))
    (h,) = asyncio.run(rig.hash(rig.queued_hash()))
    assert h is not None
    assert h.content is not None
    media = MediaId(job.media_id)
    item = Media.of(UrlRef.of(job.locator_url), MediaKind.VIDEO_FILE)
    # An observation carrying the same content at an earlier instant (observation path).
    early = h.hashed_at - timedelta(days=2)
    page = page_observation(UrlRef.of(f"https://site.example/{rig.run}"), at=early)
    observation = media_observation(item, page, content=h.content)
    storage.media.record_observation(observation)
    window = {"since": EPOCH, "until": datetime.now(UTC) + timedelta(days=1)}
    versions = storage.media.content_versions(media, **window)
    assert {v.content_id for v in versions} == {h.content_id}
    merged = versions[0]
    assert merged.first_seen == early  # earliest of observation and hash time
    assert merged.last_seen == h.hashed_at  # latest of both
    (by_content,) = storage.media.by_content(h.content.content_id)
    assert by_content.first_seen == early

    storage.media.rebuild_media(media, **window)
    assert storage.media.content_versions(media, **window) == versions


def test_rebuild_repairs_lost_derived_writes(storage: ScyllaStorage) -> None:
    """A crash between the authoritative probe/hash rows and their derived writes
    leaves the latest view and M5/M6 missing; ``rebuild`` re-derives them (B.5 #3)."""
    store = ScyllaRegistryStore(storage.session)
    url = UrlRef.of(f"https://cdn-{uuid.uuid4().hex[:8]}.example/lost.mp4").url
    media = MediaId.of_locator(url)
    at = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    probe = ProbeRecord(
        probe_id=str(uuid.uuid4()),
        media_id=str(media),
        locator_url=url,
        kind=MediaKind.VIDEO_FILE,
        probed_at=at,
        probe_status=ProbeStatus.PROBED,
        worker="w",
        fetch_outcome="media",
        total_length=10,
        etag='"x"',
    )
    digest = "sha256:" + "c" * 64
    record = HashRecord(
        hash_id=str(uuid.uuid4()),
        media_id=str(media),
        locator_url=url,
        hashed_at=at + timedelta(minutes=1),
        outcome=HashOutcome.OK,
        worker="w",
        probe_id=probe.probe_id,
        validators=probe.validators,
        digest=digest,
        byte_count=10,
    )
    session = storage.session
    session.execute(
        session.bind(
            store_module._PROBE_INSERT,
            Consistency.AUTHORITATIVE_WRITE,
            (media.uuid, month_bucket(at), at, uuid.UUID(probe.probe_id), probe.to_json()),
        )
    )
    session.execute(
        session.bind(
            store_module._HASH_INSERT,
            Consistency.AUTHORITATIVE_WRITE,
            (
                media.uuid,
                month_bucket(record.hashed_at),
                record.hashed_at,
                uuid.UUID(record.hash_id),
                "ok",
                record.content_id.uuid,  # type: ignore[union-attr]
                record.to_json(),
            ),
        )
    )
    window = {"since": EPOCH, "until": datetime.now(UTC) + timedelta(days=1)}
    assert store.latest(media) is None  # the derived writes were lost
    assert store.rebuild(media, **window) == (1, 1)
    latest = store.latest(media)
    assert latest is not None
    assert latest.probe == probe
    assert latest.ok_hash == record
    assert latest.current_content() == record.content
    assert record.content_id is not None
    assert [m.media_id for m in storage.media.by_content(record.content_id)] == [media]
    (version,) = storage.media.content_versions(media, **window)
    assert version.content_id == record.content_id
    assert store.rebuild(media, **window) == (1, 1)  # idempotent
    assert store.latest(media) == latest


def test_busy_host_requeues_and_cli_show_rebuild(
    storage: ScyllaStorage,
    work: RedisWork,
    site: MediaSite,
    keyspace: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rig = Rig(storage, work, site)
    site.put("/b.mp4", blob(f"b-{rig.run}", 100 * KIB))
    job = rig.job("/b.mp4")
    asyncio.run(rig.probe(job))
    hash_job = rig.queued_hash()
    slot = work.host_slot(
        f"cdn-{rig.run}.example", "someone-else", slots=1, ttl_s=30
    )  # the host's only hash slot is taken
    assert slot is not None
    (busy,) = asyncio.run(rig.hash(hash_job))
    assert busy is None
    assert work.depth("hash") == 1  # put back, not lost
    work.release_slot(slot, "someone-else")
    (done,) = asyncio.run(rig.hash(rig.queued_hash()))
    assert done is not None
    assert done.outcome is HashOutcome.OK

    from crawler2.media import cli

    monkeypatch.setenv("CRAWLER2_SCYLLA__KEYSPACE", keyspace)
    assert cli.main(["show", job.locator_url]) == 0
    shown = capsys.readouterr().out
    assert str(done.content_id) in shown
    assert cli.main(["rebuild", job.media_id, "--since", "2020-01-01T00:00:00+00:00"]) == 0
    assert '"hashes": 1' in capsys.readouterr().out
