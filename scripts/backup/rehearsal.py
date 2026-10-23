"""Single-node backup -> restore REHEARSAL against the local dev stack (docker compose).

Creates throwaway ``c2_drill_src`` / ``c2-drill-src`` data, backs it up with ``crawler2-backup``,
restores into ``c2_drill_dst`` / ``c2-drill-dst`` and compares rows and object hashes. It never
reads or writes any other keyspace or bucket and removes its own artefacts at the end.

This is a LOCAL REHEARSAL of the tooling, not the production M6 restore drill: one node, RF=1,
no multi-host, no real evidence, no timing against an RTO.

    set -a; . ./.env; set +a
    sg docker -c "env/bin/python scripts/backup/rehearsal.py"
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

from crawler2.core.configuration import Settings
from crawler2.ops import backup as b
from crawler2.ops.cli import main as backup_main

SRC_KS, DST_KS = "c2_drill_src", "c2_drill_dst"
SRC_BKT, DST_BKT = "c2-drill-src", "c2-drill-dst"
CONTAINER = os.environ.get("COMPOSE_PROJECT_NAME", "crawler2") + "-scylla-1"
ROWS = 50


def cql(statement: str) -> str:
    return b.run_command(["docker", "exec", CONTAINER, "cqlsh", "-e", statement], timeout=120)


def put_object(client: b.ObjectClient, settings, bucket: str, data: bytes) -> str:
    from antipiracy_contracts.digests import ContentDigest

    from crawler2.storage.objectstore.s3 import S3ObjectStore

    store = S3ObjectStore(settings.minio, scratch_dir=Path("var/backup-scratch"), bucket=bucket)
    store.ensure_bucket()
    return store.put(data, expected_digest=ContentDigest.of_bytes(data)).digest.hex


def delete_bucket(settings, bucket: str) -> None:
    client = b.ObjectClient(settings.minio)
    try:
        keys = client.list_keys(bucket)
    except b.BackupError:
        return
    for key, _ in keys:
        client._call("DELETE", f"/{bucket}/{key}")
    client._call("DELETE", f"/{bucket}")


def main() -> int:
    settings = Settings()
    started = time.monotonic()
    work = Path("var/backup-drill")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    report: dict[str, object] = {"container": CONTAINER, "scope": "local single-node rehearsal"}
    try:
        for ks in (SRC_KS, DST_KS):
            cql(f"DROP KEYSPACE IF EXISTS {ks}")
        delete_bucket(settings, DST_BKT)
        delete_bucket(settings, SRC_BKT)
        cql(
            f"CREATE KEYSPACE {SRC_KS} WITH replication = {{'class': 'SimpleStrategy', "
            "'replication_factor': 1} AND tablets = {'enabled': false}"
        )
        cql(f"CREATE TABLE {SRC_KS}.items (id int PRIMARY KEY, body text)")
        for i in range(ROWS):
            cql(f"INSERT INTO {SRC_KS}.items (id, body) VALUES ({i}, 'row-{i}-" + "x" * 200 + "')")
        artifact = b"drill-artifact-" + os.urandom(64)
        a_hash = put_object(None, settings, SRC_BKT, artifact)  # type: ignore[arg-type]
        manifest = json.dumps(
            {
                "schema": b.EVIDENCE_SCHEMA,
                "artifacts": [{"uri": f"s3://{SRC_BKT}/x", "sha256": a_hash}],
            }
        ).encode()
        put_object(None, settings, SRC_BKT, manifest)  # type: ignore[arg-type]
        for i in range(18):
            put_object(None, settings, SRC_BKT, f"filler-{i}".encode() * 50)
        src_rows = cql(f"SELECT id, body FROM {SRC_KS}.items")

        dest = work / "set"
        t0 = time.monotonic()
        rc = backup_main(
            [
                "create",
                str(dest),
                "--scylla-container",
                CONTAINER,
                "--keyspace",
                SRC_KS,
                "--bucket",
                SRC_BKT,
            ]
        )
        report["backup_s"] = round(time.monotonic() - t0, 1)
        assert rc == 0, "backup create/verify failed"

        # damage test: a flipped byte must be caught before anything is restored
        victim = next((dest / "scylla" / SRC_KS / "tables").rglob("*Data.db"))
        original = victim.read_bytes()
        victim.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        assert backup_main(["verify", str(dest)]) == 1, "tampering was not detected"
        victim.write_bytes(original)
        assert backup_main(["verify", str(dest)]) == 0

        t1 = time.monotonic()
        rc = backup_main(
            [
                "restore",
                str(dest),
                "--scylla-container",
                CONTAINER,
                "--keyspace-map",
                f"{SRC_KS}={DST_KS}",
                "--bucket-map",
                f"{SRC_BKT}={DST_BKT}",
            ]
        )
        report["restore_s"] = round(time.monotonic() - t1, 1)
        assert rc == 0, "restore failed"

        dst_rows = cql(f"SELECT id, body FROM {DST_KS}.items")
        report["rows_src"] = src_rows.count("row-")
        report["rows_dst"] = dst_rows.count("row-")
        report["rows_identical"] = (
            hashlib.sha256(sorted(src_rows.split("\n")).__repr__().encode()).hexdigest()
            == hashlib.sha256(sorted(dst_rows.split("\n")).__repr__().encode()).hexdigest()
        )
        src_keys = {k: s for k, s in b.ObjectClient(settings.minio).list_keys(SRC_BKT)}
        dst_keys = {k: s for k, s in b.ObjectClient(settings.minio).list_keys(DST_BKT)}
        report["objects_src"], report["objects_dst"] = len(src_keys), len(dst_keys)
        report["objects_identical"] = src_keys == dst_keys
        # restoring again must be refused: the targets now hold data
        again = backup_main(
            [
                "restore",
                str(dest),
                "--scylla-container",
                CONTAINER,
                "--keyspace-map",
                f"{SRC_KS}={DST_KS}",
            ]
        )
        report["second_restore_refused"] = again == 2
        ok = (
            report["rows_src"] == report["rows_dst"] == ROWS
            and report["rows_identical"]
            and report["objects_identical"]
            and report["second_restore_refused"]
        )
        report["passed"] = bool(ok)
    finally:
        for ks in (SRC_KS, DST_KS):
            cql(f"DROP KEYSPACE IF EXISTS {ks}")
        delete_bucket(settings, DST_BKT)
        delete_bucket(settings, SRC_BKT)
        shutil.rmtree(work, ignore_errors=True)
        report["total_s"] = round(time.monotonic() - started, 1)
    print(json.dumps(report, indent=2))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
