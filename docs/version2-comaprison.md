| Area                               | V1                                                                      | Problem / limitation                                                              | V2                                                                                                                                                                 |
| ---------------------------------- | ----------------------------------------------------------------------- | --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Overall architecture**           | `Crawler → Bridge → Fingerprinter → Result Consumer → Crawler feedback` | Components are tightly coupled around a single campaign/target                    | **Persistent closed-loop intelligence system** with Discovery, Crawl Intelligence, Crawlers, Media Intelligence, Target Management, Matching, Results and Evidence |
| **Primary unit of knowledge**      | Visited URL                                                             | URL being visited once does not mean its content remains unchanged                | Persistent **domain/page/media/target entities with history**                                                                                                      |
| **Crawler memory**                 | Mostly visited/skip state                                               | Previous campaigns can prevent useful pages from being revisited                  | Persistent web memory containing page versions, hashes, discoveries and historical observations                                                                    |
| **Campaign isolation**             | Crawl is strongly target/campaign oriented                              | S1 knowledge isn't properly exploited by S2                                       | **Global knowledge reused across campaigns**                                                                                                                       |
| **Example: S1 → S2**               | S1 crawls `/movies/`; S2 may skip it because it was already visited     | Newly added S2 movie may never be discovered                                      | `/movies/` has learned recrawl/change state and can be revisited when appropriate                                                                                  |
| **Frontier**                       | Primarily URL visited/skip + queue                                      | Doesn't understand why a URL should be crawled again                              | Redis hot frontier + persistent scheduling/history                                                                                                                 |
| **Frontier persistence**           | Redis/SQLite-oriented V1 model                                          | Hot state and long-term knowledge are mixed conceptually                          | Redis = **execution state**; ScyllaDB = **persistent intelligence**                                                                                                |
| **Production scaling**             | SQLite useful during development                                        | SQLite isn't appropriate as the production distributed frontier/knowledge backend | Redis + distributed durable DB designed for multi-worker/multi-system operation                                                                                    |
| **Crawler types**                  | HTTP, async I/O, Tor, Playwright, Selenium, hybrid                      | Hybrid pipeline can force a fixed execution strategy                              | Independent **worker pools by capability**                                                                                                                         |
| **Worker allocation**              | Multiple hybrid crawlers/terminals                                      | Harder to independently scale HTTP/browser/Tor capacity                           | HTTP queue → HTTP workers; async queue → async workers; Playwright queue → browser workers, etc.                                                                   |
| **Crawler intelligence**           | Crawler largely decides execution                                       | Expensive fetch decisions aren't globally learned                                 | Intelligence layer determines **WHAT / WHEN / HOW**                                                                                                                |
| **Fetch strategy**                 | Mostly static/hybrid escalation                                         | Same site/path repeatedly gets expensive treatment                                | Persistent **fetch profile** per domain/path/page type                                                                                                             |
| **Preferred engine**               | Configuration-driven                                                    | Doesn't learn that `/movie/*` works over HTTP while `/player/*` needs browser     | Learned `preferred_engine`, fallback, success rate, JS probability, latency, failures                                                                              |
| **Browser rendering**              | Playwright/Selenium available                                           | Browser work can be unnecessarily expensive                                       | Cheap-first progressive fetching; browser only when justified                                                                                                      |
| **Selenium**                       | One of the normal crawler options                                       | Expensive/redundant if Playwright handles most browser work                       | Primarily **compatibility/fallback capability**                                                                                                                    |
| **Dynamic page waiting**           | Fixed waits / crawler-specific behavior                                 | Wastes time and resources                                                         | Condition/state-based page processing and bounded dynamic observation                                                                                              |
| **API discovery**                  | Not a major intelligence layer                                          | Browser may be used where an underlying data endpoint could suffice               | Learn reusable API/data-fetch patterns where appropriate                                                                                                           |
| **Browser lifecycle**              | Basic browser workers                                                   | Long-running browser processes can accumulate resource cost                       | Context/page reuse + controlled recycling + memory/crash/render metrics                                                                                            |
| **Change detection**               | Weak/mostly absent                                                      | Can't distinguish unchanged pages from genuinely changed pages efficiently        | HTTP validators + multiple content/change hashes                                                                                                                   |
| **HTTP caching**                   | Not central to intelligence                                             | Re-fetching unchanged resources wastes bandwidth                                  | `ETag` / `Last-Modified` / conditional requests / `304` handling                                                                                                   |
| **Content hashing**                | Not persistent enough                                                   | Changes aren't represented as durable page history                                | Raw, normalized, visible-text, link-set, media-set and structural hashes                                                                                           |
| **Page versions**                  | No strong historical model                                              | Cannot reconstruct what changed over time                                         | `page → page_version_1 → page_version_2 → ...`                                                                                                                     |
| **Recrawl scheduling**             | Mostly fixed/frontier driven                                            | Static and rapidly changing pages treated similarly                               | Learned crawl frequency based on observed change rate                                                                                                              |
| **Temporal intelligence**          | Limited                                                                 | Current campaigns aren't prioritized intelligently                                | Release date, publication date, modification date, URL year, media metadata, discovery time, etc.                                                                  |
| **URL intelligence**               | Basic URL discovery                                                     | Doesn't understand URL-space structure                                            | Learned URL/path patterns such as `/movie/{year}/{slug}`                                                                                                           |
| **Year relevance**                 | No persistent learned temporal model                                    | Old archive years can consume resources while current content matters more        | Temporal/path relevance scoring                                                                                                                                    |
| **Site intelligence**              | V1 domain scoring                                                       | Primarily campaign-local scoring                                                  | Persistent **source profile**                                                                                                                                      |
| **Source profile**                 | Limited                                                                 | Knowledge isn't retained sufficiently                                             | CMS, templates, URL patterns, page types, players, media patterns, pagination, ad behavior, change frequency, reliability                                          |
| **Source lifecycle**               | Not modeled                                                             | Dead/weak sources continue receiving effort                                       | Unknown → discovered → validated → active → high-value → stale/dead                                                                                                |
| **Negative knowledge**             | Weak                                                                    | Repeatedly bad paths/sites may be retried                                         | Store dead links, low-yield patterns, ad-heavy paths, repeated failures, etc.                                                                                      |
| **Diminishing returns**            | Not deeply modeled                                                      | A source can consume resources after producing little new information             | Productivity decay reduces crawl priority                                                                                                                          |
| **Reactivation**                   | Limited                                                                 | Previously unproductive sources may stay ignored                                  | New target/release/event can reactivate previously known sources                                                                                                   |
| **Site cloning**                   | Not a major feature                                                     | Clone sites require rediscovery                                                   | Template/CMS/player/URL-pattern clustering can bootstrap knowledge                                                                                                 |
| **Ad handling**                    | Heuristic such as many redirects/hits → block                           | Legitimate sites can be falsely classified as ads                                 | Dedicated **filter/classification engine**                                                                                                                         |
| **Ad classification**              | Binary-ish block/allow                                                  | Redirect ≠ advertisement                                                          | AD / TRACKER / NAVIGATION / CONTENT / MEDIA / PLAYER / UNKNOWN + confidence                                                                                        |
| **Ad filtering**                   | Hit-count heuristic                                                     | Doesn't exploit existing filter-list ecosystem                                    | uBlock/EasyList-inspired hostname/path/resource/third-party/redirect/page rules                                                                                    |
| **Filter provenance**              | Not strong                                                              | Difficult to know why something was blocked                                       | Rule source, confidence and classification retained                                                                                                                |
| **False-positive handling**        | Limited                                                                 | Aggressive blocking can destroy crawl coverage                                    | Confidence thresholds + domain/path overrides + explainable decisions                                                                                              |
| **Graph model**                    | Not really persistent                                                   | Relationship intelligence isn't available globally                                | Store relationships as normal records/edges                                                                                                                        |
| **Live graph**                     | Would be tempting to put in crawl path                                  | Graph DB can become unnecessary hot-path complexity                               | **No graph dependency in crawler hot path**                                                                                                                        |
| **Graph analytics**                | —                                                                       | —                                                                                 | Scylla → Parquet → DuckDB/graph/ML as derived analytical layer                                                                                                     |
| **Media identity**                 | Media largely represented as URL                                        | Same content can appear under multiple URLs; same URL can change                  | Separate **URL identity from media/content identity**                                                                                                              |
| **Fingerprint architecture**       | Target-centric                                                          | Every new target can cause old media to be fingerprinted again                    | **Media-centric representation registry**                                                                                                                          |
| **Media embeddings**               | Not necessarily persisted/reused                                        | Expensive embeddings can be recomputed across campaigns                           | Persist media embedding + model/version                                                                                                                            |
| **Target embeddings**              | Target-specific                                                         | Correctly target-specific, but tied too closely to processing flow                | One reusable target representation per target/model version                                                                                                        |
| **Repeated media**                 | Reprocessed                                                             | Same media discovered by multiple campaigns/sites costs again                     | `media_id` + content identity allows reuse                                                                                                                         |
| **Media versioning**               | Weak                                                                    | Same URL can point to different content later                                     | `media_id + media_version/content_hash`                                                                                                                            |
| **Fingerprint queue**              | Target + target version + media URL                                     | Queue semantics force target-centric processing                                   | `media_id + required_embedding_version + priority`                                                                                                                 |
| **Matching**                       | Closely attached to fingerprinting                                      | Representation and comparison become one operation                                | Separate **Media Encoder → Vector Index → Matcher**                                                                                                                |
| **Vector search**                  | Potentially brute-force                                                 | Historical media volume will become huge                                          | Derived ANN/vector index                                                                                                                                           |
| **Embedding storage**              | Fingerprinter-local concept                                             | Crawler and fingerprinter don't share reusable intelligence                       | Shared durable knowledge store                                                                                                                                     |
| **Crawler DB vs fingerprinter DB** | Could become separate                                                   | Duplicate state and extra lookups                                                 | **One shared persistent intelligence store**, logically separated by tables/access layers                                                                          |
| **Bridge**                         | Important runtime component                                             | Can accidentally become authoritative state                                       | Event/compatibility boundary; durable DB is authoritative                                                                                                          |
| **Crawler ↔ fingerprint coupling** | Stronger                                                                | Crawler pipeline waits around expensive fingerprint work                          | Discovery and representation decoupled                                                                                                                             |
| **Media discovery**                | Immediately target-driven fingerprinting                                | Expensive work triggered too early                                                | Crawler registers media; encoder processes only missing representations                                                                                            |
| **Target management**              | Target controls much of processing                                      | Makes computation less reusable                                                   | Target Manager maintains target metadata/representations separately                                                                                                |
| **Feedback loop**                  | Mainly crawler ↔ fingerprint feedback                                   | Limited global learning                                                           | Results feed source/page/media/fetch intelligence                                                                                                                  |
| **Operational DB**                 | Primarily crawler persistence                                           | Doesn't represent the whole system                                                | Shared durable **knowledge database**                                                                                                                              |
| **Cold storage**                   | Not clearly separated                                                   | Raw page evidence and hot operational data compete                                | Hot metadata in Scylla + compressed raw/archive/object storage                                                                                                     |
| **Raw page storage**               | Not central                                                             | Historical evidence/reconstruction is difficult                                   | Compressed response/page snapshots when valuable                                                                                                                   |
| **Storage architecture**           | One storage concept                                                     | Doesn't distinguish hot vs cold vs analytical access                              | Scylla hot durable knowledge + object storage/archive + Parquet analytical layer                                                                                   |
| **Search**                         | Basic crawler lookup                                                    | Large historical knowledge becomes difficult to query                             | Derived search/index layer                                                                                                                                         |
| **Data science**                   | Not first-class                                                         | Hard to analyze years of crawling                                                 | Scylla → Parquet → DuckDB/ML/graph                                                                                                                                 |
| **Evidence**                       | Results primarily indicate matches                                      | Doesn't automatically create a defensible case package                            | Dedicated **Evidence/Case Builder**                                                                                                                                |
| **Evidence collection**            | Not a formal subsystem                                                  | Investigator must manually reconstruct crawl history                              | Evidence collector stores pages, timestamps, URLs, screenshots/clips, logs and provenance                                                                          |
| **Target coverage statistics**     | Limited                                                                 | Hard to communicate campaign scope                                                | Number of URLs inspected, domains/sites, pages, media discoveries, matches, failures, etc.                                                                         |
| **Piracy-source statistics**       | Not formalized                                                          | Client reports require manual counting                                            | Distinct domains/sources, source relationships and historical occurrences                                                                                          |
| **Evidence timestamping**          | Crawl logs exist but aren't case-oriented                               | Harder to demonstrate when evidence was observed                                  | Timestamped evidence events and crawl observations                                                                                                                 |
| **Evidence provenance**            | Not a dedicated concern                                                 | Need to establish where/when/how evidence was obtained                            | URL + source + timestamp + crawl method + page/media identity + capture metadata                                                                                   |
| **Case/report generation**         | Manual                                                                  | Complaint/client preparation takes extra work                                     | Evidence Finalizer produces structured case packages                                                                                                               |
| **V1 coexistence**                 | N/A                                                                     | —                                                                                 | Not a V2 architectural constraint; V1 can remain separately for reference/experiments                                                                              |
| **Future versions**                | V1 assumptions constrain evolution                                      | Compatibility can freeze bad abstractions                                         | V2 uses stable contracts/events/entities so V3 can replace individual subsystems                                                                                   |
| **Extensibility**                  | Crawler-centric                                                         | New intelligence features require modifying crawler                               | New intelligence modules can consume/produce durable events without rewriting crawler core                                                                         |
| **System philosophy**              | “Crawl target and fingerprint results”                                  | Campaign-centric                                                                  | **“Continuously learn the web, then answer target-specific questions using accumulated knowledge.”**                                                               |


