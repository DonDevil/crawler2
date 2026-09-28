"""S3-API object store (MinIO in dev) using only the standard library (ADR-014).

Why no SDK: the store needs four S3 calls (PUT/GET/HEAD object, bucket
create) and AWS Signature V4; boto3/minio would add large dependency trees
for that. The signer is tested against AWS's published example.

Integrity, end to end:
1. ``put`` hashes the bytes once while spooling them (memory up to
   ``spool_memory_bytes``, then ``scratch_dir``); a supplied digest must match.
2. The PUT is signed with ``x-amz-content-sha256 = <that hash>``: the server
   recomputes it and rejects the upload on mismatch (no silent corruption in
   transit).
3. The key is the hash (``layout.raw_key``) and the PUT carries
   ``If-None-Match: *``: an existing object is never overwritten; a present
   object is accepted only if its recorded size/hash match.
4. ``get`` re-hashes while reading and rejects a mismatch.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, BinaryIO, cast

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.models.blobs import BlobRef
from prometheus_client import Counter, Histogram

from crawler2.core.configuration import MinioSettings
from crawler2.core.observability import Metrics
from crawler2.storage.errors import IntegrityError, StorageError, StorageUnavailableError
from crawler2.storage.objectstore.base import ObjectInfo, Payload
from crawler2.storage.objectstore.layout import parse_uri, raw_key, uri_of

_CHUNK = 64 * 1024
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_UNRESERVED = "-_.~"
_META_SHA256 = "x-amz-meta-sha256"


@dataclass(frozen=True, slots=True)
class Credentials:
    access_key: str
    secret_key: str
    region: str


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def sign_v4(
    *,
    method: str,
    host: str,
    path: str,
    query: Mapping[str, str],
    headers: Mapping[str, str],
    payload_sha256: str,
    credentials: Credentials,
    now: datetime,
) -> dict[str, str]:
    """AWS Signature V4 for S3: ``headers`` plus x-amz-date, x-amz-content-sha256, Authorization."""
    amz_date = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    signed = {k.lower(): " ".join(v.strip().split()) for k, v in headers.items()}
    signed.update({"host": host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_sha256})
    names = sorted(signed)
    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe=_UNRESERVED)}={urllib.parse.quote(v, safe=_UNRESERVED)}"
        for k, v in sorted(query.items())
    )
    canonical_request = "\n".join(
        [
            method,
            urllib.parse.quote(path, safe="/" + _UNRESERVED),
            canonical_query,
            "".join(f"{name}:{signed[name]}\n" for name in names),
            ";".join(names),
            payload_sha256,
        ]
    )
    scope = f"{day}/{credentials.region}/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    key = _hmac(("AWS4" + credentials.secret_key).encode("utf-8"), day)
    for part in (credentials.region, "s3", "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    result = dict(headers)
    result["x-amz-date"] = amz_date
    result["x-amz-content-sha256"] = payload_sha256
    result["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={credentials.access_key}/{scope}, "
        f"SignedHeaders={';'.join(names)}, Signature={signature}"
    )
    return result


@dataclass(frozen=True, slots=True)
class _Response:
    status: int
    headers: dict[str, str]
    body: bytes


def _chunks(data: Payload) -> Iterator[bytes]:
    if isinstance(data, bytes | bytearray | memoryview):
        view = memoryview(data)
        for start in range(0, len(view), _CHUNK):
            yield bytes(view[start : start + _CHUNK])
    elif isinstance(data, Iterable) and not hasattr(data, "read"):
        for chunk in data:
            if chunk:
                yield bytes(chunk)
    else:
        reader = cast(BinaryIO, data)
        while block := reader.read(_CHUNK):
            yield block


class S3ObjectStore:
    def __init__(
        self,
        settings: MinioSettings,
        *,
        scratch_dir: Path,
        bucket: str | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        if settings.access_key is None or settings.secret_key is None:
            raise StorageError("object store credentials are not configured")
        self._settings = settings
        self._bucket = bucket or settings.bucket_raw
        self._scratch_dir = scratch_dir
        self._credentials = Credentials(
            settings.access_key.get_secret_value(),
            settings.secret_key.get_secret_value(),
            settings.region,
        )
        self._local = threading.local()
        self._latency: Histogram | None = None
        self._errors: Counter | None = None
        if metrics is not None:
            self._latency = metrics.histogram(
                "object_store_seconds", "Object store call latency", ["operation"]
            )
            self._errors = metrics.counter(
                "object_store_errors_total", "Failed object store calls", ["operation"]
            )

    @property
    def bucket(self) -> str:
        return self._bucket

    # --- HTTP -------------------------------------------------------------------

    def _connection(self) -> http.client.HTTPConnection:
        conn: http.client.HTTPConnection | None = getattr(self._local, "conn", None)
        if conn is None:
            factory = (
                http.client.HTTPSConnection if self._settings.secure else http.client.HTTPConnection
            )
            conn = factory(self._settings.endpoint, timeout=self._settings.request_timeout_s)
            self._local.conn = conn
        return conn

    def _drop_connection(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _request(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: IO[bytes] | bytes | None = None,
        payload_sha256: str = _EMPTY_SHA256,
        sink: hashlib._Hash | None = None,
        limit: int | None = None,
    ) -> _Response:
        started = time.perf_counter()
        try:
            for attempt in (1, 2):  # one retry: a pooled keep-alive socket may be stale
                signed = sign_v4(
                    method=method,
                    host=self._settings.endpoint,
                    path=path,
                    query={},
                    headers=dict(headers or {}),
                    payload_sha256=payload_sha256,
                    credentials=self._credentials,
                    now=datetime.now(UTC),
                )
                if hasattr(body, "seek"):
                    body.seek(0)  # type: ignore[union-attr]
                try:
                    conn = self._connection()
                    conn.request(method, urllib.parse.quote(path, safe="/-_.~"), body, signed)
                    response = conn.getresponse()
                    return self._read(response, sink=sink, limit=limit)
                except (http.client.HTTPException, ConnectionError, TimeoutError, OSError) as exc:
                    self._drop_connection()
                    if attempt == 2:
                        raise StorageUnavailableError(f"object store {operation}: {exc}") from exc
            raise AssertionError("unreachable")  # pragma: no cover
        except StorageError:
            if self._errors is not None:
                self._errors.labels(operation=operation).inc()
            raise
        finally:
            if self._latency is not None:
                self._latency.labels(operation=operation).observe(time.perf_counter() - started)

    def _read(
        self,
        response: http.client.HTTPResponse,
        *,
        sink: hashlib._Hash | None,
        limit: int | None,
    ) -> _Response:
        headers = {k.lower(): v for k, v in response.getheaders()}
        parts: list[bytes] = []
        received = 0
        while chunk := response.read(_CHUNK):
            received += len(chunk)
            if limit is not None and received > limit and response.status == 200:
                self._drop_connection()
                raise IntegrityError(f"object is larger than its reference ({limit} bytes)")
            if sink is not None and response.status == 200:
                sink.update(chunk)
            parts.append(chunk)
        return _Response(response.status, headers, b"".join(parts))

    @staticmethod
    def _fail(operation: str, response: _Response) -> StorageError:
        detail = response.body[:300].decode("utf-8", "replace")
        if response.status >= 500:
            return StorageUnavailableError(f"{operation}: HTTP {response.status} {detail}")
        if b"XAmzContentSHA256Mismatch" in response.body:
            return IntegrityError(f"{operation}: server rejected the payload digest")
        return StorageError(f"{operation}: HTTP {response.status} {detail}")

    # --- buckets ------------------------------------------------------------------

    def ensure_bucket(self) -> bool:
        """Create the bucket if missing. Returns True if it was created."""
        head = self._request("head_bucket", "HEAD", f"/{self._bucket}")
        if head.status == 200:
            return False
        if head.status != 404:
            raise self._fail("head_bucket", head)
        created = self._request("create_bucket", "PUT", f"/{self._bucket}")
        if created.status == 200:
            return True
        if b"BucketAlreadyOwnedByYou" in created.body:
            return False
        raise self._fail("create_bucket", created)

    # --- ObjectStore --------------------------------------------------------------

    def put(
        self,
        data: Payload,
        *,
        media_type: str | None = None,
        expected_digest: ContentDigest | None = None,
    ) -> BlobRef:
        with tempfile.SpooledTemporaryFile(
            max_size=self._settings.spool_memory_bytes, dir=str(self._scratch_dir)
        ) as spool:
            hasher = hashlib.sha256()
            size = 0
            for chunk in _chunks(data):
                size += len(chunk)
                if size > self._settings.max_object_bytes:
                    raise StorageError(
                        f"object exceeds max_object_bytes ({self._settings.max_object_bytes})"
                    )
                hasher.update(chunk)
                spool.write(chunk)
            digest = ContentDigest("sha256:" + hasher.hexdigest())
            if expected_digest is not None and expected_digest != digest:
                raise IntegrityError(f"supplied digest {expected_digest} != content {digest}")
            key = raw_key(digest)
            ref = BlobRef(
                uri=uri_of(self._bucket, key), digest=digest, size_bytes=size, media_type=media_type
            )
            existing = self.stat(digest)
            if existing is not None:
                self._verify_existing(existing, digest, size)
                return ref
            response = self._request(
                "put",
                "PUT",
                f"/{self._bucket}/{key}",
                headers={
                    "Content-Length": str(size),
                    "Content-Type": media_type or "application/octet-stream",
                    _META_SHA256: digest.hex,
                    "If-None-Match": "*",
                },
                body=spool,
                payload_sha256=digest.hex,
            )
            if response.status == 200:
                return ref
            if response.status == 412:  # a concurrent writer stored it first
                raced = self.stat(digest)
                if raced is None:
                    raise StorageError(f"precondition failed but {key} is absent")
                self._verify_existing(raced, digest, size)
                return ref
            raise self._fail("put", response)

    @staticmethod
    def _verify_existing(info: ObjectInfo, digest: ContentDigest, size: int) -> None:
        if info.digest != digest or info.size_bytes != size:
            raise IntegrityError(
                f"{info.uri} exists with digest {info.digest}/{info.size_bytes} B, "
                f"expected {digest}/{size} B; refusing to overwrite"
            )

    def get(self, ref: BlobRef) -> bytes:
        bucket, key = parse_uri(ref.uri)
        hasher = hashlib.sha256()
        response = self._request(
            "get", "GET", f"/{bucket}/{key}", sink=hasher, limit=ref.size_bytes
        )
        if response.status != 200:
            raise self._fail("get", response)
        actual = ContentDigest("sha256:" + hasher.hexdigest())
        if actual != ref.digest or len(response.body) != ref.size_bytes:
            raise IntegrityError(f"{ref.uri}: read {actual}, reference pins {ref.digest}")
        return response.body

    def stat(self, digest: ContentDigest) -> ObjectInfo | None:
        key = raw_key(digest)
        response = self._request("head", "HEAD", f"/{self._bucket}/{key}")
        if response.status == 404:
            return None
        if response.status != 200:
            raise self._fail("head", response)
        recorded = response.headers.get(_META_SHA256)
        return ObjectInfo(
            uri=uri_of(self._bucket, key),
            digest=ContentDigest("sha256:" + recorded) if recorded else digest,
            size_bytes=int(response.headers.get("content-length", "-1")),
            media_type=response.headers.get("content-type"),
        )

    def exists(self, digest: ContentDigest) -> bool:
        return self.stat(digest) is not None

    def close(self) -> None:
        self._drop_connection()
