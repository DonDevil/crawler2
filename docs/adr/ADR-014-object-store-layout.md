# ADR-014 — Object-store key layout and integrity

Status: Accepted (P2, 2026-09-28). Refines ADR-003.

## Context

P1 made `BlobRef` an opaque URI pinned by a SHA-256 digest, and requires a
page snapshot's digest to equal the observation's `body_digest`. The plan
draft proposed `raw/{domain}/{page_id}/{version}.warc.gz`.

## Decision

- **Key = content address:** `raw/sha256/<h0h1>/<h2h3>/<h>` in the
  `crawler2-raw` bucket; `BlobRef.uri = s3://crawler2-raw/<key>`.
  - stable identity and collision-free (SHA-256); identical on every host;
  - immutable by construction (a key can only ever hold one byte string);
  - host/path safe (hex only, no URL or domain normalization issues);
  - dedupes identical bodies across URLs, hosts and recrawls;
  - no mutable metadata in the key: URL, domain, time and media type live
    in the Scylla rows that reference the object;
  - two fan-out levels keep file-backed stores (MinIO) from huge directories.
- **The object is the exact body bytes, not a WARC record.** A WARC record
  (or gzip) would have a different digest and break the P1 invariant
  `snapshot.digest == body_digest`. HTTP metadata (status, validators,
  redirects, capability, worker) is already in the page observation. A WARC
  *export* can be derived later (P12/P13) from observation + object.
- **Integrity end to end** (`storage/objectstore/s3.py`): hash while
  spooling (memory up to 8 MiB, then scratch); verify a supplied digest;
  sign the PUT with `x-amz-content-sha256 = <digest>` so the server rejects
  corrupted uploads; `If-None-Match: *` so an existing object is never
  overwritten; accept an existing object only if its recorded size and hash
  match; re-hash on every read.
- **Client:** the S3 API over the standard library (SigV4 tested against
  AWS's published example) instead of boto3/minio — four calls do not
  justify either dependency tree. Domain code sees only `ObjectStore`.

## Consequences

- Retention of raw objects cannot be a per-object lifecycle rule on the raw
  bucket: one object may back many observations and evidence. Deletion
  must be reference-driven (garbage collection from Scylla references,
  P13/P14) and stays disabled until ADR-005 defines retention.
- Evidence artifacts go to a separate `crawler2-evidence` bucket with
  object lock/retention, created in P12 (ADR-003).
- Storage stores bytes and metadata only; it never parses content.
