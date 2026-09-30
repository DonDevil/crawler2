# Crawler V2 — Phase Plan

Status: **PLAN — no code yet.**
Inputs: `gpt-disscussed-plan.md`, `version2-comaprison.md`, V1 crawler
(`~/anti_piracy/crawler`, 37 commits, ~13k LOC non-test), V1 fingerprinter
(`~/anti_piracy/fingerprinter`).

### Decisions log

| Date | Decision | Consequence in this plan |
|---|---|---|
| 2026-09-28 | **ScyllaDB** is the durable store | ADR-002 closed; P1 still produces the access-pattern catalog, now used for table design, not DB selection. No joins/ad-hoc aggregation → derived tables + DuckDB (P12/P13). |
| 2026-09-28 | **Fingerprinter stays a separate service** (its own repo/process) | V2 defines the media-centric contract; encoder/matcher work (P9/P10) happens **in the fingerprinter repo**, planned here. Shared versioned contract package replaces V1's bridge workaround (D12). |
| 2026-09-28 | **One shared Scylla deployment, service-owned keyspaces** | `crawler2` keyspace (pages, page_versions, media, observations, source intelligence, evidence metadata) and `fingerprinter` keyspace (representations, targets, target representations, match state, fingerprint metadata) live in the **same cluster**. Each service reads/writes only its own keyspace; the other side's data is reached only through events/contracts. No second cluster, no cross-keyspace queries. |
| 2026-09-28 | Review correction pass (10 points) | B.5 invariants added; event ownership table (P1); contracts = stable semantics + additive schemas; P7 data sufficiency gate; P8 identity hierarchy; P10 ANN-mandatory + rebuildable index; P6 filter wording; 3-node Scylla tests moved off the 15 GB dev host. |
| 2026-09-28 | **Single machine and multi-machine supported from day one** | Multi-host safety is a P0 invariant, not a P14 add-on (see B.4). |
| 2026-09-28 | GPU: **NVIDIA RTX 2050, 4 GB VRAM** (driver 610.57) on a 12-core / 15 GB RAM host | Encoder must be VRAM-budgeted (P9); one GPU-owning encoder process per host; local dev stack must fit in ~15 GB alongside browsers. |
| 2026-09-29 | **P3 frontier**: execution queues with one shared domain gate, eligible-domain index instead of `domain_scan_limit` (benchmarked), active-task dedup, per-queue admission limits | ADR-015/016; D10 and D13 closed in P3; `selenium` queue has no P1 capability until P4 decides D14 |
| 2026-09-29 | **P4 fetch layer**: one runtime, httpx + Playwright pool; Scrapling (stealth) and Selenium dropped on measurement; `blocked`/`captcha` recorded, not escalated; gate workload = the 691 URLs of the P0 run | ADR-017/018; D1–D4, D14 closed in P4; `selenium` queue stays unused |
| 2026-09-29 | **P3 correction**: global per-domain in-flight limit in the frontier, frozen at 2 | ADR-019; found by P4's gate run |
| 2026-09-29 | **P4 success gate kept unchanged** (V1 `visited`); status OPEN at 95.51 % vs 96.82 %; V1 `visited` shown to include Selenium 400/403/404 false successes | success-metric methodology (keep historical gate vs documented status-aware redefinition with a V1+V2 rerun) pending — P4 doc §33a |
| 2026-09-29 | **P5 extraction**: page *revisions* (`PageRevisionId`, normalized content) added beside raw page versions, `page.changed` in contract 1.1; W691 HTML captured twice as the benchmark/precision corpus (not in git) | ADR-020; D8, D9 closed in P5; CPU gate passed (17.9 % of V1); change-detection precision checked: 1/23 on real pairs, root cause = in-content template widgets → P6 rules / P7 |
| 2026-09-29 | **P6 filter + discovery**: rules as durable versioned data with hot reload, filtering at admission (P5 facts unchanged), rooted-site scope with external leaves, static 6 h/24 h revisits, operator-supplied search queries; V1 blacklist (98 entries, not 1,463) imported through a reviewed manifest | ADR-021; D6, D7 closed in P6; Gates A–F, H passed; Gate G (24 h M1) open — run 1 reached 16.7 h; M1 to be restarted detached and continued as P7 history collection |
| open | Evidence/legal requirements (jurisdictions, screenshots/clips, retention) | Blocks P12 design only; ADR-005 stays open. |

This document turns the V2 design into phases. Every phase follows the
same four-step cycle V1 already used successfully (audit → design →
implement → validate, e.g. N1–N7, frontier steps 1–5), with an explicit
exit gate so no phase starts on an unverified foundation.

---

## Part A — V1 audit: what V2 inherits, fixes, and drops

These findings come from reading V1 code, not only its docs. Each is
referenced by the phase that resolves it.

### A.1 Proven — port into V2 (as implementations of V2 interfaces)

