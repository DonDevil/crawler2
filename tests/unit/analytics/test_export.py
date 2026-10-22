"""P13 export: completeness, partitioning, idempotency, failure and recovery (design §4-§6).

All inputs are the SYNTHETIC world of ``world.py`` served by ``MemoryRowSource``.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from crawler2.analytics.datasets import DATASETS
from crawler2.analytics.export import (
    CURRENT,
    LOCK,
    MANIFEST,
    ExportError,
    PublishLockedError,
    current_revision,
    list_revisions,
    read_manifest,
    repair_current,
    resolve_export,
)
from crawler2.analytics.query import ExportView, validate_revision
from crawler2.analytics.source import MemoryRowSource
from crawler2.storage.errors import StorageUnavailableError
from tests.fixtures.contracts import T0
from tests.unit.analytics.conftest import Exported, make_exporter
from tests.unit.analytics.world import BOUNDARY, D1_DAY, World, build

# Hand-counted from world.py: (source rows, rejected, out of window, rows written).
EXPECTED = {
    "fetch_attempts": (8, 0, 1, 7),
    "page_observations": (9, 1, 1, 7),
    "page_extracts": (8, 0, 1, 7),
    "page_links": (8, 0, 1, 11),
    "page_media_refs": (8, 0, 0, 6),
    "urls": (8, 0, 0, 8),
    "domains": (4, 0, 0, 4),
    "page_revisions": (7, 0, 1, 6),
    "media": (3, 0, 0, 3),
    "media_observations": (6, 0, 0, 6),
    "targets": (2, 0, 0, 2),
    "matches": (2, 0, 0, 2),
    "evidence_candidates": (2, 0, 0, 2),
    "evidence_sources": (2, 0, 0, 2),
    "evidence_versions": (1, 0, 0, 1),
    "campaigns": (3, 0, 0, 3),
    "source_profiles": (1, 0, 0, 1),
}


def _datasets(e: Exported) -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = e.result.manifest["datasets"]
    return docs


def test_every_dataset_reconciles_with_the_source(exported: Exported) -> None:
    assert exported.result.outcome == "published"
    assert exported.result.revision == 1
    docs = _datasets(exported)
    assert set(docs) == set(DATASETS)
    for name, (source_rows, rejected, out_of_window, written) in EXPECTED.items():
        doc = docs[name]
        c = doc["counts"]
        assert doc["status"] == "complete", (name, doc["problems"])
        assert (c["source_rows"], c["rejected"], c["out_of_window"], doc["rows"]) == (
            source_rows,
            rejected,
            out_of_window,
            written,
        ), name
        assert c["readback"] == c["written"] == doc["rows"]
        assert c["missing_keys"] == c["unexpected_keys"] == c["key_conflicts"] == 0
    assert docs["page_observations"]["rejected_samples"][0].endswith("not JSON")


def test_parquet_schema_and_types_are_the_declared_ones(exported: Exported) -> None:
    con = duckdb.connect()
    for name, doc in _datasets(exported).items():
        path = exported.revision_dir / doc["files"][0]["path"]
        described = con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{path}', hive_partitioning = false)"
        ).fetchall()
        got = [(r[0], r[1]) for r in described]
        declared = [
            (n, t.replace("TIMESTAMPTZ", "TIMESTAMP WITH TIME ZONE"))
            for n, t in DATASETS[name].columns
        ]
        if DATASETS[name].time_column:
            declared.append(("date", "VARCHAR"))
        assert got == declared, name


def test_partitioning_by_date_with_unknown_and_unpartitioned_dimensions(
    exported: Exported,
) -> None:
    docs = _datasets(exported)
    obs_dates = sorted(f["date"] for f in docs["page_observations"]["files"])
    assert obs_dates == ["2026-06-01", "2026-06-02"]  # two partitions; the late row is out
    for f in docs["page_observations"]["files"]:
        assert f"/date={f['date']}/" in f["path"]
    assert [f["date"] for f in docs["targets"]["files"]] == [None]  # dimension, one file
    assert docs["targets"]["files"][0]["path"] == "targets/schema_version=1/part-0.parquet"
    with ExportView(exported.revision_dir) as v:
        assert v.scalar("SELECT count(DISTINCT site) FROM page_observations") == 3
        assert v.scalar("SELECT count(*) FROM urls WHERE site IS NULL") == 1  # urn: no host
        # a row's date column always equals its partition directory
        assert (
            v.scalar(
                "SELECT count(*) FROM page_observations WHERE date <> "
                "strftime(observed_at, '%Y-%m-%d')"
            )
            == 0
        )


def test_null_time_rows_go_to_the_unknown_partition(tmp_path: Path) -> None:
    world, _ = build()
    world.tables["url_state"][0]["first_seen"] = None
    e = make_exporter(MemoryRowSource(world.tables), tmp_path).export(
        until=BOUNDARY, datasets=["urls"]
    )
    doc = e.manifest["datasets"]["urls"]
    assert doc["counts"]["unknown_time"] == 1
    assert doc["rows"] == 8
    assert "unknown" in {f["date"] for f in doc["files"]}


def test_since_bounds_the_window(tmp_path: Path) -> None:
    world, _ = build()
    e = make_exporter(MemoryRowSource(world.tables), tmp_path).export(
        since=D1_DAY + timedelta(days=1), until=BOUNDARY, datasets=["page_observations"]
    )
    doc = e.manifest["datasets"]["page_observations"]
    assert {f["date"] for f in doc["files"]} == {"2026-06-02"}
    assert doc["counts"]["out_of_window"] == 1 + 3  # the late row + three day-1 rows


def test_pagination_across_many_small_pages(tmp_path: Path) -> None:
    world, _ = build()
    source = MemoryRowSource(world.tables, page_size=1)
    e = make_exporter(source, tmp_path, segments=1).export(until=BOUNDARY)
    assert source.pages_served >= sum(len(rows) for rows in world.tables.values())
    assert e.manifest["datasets"]["page_observations"]["rows"] == 7


def test_segment_count_does_not_change_the_content(tmp_path: Path) -> None:
    world, _ = build()
    one = make_exporter(MemoryRowSource(world.tables), tmp_path / "a", segments=1).export(
        until=BOUNDARY
    )
    many = make_exporter(MemoryRowSource(world.tables), tmp_path / "b", segments=7).export(
        until=BOUNDARY
    )
    assert one.export_key == many.export_key
    assert one.manifest["content_id"] == many.manifest["content_id"]


def test_empty_and_unavailable_datasets(tmp_path: Path) -> None:
    world, _ = build()
    world.tables["matches_by_content"] = []
    del world.tables["evidence_versions"]  # e.g. a keyspace not migrated to V007
    e = make_exporter(MemoryRowSource(world.tables), tmp_path).export(until=BOUNDARY)
    docs = e.manifest["datasets"]
    assert docs["matches"]["status"] == "empty"
    assert docs["matches"]["rows"] == 0
    assert len(docs["matches"]["files"]) == 1  # a zero-row file keeps the schema readable
    assert docs["evidence_versions"]["status"] == "unavailable"
    assert "evidence_versions does not exist" in docs["evidence_versions"]["reason"]
    with ExportView(e.revision_dir) as v:  # type: ignore[arg-type]
        assert v.scalar("SELECT count(*) FROM matches") == 0
        assert "evidence_versions" in v.unavailable


def test_null_optional_fields_survive(exported: Exported) -> None:
    with ExportView(exported.revision_dir) as v:
        row = v.rows(
            "SELECT snapshot_uri, redirect_urls FROM page_observations "
            "WHERE redirect_count = 0 LIMIT 1"
        )[0]
        assert row == {"snapshot_uri": None, "redirect_urls": []}
        redirected = v.rows("SELECT redirect_urls FROM page_observations WHERE redirect_count = 1")
        assert redirected == [{"redirect_urls": ["https://site-a.example/watch/2"]}]
        assert v.scalar("SELECT count(*) FROM media_observations WHERE content_id IS NULL") == 2


def test_exact_duplicate_source_rows_are_collapsed_and_counted(tmp_path: Path) -> None:
    world, _ = build()
    world.tables["media"].append(dict(world.tables["media"][0]))
    e = make_exporter(MemoryRowSource(world.tables), tmp_path).export(until=BOUNDARY)
    doc = e.manifest["datasets"]["media"]
    assert doc["status"] == "complete"
    assert doc["rows"] == 3
    assert doc["counts"]["source_rows"] == 4
    assert doc["counts"]["duplicates_collapsed"] == 1


def test_conflicting_duplicates_fail_the_export_and_publish_nothing(tmp_path: Path) -> None:
    world, _ = build()
    clash = dict(world.tables["media"][0])
    clash["last_seen"] = clash["last_seen"] + timedelta(hours=1)
    world.tables["media"].append(clash)
    exporter = make_exporter(MemoryRowSource(world.tables), tmp_path)
    with pytest.raises(ExportError, match="key conflicts"):
        exporter.export(until=BOUNDARY)
    assert not list((tmp_path / "exports").glob("*/rev-*"))
    (run,) = (tmp_path / "runs").glob("*.json")
    assert json.loads(run.read_text())["status"] == "failed"


def test_transient_failures_are_retried_per_segment(tmp_path: Path) -> None:
    world, _ = build()
    source = MemoryRowSource(world.tables, page_size=1)
    for segment in range(4):
        source.faults[("page_observations", segment)] = 1
    e = make_exporter(source, tmp_path, max_retries=2).export(until=BOUNDARY)
    doc = e.manifest["datasets"]["page_observations"]
    assert doc["status"] == "complete"
    assert doc["rows"] == 7
    assert doc["counts"]["segment_retries"] >= 1


def test_interrupted_export_is_not_published_and_resumes(tmp_path: Path) -> None:
    world, _ = build()
    source = MemoryRowSource(world.tables, page_size=1)
    source.faults[("media_observations", 0)] = 99
    source.faults[("media_observations", 1)] = 99
    source.faults[("media_observations", 2)] = 99
    source.faults[("media_observations", 3)] = 99
    exporter = make_exporter(source, tmp_path, max_retries=1)
    with pytest.raises(StorageUnavailableError):
        exporter.export(until=BOUNDARY)
    (run_dir,) = (tmp_path / "_staging").iterdir()
    state = json.loads((run_dir / "run.json").read_text())
    assert state["status"] == "failed"
    assert "StorageUnavailableError" in state["error"]
    assert not (tmp_path / "exports").exists() or not list((tmp_path / "exports").rglob(CURRENT))
    done = {p.parent.name for p in (run_dir / "segments").rglob("seg-*.json")}
    assert "fetch_attempts" in done  # tables scanned before the failure are checkpointed
    source.faults.clear()
    before = source.pages_served
    result = exporter.export(resume=run_dir.name)
    assert result.outcome == "published"
    assert result.run_id == run_dir.name
    assert result.manifest["datasets"]["media_observations"]["rows"] == 6
    assert not run_dir.exists()  # staging removed after publication
    fresh = make_exporter(MemoryRowSource(world.tables), tmp_path / "fresh").export(until=BOUNDARY)
    assert fresh.manifest["content_id"] == result.manifest["content_id"]
    assert source.pages_served - before < fresh_pages(world)  # completed segments not re-read


def fresh_pages(world: World) -> int:
    return sum(len(v) for v in world.tables.values())


def test_unchanged_re_export_publishes_nothing_new(exported: Exported) -> None:
    again = exported.exporter.export(until=BOUNDARY)
    assert again.outcome == "unchanged"
    assert again.revision == 1
    assert again.export_key == exported.result.export_key
    export_dir = exported.root / "exports" / again.export_key
    assert list_revisions(export_dir) == [1]
    runs = sorted(json.loads(p.read_text())["status"] for p in (exported.root / "runs").iterdir())
    assert runs == ["published", "unchanged"]
    assert not list((exported.root / "_staging").iterdir())


def test_changed_source_publishes_a_new_revision_without_merging(exported: Exported) -> None:
    w = exported.world
    w.page("https://site-b.example/watch/9", at=D1_DAY + timedelta(hours=5), media_urls=[])
    w.tables["media"].pop()  # a row disappeared (e.g. TTL) between the exports
    again = exported.exporter.export(until=BOUNDARY)
    assert again.outcome == "published"
    assert again.revision == 2
    export_dir = exported.root / "exports" / again.export_key
    assert current_revision(export_dir) == 2
    assert list_revisions(export_dir) == [1, 2]
    assert again.manifest["previous_revision"] == 1
    old = read_manifest(export_dir / "rev-0001")
    assert old["content_id"] == exported.result.manifest["content_id"]  # rev 1 untouched
    with ExportView(export_dir / "rev-0002") as v:
        assert v.scalar("SELECT count(*) FROM page_observations") == 8
        assert v.scalar("SELECT count(*) FROM media") == 2  # the new state only
    with ExportView(export_dir / "rev-0001") as v:
        assert v.scalar("SELECT count(*) FROM page_observations") == 7
        assert v.scalar("SELECT count(*) FROM media") == 3
    assert validate_revision(export_dir / "rev-0001")["ok"]


def test_different_boundary_is_a_different_export(exported: Exported) -> None:
    other = exported.exporter.export(until=BOUNDARY + timedelta(days=2))
    assert other.export_key != exported.result.export_key
    assert other.revision == 1
    assert other.manifest["datasets"]["page_observations"]["rows"] == 8  # the late row


def test_crash_between_rename_and_pointer_is_repaired(exported: Exported) -> None:
    export_dir = exported.root / "exports" / exported.result.export_key
    exported.world.page("https://site-b.example/watch/10", at=D1_DAY, media_urls=[])
    exported.exporter.export(until=BOUNDARY)
    (export_dir / CURRENT).write_text(json.dumps({"revision": 1, "content_id": "x"}))
    assert repair_current(export_dir) == 2
    assert resolve_export(export_dir).name == "rev-0002"


def test_a_directory_without_manifest_is_never_read(exported: Exported) -> None:
    bogus = exported.root / "exports" / exported.result.export_key / "rev-0009"
    shutil.copytree(exported.revision_dir, bogus)
    (bogus / MANIFEST).unlink()
    with pytest.raises(ExportError, match="not a published revision"):
        ExportView(bogus)
    export_dir = bogus.parent
    assert repair_current(export_dir) == 1


def test_publish_lock_blocks_a_concurrent_publisher(exported: Exported) -> None:
    export_dir = exported.root / "exports" / exported.result.export_key
    (export_dir / LOCK).write_text("other-run host pid=1\n")
    with pytest.raises(PublishLockedError, match="other-run"):
        exported.exporter.export(until=BOUNDARY)
    assert list_revisions(export_dir) == [1]
    (run,) = (exported.root / "_staging").iterdir()  # kept for a later --resume
    (export_dir / LOCK).unlink()
    assert exported.exporter.export(resume=run.name).outcome == "unchanged"


def test_resume_refuses_a_different_source(exported: Exported, tmp_path: Path) -> None:
    run_dir = exported.root / "_staging" / "r1"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "r1",
        "scope": exported.result.manifest["scope"] | {"source": "scylla:other"},
        "segments": 4,
        "started_at": T0.isoformat(),
    }
    (run_dir / "run.json").write_text(json.dumps(state))
    with pytest.raises(ExportError, match="reads scylla:other"):
        exported.exporter.export(resume="r1")
    with pytest.raises(ExportError, match="no resumable run"):
        exported.exporter.export(resume="missing")


def test_validate_detects_tampering(exported: Exported) -> None:
    assert validate_revision(exported.revision_dir)["ok"]
    doc = _datasets(exported)["page_links"]
    path = exported.revision_dir / doc["files"][0]["path"]
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    result = validate_revision(exported.revision_dir)
    assert not result["ok"]
    assert any("sha256" in p for p in result["datasets"]["page_links"]["problems"])
    with pytest.raises(ExportError, match="SHA-256"):
        ExportView(exported.revision_dir, verify_checksums=True)


def test_missing_file_is_refused_by_readers(exported: Exported) -> None:
    doc = _datasets(exported)["urls"]
    os.remove(exported.revision_dir / doc["files"][0]["path"])
    with pytest.raises(ExportError, match="missing or truncated"):
        ExportView(exported.revision_dir)
    assert not validate_revision(exported.revision_dir, checksums=False)["ok"]


def test_unknown_schema_version_is_not_read(exported: Exported) -> None:
    path = exported.revision_dir / MANIFEST
    manifest = json.loads(path.read_text())
    bumped = copy.deepcopy(manifest)
    bumped["datasets"]["media"]["schema_version"] = 99
    path.write_text(json.dumps(bumped))
    with ExportView(exported.revision_dir) as v:
        assert "media" not in v.available
        assert "schema_version 99" in v.unavailable["media"]


def test_scope_key_includes_schema_versions_and_window(exported: Exported) -> None:
    scope = exported.result.manifest["scope"]
    assert scope["until"] == BOUNDARY.isoformat()
    assert scope["since"] is None
    assert scope["datasets"]["page_links"] == 1
    sub = exported.exporter.export(until=BOUNDARY, datasets=["media"])
    assert sub.export_key != exported.result.export_key


def test_unknown_dataset_and_bad_window_are_usage_errors(exported: Exported) -> None:
    with pytest.raises(ValueError, match="unknown dataset"):
        exported.exporter.export(until=BOUNDARY, datasets=["nope"])
    with pytest.raises(ValueError, match="since must be before until"):
        exported.exporter.export(until=BOUNDARY, since=BOUNDARY)


def test_verify_counts_records_drift(tmp_path: Path) -> None:
    world, _ = build()
    e = make_exporter(MemoryRowSource(world.tables), tmp_path, verify_counts=True).export(
        until=BOUNDARY, datasets=["media"]
    )
    assert e.manifest["boundary"]["count_drift"] == {
        "media": {"scanned": 3, "recounted": 3, "delta": 0}
    }


def test_export_writes_only_under_its_root(tmp_path: Path) -> None:
    world, _ = build()
    root = tmp_path / "root"
    before = {p for p in tmp_path.rglob("*")}
    make_exporter(MemoryRowSource(world.tables), root).export(until=BOUNDARY)
    after = {p for p in tmp_path.rglob("*")} - before
    assert all(root in p.parents or p == root for p in after)
