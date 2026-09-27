"""References to immutable objects in object storage."""

from __future__ import annotations

from typing import Annotated, ClassVar

from pydantic import Field

from antipiracy_contracts.base import ContractKind, ContractModel
from antipiracy_contracts.digests import ContentDigest


class BlobRef(ContractModel):
    """An immutable stored object, located by an opaque URI and pinned by its digest.

    The key layout behind ``uri`` belongs to the storage layer (P2); consumers
    only dereference it and verify ``digest``. Because the digest is part of
    the reference, a reference can never silently point at different bytes.
    """

    KIND: ClassVar[ContractKind] = ContractKind.VALUE

    uri: Annotated[str, Field(pattern=r"^[a-z][a-z0-9+.-]*://\S+$", max_length=2048)]
    digest: ContentDigest
    size_bytes: Annotated[int, Field(ge=0)]
    media_type: Annotated[str, Field(min_length=1, max_length=200)] | None = None
