"""Backup, verification and restore of the stores crawler2 treats as authoritative.

What is backed up (docs/phases/p14-production-hardening/backup-restore.md):

* **Scylla** keyspaces, as snapshot SSTables (Scylla REST API) plus the schema (``DESCRIBE``);
* **MinIO** buckets, object by object through the S3 API; every object is content-addressed,
  so each is re-hashed on the way out and again by ``verify``;
* **Redis** (optional): an RDB copy. Whether the frontier is rebuildable or must be restored is
  an open owner decision (OD-P14-5); the tool records the RDB but ``restore`` never writes Redis.

A backup set is a directory with ``MANIFEST.json`` listing every file with its SHA-256. ``verify``
re-reads all of it and cross-checks the stores against each other (an evidence manifest's
artefacts must exist in the set with matching hashes). ``restore`` only ever writes into
*new* keyspace/bucket names and refuses a target that already holds data: restoring over a live
store is a deliberate manual act, not something this tool does.

Nothing here claims an RPO/RTO: ``MANIFEST.json`` carries empty target fields for the owner.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from antipiracy_contracts.digests import ContentDigest

from crawler2.core.configuration import MinioSettings
from crawler2.storage.objectstore.s3 import Credentials, S3ObjectStore, sign_v4

FORMAT = "crawler2.backup/v1"
EVIDENCE_SCHEMA = "crawler2.evidence.manifest/v1"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_RAW_KEY = re.compile(r"^raw/sha256/[0-9a-f]{2}/[0-9a-f]{2}/([0-9a-f]{64})$")
_CHUNK = 1 << 20


class BackupError(RuntimeError):
    pass


def sha256_file(path: Path) -> tuple[str, int]:
    h, size = hashlib.sha256(), 0
    with open(path, "rb") as fh:
        while block := fh.read(_CHUNK):
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


# --- command execution -----------------------------------------------------


class Runner(Protocol):
    def __call__(self, argv: Sequence[str], *, timeout: float = ...) -> str: ...


def run_command(argv: Sequence[str], *, timeout: float = 600) -> str:
    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise BackupError(f"{argv[0]}: {exc}") from exc
    if done.returncode != 0:
        raise BackupError(
            f"{' '.join(argv[:4])}... exit {done.returncode}: {done.stderr.strip()[:300]}"
        )
    return done.stdout


def check_name(value: str, what: str) -> str:
    if not _SAFE_NAME.match(value):
        raise BackupError(f"{what} {value!r} must match {_SAFE_NAME.pattern}")
    return value


# --- Scylla -----------------------------------------------------


@dataclass(frozen=True)
class ScyllaHost:
    """Where Scylla runs. ``container`` set: reached with docker exec/cp; else this host."""

    container: str | None = None
    data_dir: str = "/var/lib/scylla/data"
    cqlsh: str = "cqlsh"

    def exec(self, run: Runner, *argv: str, timeout: float = 600) -> str:
        prefix = ["docker", "exec", self.container] if self.container else []
        return run([*prefix, *argv], timeout=timeout)

    def run_cql_file(self, run: Runner, text: str, name: str) -> None:
        """Run a multi-statement script (cqlsh -e mishandles multiple statements)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / name
            local.write_text(text)
            if self.container:
                remote = f"/tmp/{name}"  # noqa: S108 -- inside the Scylla container
                run(["docker", "cp", str(local), f"{self.container}:{remote}"])
                self.exec(run, self.cqlsh, "-f", remote, timeout=300)
                self.exec(run, "rm", "-f", remote)
            else:
                run([self.cqlsh, "-f", str(local)])

    def rest(self, run: Runner, method: str, path: str) -> str:
        """Scylla REST API (port 10000, loopback). Used instead of ``nodetool``: Scylla's native
        nodetool starts a Seastar app that fails when the host's AIO budget is used up."""
        return self.exec(
            run, "curl", "-sfS", "-X", method, f"http://127.0.0.1:10000{path}", timeout=300
        )

    def copy_out(self, run: Runner, remote: str, local: Path) -> None:
        local.mkdir(parents=True, exist_ok=True)
        if self.container:
            run(["docker", "cp", f"{self.container}:{remote}/.", str(local)])
        else:
            run(["cp", "-a", f"{remote}/.", str(local)])

    def copy_in(self, run: Runner, local: Path, remote: str) -> None:
        if self.container:
            run(["docker", "cp", f"{local}/.", f"{self.container}:{remote}"])
            self.exec(run, "chown", "-R", "scylla:scylla", remote)
        else:
            run(["cp", "-a", f"{local}/.", remote])


