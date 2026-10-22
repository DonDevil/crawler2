# P12 design — evidence collector & finalizer (as implemented)

Status: implemented 2026-10-22. Code: `crawler2/evidence/` — `model.py`
(candidate, gaps, validation types), `collector.py` (consumer + resolution),
`validate.py`, `manifest.py` (canonical form), `finalize.py`, `store.py`
(V007), `stats.py`, `export.py`, `report.py`, `redact.py`, `cli.py`
(`crawler2-evidence`). Migration: `storage/scylla/cql/V007__evidence_collector.cql`.
Operator guide: [../../architecture/evidence.md](../../architecture/evidence.md).

## 1. Data flow

```
fingerprinter matcher ──match.found.v1──► Redis stream, group "evidence-collector"
                                              │ IdempotentConsumer (processed_events)
                                              ▼
crawler2-evidence collect run ── EvidenceCollector
   E1 open (LWT, one evidence id per match; opened_at = collected_at)
   M5 content→media, M3 media→sightings, W4 observation, W1 attempt, M3 probe
   P2 target projection; P3–P5 record_match (same inputs as P11)
   └─► E5 candidate (upsert, revision++ only on change) + E4 index + E7 audit
                                              ▼
operator: validate → finalize [--allow-partial] → (supersede --reason)
   validate: required checks, W4 re-read, artifact stat + SHA-256 read-back
   manifest: canonical JSON → object store (content-addressed, read back)
   E6 version claim (LWT IF NOT EXISTS) → E2 candidate + seal (LWT) →
   evidence.finalized (outbox) → E5 status finalized → E7 audit
                                              ▼
crawler2-evidence export --campaign … → case.json, manifests, artifacts,
   report.html, SHA256SUMS (+ deterministic .tar.gz)
```

Collection never blocks discovery or matching: it is a separate consumer
group on `match.found`. Nothing is fetched; P12 only references what the
crawl already stored.

## 2. Provenance model

Each fact carries its origin, and the manifest groups facts by origin:

| Section | Origin | Content |
|---|---|---|
| `claims` | fingerprinter matcher (`match.found`) | match id, verdict, confidence, technique, segments, `decided_at` (matcher clock) |
| `observed` | crawler2 fetch workers (W4, W1) | requested/final URL, redirect chain, status, content type, capability, `observed_at`, body SHA-256/size, worker, fetch start/finish |
| `derived` | crawler2 stored records | attribution method and basis, sources total/recorded, source domains, media entities, target (P2 projection) |
| `artifacts` | object store, verified at validation | role, page observation, URI, SHA-256, size, state |
| `review_required`, `limitations`, `disclaimer` | fixed text | what no automated component decides |

**Attribution basis** per source: `content_verified` (the M3 sighting
probed the matched content id at that page) or `locator_only` (the locator
was on the page but its bytes were not probed there). A sighting that
probed *other* bytes is excluded (P11 rule). If no source is
content-verified the candidate is `ambiguous` and **no source is
selected**; if no page carries the content it is `unattributed`. Sources
are bounded (`max_sources`, default 50; content-verified first, then
newest, then ids — deterministic) and truncation is a declared gap.

## 3. Candidate lifecycle (E5)

`status` ∈ `complete` | `incomplete` | `ambiguous` | `unattributed` |
`rejected` | `finalized`, computed from gaps (and set to `rejected` by a
validation integrity failure, `finalized` by finalization).

- Required gaps (block *complete*): `unattributed`,
  `ambiguous_attribution`, `target_unknown`, `verdict_not_confirmed`,
  `page_observation_missing`, `snapshot_not_archived`.
- Informational gaps (always declared, never fabricated):
  `screenshot_not_collected`, `media_sample_not_retained`,
  `response_headers_not_retained`, `fetch_attempt_missing`,
  `sources_truncated`.
- `collected_at` is E1's `opened_at` (the LWT winner's instant), so all
  replays and hosts derive byte-identical candidates; an unchanged
  candidate is not rewritten (`unchanged`); a changed one gets
  `revision + 1`.
