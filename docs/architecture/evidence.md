# Evidence collector & finalizer (operator guide)

P12. Design: [../phases/p12-evidence-collector/design.md](../phases/p12-evidence-collector/design.md).
Status: implemented; **M5 not passed (awaiting owner review)**. Nothing
here is a legal determination; packages state that on every page.

## What it does

`match.found` → evidence candidate (provenance from crawler2's registry; no
re-fetch) → validation (completeness + SHA-256 of stored artifacts) →
finalized version (immutable manifest, append-only, audited) → case
package (JSON + HTML + artifacts + checksums) per campaign.

## Setup

```bash
crawler2-storage migrate                      # applies V007 (additive)
crawler2-evidence collect run                 # consumer group "evidence-collector"; scale freely
```

Settings `CRAWLER2_EVIDENCE__*`: `consumer_group`, `batch_size`, `block_ms`,
`claim_idle_ms`, `max_media_per_match`, `max_sources` (50),
`sighting_window_days` (90), `max_artifact_bytes` (32 MiB),
`max_package_bytes` (256 MiB). A stopped collector loses nothing: entries
stay pending in the stream and are reclaimed after `claim_idle_ms`.

## Reviewing and finalizing

```bash
crawler2-evidence campaign create film-x --name "Film X" --since 2026-09 --target tgt_…
crawler2-evidence list --campaign film-x [--status complete]
crawler2-evidence show evd_… [--audit] [--detail]      # detail: redacted provenance
crawler2-evidence validate evd_… [--dry-run]           # exit 0 complete, 2 otherwise
crawler2-evidence finalize evd_… --dry-run             # plan: outcome, version, manifest digest
crawler2-evidence finalize evd_… --actor alice         # exit 2 refused, 3 conflict
crawler2-evidence finalize evd_… --allow-partial       # explicit partial version
crawler2-evidence supersede evd_… --reason "new source" [--allow-partial]
crawler2-evidence collect refresh evd_… [--finalized]  # re-resolve provenance
crawler2-evidence collect rebuild --target tgt_… --months 3   # never touches finalized items
crawler2-evidence campaign stats film-x
crawler2-evidence export --campaign film-x --out /path/case-film-x [--archive] [--no-artifacts] [--dry-run]
```

Exit codes: 0 ok · 1 not found/usage · 2 refused by validation · 3
conflict with a finalized version · 4 export completed with failures.

Rules: a `rejected` candidate (corrupted artifact, changed observation,
no observed source) can never be finalized; a `partial` one only with
`--allow-partial` and is marked partial everywhere; a finalized version is
never changed — corrections are `supersede` (new version naming the old
one). Every refusal and conflicting attempt is in `show --audit`.

## Reading a package offline

```bash
cd case-film-x && sha256sum -c SHA256SUMS      # integrity of every file
xdg-open report.html                           # no network access needed
```

`case.json` is canonical (sorted keys, no export time): two exports of the
same stored data are byte-identical. `evidence/<id>/manifest-v<N>.json` are
the exact sealed bytes; their SHA-256 is in `case.json` and in E6.
`artifacts/sha256/<hex>` are verified bytes named by their digest. URLs in
manifests and reports are display forms (secret parameters `REDACTED`,
with the SHA-256 of the original); originals stay in the protected rows.

## Limits (state them when sharing a package)

- Timestamps are host clocks; no trusted timestamp, no signature.
- Page snapshots are response bodies, not WARC; no screenshots, clips or
  media bytes.
- Immutability is application-level (content-addressed, never-overwritten
  objects; append-only LWT rows); without MinIO object lock an
  administrator can still delete data — tampering is detected by digest,
  not prevented.
- Match verdict/confidence are matcher claims; P10's matcher has not
  passed M3.

## Enabling stronger protection later

1. Object lock: create the evidence bucket with object lock + versioning
   (`mc mb --with-lock`, default retention in governance or compliance
   mode) and point manifests/artifacts at it (ADR-003/005 decision).
2. Trusted timestamp: obtain an RFC 3161 token over the E6
   `manifest_digest` from a TSA, store the token as a content-addressed
   object and reference it from E6 (new column); verify with
   `openssl ts -verify`. Manifest bytes do not change.
3. Signing: detached signature over the manifest digest with an
   operator-held key (never in the repository), stored like the token.
