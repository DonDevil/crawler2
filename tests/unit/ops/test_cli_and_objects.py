"""The operator CLI and the S3 listing/reading path of the backup tool, against a local fake S3."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from crawler2.core.configuration import MinioSettings
from crawler2.ops import backup as b
from crawler2.ops import cli

GOOD = b"good-object-bytes"
GOOD_HASH = hashlib.sha256(GOOD).hexdigest()
GOOD_KEY = f"raw/sha256/{GOOD_HASH[:2]}/{GOOD_HASH[2:4]}/{GOOD_HASH}"
LIAR_KEY = f"raw/sha256/00/00/{'0' * 64}"  # key promises a hash the bytes do not have
OBJECTS = {GOOD_KEY: GOOD, "misc/plain.txt": b"plain"}


class FakeS3(BaseHTTPRequestHandler):
    objects: dict[str, bytes] = OBJECTS

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        bucket, _, key = parts.path.lstrip("/").partition("/")
        if bucket == "missing":
            self.send_response(404)
            self.end_headers()
            return
        if key:
            data = self.objects.get(key)
            self.send_response(200 if data is not None else 404)
            self.end_headers()
            self.wfile.write(data or b"")
            return
        token = parse_qs(parts.query).get("continuation-token", [""])[0]
        keys = sorted(self.objects)
        page, nxt = (keys[:1], "t1") if not token else (keys[1:], "")
        items = "".join(
            f"<Contents><Key>{k}</Key><Size>{len(self.objects[k])}</Size></Contents>" for k in page
        )
        truncated = "true" if nxt else "false"
        extra = f"<NextContinuationToken>{nxt}</NextContinuationToken>" if nxt else ""
        body = (
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<IsTruncated>{truncated}</IsTruncated>{extra}{items}</ListBucketResult>"
        ).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def s3() -> Iterator[MinioSettings]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeS3)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield MinioSettings(
        endpoint=f"127.0.0.1:{server.server_address[1]}",
        access_key="ak",  # type: ignore[arg-type]
        secret_key="s" * 24,  # type: ignore[arg-type]
    )
    server.shutdown()
    thread.join(5)


def test_listing_follows_continuation_tokens_and_reads_objects(s3: MinioSettings) -> None:
    client = b.ObjectClient(s3)
    listing = client.list_keys("bkt")
    assert sorted(listing) == sorted((k, len(v)) for k, v in OBJECTS.items())
    assert client.get("bkt", GOOD_KEY) == GOOD
    with pytest.raises(b.BackupError, match="does not exist"):
        client.list_keys("missing")
    with pytest.raises(b.BackupError, match="HTTP 404"):
        client.get("bkt", "no/such")


def test_minio_backup_stores_objects_with_an_index_and_verifies(
    s3: MinioSettings, tmp_path: Path
) -> None:
    targets = b.Targets(minio=s3, buckets=["bkt"])
    manifest = b.create_backup(tmp_path / "set", targets, tag="t1")
    assert manifest["minio"]["bkt"] == {"objects": 2, "bytes": len(GOOD) + 5}
    index = json.loads((tmp_path / "set/minio/bkt/index.json").read_text())
    assert index[GOOD_KEY]["content_addressed"] is True
    assert index["misc/plain.txt"]["content_addressed"] is False
    assert b.verify_backup(tmp_path / "set").ok


def test_an_object_whose_bytes_do_not_match_its_content_address_aborts_the_backup(
    s3: MinioSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeS3, "objects", {LIAR_KEY: b"not-zero-hash"})
    with pytest.raises(b.BackupError, match="does not match its key"):
        b.create_backup(tmp_path / "set", b.Targets(minio=s3, buckets=["bkt"]))


def test_restore_refuses_non_content_addressed_objects(s3: MinioSettings, tmp_path: Path) -> None:
    b.create_backup(tmp_path / "set", b.Targets(minio=s3, buckets=["bkt"]))
    with pytest.raises(b.BackupError, match="same bucket name"):
        b.restore_minio(s3, "bkt", "bkt", tmp_path / "set", tmp_path / "scratch")


# --- CLI ----------------------------------------------------------------------------------------


def test_cli_create_verify_prune_roundtrip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("2026-01", "2026-02", "2026-03"):
        assert cli.main(["create", str(tmp_path / name), "--tag", f"t{name[-1]}"]) == 0
    capsys.readouterr()
    assert cli.main(["verify", str(tmp_path / "2026-02")]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert cli.main(["prune", str(tmp_path), "--keep", "1"]) == 0
    out = capsys.readouterr()
    assert "would delete" in out.out
    assert (tmp_path / "2026-01").exists()
    assert cli.main(["prune", str(tmp_path), "--keep", "1", "--yes"]) == 0
    assert not (tmp_path / "2026-01").exists()
    assert not (tmp_path / "2026-02").exists()
    assert (tmp_path / "2026-03").exists(), "the newest set is always kept"


def test_cli_verify_fails_on_damage_and_restore_refuses_a_damaged_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["create", str(tmp_path / "set")])
    manifest = json.loads((tmp_path / "set/MANIFEST.json").read_text())
    manifest["files"]["ghost.bin"] = {"sha256": "0" * 64, "size": 1}
    (tmp_path / "set/MANIFEST.json").write_text(json.dumps(manifest))
    capsys.readouterr()
    assert cli.main(["verify", str(tmp_path / "set")]) == 1
    assert cli.main(["restore", str(tmp_path / "set"), "--keyspace-map", "a=b"]) == 1
    assert "does not verify" in capsys.readouterr().err


def test_cli_rejects_malformed_arguments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["create", str(tmp_path / "set")])
    capsys.readouterr()
    assert cli.main(["restore", str(tmp_path / "set"), "--keyspace-map", "no-equals-sign"]) == 2
    assert "expected SRC=DST" in capsys.readouterr().err
    assert cli.main(["verify", str(tmp_path / "nowhere")]) == 2
