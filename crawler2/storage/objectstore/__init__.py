"""Object storage: interface, content-addressed layout, S3/MinIO implementation."""

from crawler2.storage.objectstore.base import ObjectInfo, ObjectStore, Payload
from crawler2.storage.objectstore.s3 import S3ObjectStore

__all__ = ["ObjectInfo", "ObjectStore", "Payload", "S3ObjectStore"]
