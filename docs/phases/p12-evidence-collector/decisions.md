# P12 decisions

Date: 2026-10-22. Engineering decisions taken without owner input (all
reversible), then the owner decisions that remain open.

## 1. Engineering decisions

| # | Decision | Why / alternative |
|---|---|---|
| D-1 | Keep the P1 contracts and events unchanged; add a crawler2-internal candidate model (E5 doc) | E2's contract candidate is write-once and needs ≥ 1 observed source; changing a shared contract was not necessary |
| D-2 | E1 stays the identity (one evidence item per match); `collected_at` = E1 `opened_at` | deterministic across replays and hosts (no per-host clock disagreement) |
| D-3 | Finalization authority is an append-only E6 row claimed with LWT; E2 seal = version 1 | E2 is write-once per evidence id, so supersession needs its own versions; alternative (new evidence id per correction) would break E1's one-per-match rule |
| D-4 | Incomplete candidates are **refused** by default; `--allow-partial` produces a version marked `partial` in the manifest, case and report; integrity failures and "no observed source" are never finalizable | never silently promote incomplete evidence; still lets an operator preserve a probable/partial item |
| D-5 | `probable` verdict is a required-check failure (`match.confirmed`) | the contract says probable "needs review before evidence" |
| D-6 | Ambiguous attribution selects **no** source | never pick an arbitrary page |
| D-7 | No new capture: screenshots, media clips and full headers are declared `not_collected` | no capture capability exists, recapture would re-visit live sites (forbidden in tests, ADR-005 Q2 open); no second crawler/browser framework |
| D-8 | Manifests stored in the existing content-addressed raw bucket | write-once by construction (`If-None-Match`); no new bucket or service |
| D-9 | Application-level immutability only; object lock documented, not enabled | needs bucket re-creation + retention policy = ADR-003/005 decision |
| D-10 | HTML report only, no PDF | no PDF dependency in crawler2; HTML prints to PDF; one maintainable format |
| D-11 | Statistics computed on demand, not stored; no DuckDB | small bounded reads; DuckDB belongs to P13 |
| D-12 | Display URLs redact userinfo, fragments and secret-looking query values; originals stay in protected rows, tied by SHA-256 | exported files must not carry tokens/signatures |
| D-13 | Package assembled in memory, bounded by `max_package_bytes` (256 MiB) | simple and deterministic; streaming export deferred until packages need to be larger |
| D-14 | Campaign = named set of targets + `since` month (E8) | no campaign concept existed; minimal and sufficient for coverage stats |
| D-15 | No RFC 3161 / signing implementation; status shown as `not_configured` | no dependency or service exists; never fabricate trusted time |
| D-16 | Supersession emits no second `evidence.finalized` | its idempotency key is the evidence id; consumers would drop it as a duplicate |

## 2. Configuration defaults (`CRAWLER2_EVIDENCE__*`)

`consumer_group=evidence-collector`, `batch_size=50`, `block_ms=2000`,
`claim_idle_ms=60000`, `max_media_per_match=50`, `max_sources=50`,
`sighting_window_days=90`, `max_artifact_bytes=32 MiB`,
`max_package_bytes=256 MiB`. Nothing in P12 is enabled implicitly: the
collector runs only when `crawler2-evidence collect run` is started, and
P11's `CRAWLER2_FEEDBACK__ENABLED` is untouched.

## 3. Open owner decisions (ADR-005 — not settled by P12)

| # | Question | P12 default until answered |
|---|---|---|
| OD-P12-1 | Jurisdictions and admissibility standard (chain of custody, timestamping, signing) | technical traceability only; no legal claim |
| OD-P12-2 | Are screenshots / video clips / WARC records required? May the system re-visit a source to capture them? | not collected; declared as gaps |
| OD-P12-3 | Retention and deletion of evidence and raw crawl data; WORM/object-lock requirement | never delete; no TTL on V007; no object lock |
| OD-P12-4 | Trusted timestamp authority and signing key custody | not configured; shown in every package |
| OD-P12-5 | Is a `partial` package acceptable for any use, and who may finalize | allowed only with `--allow-partial`; actor recorded in E6/E7 |
| OD-P12-6 | Personal data in snapshots (GDPR/DPDP) and redaction policy for exported artifacts | URL redaction only; artifact bytes are exported as stored |
| OD-P12-7 | M5 acceptance: review of the fixture case package (this phase) and later of a package from a real crawl + real matcher | M5 NOT PASSED. Fixture-package review **reported accepted by the operator** (2026-10-22 audit brief); no durable record - reviewer, review date and scope were not supplied, so this row is not a formal acceptance. Real-package check pending; ADR-005 open |
