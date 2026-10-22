"""P12 against the real stack (validation group F): Scylla (V007 + E1/E2/E4 LWT),
MinIO (content-addressed artifacts and manifests), Redis Streams (consumer group,
pending-entry recovery) and the ``crawler2-evidence`` CLI.

The registry is populated with the synthetic fixture campaign of the unit tests
(``tests/unit/evidence/campaign.py``: reserved ``.example`` hosts, nothing is
fetched) and the matcher is simulated by publishing contract-valid
``match.found`` events. The statistics must equal the same hand-counted values.
With ``P12_CASE_OUT=<dir>`` the test also writes the case package there for review.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import redis
from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events import encode_event
from antipiracy_contracts.events.fingerprinting import MatchFound
from antipiracy_contracts.ids import EvidenceId
from antipiracy_contracts.models.matching import MatchResult, MatchVerdict
from antipiracy_contracts.models.targets import Target
from antipiracy_contracts.models.web import FetchAttempt, FetchOutcome, UrlRef
from antipiracy_contracts.ownership import Component

from crawler2.core.configuration import EventSettings, EvidenceSettings, Settings
from crawler2.evidence import cli
from crawler2.evidence.collector import EvidenceCollector, EvidenceLoop
from crawler2.evidence.export import export_case
from crawler2.evidence.finalize import EvidenceFinalizer
from crawler2.evidence.stats import campaign_stats, load_campaign
from crawler2.evidence.store import Campaign, ScyllaEvidenceStore
from crawler2.storage.events.consumer import IdempotentConsumer, RedisStreamReader
from crawler2.storage.events.publisher import OutboundEvent, RedisStreamPublisher, stream_name
from crawler2.storage.objectstore import S3ObjectStore
from crawler2.storage.repositories import EvidenceState
from crawler2.storage.scylla import ScyllaStorage
from tests.fixtures.contracts import (
    T0,
    WORKER,
    content_key,
    event_of,
    match,
    media,
    media_observation,
    page_observation,
    target,
)
from tests.unit.evidence.campaign import EXPECTED

pytestmark = pytest.mark.integration


@dataclass
class Stack:
    storage: ScyllaStorage
    objects: S3ObjectStore
    redis_bytes: redis.Redis
    events: EventSettings
    run: str
    store: ScyllaEvidenceStore

    def register(self, seed: str) -> Target:
        t = target(f"{self.run}-{seed}")
        self.storage.projections.apply_target_registered(t)
        return t

    def page(
        self,
        url: str,
        media_path: str,
        data: bytes,
        *,
        at: datetime = T0,
        cdn: str = "cdn.example",
        snapshot: bool = True,
    ) -> None:
        ref = UrlRef.of(url)
        html = f"<html>{self.run}<video src='https://{cdn}/{media_path}'></video></html>".encode()
        blob = self.objects.put(html, media_type="text/html") if snapshot else None
        page = page_observation(ref, at=at, body=html, snapshot=blob, seed=f"{self.run}:{url}:{at}")
        self.storage.pages.record(page)
        self.storage.fetch_attempts.record(
            FetchAttempt(
                fetch_attempt_id=page.fetch_attempt_id,
                requested=page.requested,
                capability=page.capability,
                worker=WORKER,
                started_at=at - timedelta(seconds=2),
                finished_at=at,
                outcome=FetchOutcome.RESPONSE,
                http_status=page.http_status,
                final=page.final,
                bytes_received=len(html),
            )
        )
        item = media(f"{self.run}/{media_path}", host=cdn)
        self.storage.media.record_observation(
            media_observation(item, page, content=content_key(data))
        )

    def publish(self, matches: list[MatchResult]) -> None:
        envs = [event_of(MatchFound(match=m), Component.MATCHER, at=m.decided_at) for m in matches]
        RedisStreamPublisher(self.redis_bytes, self.events).publish(
            [
                OutboundEvent(str(e.event_id), e.event_type, e.schema_major, encode_event(e))
                for e in envs
            ]
        )

    def reader(self, consumer: str) -> RedisStreamReader:
        r = RedisStreamReader(
            self.redis_bytes,
            stream_name(self.events, MatchFound.EVENT_TYPE, MatchFound.SCHEMA_MAJOR),
            "evidence-collector",
            consumer,
        )
        r.ensure_group()
        return r

    def collector(self) -> EvidenceCollector:
        s = self.storage
        return EvidenceCollector(
            evidence=s.evidence,
            store=self.store,
            media=s.media,
            pages=s.pages,
            attempts=s.fetch_attempts,
            projections=s.projections,
            actor=f"it:{self.run}",
        )

    def finalizer(self) -> EvidenceFinalizer:
        return EvidenceFinalizer(
            store=self.store,
            seals=self.storage.evidence,
            objects=self.objects,
            pages=self.storage.pages,
            producer_instance=WORKER,
            bucket=self.objects.bucket,
            max_artifact_bytes=EvidenceSettings().max_artifact_bytes,
        )


@pytest.fixture
def stack(
    storage: ScyllaStorage,
    object_store: S3ObjectStore,
    settings: Settings,
    event_settings: EventSettings,
) -> Iterator[Stack]:
    client = redis.Redis(host=settings.redis.host, port=settings.redis.port, db=settings.redis.db)
    yield Stack(
        storage,
        object_store,
        client,
        event_settings,
        uuid.uuid4().hex[:8],
        ScyllaEvidenceStore(storage.session),
    )
    client.close()


def _data(s: str, run: str) -> bytes:
    return f"{run}:{s}".encode()


def _fixture_campaign(s: Stack) -> tuple[Campaign, dict[str, MatchResult]]:
    """The unit fixture campaign (``tests/unit/evidence/campaign.py``) on the real stack."""
    r = s.run
    alpha, beta, delta = s.register("alpha"), s.register("beta"), s.register("delta")
    gamma = target(f"{r}-gamma")  # never registered in crawler2
    d1, d2, d3, d4 = (_data(x, r) for x in ("one", "two", "three", "four"))
    s.page(f"https://site-a.example/{r}/watch/1", "v/1.mp4", d1)
    s.page(f"https://site-a.example/{r}/watch/1", "v/1.mp4", d1, at=T0 + timedelta(minutes=30))
    s.page(f"https://site-a.example/{r}/watch/2", "v/1.mp4", d1, at=T0 + timedelta(minutes=5))
    s.page(f"https://mirror.example/{r}/e/9", "v/2.mp4", d2, cdn="cdn2.example", snapshot=False)
    s.page(f"https://other.example/{r}/x", "v/4.mp4", d4)
    decided = T0 + timedelta(hours=1)
    matches = {
        "m1": match(content_key(d1).content_id, alpha.ref, at=decided),
        "m2": match(content_key(d2).content_id, beta.ref, at=decided).model_copy(
            update={"verdict": MatchVerdict.PROBABLE, "confidence": 0.74}
        ),
        "m3": match(content_key(d3).content_id, alpha.ref, at=decided),
        "m4": match(content_key(d4).content_id, delta.ref, at=decided),
    }
    campaign = Campaign(
        campaign_id=f"fixture-{r}",
        name="P12 fixture campaign (synthetic, real stack)",
        since=T0,
        created_at=datetime.now(UTC),
        target_ids=tuple(
            sorted((alpha.ref.target_id, beta.ref.target_id, gamma.ref.target_id), key=str)
        ),
    )
    s.store.create_campaign(campaign)
    return campaign, matches


def _opened(s: Stack, m: MatchResult) -> EvidenceId:
    found = s.storage.evidence.opened(m.match_id)
    assert found is not None
    return found[0]


def test_fixture_campaign_end_to_end_on_the_real_stack(stack: Stack, tmp_path: Path) -> None:
    s = stack
    campaign, ms = _fixture_campaign(s)
    s.publish([ms["m1"], ms["m2"], ms["m3"], ms["m1"], ms["m4"]])  # m1 delivered twice

    # A worker reads the batch and dies before handling it (no ack): pending entries.
    crashed = s.reader("host-a:finalizer:1")
    assert len(crashed.read(count=100, block_ms=100)) == 5
    # A restarted worker takes them over (XAUTOCLAIM) and processes them idempotently.
    time.sleep(0.05)
    cfg = EvidenceSettings(claim_idle_ms=10, block_ms=100)
    consumer = IdempotentConsumer(
        cfg.consumer_group, MatchFound, s.collector().handle, s.storage.processed_events
    )
    loop = EvidenceLoop(s.reader("host-b:finalizer:1"), consumer, settings=cfg)
    assert loop.run_once() == 5
    loop.run(max_idle_polls=2)
    pending: Any = s.redis_bytes.xpending(crashed.stream, "evidence-collector")
    assert pending["pending"] == 0

    ids = {n: _opened(s, m) for n, m in ms.items()}
    assert len(set(ids.values())) == 4  # m1's duplicate created no second item
    c1 = s.store.candidate(ids["m1"])
    assert c1 is not None
    assert c1.revision == 1
    assert len(c1.dossier.sources) == 3

    f = s.finalizer()
    v1 = f.finalize(ids["m1"], actor="it").version
    assert f.finalize(ids["m1"], actor="it").result == "idempotent"
    f.finalize(ids["m2"], allow_partial=True, actor="it")
    record = s.storage.evidence.get(ids["m1"])
    assert record is not None
    assert record.state is EvidenceState.SEALED
    assert record.seal is not None
    assert record.seal.manifest.digest == v1.manifest_digest
    assert s.objects.get(record.seal.manifest)  # manifest bytes retrievable and verified

    # Real LWT: a different manifest under the same version is refused.
    applied, kept = s.store.claim_version(
        replace(v1, manifest_digest=ContentDigest("sha256:" + "0" * 64))
    )
    assert not applied
    assert kept.manifest_digest == v1.manifest_digest

    data = load_campaign(
        campaign,
        store=s.store,
        index=s.storage.evidence,
        projections=s.storage.projections,
        until=datetime.now(UTC),
    )
    stats = campaign_stats(data)
    for section, expected in EXPECTED.items():
        got = stats[section]
        if isinstance(expected, dict):
            assert {k: got[k] for k in expected} == expected, section
        else:
            assert got == expected, section

    first = export_case(
        data,
        objects=s.objects,
        bucket=s.objects.bucket,
        out=tmp_path / "a" / "case",
        include_artifacts=True,
        archive=True,
        max_artifact_bytes=10**7,
        max_package_bytes=10**8,
    )
    second = export_case(
        data,
        objects=s.objects,
        bucket=s.objects.bucket,
        out=tmp_path / "b" / "case",
        include_artifacts=True,
        archive=True,
        max_artifact_bytes=10**7,
        max_package_bytes=10**8,
    )
    assert first.failures == ()
    assert first.case_digest == second.case_digest
    assert first.archive is not None
    assert second.archive is not None
    assert first.archive.read_bytes() == second.archive.read_bytes()
    case = json.loads((tmp_path / "a" / "case" / "case.json").read_bytes())
    assert sorted(e["status"] for e in case["evidence"]) == ["complete", "partial"]

    out = os.environ.get("P12_CASE_OUT")
    if out:
        export_case(
            data,
            objects=s.objects,
            bucket=s.objects.bucket,
            out=Path(out),
            include_artifacts=True,
            archive=True,
            max_artifact_bytes=10**7,
            max_package_bytes=10**8,
        )


def test_supersession_and_audit_on_the_real_stack(stack: Stack) -> None:
    s = stack
    t = s.register("sup")
    d = _data("sup", s.run)
    s.page(f"https://site-s.example/{s.run}/a", "s/1.mp4", d)
    m = match(content_key(d).content_id, t.ref, at=T0 + timedelta(hours=1))
    c = s.collector()
    assert c.collect(m)[0] == "created"
    eid = _opened(s, m)
    v1 = s.finalizer().finalize(eid, actor="it").version
    s.page(f"https://site-s.example/{s.run}/b", "s/1.mp4", d, at=T0 + timedelta(minutes=9))
    assert c.collect(m)[0] == "frozen"
    assert c.collect(m, refresh=True)[0] == "updated"
    v2 = s.finalizer().supersede(eid, reason="second page", actor="it").version
    assert v2.version == 2
    assert v2.supersedes == 1
    assert [v.version for v in s.store.versions(eid)] == [2, 1]
    assert s.store.versions(eid)[1] == v1
    actions = [a.action for a in s.store.audit_log(eid)]
    assert actions[0] == "candidate_created"
    assert {"finalized", "candidate_updated", "superseded"} <= set(actions)


def test_cli_against_the_real_stack(
    stack: Stack,
    settings: Settings,
    keyspace: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    s = stack
    t = s.register("cli")
    d = _data("cli", s.run)
    s.page(f"https://site-c.example/{s.run}/a", "c/1.mp4", d)
    m = match(content_key(d).content_id, t.ref, at=T0 + timedelta(hours=1))
    s.collector().collect(m)
    eid = str(_opened(s, m))
    monkeypatch.setenv("CRAWLER2_SCYLLA__KEYSPACE", keyspace)
    monkeypatch.setenv("CRAWLER2_MINIO__BUCKET_RAW", s.objects.bucket)
    monkeypatch.setenv("CRAWLER2_SCRATCH_DIR", str(tmp_path / "scratch"))

    def run(*argv: str) -> tuple[int, list[dict[str, Any]]]:
        code = cli.main(list(argv))
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
        return code, [json.loads(ln) for ln in lines]

    camp = f"cli-{s.run}"
    assert (
        run(
            "campaign",
            "create",
            camp,
            "--name",
            "CLI",
            "--since",
            "2026-06",
            "--target",
            str(t.ref.target_id),
        )[0]
        == 0
    )
    code, rows = run("list", "--campaign", camp)
    assert code == 0
    assert [r["evidence_id"] for r in rows] == [eid]
    code, (doc,) = run("validate", eid)
    assert code == 0
    assert doc["outcome"] == "complete"
    code, (doc,) = run("finalize", eid, "--dry-run")
    assert code == 0
    assert doc["dry_run"] is True
    assert s.store.versions(EvidenceId(eid)) == []
    code, (doc,) = run("finalize", eid, "--actor", "cli-test")
    assert code == 0
    assert doc["result"] == "finalized"
    code, (doc,) = run("finalize", eid)
    assert doc["result"] == "idempotent"
    code, (doc,) = run("supersede", eid, "--reason", "no change")
    assert code == cli.EXIT_REFUSED
    code, (doc,) = run("show", eid, "--audit")
    assert doc["status"] == "finalized"
    assert any(a["action"] == "finalized" for a in doc["audit"])
    code, (doc,) = run("campaign", "stats", camp)
    assert doc["evidence"]["finalized"] == 1
    out = tmp_path / "case"
    code, (doc,) = run("export", "--campaign", camp, "--out", str(out))
    assert code == 0
    assert doc["failures"] == []
    assert (out / "report.html").exists()
    code, _ = run("export", "--campaign", camp, "--out", str(out))
    assert code != 0  # refuses to overwrite
    code, _ = run("show", str(EvidenceId.new()))
    assert code == cli.EXIT_NOT_FOUND
