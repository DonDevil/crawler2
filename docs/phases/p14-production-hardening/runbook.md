# Runbook: diagnosing a stuck or failing pipeline

Each section is the target of an alert's `runbook` annotation. Commands assume the app environment
(`CRAWLER2_*` set, or `docker compose exec app …`). Status: written from the code and the CLIs that
exist; the procedures were **not executed against an injected stall** (that validation is open), except
where stated.

**Orientation, in order:** (1) are the *dependencies* up (`crawler2-check`); (2) is anything *waiting*
(Pipeline dashboard: frontier depth, lag, oldest pending age, outbox lag); (3) which *stage* stopped
(`crawler2-trace`); (4) is it a *single item* (`crawler2-media show`, `crawler2-evidence show`).

Pipeline stages and where each one shows up:

| Stage | Producer → consumer | Look at |
|---|---|---|
| frontier → fetch | `worker-*` | `crawler2_frontier_depth`, `fetch_attempts_total{outcome}` |
| fetch → page | worker → outbox → relay → `page.observed` | `crawler2_outbox_checkpoint_lag_seconds` |
| page → extract | `extract` group `extraction` | `stream_group_lag{event_type="page.observed"}` |
| extract → admission / media | `admit`, `resolve` | `urls.discovered`, `media.discovered` groups |
| media → encode | `resolve`/`probe`/`hash` → fingerprinter | `encode.requested` pending age; `crawler2-media resolve status` |
| encode → match | fingerprinter → `representation.ready`/`match.found` | held while P10 calibration is UNAVAILABLE |
| match → evidence | `evidence-collect` | `crawler2-evidence list/show` |

## Target down {#target-down}
`up == 0` for a scrape target. `systemctl status crawler2-role@<role>`; `journalctl -u … -n 100`. A unit in
`failed` exhausted its restart budget (5 in 10 min): read the log for the cause (usually a dependency:
`crawler2-check --wait 5`), fix, `systemctl reset-failed` then `start`. If only the *exporter* source is down
(`ExporterSourceDown`), the named backend (redis/frontier/outbox/gpu) is unreachable from the exporter host.

## Restarts {#restarts}
`journalctl -u crawler2-role@<role> --since -1h | grep -E "Main process exited|worker_state|metrics_bind_failed"`.
Browser pools relaunch by design after `browser_recycle_pages`/RSS limit (that is *not* a crash); an unexpected
death logs `browser_disconnected`, a failed relaunch `browser_launch_failed`. A pool in `unavailable`/`offline` has lost Redis or its fetcher (`WorkerNotReady`).

## Stuck consumer {#stuck-consumer}
`crawler2_stream_group_oldest_pending_age_seconds` high = an entry was delivered and never acknowledged
(handler crashing or blocked on a dependency). 1) Which group: the alert labels. 2) The consumer's log for
`dependency_unavailable`/`poll_failed`/`invalid_event_skipped`. 3) Entries are redelivered to another consumer
after `claim_idle_ms`; if the same entry keeps failing it is the payload:
`redis-cli XPENDING events:<type>.v1 <group> - + 5` then `XRANGE events:<type>.v1 <id> <id>`.
Poison events that fail validation are skipped and acknowledged (logged `invalid_event_skipped`).

## Event lag {#event-lag}
Lag rising with a *healthy* consumer = throughput shortfall (add a process/host); lag rising with the
consumer idle = it reads a different stream/group than the producer writes. `XINFO GROUPS events:<type>.v1`.
Streams with groups are never trimmed past an unacknowledged entry (ADR-022): a stalled group grows Redis.

## Outbox backlog {#outbox-backlog}
`crawler2_outbox_checkpoint_lag_seconds` high = the relay is not advancing. `systemctl status
crawler2-role@relay`; its log shows `outbox_relay_retry` (transport/storage error — retried in-process with
back-off) or `outbox_backpressure` (a consumer group is behind; the relay waits by design: find that group).
`outbox_late_rows_total` > 0 means rows became durable after their bucket settled (raise `settle_s`; ADR-013).
`crawler2-storage status` for the schema. Events are never lost while they are in the outbox.

