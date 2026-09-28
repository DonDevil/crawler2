-- crawler2 keyspace, schema version 1 (P2). Design: docs/phases/p02-storage/schema.md.
-- One table per access pattern of docs/phases/p01-contracts/access-patterns.md.
-- Rules: IDs are stored as uuid (the type prefix is implied by the column);
-- `doc` columns hold the exact contract JSON (lossless, additive-evolution
-- safe); buckets/shards come from crawler2/storage/layout.py.
-- Released migrations are immutable (their checksum is recorded).

-- W1: fetch attempt by id. Authoritative, retained (ADR-005: no unagreed deletion).
CREATE TABLE IF NOT EXISTS ${keyspace}.fetch_attempts (
    fetch_attempt_id uuid PRIMARY KEY,
    url_id uuid,
    domain_id uuid,
    finished_at timestamp,
    doc text
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- W2: recent attempts of a domain, per UTC day and shard (fetch-profile learning window).
CREATE TABLE IF NOT EXISTS ${keyspace}.fetch_attempts_by_domain_day (
    domain_id uuid,
    day int,
    shard int,
    finished_at timestamp,
    fetch_attempt_id uuid,
    url_id uuid,
    capability text,
    outcome text,
    http_status int,
    bytes_received bigint,
    elapsed_ms int,
    PRIMARY KEY ((domain_id, day, shard), finished_at, fetch_attempt_id)
) WITH CLUSTERING ORDER BY (finished_at DESC, fetch_attempt_id ASC)
  AND compaction = {'class': 'TimeWindowCompactionStrategy', 'compaction_window_unit': 'DAYS', 'compaction_window_size': 1}
  AND default_time_to_live = 2592000
  AND gc_grace_seconds = 10800;

-- W3: recent attempts of one URL (escalation history), TTL-bounded.
CREATE TABLE IF NOT EXISTS ${keyspace}.fetch_attempts_by_url (
    url_id uuid,
    finished_at timestamp,
    fetch_attempt_id uuid,
    capability text,
    outcome text,
    http_status int,
    elapsed_ms int,
    PRIMARY KEY ((url_id), finished_at, fetch_attempt_id)
) WITH CLUSTERING ORDER BY (finished_at DESC, fetch_attempt_id ASC)
  AND compaction = {'class': 'TimeWindowCompactionStrategy', 'compaction_window_unit': 'DAYS', 'compaction_window_size': 1}
  AND default_time_to_live = 2592000
  AND gc_grace_seconds = 10800;

-- W7 (+W4 point): page observation by id. Authoritative, retained.
CREATE TABLE IF NOT EXISTS ${keyspace}.page_observations (
    observation_id uuid PRIMARY KEY,
    url_id uuid,
    observed_at timestamp,
    doc text
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- W6 (+W4 history): observation history of a URL, per UTC month. Authoritative, retained.
CREATE TABLE IF NOT EXISTS ${keyspace}.page_observations_by_url (
    url_id uuid,
    month int,
    observed_at timestamp,
    observation_id uuid,
    doc text,
    PRIMARY KEY ((url_id, month), observed_at, observation_id)
) WITH CLUSTERING ORDER BY (observed_at DESC, observation_id ASC)
  AND compaction = {'class': 'SizeTieredCompactionStrategy'};

-- W5: latest observation of a URL. Derived (W6); cells use latest_wins(observed_at).
CREATE TABLE IF NOT EXISTS ${keyspace}.latest_observation_by_url (
    url_id uuid PRIMARY KEY,
    observation_id uuid,
    observed_at timestamp,
    http_status int,
    page_version_id uuid,
    content_type text,
    etag text,
    last_modified text,
    snapshot_uri text
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- W8: page versions of a URL, per UTC month of sighting. Derived (W6).
-- first_* cells use earliest_wins, last_seen uses latest_wins.
CREATE TABLE IF NOT EXISTS ${keyspace}.page_versions_by_url (
    url_id uuid,
    month int,
    page_version_id uuid,
    first_seen timestamp,
    first_observation_id uuid,
    last_seen timestamp,
    body_digest text,
    body_size bigint,
    content_type text,
    PRIMARY KEY ((url_id, month), page_version_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- W9: links out of a page version. Authoritative (written by extraction), immutable per version.
CREATE TABLE IF NOT EXISTS ${keyspace}.links_by_page_version (
    page_version_id uuid,
    target_url_id uuid,
    relation text,
    target_url text,
    anchor_text text,
    nofollow boolean,
    PRIMARY KEY ((page_version_id), target_url_id, relation)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- W10: inlinks of a URL ("who links here"), hash-sharded by source URL. Derived (W9).
CREATE TABLE IF NOT EXISTS ${keyspace}.inlinks_by_url (
    target_url_id uuid,
    shard int,
    source_url_id uuid,
    source_url text,
    first_seen timestamp,
    last_seen timestamp,
    last_page_version_id uuid,
    PRIMARY KEY ((target_url_id, shard), source_url_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- W11: known URLs of a domain, hash-sharded by URL. Derived (W12).
CREATE TABLE IF NOT EXISTS ${keyspace}.urls_by_domain (
    domain_id uuid,
    shard int,
    url_id uuid,
    url text,
    first_seen timestamp,
    last_observed_at timestamp,
    last_status int,
    PRIMARY KEY ((domain_id, shard), url_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- W12: known URL + minimal state. Derived (discovered links, W6).
CREATE TABLE IF NOT EXISTS ${keyspace}.url_state (
    url_id uuid PRIMARY KEY,
    url text,
    domain_id uuid,
    first_seen timestamp,
    last_observed_at timestamp,
    last_status int,
    last_page_version_id uuid
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- W13: domain record (source intelligence columns arrive with P7, additively).
CREATE TABLE IF NOT EXISTS ${keyspace}.domains (
    domain_id uuid PRIMARY KEY,
    host text,
    first_seen timestamp
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- W14: recent observation summaries per domain, UTC day and shard (scheduling window).
CREATE TABLE IF NOT EXISTS ${keyspace}.observations_by_domain_day (
    domain_id uuid,
    day int,
    shard int,
    observed_at timestamp,
    observation_id uuid,
    url_id uuid,
    http_status int,
    page_version_id uuid,
    content_type text,
    body_size bigint,
    PRIMARY KEY ((domain_id, day, shard), observed_at, observation_id)
) WITH CLUSTERING ORDER BY (observed_at DESC, observation_id ASC)
  AND compaction = {'class': 'TimeWindowCompactionStrategy', 'compaction_window_unit': 'DAYS', 'compaction_window_size': 1}
  AND default_time_to_live = 2592000
  AND gc_grace_seconds = 10800;

-- M1: media entity. first_seen earliest_wins, last_seen latest_wins.
CREATE TABLE IF NOT EXISTS ${keyspace}.media (
    media_id uuid PRIMARY KEY,
    locator_url text,
    kind text,
    doc text,
    first_seen timestamp,
    last_seen timestamp
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- M2: media observation by (page observation, media). Authoritative, retained.
CREATE TABLE IF NOT EXISTS ${keyspace}.media_observations (
    page_observation_id uuid,
    media_id uuid,
    observed_at timestamp,
    doc text,
    PRIMARY KEY ((page_observation_id), media_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- M3: observations of a media entity, per UTC month and shard. Authoritative copy of M2.
CREATE TABLE IF NOT EXISTS ${keyspace}.media_observations_by_media (
    media_id uuid,
    month int,
    shard int,
    observed_at timestamp,
    page_observation_id uuid,
    page_url_id uuid,
    page_url text,
    page_version_id uuid,
    probe_status text,
    content_id uuid,
    PRIMARY KEY ((media_id, month, shard), observed_at, page_observation_id)
) WITH CLUSTERING ORDER BY (observed_at DESC, page_observation_id ASC)
  AND compaction = {'class': 'SizeTieredCompactionStrategy'};

-- M4: media on a page version. Derived (M2).
CREATE TABLE IF NOT EXISTS ${keyspace}.media_by_page_version (
    page_version_id uuid,
    media_id uuid,
    kind text,
    locator_url text,
    content_id uuid,
    PRIMARY KEY ((page_version_id), media_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- M5: media by content identity, hash-sharded by media. Derived (M2).
CREATE TABLE IF NOT EXISTS ${keyspace}.media_by_content (
    content_id uuid,
    shard int,
    media_id uuid,
    locator_url text,
    first_seen timestamp,
    PRIMARY KEY ((content_id, shard), media_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- M6: content versions of a media entity, per UTC month. Derived (M2).
CREATE TABLE IF NOT EXISTS ${keyspace}.media_content_versions (
    media_id uuid,
    month int,
    content_id uuid,
    scheme text,
    digest text,
    first_seen timestamp,
    last_seen timestamp,
    PRIMARY KEY ((media_id, month), content_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- P1: representation status per content and spec. Projection of fingerprinter events.
-- failure cells use version_wins(attempt): the highest attempt is kept.
CREATE TABLE IF NOT EXISTS ${keyspace}.representation_status (
    content_id uuid,
    spec_key text,
    requested_at timestamp,
    request_event_id uuid,
    ready_at timestamp,
    representation_id uuid,
    representation_doc text,
    failed_at timestamp,
    failure text,
    retryable boolean,
    failed_attempt int,
    PRIMARY KEY ((content_id), spec_key)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- P2: targets (one listing partition). doc/version cells use version_wins(version).
CREATE TABLE IF NOT EXISTS ${keyspace}.targets (
    bucket int,
    target_id uuid,
    version int,
    doc text,
    retired_at timestamp,
    retire_reason text,
    PRIMARY KEY ((bucket), target_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- P3: matches by content identity. Projection of match.found.
CREATE TABLE IF NOT EXISTS ${keyspace}.matches_by_content (
    content_id uuid,
    match_id uuid,
    decided_at timestamp,
    doc text,
    PRIMARY KEY ((content_id), match_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- P4: matches by target, per UTC month of decision and shard.
CREATE TABLE IF NOT EXISTS ${keyspace}.matches_by_target (
    target_id uuid,
    month int,
    shard int,
    match_id uuid,
    target_version int,
    content_id uuid,
    verdict text,
    confidence double,
    decided_at timestamp,
    media_ids set<uuid>,
    PRIMARY KEY ((target_id, month, shard), match_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- P5: matches by source domain, per UTC month of decision and shard.
CREATE TABLE IF NOT EXISTS ${keyspace}.matches_by_domain (
    domain_id uuid,
    month int,
    shard int,
    match_id uuid,
    target_id uuid,
    target_version int,
    content_id uuid,
    verdict text,
    confidence double,
    decided_at timestamp,
    PRIMARY KEY ((domain_id, month, shard), match_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- E1: at most one evidence item per match (LWT INSERT IF NOT EXISTS).
CREATE TABLE IF NOT EXISTS ${keyspace}.evidence_by_match (
    match_id uuid PRIMARY KEY,
    evidence_id uuid,
    opened_at timestamp
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- E2: evidence item and state (LWT: collecting -> sealed; sealed is write-once).
CREATE TABLE IF NOT EXISTS ${keyspace}.evidence (
    evidence_id uuid PRIMARY KEY,
    match_id uuid,
    target_id uuid,
    state text,
    collected_at timestamp,
    candidate_doc text,
    finalized_at timestamp,
    seal_doc text
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- E4: evidence by target, per UTC month of collection and shard. Derived (E2).
CREATE TABLE IF NOT EXISTS ${keyspace}.evidence_by_target (
    target_id uuid,
    month int,
    shard int,
    evidence_id uuid,
    match_id uuid,
    collected_at timestamp,
    finalized_at timestamp,
    PRIMARY KEY ((target_id, month, shard), evidence_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- X1/X2: transactional outbox, per shard and minute bucket (ADR-013).
CREATE TABLE IF NOT EXISTS ${keyspace}.outbox (
    shard int,
    bucket bigint,
    event_id uuid,
    event_type text,
    schema_version text,
    producer_component text,
    occurred_at timestamp,
    correlation_id uuid,
    causation_id uuid,
    idempotency_key text,
    envelope text,
    enqueued_at timestamp,
    published_at timestamp,
    PRIMARY KEY ((shard, bucket), event_id)
) WITH compaction = {'class': 'TimeWindowCompactionStrategy', 'compaction_window_unit': 'DAYS', 'compaction_window_size': 1}
  AND default_time_to_live = 1209600
  AND gc_grace_seconds = 10800;

-- X2: relay progress per outbox shard: every bucket below `next_bucket` is published.
CREATE TABLE IF NOT EXISTS ${keyspace}.outbox_relay_checkpoints (
    shard int PRIMARY KEY,
    next_bucket bigint,
    updated_at timestamp,
    updated_by text
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- X3: consumer idempotency markers; TTL = outbox retention (the redelivery horizon).
CREATE TABLE IF NOT EXISTS ${keyspace}.processed_events (
    consumer text,
    idempotency_key text,
    event_id uuid,
    event_type text,
    processed_at timestamp,
    PRIMARY KEY ((consumer, idempotency_key))
) WITH compaction = {'class': 'TimeWindowCompactionStrategy', 'compaction_window_unit': 'DAYS', 'compaction_window_size': 1}
  AND default_time_to_live = 1209600
  AND gc_grace_seconds = 10800;
