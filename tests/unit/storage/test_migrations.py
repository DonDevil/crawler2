from pathlib import Path

import pytest

from crawler2.storage.errors import SchemaError
from crawler2.storage.scylla.migrations import MIGRATIONS_DIR, load_migrations

# Every table of the P2 design and the access pattern(s) it serves (schema.md).
EXPECTED_TABLES = {
    "fetch_attempts": "W1",
    "fetch_attempts_by_domain_day": "W2",
    "fetch_attempts_by_url": "W3",
    "page_observations": "W4/W7",
    "page_observations_by_url": "W4/W6",
    "latest_observation_by_url": "W5",
    "page_versions_by_url": "W8",
    "links_by_page_version": "W9",
    "inlinks_by_url": "W10",
    "urls_by_domain": "W11",
    "url_state": "W12",
    "domains": "W13",
    "observations_by_domain_day": "W14",
    "media": "M1",
    "media_observations": "M2",
    "media_observations_by_media": "M3",
    "media_by_page_version": "M4",
    "media_by_content": "M5",
    "media_content_versions": "M6",
    "representation_status": "P1",
    "targets": "P2",
    "matches_by_content": "P3",
    "matches_by_target": "P4",
    "matches_by_domain": "P5",
    "evidence_by_match": "E1",
    "evidence": "E2",
    "evidence_by_target": "E4",
    "outbox": "X1/X2",
    "outbox_relay_checkpoints": "X2",
    "processed_events": "X3",
    # V002 (P5, ADR-020): extraction facts, page revisions, snapshot archival decisions.
    "page_extracts": "P5",
    "page_revisions_by_url": "P5/W8",
    "snapshot_retention": "P5",
}


def test_shipped_migrations_load_and_cover_every_designed_table() -> None:
    migrations = load_migrations()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    tables = [t for m in migrations for t in m.tables]
    assert sorted(tables) == sorted(EXPECTED_TABLES)
    assert len(tables) == len(set(tables))


def test_statements_substitute_keyspace_and_drop_comments() -> None:
    (first, *_) = load_migrations()
    statements = first.statements("ks_test")
    assert len(statements) == len(first.tables)
    assert all(s.startswith("CREATE TABLE IF NOT EXISTS ks_test.") for s in statements)
    assert not any("${keyspace}" in s or "--" in s for s in statements)


def test_checksum_is_of_the_file_and_stable() -> None:
    a, b = load_migrations()[0], load_migrations()[0]
    assert a.checksum == b.checksum
    assert len(a.checksum) == 64


def _write(
    directory: Path,
    name: str,
    body: str = "CREATE TABLE IF NOT EXISTS ${keyspace}.t (k int PRIMARY KEY);",
) -> None:
    (directory / name).write_text(body, encoding="utf-8")


def test_ordering_is_numeric_and_gapless(tmp_path: Path) -> None:
    _write(tmp_path, "V002__second.cql")
    _write(tmp_path, "V001__first.cql")
    assert [m.name for m in load_migrations(tmp_path)] == ["first", "second"]
    _write(tmp_path, "V004__gap.cql")
    with pytest.raises(SchemaError, match="without gaps"):
        load_migrations(tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "DROP TABLE ${keyspace}.t;",
        "TRUNCATE ${keyspace}.t;",
        "ALTER TABLE ${keyspace}.t DROP v;",
        "DELETE FROM ${keyspace}.t WHERE k = 1;",
    ],
)
def test_destructive_migrations_are_refused(tmp_path: Path, body: str) -> None:
    _write(tmp_path, "V001__bad.cql", body)
    with pytest.raises(SchemaError, match="destructive"):
        load_migrations(tmp_path)


def test_badly_named_files_are_refused(tmp_path: Path) -> None:
    _write(tmp_path, "001_initial.cql")
    with pytest.raises(SchemaError, match="file name"):
        load_migrations(tmp_path)


def test_migrations_ship_inside_the_package() -> None:
    assert MIGRATIONS_DIR.is_dir()
    assert (MIGRATIONS_DIR / "V001__initial_schema.cql").is_file()