- A candidate with any finalized version is **frozen**: the collector
  returns `frozen` and writes nothing. `collect refresh --finalized`
  (or `supersede`, which refreshes first) is the explicit correction path.

## 4. Validation (policy `evidence-finalization/v1`)

Required checks: `match.identity` (match id derives from its inputs —
integrity), `target.known`, `match.confirmed`,
`attribution.content_verified`, `provenance.observations` (every recorded
W4 row exists and still has the recorded digest, final URL, status and
snapshot — a difference is an integrity failure), `artifacts.page_snapshots`
(every snapshot: URI is a content-addressed key of the recorded digest,
object exists with the recorded size, bytes re-hash to the recorded
SHA-256 within `max_artifact_bytes`).

Artifact states: `verified`, `missing`, `corrupted`, `oversized` (exists,
not read), `unverifiable` (another bucket), `not_collected`.

Outcome: `rejected` if any integrity failure or no source with an
observation; else `complete` if every required check passes; else
`partial`. Transient storage errors propagate (an unknown is never stored
as a result). The last validation is stored on E5.

## 5. Storage (V007, additive; no TTL)

| Table | Key | Writes |
|---|---|---|
| E5 `evidence_candidates` | `evidence_id` | upsert (AUTHORITATIVE_WRITE): doc, status, revision, validation |
| E6 `evidence_versions` | `((evidence_id), version DESC)` | `INSERT … IF NOT EXISTS` per version; never updated |
| E7 `evidence_audit` | `((evidence_id), at, seq timeuuid)` | append-only |
| E8 `evidence_campaigns` | `((campaign_id), target_id)`, static name/since/created_at | insert |
| E9 `evidence_campaign_index` | `((bucket), campaign_id)` | insert |
| E4 `evidence_by_target` (V001) | `((target_id, month, shard), evidence_id)` | now also upserted with every candidate |

Partitions are bounded per evidence item (E5–E7), per campaign (E8, by
target count) and by campaign count (E9). Hot paths are point reads;
campaign reads are E4 partitions per (target, month, shard). No artifact
bytes are stored in Scylla.

## 6. Finalization and immutability

`finalize`: plan (validate + manifest) → refuse `rejected`, refuse
`partial` without `--allow-partial` (audited `finalize_refused`) → if a
version exists: identical manifest digest ⇒ `idempotent`, different ⇒
`ConflictError` (audited `finalize_conflict`) → else put the manifest
(content-addressed, `If-None-Match`, read back) → claim E6 v1 (LWT; a
concurrent claimer with another digest ⇒ conflict) → E2 contract candidate
(only observed sources) + E2 seal (LWT) with the manifest BlobRef →
`evidence.finalized` to the outbox → E5 `finalized` → audit `finalized`.
Each step after the E6 claim is idempotent, so a crash or transient error
anywhere is completed by retrying the same command.

`supersede --reason`: refreshes the candidate, refuses if the content is
unchanged, writes version n+1 whose manifest names version n and its
digest (`evidence.supersedes`) and the reason; E6 keeps every version, the
object store keeps every manifest; E2's seal stays the v1 seal (the P1
contract is write-once per evidence id), and no second `evidence.finalized`
is emitted (its idempotency key is the evidence id).

Immutability is application-level: content-addressed objects whose key is
their SHA-256 and which are never overwritten (`If-None-Match: *`; a
mismatching existing object is an `IntegrityError`), append-only LWT rows
and verification on every read. **It is not WORM**: an administrator with
bucket or keyspace credentials can delete or replace data; detection (not
prevention) is by digest. Stronger protection needs MinIO object lock
(bucket created with lock + versioning, governance/compliance retention) —
an ADR-005/ADR-003 decision.

## 7. Canonical manifest (`crawler2.evidence.manifest/v1`)

JSON, UTF-8, sorted keys, `(",", ":")` separators, no NaN, no trailing
newline; timestamps `YYYY-MM-DDTHH:MM:SS.ffffffZ`; arrays sorted (sources by
(page observation, media), artifacts by (role, subject, digest), checks by
id, gaps by (code, subject)); redirect hops keep their order. No generation
time, host, path, credential or random value: inputs are the frozen
candidate, the validation and the version identity. `finalized_at` lives
in E6/E7/the E2 seal, **not** in the manifest. The manifest digest
(`sha256:` of these bytes) is distinct from every artifact digest inside it.

