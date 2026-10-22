"""P13 against the real Scylla stack (validation group I): token-range export of a
throwaway keyspace, reconciliation against independent ``count(*)``, idempotent and
changed re-export, reports/graph/clustering on the result.

Rows are written through the real repositories and the real P12 collector/finalizer
(E5/E6 documents as produced in production code), on reserved ``.example`` hosts with
SYNTHETIC matches — the P10 matcher has not passed M3, so no real match exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from antipiracy_contracts.models.matching import MatchVerdict
from antipiracy_contracts.models.web import UrlRef

from crawler2.analytics.datasets import SOURCES
from crawler2.analytics.export import Exporter, ExportOptions
from crawler2.analytics.graph import build_graph
from crawler2.analytics.query import ExportView, validate_revision
from crawler2.analytics.reports import campaign_report, source_report
from crawler2.analytics.source import ScyllaRowSource
from crawler2.evidence.store import Campaign
from crawler2.extraction.extract import extract
from crawler2.storage.repositories import ExtractRecord
from tests.fixtures.contracts import (
    T0,
    content_key,
    fetch_attempt,
    match,
    media,
    media_observation,
    page_observation,
)
from tests.integration.storage.test_evidence import Stack, stack  # noqa: F401 -- fixture

pytestmark = pytest.mark.integration


def _page(s: Stack, url: str, media_path: str | None, data: bytes | None, at: datetime) -> None:
    ref = UrlRef.of(url)
    video = f"<video src='https://cdn.example/{s.run}/{media_path}'></video>" if media_path else ""
    body = (
        f"<html><head><title>{s.run}</title></head><body><a href='https://site-b.example/{s.run}/"
        f"x'>x</a><p>synthetic</p>{video}</body></html>"
    ).encode()
    blob = s.objects.put(body, media_type="text/html")
    obs = page_observation(ref, at=at, body=body, snapshot=blob, seed=f"{s.run}:{url}:{at}")
    s.storage.pages.record(obs)
    s.storage.fetch_attempts.record(
        fetch_attempt(ref, at=at - timedelta(seconds=1)).model_copy(
            update={"fetch_attempt_id": obs.fetch_attempt_id}
        )
    )
    ex = extract(body, page=ref, content_type="text/html")
    s.storage.urls.record_discovered([ref, *(lk.target for lk in ex.links)], seen_at=at)
    s.storage.page_intel.record_extract(
        ExtractRecord(
            page_version_id=ex.page_version_id,
            url_id=ref.url_id,
            extractor=ex.extractor,
            normalization=ex.normalization,
            revision_id=ex.revision_id,
            normalized_digest=ex.hashes.normalized,
            observed_at=at,
            doc=ex.model_dump_json(),
        )
    )
    if media_path and data is not None:
        item = media(f"{s.run}/{media_path}")
        s.storage.media.record_observation(media_observation(item, obs, content=content_key(data)))


def _exporter(s: Stack, root: Path, **options: object) -> Exporter:
    opts: dict[str, object] = {"segments": 8, "retry_backoff_s": 0.0, "duckdb_threads": 1}
    return Exporter(
        ScyllaRowSource(s.storage.session),
        root,
        options=ExportOptions(**(opts | options)),  # type: ignore[arg-type]
        sleep=lambda _s: None,
    )


def test_real_keyspace_export_reconciles_and_is_idempotent(stack: Stack, tmp_path: Path) -> None:  # noqa: F811
    s = stack
    alpha, beta = s.register("p13-alpha"), s.register("p13-beta")
    d1, d2 = f"{s.run}:one".encode(), f"{s.run}:two".encode()
    _page(s, f"https://site-a.example/{s.run}/watch/1", "v/1.mp4", d1, T0)
    _page(s, f"https://site-a.example/{s.run}/watch/2", "v/1.mp4", d1, T0 + timedelta(hours=1))
    _page(s, f"https://mirror.example/{s.run}/e/9", "v/2.mp4", d2, T0 + timedelta(days=1))
    m1 = match(content_key(d1).content_id, alpha.ref, at=T0 + timedelta(days=1))
    m2 = match(content_key(d2).content_id, beta.ref, at=T0 + timedelta(days=1)).model_copy(
        update={"verdict": MatchVerdict.PROBABLE, "confidence": 0.74}
    )
    collector = s.collector()
    collector.collect(m1)
    collector.collect(m2)
    e1, e2 = (s.storage.evidence.opened(m.match_id) for m in (m1, m2))
    assert e1 is not None
    assert e2 is not None
    s.finalizer().finalize(e1[0], actor="it")
    s.finalizer().finalize(e2[0], allow_partial=True, actor="it")
    campaign_id = f"p13-{s.run}"
    s.store.create_campaign(
        Campaign(
            campaign_id=campaign_id,
            name="P13 synthetic campaign (real stack)",
            since=T0,
            created_at=datetime.now(UTC),
            target_ids=tuple(sorted((alpha.ref.target_id, beta.ref.target_id), key=str)),
        )
    )
    until = datetime.now(UTC) + timedelta(minutes=1)
    exporter = _exporter(s, tmp_path, verify_counts=True)
    first = exporter.export(until=until)
    assert first.outcome == "published"
    assert first.revision_dir is not None
    docs = first.manifest["datasets"]
    assert {d["status"] for d in docs.values()} <= {"complete", "empty"}, {
        n: d.get("reason") or d.get("problems") for n, d in docs.items()
    }
    ks = s.storage.session.keyspace
    for table in SOURCES:
        (row,) = s.storage.session.execute_raw(f"SELECT count(*) FROM {ks}.{table.table}")
        for d in table.datasets:
            assert docs[d.name]["counts"]["source_rows"] == row[0], d.name
            assert docs[d.name]["counts"]["rejected"] == 0, docs[d.name]["rejected_samples"]
        assert first.manifest["boundary"]["count_drift"][table.table]["delta"] == 0
    assert validate_revision(first.revision_dir)["ok"]

    with ExportView(first.revision_dir) as v:
        r = campaign_report(v, campaign_id)
        assert r["complete"]
        assert r["matches"]["reported"] == 2
        assert r["matches"]["confirmed_by_matcher"] == 1
        assert r["evidence"]["candidates"] == 2
        assert r["evidence"]["latest_version_by_status"] == {"complete": 1, "partial": 1}
        assert r["sources"]["pages"] == 3
        assert r["sources"]["pages_per_site"] == {"mirror.example": 1, "site-a.example": 2}
        srcs = v.rows(
            "SELECT basis, count(*) AS n FROM evidence_sources WHERE match_id = $m GROUP BY 1",
            {"m": str(m1.match_id)},
        )
        assert srcs == [{"basis": "content_verified", "n": 2}]
        assert source_report(v, site="site-a.example")["complete"]
        g = build_graph(v, tmp_path)
        assert not any(g["checks"].values())

    assert exporter.export(until=until).outcome == "unchanged"
    _page(s, f"https://site-b.example/{s.run}/watch/3", None, None, T0 + timedelta(hours=2))
    second = exporter.export(until=until)
    assert second.outcome == "published"
    assert second.revision == 2
    before = docs["page_observations"]["rows"]
    assert second.manifest["datasets"]["page_observations"]["rows"] == before + 1
    assert validate_revision(first.revision_dir)["ok"]  # revision 1 untouched