def _tables(host: ScyllaHost, run: Runner, keyspace: str, tag: str) -> dict[str, str]:
    """table name -> snapshot directory, for one keyspace."""
    out = host.exec(
        run, "bash", "-c", f"ls -d {host.data_dir}/{keyspace}/*/snapshots/{tag} 2>/dev/null || true"
    )
    tables: dict[str, str] = {}
    for line in out.split():
        parts = line.strip().split("/")
        table_dir = parts[-3]  # <table>-<uuid>
        tables[table_dir.rsplit("-", 1)[0]] = line.strip()
    return tables


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _live_table_dir(host: ScyllaHost, run: Runner, keyspace: str, table: str) -> str:
    """The data directory of the table that exists now. A dropped table of the same name leaves
    its own ``<table>-<old uuid>`` directory behind (auto-snapshot), so globbing is ambiguous;
    the schema table holds the live id. Names were validated by ``check_name`` or come from our
    own listing."""
    out = host.exec(
        run,
        host.cqlsh,
        "-e",
        f"SELECT id FROM system_schema.tables WHERE keyspace_name='{keyspace}' "  # noqa: S608
        f"AND table_name='{table}'",
    )
    found = _UUID.findall(out)
    if len(found) != 1:
        raise BackupError(f"cannot find the table id of {keyspace}.{table}: {out.strip()[:200]!r}")
    return f"{host.data_dir}/{keyspace}/{table}-{found[0].replace('-', '')}"


def backup_scylla(
    host: ScyllaHost, keyspaces: Sequence[str], dest: Path, tag: str, run: Runner
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for ks in keyspaces:
        check_name(ks, "keyspace")
        schema = host.exec(run, host.cqlsh, "-e", f"DESCRIBE KEYSPACE {ks}")
        if not schema.strip():
            raise BackupError(f"keyspace {ks!r} has no schema (does it exist?)")
        host.rest(run, "POST", f"/storage_service/keyspace_flush/{ks}")
        host.rest(run, "POST", f"/storage_service/snapshots?tag={tag}&kn={ks}")
        base = dest / "scylla" / ks
        base.mkdir(parents=True, exist_ok=True)
        (base / "schema.cql").write_text(schema)
        tables = _tables(host, run, ks, tag)
        for table, remote in tables.items():
            host.copy_out(run, remote, base / "tables" / table)
        result[ks] = {"schema": f"scylla/{ks}/schema.cql", "tables": sorted(tables)}
    return result


def restore_scylla(
    host: ScyllaHost, src: str, dst: str, backup: Path, run: Runner
) -> dict[str, Any]:
    """Create ``dst`` from the backed-up schema of ``src`` and load the SSTables into it."""
    check_name(src, "source keyspace")
    check_name(dst, "target keyspace")
    if src == dst:
        raise BackupError("restore into the same keyspace name is refused (restore to a new name)")
    existing = host.exec(run, host.cqlsh, "-e", "DESCRIBE KEYSPACES")
    if dst in existing.split():
        raise BackupError(f"target keyspace {dst!r} already exists; refusing to restore into it")
    schema = (backup / "scylla" / src / "schema.cql").read_text()
    rewritten = re.sub(rf"(?<![\w]){re.escape(src)}(?=[\s.\"'])", dst, schema)
    host.run_cql_file(run, rewritten, f"c2-restore-{dst}.cql")
    loaded: dict[str, Any] = {}
    for table_dir in sorted((backup / "scylla" / src / "tables").iterdir()):
        table = table_dir.name
        target = _live_table_dir(host, run, dst, table)
        host.copy_in(run, table_dir, target + "/upload")
        host.rest(run, "POST", f"/storage_service/sstables/{dst}?cf={table}")
        loaded[table] = len(list(table_dir.iterdir()))
    return loaded


# --- MinIO -----------------------------------------------------


class ObjectClient:
    """Minimal signed S3 client for listing/reading/writing whole buckets (stdlib only)."""

    def __init__(self, settings: MinioSettings) -> None:
        if settings.access_key is None or settings.secret_key is None:
            raise BackupError(
                "MinIO credentials are not configured (CRAWLER2_MINIO__ACCESS_KEY/SECRET_KEY)"
            )
        self._s = settings
        self._cred = Credentials(
            settings.access_key.get_secret_value(),
            settings.secret_key.get_secret_value(),
            settings.region,
        )

    def _call(
        self, method: str, path: str, query: dict[str, str] | None = None
    ) -> tuple[int, bytes]:
        from crawler2.storage.objectstore import s3

        query = query or {}
        signed = sign_v4(
            method=method,
            host=self._s.endpoint,
            path=path,
            query=query,
            headers={},
            payload_sha256=s3._EMPTY_SHA256,
            credentials=self._cred,
            now=datetime.now(UTC),
        )
        factory = http.client.HTTPSConnection if self._s.secure else http.client.HTTPConnection
        conn = factory(self._s.endpoint, timeout=self._s.request_timeout_s)
        try:
            qs = urllib.parse.urlencode(sorted(query.items()), quote_via=urllib.parse.quote)
            conn.request(
                method,
                urllib.parse.quote(path, safe="/-_.~") + (f"?{qs}" if qs else ""),
                None,
                signed,
            )
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def list_keys(self, bucket: str) -> list[tuple[str, int]]:
        keys: list[tuple[str, int]] = []
        token: str | None = None
        while True:
            q = {"list-type": "2"}
            if token:
                q["continuation-token"] = token
            status, body = self._call("GET", f"/{bucket}", q)
            if status == 404:
                raise BackupError(f"bucket {bucket!r} does not exist")
            if status != 200:
                raise BackupError(f"list {bucket}: HTTP {status} {body[:200]!r}")
            root = ET.fromstring(body)
            ns = {"s": root.tag.partition("}")[0].lstrip("{")}
            for c in root.findall("s:Contents", ns):
                keys.append((c.findtext("s:Key", "", ns), int(c.findtext("s:Size", "0", ns))))
            if root.findtext("s:IsTruncated", "false", ns) != "true":
                return keys
            token = root.findtext("s:NextContinuationToken", None, ns)

    def get(self, bucket: str, key: str) -> bytes:
        status, body = self._call("GET", f"/{bucket}/{key}")
        if status != 200:
            raise BackupError(f"get {bucket}/{key}: HTTP {status}")
        return body


def backup_minio(settings: MinioSettings, buckets: Sequence[str], dest: Path) -> dict[str, Any]:
    client = ObjectClient(settings)
    result: dict[str, Any] = {}
    for bucket in buckets:
        check_name(bucket, "bucket")
        listing = client.list_keys(bucket)
        base = dest / "minio" / bucket
        index: dict[str, dict[str, Any]] = {}
        for key, size in listing:
            data = client.get(bucket, key)
            digest = hashlib.sha256(data).hexdigest()
            m = _RAW_KEY.match(key)
            if m and m.group(1) != digest:
                raise BackupError(f"{bucket}/{key}: content hash {digest} does not match its key")
            if len(data) != size:
                raise BackupError(f"{bucket}/{key}: read {len(data)} bytes, listing said {size}")
            target = base / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            index[key] = {"sha256": digest, "size": len(data), "content_addressed": bool(m)}
        base.mkdir(parents=True, exist_ok=True)
        (base / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True))
        result[bucket] = {"objects": len(index), "bytes": sum(v["size"] for v in index.values())}
    return result


