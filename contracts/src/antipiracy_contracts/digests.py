"""Content digests: ``<algorithm>:<lowercase hex>``.

The algorithm prefix keeps the wire format open to a future algorithm
without reinterpreting existing values; v1 accepts only SHA-256.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_DIGEST_RE = re.compile(_DIGEST_PATTERN)


class ContentDigest(str):
    """SHA-256 digest of an exact byte sequence, e.g. ``sha256:9f86…``."""

    __slots__ = ()

    def __new__(cls, value: str) -> Self:
        if not _DIGEST_RE.fullmatch(value):
            raise ValueError(f"not a content digest (expected 'sha256:<64 hex>'): {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def of_bytes(cls, data: bytes) -> Self:
        return cls("sha256:" + hashlib.sha256(data).hexdigest())

    @property
    def hex(self) -> str:
        return self.partition(":")[2]

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        # `source` is typed Any by pydantic's hook signature.
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(pattern=_DIGEST_PATTERN),
            serialization=core_schema.to_string_ser_schema(),
        )
