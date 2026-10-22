-- P12 evidence collector & finalizer (docs/phases/p12-evidence-collector/design.md §5).
-- Additive. The P2 tables E1 evidence_by_match, E2 evidence and E4 evidence_by_target
-- (V001) stay the evidence identity, the write-once contract seal and the per-target
-- index; these tables add the working candidate, the append-only finalized versions,
-- the audit trail and campaign membership. No TTL: evidence is never deleted on a
-- policy that has not been agreed (ADR-005).

-- E5: the working candidate (dossier) of one evidence item. Upserted by the collector
-- while it is not finalized; the doc is a pure function of the match and the registry
-- at collection time plus the first collection instant, so replays write identical cells.
CREATE TABLE IF NOT EXISTS ${keyspace}.evidence_candidates (
    evidence_id uuid PRIMARY KEY,
    match_id uuid,
    target_id uuid,
    status text,
    revision int,
    collected_at timestamp,
    updated_at timestamp,
    doc text,
    validation text,
    validated_at timestamp
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- E6: finalized versions, append-only (LWT INSERT IF NOT EXISTS per version). The
-- manifest bytes live in the content-addressed object store; this row pins their digest.
-- A correction is version n+1 naming the version it supersedes; no row is updated.
CREATE TABLE IF NOT EXISTS ${keyspace}.evidence_versions (
    evidence_id uuid,
    version int,
    status text,
    manifest_uri text,
    manifest_digest text,
    manifest_size bigint,
    finalized_at timestamp,
    finalized_by text,
    supersedes int,
    reason text,
    PRIMARY KEY ((evidence_id), version)
) WITH CLUSTERING ORDER BY (version DESC)
  AND compaction = {'class': 'SizeTieredCompactionStrategy'};

-- E7: audit trail per evidence item (append-only): collection, validation,
-- finalization, refusals, conflicting attempts, supersession.
CREATE TABLE IF NOT EXISTS ${keyspace}.evidence_audit (
    evidence_id uuid,
    at timestamp,
    seq timeuuid,
    action text,
    actor text,
    detail text,
    PRIMARY KEY ((evidence_id), at, seq)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- E8: campaign (case) membership: the targets a case package covers.
CREATE TABLE IF NOT EXISTS ${keyspace}.evidence_campaigns (
    campaign_id text,
    target_id uuid,
    name text STATIC,
    since timestamp STATIC,
    created_at timestamp STATIC,
    added_at timestamp,
    PRIMARY KEY ((campaign_id), target_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- E9: campaign listing (single partition: bounded by the number of cases).
CREATE TABLE IF NOT EXISTS ${keyspace}.evidence_campaign_index (
    bucket int,
    campaign_id text,
    PRIMARY KEY ((bucket), campaign_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};