def restore_minio(
    settings: MinioSettings, src: str, dst: str, backup: Path, scratch: Path
) -> dict[str, Any]:
    check_name(src, "source bucket")
    check_name(dst, "target bucket")
    if src == dst:
        raise BackupError("restore into the same bucket name is refused (restore to a new name)")
    store = S3ObjectStore(settings, scratch_dir=scratch, bucket=dst)
    if store.ensure_bucket() is False and ObjectClient(settings).list_keys(dst):
        raise BackupError(
            f"target bucket {dst!r} already holds objects; refusing to restore into it"
        )
    index = json.loads((backup / "minio" / src / "index.json").read_text())
    restored = 0
    for key, meta in index.items():
        if not meta["content_addressed"]:
            raise BackupError(
                f"{key}: not a content-addressed key; this tool restores raw-layout objects only"
            )
        data = (backup / "minio" / src / key).read_bytes()
        digest = ContentDigest.of_bytes(data)
        ref = store.put(data, expected_digest=digest)
        if ref.digest.hex != meta["sha256"]:
            raise BackupError(f"{key}: restored digest differs from the backup index")
        restored += 1
    return {"objects": restored}


# --- Redis -----------------------------------------------------


def backup_redis(container: str, dest: Path, run: Runner, cli: str = "redis-cli") -> dict[str, Any]:
    check_name(container, "redis container")
    before = run(["docker", "exec", container, cli, "LASTSAVE"]).strip()
    run(["docker", "exec", container, cli, "BGSAVE"])
    for _ in range(60):
        if run(["docker", "exec", container, cli, "LASTSAVE"]).strip() != before:
            break
        import time

        time.sleep(1)
    else:
        raise BackupError("redis BGSAVE did not finish within 60 s")
    target = dest / "redis"
    target.mkdir(parents=True, exist_ok=True)
    run(["docker", "cp", f"{container}:/data/dump.rdb", str(target / "dump.rdb")])
    return {
        "rdb": "redis/dump.rdb",
        "note": "restore policy undecided (OD-P14-5); never auto-restored",
    }


