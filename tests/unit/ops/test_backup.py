"""Backup sets: creation via a fake command runner, verification, and restore guards."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

import pytest

from crawler2.ops import backup as b


class FakeRunner:
    """Records commands; ``docker cp`` out materialises a snapshot directory with two SSTables."""

    def __init__(self, existing_keyspaces: str = "system system_schema") -> None:
        self.calls: list[list[str]] = []
        self.existing = existing_keyspaces
        self.lastsave = 100

    def __call__(self, argv: Sequence[str], *, timeout: float = 0) -> str:
        argv = list(argv)
        self.calls.append(argv)
        joined = " ".join(argv)
        if "DESCRIBE KEYSPACES" in joined:
            return self.existing
        if "DESCRIBE KEYSPACE " in joined:
            ks = argv[-1].split()[-1]
            return (
                f"CREATE KEYSPACE {ks} WITH replication = {{'class': 'SimpleStrategy'}};\n"
                f"CREATE TABLE {ks}.t (k int PRIMARY KEY);"
            )
        if argv[:2] == ["docker", "cp"] and ":" in argv[2] and "snapshots" in argv[2]:
            dest = Path(argv[3])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "me-1-big-Data.db").write_bytes(b"data-1")
            (dest / "me-1-big-TOC.txt").write_bytes(b"toc-1")
            return ""
        if "ls -d" in joined and "snapshots" in joined:
            return "/var/lib/scylla/data/ks1/t-0123abcd/snapshots/TAG\n"
        if "LASTSAVE" in joined:
            self.lastsave += 1
            return str(self.lastsave)
        if argv[:2] == ["docker", "cp"] and "dump.rdb" in argv[2]:
            Path(argv[3]).write_bytes(b"rdb")
        return ""


def targets() -> b.Targets:
    return b.Targets(scylla=b.ScyllaHost(container="scylla1"), keyspaces=["ks1"])


def test_create_writes_schema_sstables_and_a_hash_manifest(tmp_path: Path) -> None:
    run = FakeRunner()
    m = b.create_backup(tmp_path / "set", targets(), tag="TAG", run=run, provenance={"git": "abc"})
    assert m["format"] == b.FORMAT
    assert m["scylla"]["ks1"]["tables"] == ["t"]
    assert m["rpo_rto"] == {
        "rpo_target": None,
        "rto_target": None,
        "note": "owner decision OD-P14-5",
    }
    files = m["files"]
    assert "scylla/ks1/schema.cql" in files
    assert "scylla/ks1/tables/t/me-1-big-Data.db" in files
    assert (
        files["scylla/ks1/tables/t/me-1-big-Data.db"]["sha256"]
        == hashlib.sha256(b"data-1").hexdigest()
    )
    cmds = [" ".join(c) for c in run.calls]
    assert any("keyspace_flush/ks1" in c for c in cmds)
    assert any("snapshots?tag=TAG&kn=ks1" in c for c in cmds)
    assert b.verify_backup(tmp_path / "set").ok


def test_a_backup_set_is_written_once(tmp_path: Path) -> None:
    (tmp_path / "set").mkdir()
    (tmp_path / "set" / "x").write_text("old")
    with pytest.raises(b.BackupError, match="not empty"):
        b.create_backup(tmp_path / "set", targets(), run=FakeRunner())


@pytest.mark.parametrize("bad", ["ks; rm -rf /", "../x", "a b", ""])
def test_names_that_could_reach_a_shell_or_path_are_refused(tmp_path: Path, bad: str) -> None:
    with pytest.raises(b.BackupError):
        b.create_backup(
            tmp_path / "s", b.Targets(scylla=b.ScyllaHost("c"), keyspaces=[bad]), run=FakeRunner()
        )


def test_missing_keyspace_is_an_error_not_an_empty_backup(tmp_path: Path) -> None:
    class Empty(FakeRunner):
        def __call__(self, argv: Sequence[str], *, timeout: float = 0) -> str:
            return "" if "DESCRIBE KEYSPACE " in " ".join(argv) else super().__call__(argv)

    with pytest.raises(b.BackupError, match="has no schema"):
        b.create_backup(tmp_path / "s", targets(), run=Empty())


def test_redis_is_copied_after_bgsave_completes(tmp_path: Path) -> None:
    run = FakeRunner()
    m = b.create_backup(tmp_path / "s", b.Targets(redis_container="redis1"), run=run)
    assert m["redis"]["rdb"] == "redis/dump.rdb"
    assert (tmp_path / "s" / "redis" / "dump.rdb").read_bytes() == b"rdb"


# --- verification -------------------------------------------------------------------------------


def _set(tmp_path: Path) -> Path:
    b.create_backup(tmp_path / "set", targets(), tag="TAG", run=FakeRunner())
    return tmp_path / "set"


def test_verify_detects_a_flipped_byte_a_deleted_file_and_a_stray_file(tmp_path: Path) -> None:
    d = _set(tmp_path)
    (d / "scylla/ks1/tables/t/me-1-big-Data.db").write_bytes(b"data-X")
    (d / "scylla/ks1/tables/t/me-1-big-TOC.txt").unlink()
    (d / "stray.txt").write_text("hi")
    rep = b.verify_backup(d)
    assert not rep.ok
    assert rep.mismatched == ["scylla/ks1/tables/t/me-1-big-Data.db"]
    assert rep.missing == ["scylla/ks1/tables/t/me-1-big-TOC.txt"]
    assert rep.unexpected == ["stray.txt"]


def test_verify_rejects_unknown_or_missing_manifests(tmp_path: Path) -> None:
    with pytest.raises(b.BackupError, match="unreadable"):
        b.verify_backup(tmp_path)
    (tmp_path / "MANIFEST.json").write_text('{"format": "other"}')
    with pytest.raises(b.BackupError, match="unknown backup format"):
        b.verify_backup(tmp_path)


def _object_set(
    tmp_path: Path, *, evidence_artifact_present: bool = True, corrupt: bool = False
) -> Path:
    """A minimal MinIO section: one raw artefact and one evidence manifest that references it."""
    d = tmp_path / "set"
    artifact = b"screenshot-bytes"
    a_hash = hashlib.sha256(artifact).hexdigest()
    manifest = json.dumps(
        {
            "schema": b.EVIDENCE_SCHEMA,
            "artifacts": [
                {
                    "uri": f"s3://evid/raw/sha256/{a_hash[:2]}/{a_hash[2:4]}/{a_hash}",
                    "sha256": a_hash,
                }
            ],
        },
        sort_keys=True,
    ).encode()
    m_hash = hashlib.sha256(manifest).hexdigest()
    objects = {f"raw/sha256/{m_hash[:2]}/{m_hash[2:4]}/{m_hash}": manifest}
    if evidence_artifact_present:
        objects[f"raw/sha256/{a_hash[:2]}/{a_hash[2:4]}/{a_hash}"] = artifact
    index = {}
    for key, data in objects.items():
        p = d / "minio" / "evid" / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        index[key] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "content_addressed": True,
        }
    (d / "minio" / "evid" / "index.json").write_text(json.dumps(index))
    files = {
        p.relative_to(d).as_posix(): {
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "size": p.stat().st_size,
        }
        for p in d.rglob("*")
        if p.is_file()
    }
    (d / "MANIFEST.json").write_text(
        json.dumps({"format": b.FORMAT, "files": files, "minio": {"evid": {}}})
    )
    if corrupt:  # tampered after the backup recorded its hashes
        key = f"raw/sha256/{a_hash[:2]}/{a_hash[2:4]}/{a_hash}"
        (d / "minio" / "evid" / key).write_bytes(b"tampered")
    return d


def test_evidence_artifacts_must_exist_in_the_set_with_matching_hashes(tmp_path: Path) -> None:
    rep = b.verify_backup(_object_set(tmp_path))
    assert rep.ok
    assert rep.evidence_manifests == 1
    assert rep.evidence_artifacts_checked == 1


def test_a_missing_evidence_artifact_is_a_cross_store_failure(tmp_path: Path) -> None:
    rep = b.verify_backup(_object_set(tmp_path, evidence_artifact_present=False))
    assert not rep.ok
    assert len(rep.evidence_errors) == 1
    assert "not in the backup" in rep.evidence_errors[0]


def test_a_tampered_object_fails_both_the_file_hash_and_its_content_address(tmp_path: Path) -> None:
    rep = b.verify_backup(_object_set(tmp_path, corrupt=True))
    assert not rep.ok
    assert rep.mismatched
    assert rep.object_errors


# --- restore guards -----------------------------------------------------------------------------


def test_restore_refuses_same_name_and_existing_targets(tmp_path: Path) -> None:
    d = _set(tmp_path)
    host = b.ScyllaHost(container="scylla1")
    with pytest.raises(b.BackupError, match="same keyspace name"):
        b.restore_scylla(host, "ks1", "ks1", d, FakeRunner())
    with pytest.raises(b.BackupError, match="already exists"):
        b.restore_scylla(host, "ks1", "ks2", d, FakeRunner(existing_keyspaces="system ks2"))
    with pytest.raises(b.BackupError, match="must match"):
        b.restore_scylla(host, "ks1", "ks2; DROP KEYSPACE x", d, FakeRunner())


def test_restore_rewrites_the_schema_to_the_new_name_and_loads_sstables(tmp_path: Path) -> None:
    d = _set(tmp_path)

    class R(FakeRunner):
        scripts: ClassVar[list[str]] = []

        def __call__(self, argv: Sequence[str], *, timeout: float = 0) -> str:
            joined = " ".join(argv)
            if argv[:2] == ["docker", "cp"] and argv[2].endswith(".cql"):
                self.scripts.append(Path(argv[2]).read_text())
            if "system_schema.tables" in joined and "keyspace_name='ks2'" in joined:
                return " id\n------\n 11111111-2222-3333-4444-555555555555\n"
            return super().__call__(argv)

    run = R()
    loaded = b.restore_scylla(b.ScyllaHost(container="scylla1"), "ks1", "ks2", d, run)
    assert loaded == {"t": 2}
    assert any("cqlsh -f /tmp/c2-restore-ks2.cql" in " ".join(c) for c in run.calls)
    assert run.scripts, "the rewritten schema was handed to cqlsh as a file"
    text = run.scripts[0]
    assert "CREATE KEYSPACE ks2" in text
    assert "ks2.t" in text
    assert "ks1" not in text
    cmds = [" ".join(c) for c in run.calls]
    assert any(
        "docker cp" in c
        and "scylla1:/var/lib/scylla/data/ks2/t-11111111222233334444555555555555/upload" in c
        for c in cmds
    )
    assert any("sstables/ks2?cf=t" in c for c in cmds)


def test_minio_credentials_are_required() -> None:
    from crawler2.core.configuration import MinioSettings

    with pytest.raises(b.BackupError, match="credentials"):
        b.ObjectClient(MinioSettings(access_key=None, secret_key=None))
