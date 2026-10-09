-- P8 media registry & identity (docs/phases/p08-media-registry/design.md §8).
-- P8-owned and additive: M1-M6 and representation_status (V001) are reused
-- unchanged through their repositories. LWT tables are single-partition only
-- (the keyspace has tablets disabled, migrations.py).

-- R1: the resolver's reader state, one partition per reader (ADR-024 rule 3).
-- Every transition (acquire, renew, advance, gap, accept-gap) is an LWT
-- conditioned on epoch. state: running | gap.
CREATE TABLE IF NOT EXISTS ${keyspace}.media_reader_state (
    reader text PRIMARY KEY,
    epoch int,
    owner text,
    lease_until timestamp,
    next_bucket bigint,
    state text,
    gap_from bigint,
    gap_to bigint,
    gaps int,
    started_bucket bigint,
    updated_at timestamp,
    updated_by text
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- R2: outbox retention gaps, recorded on detection and on operator acceptance
-- (ADR-024 rule 6). Never expires.
CREATE TABLE IF NOT EXISTS ${keyspace}.media_reader_gaps (
    reader text,
    gap_from bigint,
    gap_to bigint,
    detected_at timestamp,
    detected_by text,
    accepted_at timestamp,
    accepted_by text,
    note text,
    PRIMARY KEY ((reader), gap_from)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- R3: the write-once decision per observation (design §4.4). envelope is the
-- complete media.observed envelope; every commit writes exactly this envelope
-- through P2's record_observation. No TTL, like M2.
CREATE TABLE IF NOT EXISTS ${keyspace}.media_observation_claims (
    page_observation_id uuid,
    media_id uuid,
    event_id uuid,
    envelope text,
    claimed_at timestamp,
    claimed_epoch int,
    claimed_by text,
    committed_at timestamp,
    PRIMARY KEY ((page_observation_id), media_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- R4: probe facts (level 2), authoritative, one row per probe (design §5.4).
CREATE TABLE IF NOT EXISTS ${keyspace}.media_probes (
    media_id uuid,
    month int,
    probed_at timestamp,
    probe_id uuid,
    doc text,
    PRIMARY KEY ((media_id, month), probed_at, probe_id)
) WITH CLUSTERING ORDER BY (probed_at DESC, probe_id ASC)
  AND compaction = {'class': 'SizeTieredCompactionStrategy'};

-- R5: full-content hash attempts (level 3, full-bytes/v1), authoritative.
CREATE TABLE IF NOT EXISTS ${keyspace}.media_hashes (
    media_id uuid,
    month int,
    hashed_at timestamp,
    hash_id uuid,
    outcome text,
    content_id uuid,
    doc text,
    PRIMARY KEY ((media_id, month), hashed_at, hash_id)
) WITH CLUSTERING ORDER BY (hashed_at DESC, hash_id ASC)
  AND compaction = {'class': 'SizeTieredCompactionStrategy'};

-- R6: derived, rebuildable from R4/R5: the latest probe, the latest hash attempt
-- and the latest ok hash per media (latest-wins cell timestamps).
CREATE TABLE IF NOT EXISTS ${keyspace}.media_probe_latest (
    media_id uuid PRIMARY KEY,
    probe_doc text,
    hash_doc text,
    ok_hash_doc text
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- R7: at most one logical encode.requested per (content, spec) (design §7.1).
-- envelope is the complete stored encode.requested; every (re-)emission writes it.
CREATE TABLE IF NOT EXISTS ${keyspace}.encode_claims (
    content_id uuid,
    spec_key text,
    event_id uuid,
    envelope text,
    claimed_at timestamp,
    claimed_by text,
    source_media_id uuid,
    referer text,
    emitted_at timestamp,
    PRIMARY KEY ((content_id), spec_key)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- R8: claims by day for the emission sweeper (written before the claim).
CREATE TABLE IF NOT EXISTS ${keyspace}.encode_claims_by_day (
    day int,
    shard int,
    content_id uuid,
    spec_key text,
    claimed_at timestamp,
    PRIMARY KEY ((day, shard), content_id, spec_key)
) WITH compaction = {'class': 'TimeWindowCompactionStrategy', 'compaction_window_unit': 'DAYS', 'compaction_window_size': 1}
  AND default_time_to_live = 1209600
  AND gc_grace_seconds = 10800;
