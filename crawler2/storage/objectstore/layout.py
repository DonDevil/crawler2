"""Physical key layout of the raw bucket (ADR-014).

``raw/sha256/<h[0:2]>/<h[2:4]>/<h>`` — the key is a pure function of the
exact bytes, so it is stable, collision-free (SHA-256), safe for any host
or path (hex only), identical on every host, and immutable by construction.
Mutable facts (URL, domain, time, media type) live in Scylla rows that
reference the object, never in the key. The two fan-out levels keep
filesystem-backed stores (MinIO on disk) from growing huge directories.
"""

from __future__ import annotations

import re

from antipiracy_contracts.digests import ContentDigest

RAW_PREFIX = "raw/sha256"
URI_SCHEME = "s3"
_KEY_RE = re.compile(r"^raw/sha256/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})$")


def raw_key(digest: ContentDigest) -> str:
    h = digest.hex
    return f"{RAW_PREFIX}/{h[0:2]}/{h[2:4]}/{h}"


def uri_of(bucket: str, key: str) -> str:
    return f"{URI_SCHEME}://{bucket}/{key}"


def parse_uri(uri: str) -> tuple[str, str]:
    """``s3://bucket/key`` → (bucket, key), for raw-layout keys only."""
    prefix = f"{URI_SCHEME}://"
    if not uri.startswith(prefix):
        raise ValueError(f"not an {URI_SCHEME}:// object URI: {uri!r}")
    bucket, _, key = uri[len(prefix) :].partition("/")
    match = _KEY_RE.fullmatch(key)
    if (
        not bucket
        or match is None
        or not match.group(3).startswith(match.group(1) + match.group(2))
    ):
        raise ValueError(f"not a raw content-addressed key: {uri!r}")
    return bucket, key


def digest_of_key(key: str) -> ContentDigest:
    match = _KEY_RE.fullmatch(key)
    if match is None:
        raise ValueError(f"not a raw content-addressed key: {key!r}")
    return ContentDigest("sha256:" + match.group(3))
