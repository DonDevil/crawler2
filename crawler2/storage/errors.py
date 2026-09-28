"""Storage errors. Driver exceptions never cross the repository boundary untranslated
when they carry domain meaning; transport failures propagate as ``StorageUnavailableError``."""

from __future__ import annotations


class StorageError(Exception):
    """Base class for storage-layer failures."""


class StorageUnavailableError(StorageError):
    """A backend could not be reached or timed out. The operation may be retried."""


class IntegrityError(StorageError):
    """Stored or supplied bytes do not match their digest/size. Never retried blindly."""


class ConflictError(StorageError):
    """A write-once or compare-and-set invariant would be violated."""


class SchemaError(StorageError):
    """The database schema is missing, outdated, or its history was tampered with."""