| V1 component | Why it's worth keeping | V2 home | Phase |
|---|---|---|---|
| Redis frontier Lua scripts (`core/redis_frontier.py`): atomic claim/complete/renew/reclaim, Redis `TIME`, per-domain gates | Benchmarked (~13.7k URLs/s ceiling, crash-recovery + heartbeat endurance validated) | `frontier/redis` | P3 |
| Claim/lease/heartbeat (`core/claim_heartbeat.py`) | Shared, generic, tested; reused unchanged by media evidence | `core/leases` | P3 |
| Failure semantics: `FrontierUnavailable`/`MediaEvidenceUnavailable` raised, never swallowed | Load-bearing correctness rule | all stores | P1 |
| `core/failure_classifier.py` + `core/network_health.py` (N1–N7) | Distinguishes local-network outage from per-site failure | `crawlers/runtime` | P4 |
| Deterministic identity `sha256(clean_url)` | Coordination-free convergence across machines | `core/ids` | P1 |
| URL normalization/cleaning, tracking-param stripping, media-URL classification (parts of `utils/url_utils.py`) | Real-world tuned | `extraction/urls` | P5 |
| HLS/DASH manifest parser | Works | `extraction/media` | P5 |
| Search-engine adapters (6) + blocked-engine cooldown | Genuine scrapers | `discovery/search` | P6 |
| Tor proxy resolution (`tor/proxy_config.py`) | The one live Tor path | `crawlers/tor` | P4 |
| Benchmark harness pattern (`tests/benchmarks/common.py`: ResourceMonitor, isolated Redis db, run-id'd synthetic domains) | Caught real bugs (blacklist contamination, mis-normalized CPU) | `tests/benchmarks` | P0 |
| Fingerprinter: SSRF-hardened acquirer + ffprobe validation, DINOv2 segment engine, temporal matcher | Tested end to end (269 tests) | `media/acquisition`, `media/encoder`, `media/matcher` | P9–P10 |

### A.2 V1 defects / limitations V2 must design out

| # | Finding (V1 location) | Consequence | Fixed in |
|---|---|---|---|
| D1 | Every engine's `fetch()` returns only `(html, error)` (`crawler/*.py`) — no status, headers, final URL, timings, bytes | No ETag/304, no page versions, no evidence provenance, no fetch-profile learning | P4 (`FetchResult` contract) |
| D2 | On a media content-type, `async_crawler.fetch` calls `await response.text()` — reads the **entire media body** into memory before recording it | A single video URL can pull GBs through a crawler worker | P4 (HEAD/range probe, never body) |
| D3 | Worker/scheduler loop is copy-pasted across 7 engines; no base class; `HybridCrawler` instantiates all 6 others | Behavior drift between engines; can't scale capabilities independently | P4 (one worker runtime, fetchers are plugins) |
| D4 | Nested retries: fetch-internal loop (`max_retries`) × frontier retries (`max_retries`) × hybrid escalation chain | Up to 9+ attempts per URL, hidden from frontier accounting | P4 (frontier is sole retry authority) |
| D5 | `PiracyDomainClassifier` loads `datasets/known_pirate_domains.txt`, which does not exist → always `False`; `CrawlerRouter.prefers_browser()` has no callers | "Intelligence" in routing is effectively static | P7 |
| D6 | Blacklist is a flat file **mutated at runtime by crawler processes** (`URLUtils.add_to_blacklist`), auto-condemns whole registered domains, provenance only in a log line | Not multi-host safe, no confidence/explainability, false positives destroy coverage | P6 (filter engine) |
| D7 | "Suspicious redirect" → fetch rejected (`is_suspicious_redirect`) | Redirect ≠ ad; legit mirrors lost | P6 |
| D8 | HTML parsed with BeautifulSoup **twice** per page (link extractor + media detector each build a soup); `selectolax` in requirements but unused | CPU waste on the hot path | P5 |
| D9 | `URLUtils`: ~800-line classmethod object with global mutable state (blacklist path/mtime cache) | Hard to test, caused the 77× latency bug | P5 (split into pure functions + injected policy) |
| D10 | `visited` is terminal; frontier dedup is "known forever" | The S1→S2 `/movies/` problem; no recrawl | P3 + P7 |
| D11 | Run is scoped to one `(target_id, target_version)` (`core/target_scope.py`); fingerprinter embeds the candidate **per job** and doesn't persist candidate embeddings (`worker/matching_handler.py`) | Same media re-embedded per target | P9 |
| D12 | Cross-repo contract mismatch: bridge priority banding pinned to `-1/1000000` because the fingerprinter only consumes the `default` stream (see `config.yaml` comment, 2026-09-02 wedge) | Integration by config workaround | P1 (single contract package) |
| D13 | `domain_scan_limit` hard visibility cutoff (250) | Domains beyond top-K invisible | P3 (eligible-domain index, already designed in V1 history) |
| D14 | Selenium spawns a new Chrome per fetch; Playwright a new context per page; no recycling metrics | Browser cost | P4 |
| D15 | ~15 dead modules (`system-architecture.md §25`) | Noise | Not ported |
| D16 | No stored page content/HTTP metadata anywhere | No evidence reconstruction | P2 + P5 + P12 |

### A.3 Explicitly not ported

Dead modules (§25 list), `HybridCrawler` as a class, SQLite frontier/evidence
backends (V2 dev mode = docker-compose with real Redis/Scylla/MinIO — one
code path), the V1 bridge process and its priority-banding workaround
(replaced by a thin V2 fingerprinter adapter speaking the shared,
versioned contract — see P1/P9),
Scrapling (keep only if P4's audit shows it beats Playwright on a measured
site set).

---

## Part B — How every phase is run

### B.1 Phase cycle

Each phase produces these artifacts in `docs/phases/pNN-<name>/`:

1. **Audit** (`audit.md`) — read the relevant V1/fingerprinter code, list
   what's reused/rewritten, list risks and unknowns, measure the V1
   baseline where one exists. No code.
2. **Design** (`design.md`) — interfaces, data model changes, Redis keys,
   Scylla tables with their access patterns, event contracts, failure
   semantics, config, test plan. ADRs for irreversible choices.
   **Design review gate:** you approve before implementation starts.
3. **Implement** — small commits; contract tests written before
   implementations; no feature beyond the design.
4. **Validate** (`validation.md`) — test results, benchmark numbers vs.
   targets, known limitations, deviations from design with reasons.
5. **Integrate** — wired into the running system behind config; the
   end-to-end smoke suite still passes.

### B.2 Test layers (applied per phase as relevant)

| Layer | Tooling | Runs |
|---|---|---|
| Unit | pytest | every commit |
| Contract | shared suite each implementation of an interface must pass (e.g. every `Fetcher`, every repository) | every commit |
| Property-based | hypothesis (URL normalization, hashing, state machines) | every commit |
| Integration | pytest + docker-compose (Redis, Scylla, MinIO) | every commit (CI) |
| Local fixture web | a local test site server (static, JS-rendered, redirects, 304s, ad iframes, HLS) — **no live internet in CI** | every commit |
| Benchmark | ported V1 harness, results committed as JSON | per phase, manually |
| Chaos / crash | kill workers, drop Redis/Scylla, network partition | P3, P4, P14 |
| Soak | multi-hour real crawl on a fixed seed set | milestones |

### B.3 Definition of done (every phase)

- Design doc approved; validation doc written.
- Contract + integration tests green in CI; coverage of new code ≥ 85%.
- No silent degradation: every infra failure raises a typed error.
- Metrics emitted for the new component (Prometheus-style counters/histograms).
- Config documented; defaults justified.
- Benchmark gate (if any) met and recorded.
- Multi-host invariants (B.4) hold, tested with the two-host topology.
- Architectural invariants (B.5) re-checked at design review and validation.

### B.4 Multi-host invariants (from day one)

Every component must satisfy these on a single machine *and* on N machines,
with no code-path difference — only configuration:

1. **No authoritative local state.** Anything that must survive or be
   shared lives in Redis (coordination), ScyllaDB (memory) or the object
   store (blobs). Local disk is cache/scratch only, safe to delete.
2. **All shared config/rules are central** (filter rules, fetch profiles,
   seeds): stored in Scylla, hot-reloaded; no process writes shared files (V1 D6).
3. **Time from one source**: Redis `TIME` for leases/schedules (as V1); wall clock only for logs.
4. **Worker identity** = `{host_id}:{role}:{pid}:{uuid}`; every claim,
   event and evidence record carries it.
5. **Idempotent consumers**: every event handler tolerates redelivery and
   concurrent delivery on another host.
6. **Per-host resource roles** via config: a host runs any subset of
   {http pool, browser pool, tor pool, intelligence, media probe,
   finalizer}; the GPU encoder runs only where a GPU exists.
7. **Scylla keyspaces parameterized** by replication: `RF=1` single host,
   `NetworkTopologyStrategy RF=3` multi-host; consistency levels chosen
   per access pattern (e.g. `LOCAL_QUORUM` writes for evidence), and
   identical code on both.
8. **Test topology**: CI and local runs include a *two-simulated-host*
   compose profile (two app containers with separate filesystems and
   host_ids sharing one Redis/Scylla/MinIO) so single-host shortcuts fail
   tests immediately. This profile uses **one** Scylla node; multi-node
   Scylla (RF=3) is tested only in a separate cluster profile run on
   additional machines or a dedicated resource-sized environment — never
   on the 15 GB dev host alongside the full stack.

### B.5 Architectural invariants

Checked at every design review; a design that breaks one needs an ADR.

1. **The crawler runtime never depends on intelligence.** No
   intelligence component (crawl/source/temporal intelligence, filter
   learning, feedback, matching) is a mandatory dependency of the crawler
   execution runtime. With every intelligence service stopped, workers
   still claim `CrawlTask`s, fetch, extract, persist and emit events
   using defaults (default fetch profile, default priority). Intelligence
   only *produces* tasks/profiles; it is never called synchronously on
   the fetch path.
2. **Service-owned data.** One Scylla cluster; each service owns its
   keyspace; cross-service access only via events/contracts.
3. **Derived state is rebuildable.** The vector index, search index,
   Parquet datasets and graph are derived from authoritative Scylla +
   object-store data. Losing any of them means *rebuild*, never data loss;
   every derived store has a documented and tested rebuild procedure.
4. **No all-to-all matching.** Media↔target comparison always goes
   through ANN candidate reduction before temporal verification (P10).
5. **Contracts: stable semantics, extensible schemas.** A field's meaning
   never changes; schemas evolve additively; consumers ignore unknown
   fields; breaking changes require a new event type or major version
   with a migration window.
6. **Evidence hooks exist before the evidence system.** P4/P5 retain
   everything P12 needs (FetchResult metadata, redirect chains, raw
   snapshots, hashes, worker identity), so P12 assembles rather than
   re-collects.

---

## Part C — Phases

Sizing is relative (S/M/L/XL), not calendar time.

```
P0 Foundations ─► P1 Contracts/Model ─► P2 Storage ─► P3 Frontier ─► P4 Fetch/Workers ─► P5 Extraction/Page Intel ─► P6 Filter + Discovery
                                                                                                        │
                                                            M1: dumb-but-persistent crawl loop ◄───────┘
P7 Crawl Intelligence (M2) ─► P8 Media Registry ─► P9 Targets + Encoder ─► P10 Vector Index + Matcher (M3)
─► P11 Feedback Loop (M4) ─► P12 Evidence (M5) ─► P13 Analytics ─► P14 Production Hardening (M6)
```

---

### P0 — Foundations & baseline (M)

**Goal:** a repo where the rest can be built safely, plus V1 numbers to beat.

- **Audit:** inventory V1 + fingerprinter dependencies; pin Python (3.12);
  record V1 baselines on a fixed seed set: pages/s, fetch success rate by
  engine, bytes/page, browser share, CPU/RSS, media found per 1k pages.
  These become the regression bar.
- **Design / ADRs:**
  - ADR-001 repository layout (the logical boundaries from the plan §14).
  - ADR-002 durable store: **ScyllaDB (decided).** ADR records the
    consequences: query-first table design, no joins, LWT (Paxos) only
    where required (evidence write-once, lifecycle transitions), counters
    in separate tables, aggregates via derived tables/DuckDB. Driver:
    `scylla-driver` (shard-aware fork of the DataStax Python driver).
  - ADR-003 object store (MinIO locally and on self-hosted multi-host; any S3-compatible later).
  - ADR-004 event transport: **Redis Streams** (the fingerprinter already
    speaks Streams) with an outbox in Scylla so "write entity + emit
    event" survives a crash between the two.
  - ADR-005 legal/compliance posture for evidence capture & crawling (Tor, retention, jurisdictions) — **open**.
  - ADR-006 multi-host topology: host roles, host_id, Scylla RF per
    deployment, Redis single instance (+ replica/Sentinel option later),
    MinIO single vs distributed mode.
  - ADR-007 contract package: one versioned package (`antipiracy-contracts`)
    containing JSON schemas + generated Python models, vendored/pinned by
    both crawler2 and fingerprinter; compatibility tests in both repos (fixes D12).
- **Develop:** package skeleton, `pyproject.toml`, ruff/mypy/pytest,
  CI pipeline, structured logging, metrics scaffold, config system
  (pydantic settings, one schema, per-host role overrides), port benchmark harness.
  - **Install Docker** (not present on this machine today) + compose.
  - Compose profiles: `single` (Redis, 1-node Scylla, MinIO, app) and
    `two-host` (B.4 #8).
  - Memory budget for this 15 GB host: Scylla dev node `--smp 2
    --memory 2G --overprovisioned 1`, Redis ≤ 1 GB, MinIO ≤ 512 MB,
    leaving room for browser pool and the fingerprinter GPU worker.
- **Test:** CI green on empty skeleton; compose stack health checks for both profiles.
- **Exit gate:** baselines recorded in `docs/phases/p00/baseline.md`;
  ADRs 001–004, 006, 007 accepted (005 may stay open until P12).

### P1 — Core contracts, IDs & data model (L)

**Goal:** the stable contracts the plan §11 asks for, so V3 can swap implementations.

- **Audit:** V1 `FrontierClaim`, `MediaEvidenceStore` protocol,
  fingerprinter `Job`/`Result`/`FingerprintCandidate`; list every field
  and who produces/consumes it. Document D12.
- **Design:**
  - Entities: Domain, Page, PageVersion, Link, Media, MediaVersion,
    Representation (embedding), Target, TargetRepresentation, Observation,
    MatchResult, EvidenceCandidate, EvidenceRecord, Campaign, FetchProfile,
    SourceProfile.
  - IDs: deterministic where convergence matters (`page_id = sha256(canonical_url)`,
    `domain_id`), content-addressed for versions (`page_version = page_id + normalized_hash`),
    ULIDs for events/evidence.
  - Work contracts: `CrawlTask {url, page_id, fetch_profile, priority, deadline, crawl_context}`,
    `FetchResult` (status, headers, final_url, redirect chain, timings,
    bytes, body ref, engine, validators), `EncodeTask {media_id, media_version,
    source_ref, required_representation_version}`, `MatchTask`.
  - Events (versioned schemas): `page.fetched`, `page.changed`,
    `media.discovered`, `representation.created`, `match.found`,
    `evidence.candidate`, `evidence.finalized`, etc.
  - **Access-pattern catalog**: every read/write each component will do,
    with frequency and consistency need — the direct input to Scylla
    table design in P2 (one table per query shape).
  - **Crawler ↔ fingerprinter boundary** (ADR-007 contents): crawler2
    owns pages/media/observations/evidence; fingerprinter owns
    representations, vector index, target representations and match
    computation. Messages over Redis Streams:
    `encode.requested {media_id, media_version, content_identity, source_ref, required_representation_version, priority}` →
    `representation.ready | encode.failed`; `target.registered` →
    fingerprinter backfill match; `match.found {media_id, media_version, target_id, target_version, confidence, technique, representation_ref}`.
    Priority is an explicit field with defined consumer semantics (no
    stream-per-priority banding — D12). Both keyspaces live in the one
    shared Scylla cluster; neither side reads the other's keyspace (B.5 #2).
  - **Event ownership** — every event has exactly one producer; consumers
    are subscribers, never co-owners of the schema:

    | Event | Producer | Consumers |
    |---|---|---|
    | `page.fetched` | crawler worker runtime | page intelligence |
    | `page.changed` | page intelligence | crawl intelligence, evidence collector |
    | `fetch.outcome` (escalation/`needs_js`/`blocked`) | crawler worker runtime | crawl intelligence (fetch profiles) |
    | `media.discovered` | extraction | media registry |
    | `encode.requested` | media registry | fingerprinter |
    | `representation.ready` / `encode.failed` | fingerprinter | media registry |
    | `target.registered` | fingerprinter (target manager) | crawl intelligence (relevance/reactivation) |
    | `match.found` | fingerprinter (matcher) | feedback loop, evidence collector |
    | `evidence.candidate` | evidence collector | evidence finalizer |
    | `evidence.finalized` | evidence finalizer | reporting, analytics |

    The table is part of the contract package and is enforced by a test
    (a producer publishing an event type it doesn't own fails CI).
  - Schema evolution (B.5 #5): every event carries `event_type`,
    `schema_version`, `event_id`, `produced_at`, `producer` and an
    extensible `metadata` object; changes are additive only; consumers must
    ignore unknown fields.
- **Develop:** `antipiracy-contracts` package (ADR-007) + crawler2's
  `core/models`, `core/ids` as pure code (pydantic + JSON schema export).
- **Test:** property tests for ID determinism/normalization; schema
  round-trip tests; backward-compat fixture for each event v1; the same
  fixtures run in the fingerprinter repo's CI.
- **Test (addition):** forward-compat test — a v1 consumer handles a
  v1 event with extra unknown fields unchanged.
- **Exit gate:** access-pattern catalog complete; boundary and event
  ownership agreed; **v1 contract semantics stable** (schemas remain
  additively extensible per B.5 #5).

### P2 — Storage layer (L)

**Goal:** authoritative memory (durable store), raw archive (object store), event log.

- **Audit:** V1 has none of this (D16); Scylla data modeling against P1
  access patterns; partition sizing (pages per domain, versions per page
  — large domains need bucketed partitions, e.g. `(domain_id, bucket)`);
  tombstone risk from TTL/deletes (prefer TTL on whole partitions, avoid
  per-row deletes in hot tables); compaction strategy per table (TWCS for
  time-series observations, STCS/ICS otherwise).
- **Design:** tables per access pattern (e.g. `pages_by_domain`,
  `page_versions_by_page` clustered by `fetched_at DESC`, `media_by_content_hash`,
  `representations_by_media_and_model`, `observations_by_target_and_day`);
  TTL/retention policy; object-store key layout
  (`raw/{domain}/{page_id}/{version}.warc.gz` — consider WARC for evidence
  friendliness); outbox/event pattern so "write entity + emit event" is
  not lost on crash; idempotent upserts.
- **Develop:** repository interfaces + implementations, migrations tool,
  object-store client with content hashing on write, event publisher/consumer base.
- **Test:** repository contract suite; idempotency tests (replay same
  event twice → same state); crash-between-write-and-emit test;
  large-partition benchmark; concurrent writers from both hosts of the
  `two-host` profile (last-write-wins behavior is intended everywhere it
  occurs, LWT where it isn't).
- **Exit gate:** write/read latency p99 targets met at 10× expected
  per-host load on the 2 GB dev node; no partition > 100 MB under a
  1M-page synthetic load; keyspace creation and repositories work at RF=1
  on the dev host. The RF=3 multi-node check runs in the separate cluster
  profile (B.4 #8) on additional machines or a dedicated test environment.
  If none is available yet, it becomes a P14 entry criterion and is not
  simulated on the 15 GB host.

### P3 — Frontier & scheduling (L)

**Goal:** port V1's proven Redis frontier, reshaped for capability queues and recrawl.

- **Audit:** V1 `redis_frontier.py` Lua scripts, `frontier-adr.md`,
  `domain-scan-window-design.md`, starvation audit, throughput-ceiling
  audit. Identify what assumes "visited = forever" (D10) and single queue.
- **Design:**
  - Per-capability ready queues (`http`, `browser`, `tor`, `selenium`),
    each with per-domain politeness gates **shared across capabilities**
    (a domain's rate limit is global, not per queue).
  - Dedup becomes "currently scheduled/in-flight", not "ever seen";
    long-term "seen" lives in the durable store.
  - Scheduled (future-dated) tasks ZSET fed by P7's recrawl scheduler.
  - Eligible-domain index to remove `domain_scan_limit` (D13) — design
    exists in V1 history; decide via benchmark.
  - Frontier is the single retry authority (D4).
  - Backpressure: max queue depth per capability; admission control.
- **Develop:** Lua scripts ported with tests first; lease/heartbeat/recovery ported.
- **Test:** ported V1 benchmarks (throughput, distributed N-process,
  crash recovery, heartbeat endurance, starvation, priority/rate-limit);
  property-based state-machine test of the claim lifecycle; Redis-down
  semantics tests.
- **Exit gate:** ≥ V1 ceiling (~13k claims/s at 8 workers, no RL);
  zero lost/duplicated claims in 1M-claim distributed run with random
  worker kills; starvation benchmark passes.

### P4 — Fetch layer & worker pools (XL)

**Goal:** one worker runtime, pluggable fetchers, cheap-first, rich `FetchResult`.

- **Audit:** all 7 V1 engines, `fetch-extractor-audit.md` (timeouts,
  Selenium lifecycle, Playwright contexts), `network-failure-handling-*`.
  Measure per-engine success/cost on the P0 seed set to decide whether
  Scrapling and Selenium earn a place (D14).
- **Design:**
  - `Fetcher` interface → `FetchResult` (D1); runtime owns claim,
    heartbeat, timeouts, health, completion (D3).
  - Conditional requests (ETag/If-Modified-Since) → 304 path.
  - Media URLs: HEAD / `Range: bytes=0-N` probe only; never download the
    body in the crawler (D2). Manifests are small and parsed.
  - Browser pool: context reuse, page recycling after N pages or RSS
    threshold, crash detection, render metrics; request interception wired
    to the P6 filter engine (hook defined now, engine plugged later).
  - Escalation is **not** decided inside the fetcher: a fetcher returns
    an outcome (`ok`, `needs_js`, `blocked`, `captcha`, ...) and the task
    is re-queued to another capability with the reason recorded. This is
    what P7 learns from.
  - Per-host capacity config per worker pool; independent processes per capability.
- **Develop:** runtime, HTTP (aiohttp/httpx — pick one), Playwright pool,
  Tor, Selenium fallback, network-health integration.
- **Test:** `Fetcher` contract suite against the local fixture web
  (redirect chains, 304, gzip/brotli, JS-only page, captcha page, huge
  media file → asserts < N KB read, slowloris → timeout); **B.5 #1
  test:** with no intelligence services running, workers crawl the
  fixture web end to end using default profiles/priority; browser pool
  leak test (1k pages, RSS bounded); chaos: kill browser mid-page.
- **Exit gate:** on the P0 seed set: success rate ≥ V1, bytes per page ≤
  V1, browser share < V1, zero body downloads of media.

### P5 — Extraction & page intelligence (L)

**Goal:** single-parse extraction, page versions and change detection.

- **Audit:** V1 parsers + `url_utils` (D8, D9); list heuristics worth keeping.
- **Design:**
  - One parse per page (selectolax), extractors run over the same tree:
    links, media, metadata (title, dates, og tags), JS URLs.
  - Hash set per version: raw, normalized (boilerplate/ads stripped),
    visible text, link-set, media-set, structural (DOM shape) — defines
    "meaningful change".
  - PageVersion created only when normalized hash changes; otherwise
    update `last_seen`.
  - Raw snapshot archival policy (always for evidence candidates; sampled
    otherwise; always when normalized hash changes on a high-value page).
  - `url_utils` split into pure functions; blacklist/filter policy injected.
- **Develop:** extraction pipeline, hashers, page intelligence writer
  (emits `page.fetched` / `page.changed`).
- **Test:** golden-file tests on saved real pages (from V1 crawls);
  hash stability tests (same page, different ad rotation → same
  normalized hash); benchmark parse time vs V1.
- **Exit gate:** parse CPU/page ≤ 50% of V1; change-detection precision
  checked on a hand-labeled set of page pairs.
- **Status (2026-09-29):** implemented; CPU gate **passed** (17.9 % of V1);
  precision **checked** (1/23 on 53 real pairs, one site's in-content
  widget) — see `docs/phases/p05-extraction-page-intelligence/validation.md`.
  "page version" in this section is realised as the page *revision*
  (ADR-020); the planned `page.fetched` is `page.observed` (P1).

### P6 — Filter engine + discovery port (M)

**Goal:** explainable ad/tracker classification; seeds + search discovery.

- **Audit:** V1 `AUTO_BLACKLIST_*`, `AD_HOST_HINT_PATTERNS`,
  `is_suspicious_redirect`, `domain_blacklist.txt` (1,463 lines) (D6, D7);
  EasyList/uBlock list formats and licensing.
- **Design:** rule model (hostname / path / resource-type / third-party /
  redirect / page rules) with `classification ∈ {CONTENT, MEDIA, PLAYER,
  NAVIGATION, AD, TRACKER, UNKNOWN}`, confidence, rule id, rule source;
  decisions ALLOW / CLASSIFY / BLOCK with thresholds; domain/path
  overrides; rules stored centrally (durable store), hot-loaded, never
  written by crawler processes; every decision logged as data.
- **Develop:** filter engine (used by P4 browser interception and P5
  link admission), EasyList importer, V1 blacklist importer (tagged
  `source=v1_blacklist`), search adapters + seed loader producing
  `CrawlTask`s.
- **Test:** labeled URL/request corpus → precision/recall per class;
  false-positive guard: known target/content sources must not be
  accidentally blocked by *generic* ad/tracker rules. Explicit policy
  (domain/path overrides, operator rules) may still override
  classification, and every such override is recorded with its provenance;
  performance (≥ 100k decisions/s in-process).
- **Exit gate — Milestone M1:** seeds/search → frontier → fetch → extract
  → persist versions → new links → frontier, running 24h on the seed set
  with no leaks, with V1-or-better metrics. **No intelligence yet.**
  After the gate passes, M1 **keeps running** as the history-collection
  run that feeds P7 (see P7's data-sufficiency gate). P7 design work can
  proceed in parallel with that run.
- **Status (2026-09-29):** implemented. Filter gate **passed** (128k
  decisions/s); false-positive guard **passed** (after restoring ABP
  document semantics); M1 closed loop **demonstrated** live (Gate F) and
  ran 20.5 h (2026-09-29 17:26 → 09-30 13:59 UTC) until a terminal crash
  stopped it; Gate G (24 h) **open** at 16.7 h of its window; throughput
  on the dev host is bound by Scylla on the USB HDD — see
  `docs/phases/p06-filter-discovery/validation.md`. "Seeds + search" is
  realised as admissions through the P3 API (there is no separate
  CrawlTask type); the V1 blacklist had 98 entries, not 1,463.

### P7 — Crawl intelligence (XL) → Milestone M2

**Goal:** WHAT / WHEN / HOW / PRIORITY decided outside the crawler.

- **Audit:** M1 history data; what signals actually exist per page/domain.
- **Data-sufficiency gate (before P7 implementation):** the continuing
  M1 run must produce a representative dataset. Target duration is several
  days to about a week, but the gate is defined by **volume and coverage,
  not calendar days**. Minimum thresholds are set at P7 design review, e.g.:
  ≥ N distinct domains with ≥ K fetches each; ≥ M pages observed ≥ 3
  times (so change rates can be estimated); observed `page.changed`
  events across at least several page types; fetch outcomes for every
  capability. If the thresholds aren't met, the run continues. The gate is
  not waived.
- **Design** (start rule/statistics-based, ML later is V3):
  - **HOW — fetch profiles** per domain/path pattern: cheapest capability
    that succeeded recently; escalation outcomes from P4 update it; decay so it re-tests cheaper paths.
  - **WHEN — recrawl scheduling:** per-page change-rate estimate (e.g.
    Poisson estimator from observed changes), bounded min/max intervals,
    boosted by target relevance/release timing.
  - **WHAT — URL pattern learning** (`/movie/{year}/{slug}`) by path
    template clustering; page-type classification (listing, detail,
    player, category).
  - **Temporal relevance** (URL year, publish/modified dates, release dates of active targets).
  - **Source profile + lifecycle** (unknown → discovered → validated →
    active → high-value → stale/dead), **negative knowledge**,
    **productivity decay**, **reactivation** on new target/event.
  - Priority composition is a single documented function; the crawler only sees `priority`.
- **Develop:** intelligence services as event consumers producing
  `CrawlTask`s and profile updates; scheduler that feeds P3's future-dated ZSET.
- **Test:** replay tests on recorded history (deterministic); offline
  evaluation: new-content discovery latency, fetch cost per new page,
  % of crawl budget on dead/stale sources — each vs M1 policy.
- **Exit gate — M2:** on the same seed set and budget, ≥ X% more new
  pages/media discovered and ≥ Y% fewer browser fetches than M1
  (X/Y set in the design review from M1 data). S1→S2 scenario test:
  a page added under an already-crawled `/movies/` is found within its
  scheduled recrawl window.

### P8 — Media registry & identity (M)

**Goal:** separate URL identity from content identity (D11).

- **Audit:** V1 media evidence store (asset identity, observations,
  variants); what cheap content identity signals exist before download
  (content-length, ETag, manifest variant URIs, first-segment hash).
- **Design — identity hierarchy** (each level is stronger and more
  expensive; a cheap level never asserts what only a stronger one can):

  | Level | Basis | Asserts |
  |---|---|---|
  | 1. URL identity (`media_id`) | canonical media URL | "same address" |
  | 2. Technical identity | content-length, ETag, container/codec probe, manifest variant set | "probably same file" (hint only) |
  | 3. Candidate content identity | hash of sampled bytes / first N segments | "same bytes" — **candidate only** |
  | 4. Fingerprint representation | fingerprinter embedding (P9) | perceptual similarity |
  | 5. Verified media equivalence | fingerprinter temporal verification of representations | "same underlying media" |

  - A sampled-byte hash match is used **only to skip redundant work**
    (e.g. not re-encoding byte-identical files). It never establishes
    media equivalence and never merges records irreversibly.
  - Different encodings/packagings of the same movie (CDN A vs CDN B vs
    mirror, HLS vs MP4) will have *different* level-3 hashes. They are
    linked as equivalent only at level 5, stored as an equivalence
    relation with confidence and provenance. The relation stays reversible.
  - A level-3 hash collision between different content must be
    detectable: level-4 disagreement splits them.
  - Media versions when content at a URL changes; observations (page →
    media, when, by which crawl); `encode.requested` only when the
    required representation is missing for that level-3 identity.
- **Develop:** registry service consuming `media.discovered`; lightweight
  probe step (HEAD / range / first-segment hash, with the fingerprinter's
  SSRF guard logic ported) run by a media-probe pool, not the crawler.
  Full media download stays in the fingerprinter service. Registry emits
  `encode.requested` only for content identities lacking the required
  representation version (it learns this from `representation.ready` events).
- **Test:**
  - Byte-identical video on 3 URLs → one level-3 identity, one encode.
  - **Same underlying media, different encodings/packaging** (re-encoded
    bitrate, different container, HLS vs MP4, different segmenting) →
    distinct level-3 identities, **not** merged by P8, and later linked
    as equivalent by the fingerprinter (cross-checked in P10).
  - Crafted level-3 collision (same sampled bytes, different content) →
    not treated as the same media.
  - Same URL with changed content → new version.
  - SSRF test suite ported.
- **Exit gate:** zero duplicate encode tasks for byte-identical content
  in a synthetic dedup benchmark; zero false merges in the
  different-content test set.

### P9 — Fingerprinter service: media-centric encoder + target manager (L)

**Work happens in the `fingerprinter` repo**, planned and gated here.
crawler2 side: the fingerprinter adapter (publishes `encode.requested`,
consumes `representation.ready` / `match.found`).

**Goal:** fingerprinting as reusable infrastructure (fixes D11).

- **Audit:** fingerprinter `target/` (registry, caches, lock,
  multi-host audit 13d — "current architecture is not multi-host safe"),
  `embedding/dinov2_engine.py`, segment sampling, `worker/matching_handler.py`
  (candidate re-embedded per job, not persisted), GPU path (never
  validated on real CUDA). Measure CPU baseline: seconds per minute of video.
- **Design:**
  - Job model changes from `(candidate, target)` to `encode(media)`;
    matching becomes a separate step (P10).
  - Representation registry keyed by
    `(content_identity, media_version, model_name, model_version, sampling_config_hash)`,
    in the **fingerprinter-owned keyspace in the shared Scylla deployment**
    (authoritative: representation metadata + vectors or vector refs); vectors in the object
    store (MinIO) + index (P10). Target representations the same way.
  - Model upgrade = backfill job; old representations kept until retired.
  - Multi-host: 13d fix applied — target media and artifacts in MinIO,
    build-on-miss under a Redis lock, no host-local authoritative cache.
  - **GPU plan for the RTX 2050 (4 GB VRAM):**
    - `facebook/dinov2-base` in **fp16** (~170 MB weights); ViT-B/14 at
      224 px leaves room for batches of roughly 32–64 frames. The design
      doc fixes the value by measurement, not this estimate.
    - Adaptive batch size: start from the measured max, halve on
      `torch.cuda.OutOfMemoryError`, record the working size per host.
    - **One GPU-owning encoder process per host** (a Redis-backed
      per-host GPU lease). Several processes on 4 GB would OOM each other.
    - Frame decode/extraction (ffmpeg) on CPU worker threads feeding
      the GPU through a bounded queue, so the GPU never waits on I/O.
      NVDEC can be evaluated later.
    - CPU fallback path kept (hosts without GPUs; multi-host roles B.4 #6).
    - fp16 vs fp32 equivalence: cosine similarity ≥ 0.999 per segment
      on a fixed clip set, and match decisions identical on the labeled set.
- **Develop (fingerprinter repo):** encode worker, representation
  registry, target manager (register, build, version), backfill tool,
  GPU lease + batching. **(crawler2):** adapter + contract tests.
- **Test:** S1/S2 reuse scenario from the plan §4 as a cross-service
  integration test (C, D, E must not be re-encoded); fp16/fp32
  determinism test; OOM-recovery test (forced large batch); GPU
  throughput + VRAM benchmark; two-host test (one GPU host, one CPU
  host) producing identical representations.
- **Exit gate:** reuse scenario passes; GPU encoding ≥ 5× CPU throughput
  on this machine (target to be confirmed at design review);
  no OOM crash in a 1,000-video soak.

### P10 — Fingerprinter service: vector index & matcher (L) → Milestone M3

**Work happens in the `fingerprinter` repo.**

**Goal:** representation and comparison decoupled.

- **Audit:** fingerprinter `matching/` (temporal matcher, thresholds,
  aggregation); expected vector volume (segments × media) at 1 and 3
  years; RAM available for an index on a 15 GB host shared with the crawler.
- **Design:**
  - **Index choice (next free ADR number; ADR-008…011 were taken by P1) is decided by measurement, not up front.**
    Sequence: estimate vector volume (segments × media at 1 and 3 years)
    → dimensionality (768 for DINOv2-base; evaluate PCA/quantization) →
    RAM/disk estimate → benchmark ANN recall@k + latency on real vectors
    → choose. Candidates include in-process FAISS/HNSW, a network service
    (e.g. Qdrant), and Scylla vector search if the deployed version
    supports it. Multi-host accessibility is a requirement, not a tiebreaker.
  - **The vector index is derived, rebuildable state** (B.5 #3).
    Authoritative representations live in the fingerprinter keyspace in
    Scylla (+ object store). If the index is lost it is rebuilt from
    Scylla; no fingerprint knowledge exists only in the index. The rebuild
    tool and its duration at projected volume are part of the design.
  - **ANN candidate reduction is mandatory (B.5 #4)**, never all-to-all:
    - New media: embed → ANN search over target representations →
      candidate targets → temporal verification → match.
    - New target: embed → ANN search over historical media
      representations → candidate media → temporal verification → match
      ("S3 benefits from S1+S2").
    - Candidate count per query is bounded (top-k + similarity floor),
      and the verification workload is capped and observable.
  - Results carry confidence + technique provenance + the ANN/verification
    parameters used.
  - Level-5 media equivalence (P8) is produced here from
    representation↔representation verification.
- **Develop:** index builder/updater, matcher service, `match.found` events.
- **Test:** labeled match set (true clips, partial clips, re-encodes,
  crops, negatives) → precision/recall vs V1 fingerprinter; ANN recall@k
  vs brute force (brute force only as the offline test oracle); index
  drop-and-rebuild test (identical match results after rebuild);
  verification-workload test (per-query candidate count stays within the
  bound as corpus size grows 10×); P8's different-encoding set is linked
  as equivalent.
- **Exit gate — M3:** precision/recall ≥ V1 fingerprinter on the labeled
  set; new target matched against historical media without re-encoding.

### P11 — Feedback loop (M) → Milestone M4

**Goal:** results improve future crawling (the closed loop).

- **Design:** match results → source profile (value), page type/URL
  pattern (productive patterns), fetch profile (which capability reached
  real media), target relevance; guard against runaway feedback
  (bounded boosts, decay).
- **Test:** replay test showing priority shifts toward sources that
  produced matches; stability test (no oscillation).
- **Exit gate — M4:** end-to-end: register target → crawl → media →
  encode → match → source priority rises → next crawl cycle reaches more matching media.

### P12 — Evidence collector & finalizer (L) → Milestone M5

**Goal:** defensible, immutable case packages.

- **Audit:** ADR-005 (legal); what counts as admissible/useful evidence
  for your clients/jurisdictions; chain-of-custody requirements.
- **Design:**
  - Collector: on `match.found`, capture (or reference) raw response
    (WARC), screenshot/clip, timestamps, URL + redirect chain, crawl
    method, page/media identity, hashes → `EvidenceCandidate`.
  - Finalizer: validates completeness, freezes into `EvidenceRecord`
    (write-once; object-store object lock/versioning; content hashes;
    optional trusted timestamp (RFC 3161) and signing).
  - Campaign coverage + source statistics computed from observations
    (plan §7 lists) — computed via derived tables/DuckDB, snapshotted into the package.
  - Exporters: JSON manifest + PDF/HTML report + raw artifacts.
- **Test:** immutability (attempted mutation fails and is audited);
  package reproducibility (same inputs → same manifest hash); stats
  correctness vs hand-counted fixture campaign.
- **Exit gate — M5:** a full case package for a test campaign reviewed and accepted by you.

### P13 — Analytics layer (M)

**Goal:** offline learning & reporting without touching hot paths.

- **Design:** periodic export durable store → Parquet (partitioned by
  date/domain); DuckDB notebooks/queries; derived graph (domain↔domain,
  page→media→target) built offline; site/template clustering (plan
  feature 9) prototyped here and fed back to P7 as source-profile bootstrap.
- **Test:** export completeness (row counts vs source), idempotent re-export.
- **Exit gate:** standard campaign/source reports reproducible from Parquet alone.

### P14 — Production hardening (L) → Milestone M6

- **Entry criteria carried from P2** (no multi-node environment existed):
  run the P2 storage suite, `validate-stack.sh` P2 steps and
  `benchmarks/p2-storage` against a 3-node Scylla cluster at RF=3
  (NetworkTopologyStrategy, tablets disabled — ADR-012): LOCAL_QUORUM
  read-after-write, LWT (E1/E2) under node loss, batchlog replay of outbox
  batches after a coordinator failure, and the latency targets over a real
  network.
- Multi-host has been *supported* since P0 (B.4). This phase *proves* it
  on physically separate machines: crawler pools on host A, GPU encoder on
  this RTX 2050 host, 3-node Scylla (RF=3), Redis, and MinIO (distributed
  mode if there are ≥ 4 drives/hosts). Fingerprinter 13d findings re-verified.
- Observability: dashboards per component, SLOs, alerting; tracing of a
  `CrawlTask` → `EvidenceRecord`.
- Security: SSRF everywhere media/pages are fetched server-side,
  secrets management, network isolation for Tor/browser pools.
- Chaos suite: kill any single component; Redis failover; durable-store
  node loss; object-store outage → verify no data loss and typed failures.
- 7-day soak test; runbooks; backup/restore drill for durable store + evidence.
- **Exit gate — M6:** soak passes with no leaks/lost work; restore drill succeeds.

---

## Part D — Feature traceability (plan's 30 features → phases)

| # | Feature | Phase |
|---|---|---|
| 1 | Persistent web memory | P2, P5 |
| 2 | Page/version/change history | P5 |
| 3 | Intelligent recrawling | P7 |
| 4 | Learned URL/path patterns | P7 |
| 5 | Temporal relevance | P7 |
| 6 | Persistent source intelligence | P7, P11 |
| 7 | Negative knowledge | P7 |
| 8 | Productivity / diminishing returns | P7 |
| 9 | Site/template clustering | P13 → P7 |
| 10 | Independent crawler worker pools | P4 |
| 11 | Learned fetch profiles | P7 (data from P4) |
| 12 | Cheap-first progressive fetching | P4 + P7 |
| 13 | Ad/tracker filtering | P6 |
| 14 | Media-centric identity | P8 |
| 15 | Persistent media embeddings | P9 (fingerprinter repo) |
| 16 | Model/version-aware representations | P9 (fingerprinter repo) |
| 17 | Vector similarity infrastructure | P10 (fingerprinter repo) |
| 18 | Decoupled matching | P10 (fingerprinter repo) |
| 19 | Shared durable intelligence DB | P1–P2 |
| 20 | Redis hot frontier | P3 |
| 21 | Cold/raw evidence storage | P2, P12 |
| 22 | Parquet/DuckDB analytics | P13 |
| 23 | Derived graph layer | P13 |
| 24 | Event/contract boundaries | P1 |
| 25 | Evidence collection | P12 |
| 26 | Evidence finalization | P12 |
| 27 | Case/client reporting | P12 |
| 28 | Immutable evidence provenance | P12 |
| 29 | Historical campaign intelligence | P11, P13 |
| 30 | V3-replaceable components | P1 (contracts), enforced every phase |

---

## Part E — Top risks

| Risk | Mitigation |
|---|---|
| Scope: 30 features, XL phases, one developer | Milestones are usable stopping points; M1 alone already beats V1 on memory/versions |
| Scylla modeling mistakes (hot/huge partitions, tombstones) are expensive to undo | Query-first design from P1 catalog; bucketed partitions; partition-size gate in P2 |
| 15 GB RAM host runs Scylla + Redis + MinIO + browsers + GPU worker | Memory budget in P0; browser pool RSS limits (P4); host roles let heavy pools move to other hosts |
| 4 GB VRAM | fp16, adaptive batching, one GPU process per host, CPU fallback (P9) |
| Cross-repo coordination (fingerprinter separate) | ADR-007 contract package; shared compat fixtures in both CIs; P9/P10 gated in this plan |
| Intelligence without data | P7 starts only after M1 has produced real history; rule-based first |
| Browser cost dominates | P4 measures; P7 learns cheapest working capability; pools scale independently |
| Evidence not legally useful | ADR-005 + P12 audit with your actual client/legal requirements before building |
| Two-repo contract drift (D12) repeats | One contract package, versioned events, compat tests |
| Live-internet flakiness in tests | Local fixture web for CI; live runs only in soak/milestone tests |

---

## Part F — Open decisions

Resolved 2026-09-28: see the decisions log at the top.

Still open:
1. Legal/evidence requirements (jurisdictions, whether screenshots/clips are needed, retention period). This blocks the P12 design only.
2. Hardware for the P14 multi-host proof: which physical machines are available besides this one.
