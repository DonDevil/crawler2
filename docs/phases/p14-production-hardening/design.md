# P14 design (groundwork)

Approach: reuse what exists; add the smallest thing that closes each gap; never state a property the
environment cannot prove. The *topology* P14 ultimately targets is the plan's: crawler pools on host A,
GPU encoder on the RTX 2050 host, 3-node Scylla (RF=3), Redis, MinIO (distributed if ≥ 4 drives), fingerprinter.
None of those hosts exists in this sprint's environment, so everything below is written to be configuration-only
across hosts and **validated on one**.

| Area | Decision | Where |
|---|---|---|
| SSRF | one shared guard (`core/netguard.py`); pin the connection for direct HTTP, check-per-hop for proxied/browser; typed non-retryable outcome; no permissive allow-list in prod | security.md §1 |
| Metrics | existing `Metrics` registry; deterministic port = base + role offset; bind loopback by default; a separate exporter for what no worker can see; absent ≠ zero | observability.md §1 |
| Alerts/SLOs | Prometheus rules as code, each unit-tested with promtool; every referenced series checked against the code; destination left to the operator | observability.md §2–3 |
| Tracing | no contract change: root a `correlation_id` at the recorder and use the existing envelope fields; `crawler2-trace` reconstructs chains from retained streams | observability.md §4 |
| Supervision | systemd template units + one launcher; no orchestration framework | deployment.md |
| Secrets | startup refuses weak/placeholder prod credentials; Redis ACL users; MinIO service user; generated, never printed | security.md §2 |
| Backup/restore | file-based sets with a SHA-256 manifest; Scylla via REST snapshots; MinIO via S3 API with content-address check; restore only into new names; evidence cross-reference check | recovery.md |
| Soak | a package, not a script: config schema with explicit "undecided" limits, checksummed checkpoints, ledger-based reconciliation, five-valued verdict, escaped reports | soak.md |
| CI | add browser, observability and later-tier jobs; none run remotely yet | development.md |

Rejected: a service mesh or Kubernetes (no multi-node need that systemd + config does not meet); inventing
thresholds, alert recipients or retention periods; making the browser path look pinned; auto-restoring Redis;
raising the P10 equivalence threshold's tolerance or moving its target.