## Dead letters {#dead-letters}
`redis-cli ZCARD crawler2:fr:dead`; `redis-cli ZRANGE crawler2:fr:dead 0 9 WITHSCORES` (task ids; namespace per
`CRAWLER2_REDIS__NAMESPACE`). A task is dead after exhausting its retry budget. Read why in `crawler2-discover
decisions <url>` and `fetch_attempts` (outcome per attempt). Dead letters are a signal about *targets* (blocked,
unreachable), not necessarily a defect; growth with `unsafe_destination` is the SSRF guard working.

## Stalled crawl {#stalled-crawl}
Depth > 0 and no attempts: (1) `crawler2_worker_state` — pools `degraded`/`unavailable`? (2) frontier gates:
`crawler2_frontier_gated_domains` and `eligible_domains` — all domains inside their politeness interval or at
their in-flight limit looks idle but is not stalled; a 429 back-off widens one domain's interval
(`fetch_rate_limited_total`, X-14). (3) Redis reachable? (`RedisDown`). (4) Network offline detection:
workers defer without spending attempts when a probe confirms the local network is down.

## Fetch failures {#fetch-failures}
`fetch_attempts_total{outcome}` by pool: `dns_error`/`network_error`/`timeout` across *all* domains = local
network or DNS; across one domain = that site. `proxy_unavailable` = Tor SOCKS proxy gone (no Tor service is
defined in this repository). `tls_error` = certificate trouble. `unsafe_destination` is intentional.

## Storage {#storage}
`fetch_record_errors_total`/`object_store_errors_total` > 0: attempts were deferred (nothing lost, nothing
charged). Check `crawler2-check`; Scylla timeouts under HDD compaction are a known dev-host condition (X-12).

## SSRF blocks {#ssrf-blocks}
Informational. `crawler2-trace <url id or the page url>` shows which page linked to the refused destination;
repeated blocks from one source are a hostile or compromised page and a candidate for source negative
knowledge. A block on a legitimate site means its DNS answers include an internal address (conservative
policy: one unsafe record rejects the host).

## Redis memory {#redis-memory}
`redis-cli INFO memory`; `redis-cli --bigkeys` is *not* safe on a loaded instance. Usual cause: a stalled
consumer group pinning stream entries (see Stuck consumer). Do not raise `maxmemory` before finding the
group. `noeviction` is required: any eviction means state loss (`RedisEvictions` is critical).

## Scylla health {#scylla-health}
`nodetool status`/`compactionstats` on the node (dev: `docker exec crawler2-scylla-1 …`; note native `nodetool`
needs free AIO budget — use the REST API on :10000 if it fails). Pending compactions on a 5,400-rpm USB HDD are
expected under load and are the reason latency gates fail there.

## Disk {#disk}
Which mount (alert labels). Scylla data and MinIO growth are capacity questions (OD-P14-5: retention not
decided); scratch (`/var/lib/crawler2/scratch`) is safe to delete (ADR-006); old soak/backup sets are
deleted only by an operator (`crawler2-backup prune --keep N --yes`).

## Host memory {#host-memory}
Browser trees dominate (RSS limit `workers.browser_engine.browser_rss_limit_mb` recycles them). Check
`systemctl show crawler2-role@<role> -p MemoryCurrent`. Run heavy one-off jobs as capped units
(`systemd-run --user -p MemoryMax=…`); on the dev laptop `oomd` closes terminals first.

## GPU {#gpu}
`nvidia-smi`; one GPU-owning encoder per host (ADR-006); 4 GB VRAM on the RTX 2050. Encoder OOM is handled by the
fingerprinter's adaptive batching; persistent > 95 % means a second GPU process is running.

## Following one item end to end
`crawler2-trace <id>` (any of: url id, observation id, media id, content id, match id, evidence id,
correlation id). Durable views: `crawler2-media show <media>`, `crawler2-evidence show <evidence>`,
`crawler2-discover decisions <url>`. See [observability.md](observability.md) §4 for which hops carry the
correlation id and which do not.