# --- sets -----------------------------------------------------


@dataclass
class Targets:
    scylla: ScyllaHost | None = None
    keyspaces: Sequence[str] = ()
    minio: MinioSettings | None = None
    buckets: Sequence[str] = ()
    redis_container: str | None = None


def create_backup(
    dest: Path,
    targets: Targets,
    *,
    tag: str | None = None,
    run: Runner = run_command,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tag = check_name(tag or datetime.now(UTC).strftime("bk%Y%m%dT%H%M%SZ"), "tag")
    if dest.exists() and any(dest.iterdir()):
        raise BackupError(f"{dest} is not empty; a backup set is written once")
    dest.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "format": FORMAT,
        "tag": tag,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provenance": provenance or {},
        "rpo_rto": {"rpo_target": None, "rto_target": None, "note": "owner decision OD-P14-5"},
        "scylla": {},
        "minio": {},
        "redis": None,
    }
    if targets.scylla is not None and targets.keyspaces:
        manifest["scylla"] = backup_scylla(targets.scylla, targets.keyspaces, dest, tag, run)
    if targets.minio is not None and targets.buckets:
        manifest["minio"] = backup_minio(targets.minio, targets.buckets, dest)
    if targets.redis_container:
        manifest["redis"] = backup_redis(targets.redis_container, dest, run)
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(dest.rglob("*")):
        if path.is_file():
            digest, size = sha256_file(path)
            files[path.relative_to(dest).as_posix()] = {"sha256": digest, "size": size}
    manifest["files"] = files
    (dest / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


@dataclass
class VerifyReport:
    files_checked: int = 0
    mismatched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    objects_checked: int = 0
    object_errors: list[str] = field(default_factory=list)
    evidence_manifests: int = 0
    evidence_artifacts_checked: int = 0
    evidence_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.mismatched
            or self.missing
            or self.unexpected
            or self.object_errors
            or self.evidence_errors
        )


def verify_backup(dest: Path) -> VerifyReport:
    """Re-read every file; check hashes, content-addressed keys and evidence cross-references."""
    rep = VerifyReport()
    try:
        manifest = json.loads((dest / "MANIFEST.json").read_text())
    except (OSError, ValueError) as exc:
        raise BackupError(f"{dest}: unreadable MANIFEST.json: {exc}") from exc
    if manifest.get("format") != FORMAT:
        raise BackupError(f"unknown backup format {manifest.get('format')!r}")
    listed = manifest["files"]
    for rel, meta in listed.items():
        path = dest / rel
        if not path.is_file():
            rep.missing.append(rel)
            continue
        digest, size = sha256_file(path)
        rep.files_checked += 1
        if digest != meta["sha256"] or size != meta["size"]:
            rep.mismatched.append(rel)
    on_disk = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}
    rep.unexpected = sorted(on_disk - set(listed) - {"MANIFEST.json"})
    present: set[str] = set()
    buckets = list(manifest.get("minio", {}))
    for bucket in buckets:
        present |= _verify_objects(dest, bucket, rep)
    for bucket in buckets:  # evidence manifests may reference artefacts stored in another bucket
        _verify_evidence(dest, bucket, present, rep)
    return rep


def _verify_objects(dest: Path, bucket: str, rep: VerifyReport) -> set[str]:
    base = dest / "minio" / bucket
    index = json.loads((base / "index.json").read_text())
    hashes: set[str] = set()
    for key, meta in index.items():
        rep.objects_checked += 1
        path = base / key
        if not path.is_file():
            rep.object_errors.append(f"{bucket}/{key}: object file missing")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        m = _RAW_KEY.match(key)
        if digest != meta["sha256"] or (m and m.group(1) != digest):
            rep.object_errors.append(f"{bucket}/{key}: content does not match its hash/key")
        hashes.add(digest)
    return hashes


def _verify_evidence(dest: Path, bucket: str, present: set[str], rep: VerifyReport) -> None:
    base = dest / "minio" / bucket
    for key in json.loads((base / "index.json").read_text()):
        path = base / key
        raw = path.read_bytes() if path.is_file() else b""
        if not raw.startswith(b"{"):
            continue
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(doc, dict) or doc.get("schema") != EVIDENCE_SCHEMA:
            continue
        rep.evidence_manifests += 1
        for art in doc.get("artifacts", []):
            want = str(art.get("sha256", "")).removeprefix("sha256:")
            rep.evidence_artifacts_checked += 1
            if want not in present:
                rep.evidence_errors.append(
                    f"{bucket}/{key}: artefact {want[:16]}... is not in the backup"
                )


# Optional hook for tests / drills.
Progress = Callable[[str], None]
