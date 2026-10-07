-- P7 crawl intelligence, increment 2 (docs/phases/p07-crawl-intelligence/design.md §9.3).
-- P7-owned, derived and rebuildable from the outbox (ADR-023). Additive.

-- I1: one bounded source profile per host (design §4.2). Written by the single
-- projection reader; through_bucket makes a bucket replay idempotent. Rows of a
-- host not fetched for 30 days (and of dead generations) expire.
CREATE TABLE IF NOT EXISTS ${keyspace}.intel_source_profiles (
    domain_id uuid PRIMARY KEY,
    generation int,
    policy text,
    through_bucket bigint,
    profile text,
    updated_at timestamp
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'}
  AND default_time_to_live = 2592000;

-- I2: the projection reader's checkpoint and generation (one row per projection;
-- advanced by LWT once per outbox bucket).
CREATE TABLE IF NOT EXISTS ${keyspace}.intel_projection_state (
    projection text PRIMARY KEY,
    generation int,
    policy text,
    next_bucket bigint,
    gaps int,
    rebuilds int,
    last_gap_from bigint,
    last_gap_to bigint,
    generation_started_at timestamp,
    updated_at timestamp,
    updated_by text
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};
