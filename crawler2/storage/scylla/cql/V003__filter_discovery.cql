-- P6 filter engine and discovery (docs/phases/p06-filter-discovery/design.md §13).
-- Additive; no existing table changes.

-- F1: one immutable import of a rule source (provenance). Written after its F2 rows.
CREATE TABLE IF NOT EXISTS ${keyspace}.filter_sources (
    source text,
    revision text,
    revision_at timestamp,
    origin text,
    input_sha256 text,
    importer text,
    license text,
    title text,
    list_version text,
    rule_count int,
    enabled_count int,
    report text,
    created_by text,
    PRIMARY KEY (source, revision)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- F2: the rules of one source revision, spread over 16 buckets (bounded partitions).
CREATE TABLE IF NOT EXISTS ${keyspace}.filter_rules (
    source text,
    revision text,
    bucket int,
    rule_id text,
    enabled boolean,
    doc text,
    PRIMARY KEY ((source, revision, bucket), rule_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- F3: an immutable ruleset = source revisions + decision policy; id = content digest.
CREATE TABLE IF NOT EXISTS ${keyspace}.filter_rulesets (
    ruleset_id text PRIMARY KEY,
    sources text,
    rule_count int,
    policy text,
    semantics text,
    psl text,
    created_at timestamp,
    created_by text,
    note text
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- F4: the active ruleset pointer; changed only by LWT (compare-and-set).
CREATE TABLE IF NOT EXISTS ${keyspace}.filter_active (
    name text PRIMARY KEY,
    ruleset_id text,
    previous_ruleset_id text,
    activated_at timestamp,
    activated_by text
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- F5: revisit gate and latest filter decision per URL. Derived (rebuildable).
CREATE TABLE IF NOT EXISTS ${keyspace}.url_admission (
    url_id uuid PRIMARY KEY,
    url text,
    last_admitted_at timestamp,
    last_origin text,
    last_priority int,
    last_queue text,
    last_action text,
    last_rule_id text,
    last_ruleset text,
    last_decided_at timestamp
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- F6: decision history per URL, written on change or admission only; rows carry a TTL.
CREATE TABLE IF NOT EXISTS ${keyspace}.filter_decisions_by_url (
    url_id uuid,
    decided_at timestamp,
    context text,
    url text,
    outcome text,
    action text,
    rule_id text,
    ruleset text,
    decision text,
    source_observation_id uuid,
    PRIMARY KEY (url_id, decided_at, context)
) WITH CLUSTERING ORDER BY (decided_at DESC, context ASC)
  AND compaction = {'class': 'SizeTieredCompactionStrategy'};

-- F7: redirect-chain decision of one observation.
CREATE TABLE IF NOT EXISTS ${keyspace}.redirect_decisions_by_observation (
    observation_id uuid PRIMARY KEY,
    action text,
    decisions text,
    ruleset text,
    decided_at timestamp
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- F8: aggregated browser interception decisions of one observation.
CREATE TABLE IF NOT EXISTS ${keyspace}.interceptions_by_observation (
    observation_id uuid PRIMARY KEY,
    counts map<text, int>,
    blocked text,
    ruleset text,
    recorded_at timestamp
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- F9: seed provenance.
CREATE TABLE IF NOT EXISTS ${keyspace}.seeds_by_source (
    seed_source text,
    url_id uuid,
    url text,
    original text,
    file_path text,
    file_sha256 text,
    line int,
    imported_at timestamp,
    metadata map<text, text>,
    PRIMARY KEY (seed_source, url_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- F10: search results with provenance, per query and UTC day.
CREATE TABLE IF NOT EXISTS ${keyspace}.search_results_by_query_day (
    query_digest text,
    day int,
    retrieved_at timestamp,
    engine text,
    rank int,
    query text,
    adapter_version text,
    returned_url text,
    url text,
    title text,
    snippet text,
    outcome text,
    PRIMARY KEY ((query_digest, day), retrieved_at, engine, rank)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- F11: rooted sites of the M1 scope rule (one small partition).
CREATE TABLE IF NOT EXISTS ${keyspace}.scope_sites (
    scope text,
    domain text,
    origin text,
    added_at timestamp,
    detail text,
    PRIMARY KEY (scope, domain)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};
