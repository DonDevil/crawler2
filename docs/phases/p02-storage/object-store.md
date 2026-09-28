# P2 Object store

Rationale: **ADR-014**. Code: `crawler2/storage/objectstore/`.

## Interface

```python
from crawler2.storage.objectstore import S3ObjectStore

store = S3ObjectStore(settings.minio, scratch_dir=settings.scratch_dir)
store.ensure_bucket()  # done by `crawler2-storage migrate`
ref = store.put(
    body_bytes_or_file_or_chunks, media_type="text/html", expected_digest=None
)  # -> BlobRef (P1)
data = store.get(ref)  # verified against ref.digest / size
store.stat(ref.digest), store.exists(ref.digest)
```

`put` accepts bytes, a binary file, or an iterable of chunks; it hashes
while spooling (memory ≤ `spool_memory_bytes`, then `scratch_dir`) and
refuses objects over `max_object_bytes` (256 MiB) instead of truncating.

## Layout and identity

`s3://crawler2-raw/raw/sha256/<h0h1>/<h2h3>/<sha256 hex>` — a pure function
of the bytes. Same bytes → same `BlobRef.uri`/digest → one object, from any
host. `BlobRef.media_type` is the caller's statement and not part of the
identity (the object's Content-Type is set by the first writer).

## Integrity checks (all tested against MinIO)

| Check | Where | Test |
|---|---|---|
| digest computed from the received bytes | `put` | `test_content_addressed_put_get_and_dedupe` |
| supplied digest must match; nothing stored otherwise | `put` | `test_wrong_digest_is_rejected_and_nothing_is_stored` |
| server re-verifies the payload hash (`x-amz-content-sha256`) | MinIO | `test_server_rejects_a_payload_that_does_not_match_its_signed_hash` |
| never overwrite: `If-None-Match: *`; an existing object must match size + recorded hash | `put` | `test_an_existing_object_is_never_overwritten` |
| reads re-hashed and length-checked against the reference | `get` | `test_reads_are_verified_against_the_reference` |
| `BlobRef` persists through a page observation and dereferences | repo + store | `test_blobref_persists_through_a_page_observation` |
| SigV4 signer | unit | AWS published "GET Object" example |

## Not in the store

No HTML parsing, link or media extraction, WARC processing or lifecycle
deletion. Garbage collection of unreferenced objects is reference-driven
and deferred (P13/P14, after ADR-005).
