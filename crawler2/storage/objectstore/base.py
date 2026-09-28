"""Object-store interface: immutable, content-addressed blobs (ADR-014).

Domain code sees ``BlobRef`` (P1) and this interface only. Bytes go in,
a digest-pinned reference comes out; the store never interprets content
(no HTML parsing, link or media extraction — that is P5/P8).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import BinaryIO, Protocol

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.models.blobs import BlobRef

Payload = bytes | BinaryIO | Iterable[bytes]
"""Whole bytes, a readable binary file, or an iterable of chunks (streamed)."""


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    uri: str
    digest: ContentDigest
    size_bytes: int
    media_type: str | None


class ObjectStore(Protocol):
    def put(
        self,
        data: Payload,
        *,
        media_type: str | None = None,
        expected_digest: ContentDigest | None = None,
    ) -> BlobRef:
        """Store bytes under their SHA-256. Idempotent: equal bytes → equal ref, one object.

        ``expected_digest`` is verified against the bytes actually received;
        a mismatch raises ``IntegrityError`` and stores nothing.
        """
        ...

    def get(self, ref: BlobRef) -> bytes:
        """Read and verify size and digest; ``IntegrityError`` on mismatch."""
        ...

    def stat(self, digest: ContentDigest) -> ObjectInfo | None: ...

    def exists(self, digest: ContentDigest) -> bool: ...
