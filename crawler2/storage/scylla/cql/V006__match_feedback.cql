-- P11 match feedback (docs/phases/p11-feedback-loop/design.md §4.3).
-- P11-owned, derived and rebuildable from the P3/P4 match projections (match.found)
-- and the media registry (M3/M5) plus page observations (W4). Additive.

-- B1: one row per (source host, match, page observation that carried the media).
-- Upserted with identical values on replay, so redelivery cannot double-count.
-- Evidence older than the policy's max age is worthless; rows expire after 90 days.
CREATE TABLE IF NOT EXISTS ${keyspace}.feedback_evidence (
    domain_id uuid,
    match_id uuid,
    page_observation_id uuid,
    target_id uuid,
    target_version int,
    decided_at timestamp,
    confidence double,
    verdict text,
    page_url text,
    template text,
    page_kind text,
    capability text,
    PRIMARY KEY ((domain_id), match_id, page_observation_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'}
  AND default_time_to_live = 7776000;

-- B2: hosts that have evidence (single bucket: bounded by productive hosts, read
-- whole by the admission snapshot). Same TTL as B1.
CREATE TABLE IF NOT EXISTS ${keyspace}.feedback_sources (
    bucket int,
    domain_id uuid,
    PRIMARY KEY ((bucket), domain_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'}
  AND default_time_to_live = 7776000;