Fundamental architectural change

V1:
Target
   ↓
Crawler
   ↓
URLs
   ↓
Media
   ↓
Fingerprint
   ↓
Result

The implicit assumption is:

“I am crawling the web because I have this target.”

V2:
                         ┌─────────────────────┐
                         │   Target Manager     │
                         └──────────┬──────────┘
                                    │
                                    ▼
                            Target intelligence
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────┐
│                  Persistent Web Intelligence                │
│                                                             │
│ Domains │ Pages │ Page Versions │ Links │ Media │ Sources  │
│ Patterns│ Fetch Profiles │ History │ Embeddings │ Events   │
└──────────────────────────────┬──────────────────────────────┘
                               │
                       Crawl Intelligence
                               │
                ┌──────────────┼──────────────┐
                ▼              ▼              ▼
             WHAT/WHEN       HOW          PRIORITY
                │              │              │
                └──────────────┼──────────────┘
                               ▼
                         Redis Frontier
                               │
             ┌─────────────────┼─────────────────┐
             ▼                 ▼                 ▼
        HTTP Workers      Browser Workers    Tor Workers
             │                 │                 │
             └─────────────────┼─────────────────┘
                               ▼
                         Page / Media
                         discoveries
                               │
                 ┌─────────────┴─────────────┐
                 ▼                           ▼
          Page Intelligence            Media Registry
                                             │
                                             ▼
                                       Media Encoder
                                             │
                                             ▼
                                        Vector Index
                                             │
                                             ▼
                                           Matcher
                                             │
                                             ▼
                                          Results
                                             │
                              ┌──────────────┴──────────────┐
                              ▼                             ▼
                       Intelligence                  Evidence Builder
                              │                             │
                              ▼                             ▼
                      Better future crawl          Case / Client Package
