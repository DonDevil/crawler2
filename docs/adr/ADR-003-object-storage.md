# ADR-003 — S3-compatible object storage (MinIO)

Status: Accepted (P0, 2026-09-28)

## Context

Scylla stores metadata; large or binary artifacts do not belong in it.
V2 needs raw fetched bodies, page snapshots and, later, evidence artifacts
(screenshots, manifests, clips; scope pending ADR-005), shared by every
host (ADR-006: no authoritative local state).

## Decision

- All blobs go to an **S3-compatible object store**, accessed only through
  a small `ObjectStore` interface in `crawler2.storage` (P2). Code depends
  on the S3 API, not on MinIO specifics; AWS S3, Ceph RGW, SeaweedFS or
  Garage are drop-in alternatives.
- **MinIO** in development and on self-hosted multi-host deployments
  (single-node in dev; distributed erasure-coded mode on ≥4 drives/nodes in
  production, decided in P14).
- **Content-addressed keys** where content is immutable:
  `raw/sha256/<aa>/<bb>/<sha256>` for raw bodies and snapshots, so equal
  content is stored once and writes are idempotent across hosts.
  Scylla rows reference objects by hash.
- Buckets by lifecycle, not by host: `crawler2-raw` (raw bodies/snapshots,
  retention policy TBD in P2), `crawler2-evidence` (write-once, object
  lock/retention governed by ADR-005, created in P12).
- Credentials come only from the environment (`.env`, never committed).

## Consequences

- **Image source (P0 finding).** Official `minio/minio` images are no
  longer pullable from Docker Hub or Quay (`requested access to the
  resource is denied`, checked 2026-09-28). The dev stack uses
  `docker.io/pgsty/minio:RELEASE.2026-08-04T00-00-00Z`, a maintained
  community rebuild of AGPLv3 MinIO (`minio --version` reports
  `RELEASE.2026-08-04T00-00-00Z`). Because code targets the S3 API, this
  is swappable; revisit the provider in P14 (production).
- MinIO is AGPLv3; using it as an unmodified network service is the
  intended use. Record any modification or redistribution in ADR-005.
- Hash-addressed objects need garbage collection of unreferenced blobs
  (P2/P14), driven from Scylla references.