URLs appear as `{url: display form, redacted: bool, original_sha256}`;
the display form drops userinfo and fragments and replaces values of
secret-looking query parameters (`token`, `sig`, `signature`, `key`,
`session`, `X-Amz-*`, …) with `REDACTED`. Originals stay in W4/M3/E5
(protected provenance), tied by the digest.

## 8. Case package (`crawler2.evidence.case/v1`)

```
case.json                                 canonical: campaign, targets, evidence index
                                          (versions, digests, paths), artifact inventory,
                                          statistics, attestation, export failures, disclaimer
report.html                               generated from case.json + manifests only
evidence/<evidence_id>/manifest-v<N>.json exact sealed bytes (every version)
artifacts/sha256/<hex>                    verified bytes (deduplicated by digest)
SHA256SUMS                                every other file
<out>.tar.gz (optional)                   sorted members, mtime 0, uid/gid 0, mode 0444, gzip mtime 0
```

Every name is built from validated ids or hex digests and passes
`safe_relpath` (no absolute, `.`, `..`, empty or non-portable segments);
the writer refuses a non-empty target and paths resolving outside it.
Artifacts are re-verified while copying. Per-artifact bound
`max_artifact_bytes` (32 MiB; larger ones are `too_large_referenced`),
package bound `max_package_bytes` (256 MiB; the package is assembled in
memory). Export states: `included`, `referenced` (`--no-artifacts`),
`too_large_referenced`, `not_packaged` (recorded state was not verified),
`failed_*`. Any failure is listed in `case.json` and the report and exits 4.

The report is static HTML: every value escaped, URLs shown as text (not
links), no scripts or external resources, a disclaimer and the
attestation status at the top. PDF is not produced (no PDF dependency in
crawler2; browsers print the HTML).

## 9. Campaign statistics

Derived on demand (`stats.py`) from E8, E4, E5, E6 and the P4 match
projection over the campaign's months (`since` → now, ≤ 36): targets
(registered / not in projection), matches by verdict (dedup by match id),
media carrying matched content, candidates by status, finalized by status,
superseded versions, rejected, matches without a candidate, source records,
pages (dedup by URL), domains (dedup by host) and pages per domain, basis,
artifact availability, gaps, collection window. *Unavailable* with reason:
pages examined and media discovered (page observations and media are
target-independent by design). No DuckDB: the numbers come from a few
bounded partition reads.

## 10. Failure semantics and recovery

- Consumer: malformed/invalid events are logged, counted
  (`evidence_candidates_total{result="invalid"}`) and acknowledged; a
  storage error stops the batch with the entry unacknowledged; another
  instance takes it over with `XAUTOCLAIM` after `claim_idle_ms`. The
  processed marker is written only after the candidate is durable.
- Finalization: see §6 (retry completes; no duplicates).
- Export: writes nothing over existing files; failures are explicit.

## 11. Metrics (bounded labels only)

`evidence_candidates_total{result=created|updated|unchanged|frozen|invalid|storage_error}`,
`evidence_candidate_status_total{status}`,
`evidence_finalizations_total{result=finalized|idempotent|superseded|refused|conflict|failed}`,
`evidence_artifact_checks_total{state}`, `evidence_exports_total{result}`,
plus the existing `consumer_events_total{consumer="evidence-collector",outcome}`.
Consumer lag is visible as the stream's pending count (`XPENDING`).

## 12. Trusted time and signing

Not implemented. `case.json.attestation` and the report state
`trusted_timestamp: not_configured`, `signature: not_configured`. The
natural extension point is the manifest digest (E6 `manifest_digest`): an
RFC 3161 token over it, or a detached signature, would be stored as an
additional content-addressed object referenced from a new E6 column —
without changing manifest bytes. Steps are in the operator guide.
