-- P5 extraction & page intelligence (docs/phases/p05-extraction-page-intelligence/design.md §14,
-- ADR-020). Additive; no existing table changes.

-- Deterministic extraction facts of one exact body at one final URL (raw page version).
-- Authoritative, written once per (bytes, extractor); also the commit marker of the
-- version-level writes (links, url_state) — see design §14.
CREATE TABLE IF NOT EXISTS ${keyspace}.page_extracts (
    page_version_id uuid PRIMARY KEY,
    url_id uuid,
    extractor text,
    normalization text,
    revision_id uuid,
    normalized_digest text,
    observed_at timestamp,
    doc text
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- Page revisions of a (final) URL: one row per normalized content state (ADR-020).
-- first_* earliest-wins, last_* latest-wins; written with page.changed in one logged batch.
CREATE TABLE IF NOT EXISTS ${keyspace}.page_revisions_by_url (
    url_id uuid,
    revision_id uuid,
    normalization text,
    normalized_digest text,
    first_seen timestamp,
    first_observation_id uuid,
    first_page_version_id uuid,
    last_seen timestamp,
    last_observation_id uuid,
    last_page_version_id uuid,
    PRIMARY KEY (url_id, revision_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};

-- Raw snapshot archival decisions: one row per (snapshot digest, observation). A blob is
-- to be kept if any row retains it. Garbage collection is deferred (P13/P14, ADR-005).
CREATE TABLE IF NOT EXISTS ${keyspace}.snapshot_retention (
    digest text,
    observation_id uuid,
    decision text,
    reason text,
    decided_at timestamp,
    PRIMARY KEY (digest, observation_id)
) WITH compaction = {'class': 'SizeTieredCompactionStrategy'};
