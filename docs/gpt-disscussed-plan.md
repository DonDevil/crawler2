V2 Anti-Piracy Intelligence System — Consolidated Design
Yes. At this point I would stop treating V2 as a backward-compatible crawler upgrade.
What you are describing is a new closed-loop anti-piracy intelligence and evidence system, with V1 serving as the reference implementation and experimental baseline. That removes a lot of architectural constraints and gives V2 a cleaner foundation for V3+.
The important distinction is:
V1 is a crawler with fingerprinting attached to it.  
> V2 should be a persistent anti-piracy intelligence system in which crawling, media identification, intelligence, and evidence generation are separate components.
Below is the consolidated V1 → V2 design based on the issues we identified throughout this project.
1. V1 vs V2 — consolidated comparison
Area
V1
Problem / limitation
V2
Overall architecture
`Crawler → Bridge → Fingerprinter → Result Consumer → Crawler feedback`
Components are tightly coupled around a single campaign/target
Persistent closed-loop intelligence system with Discovery, Crawl Intelligence, Crawlers, Media Intelligence, Target Management, Matching, Results and Evidence
Primary unit of knowledge
Visited URL
URL being visited once does not mean its content remains unchanged
Persistent domain/page/media/target entities with history
Crawler memory
Mostly visited/skip state
Previous campaigns can prevent useful pages from being revisited
Persistent web memory containing page versions, hashes, discoveries and historical observations
Campaign isolation
Crawl is strongly target/campaign oriented
S1 knowledge isn't properly exploited by S2
Global knowledge reused across campaigns
Example: S1 → S2
S1 crawls `/movies/`; S2 may skip it because it was already visited
Newly added S2 movie may never be discovered
`/movies/` has learned recrawl/change state and can be revisited when appropriate
Frontier
Primarily URL visited/skip + queue
Doesn't understand why a URL should be crawled again
Redis hot frontier + persistent scheduling/history
Frontier persistence
Redis/SQLite-oriented V1 model
Hot state and long-term knowledge are mixed conceptually
Redis = execution state; ScyllaDB = persistent intelligence
Production scaling
SQLite useful during development
SQLite isn't appropriate as the production distributed frontier/knowledge backend
Redis + distributed durable DB designed for multi-worker/multi-system operation
Crawler types
HTTP, async I/O, Tor, Playwright, Selenium, hybrid
Hybrid pipeline can force a fixed execution strategy
Independent worker pools by capability
Worker allocation
Multiple hybrid crawlers/terminals
Harder to independently scale HTTP/browser/Tor capacity
HTTP queue → HTTP workers; async queue → async workers; Playwright queue → browser workers, etc.
Crawler intelligence
Crawler largely decides execution
Expensive fetch decisions aren't globally learned
Intelligence layer determines WHAT / WHEN / HOW
Fetch strategy
Mostly static/hybrid escalation
Same site/path repeatedly gets expensive treatment
Persistent fetch profile per domain/path/page type
Browser rendering
Playwright/Selenium available
Browser work can be unnecessarily expensive
Cheap-first progressive fetching; browser only when justified
Selenium
One of the normal crawler options
Expensive/redundant if Playwright handles most browser work
Primarily compatibility/fallback capability
Dynamic page waiting
Fixed waits / crawler-specific behavior
Wastes time and resources
Condition/state-based page processing and bounded dynamic observation
API discovery
Not a major intelligence layer
Browser may be used where an underlying data endpoint could suffice
Learn reusable API/data-fetch patterns where appropriate
Browser lifecycle
Basic browser workers
Long-running browser processes can accumulate resource cost
Context/page reuse + controlled recycling + memory/crash/render metrics
Change detection
Weak/mostly absent
Can't distinguish unchanged pages from genuinely changed pages efficiently
HTTP validators + multiple content/change hashes
HTTP caching
Not central to intelligence
Re-fetching unchanged resources wastes bandwidth
ETag / Last-Modified / conditional requests / 304 handling
Content hashing
Not persistent enough
Changes aren't represented as durable page history
Raw, normalized, visible-text, link-set, media-set and structural hashes
Page versions
No strong historical model
Cannot reconstruct what changed over time
`page → page_version_1 → page_version_2 → ...`
Recrawl scheduling
Mostly fixed/frontier driven
Static and rapidly changing pages treated similarly
Learned crawl frequency based on observed change rate
URL intelligence
Basic URL discovery
Doesn't understand URL-space structure
Learned URL/path patterns such as `/movie/{year}/{slug}`
Temporal intelligence
Limited
Current campaigns aren't prioritized intelligently
Release date, publication date, modification date, URL year, media metadata, discovery time, etc.
Year relevance
No persistent learned temporal model
Old archive years can consume resources while current content matters more
Temporal/path relevance scoring
Site intelligence
V1 domain scoring
Primarily campaign-local scoring
Persistent source profile
Source profile
Limited
Knowledge isn't retained sufficiently
CMS, templates, URL patterns, page types, players, media patterns, pagination, ad behavior, change frequency, reliability
Source lifecycle
Not modeled
Dead/weak sources continue receiving effort
Unknown → discovered → validated → active → high-value → stale/dead
Negative knowledge
Weak
Repeatedly bad paths/sites may be retried
Store dead links, low-yield patterns, ad-heavy paths, repeated failures, etc.
Diminishing returns
Not deeply modeled
A source can consume resources after producing little new information
Productivity decay reduces crawl priority
Reactivation
Limited
Previously unproductive sources may stay ignored
New target/release/event can reactivate previously known sources
Site cloning
Not a major feature
Clone sites require rediscovery
Template/CMS/player/URL-pattern clustering can bootstrap knowledge
Ad handling
Heuristic such as many redirects/hits → block
Legitimate sites can be falsely classified as ads
Dedicated filter/classification engine
Ad classification
Binary-ish block/allow
Redirect ≠ advertisement
AD / TRACKER / NAVIGATION / CONTENT / MEDIA / PLAYER / UNKNOWN + confidence
Ad filtering
Hit-count heuristic
Doesn't exploit existing filter-list ecosystem
uBlock/EasyList-inspired hostname/path/resource/third-party/redirect/page rules
Filter provenance
Not strong
Difficult to know why something was blocked
Rule source, confidence and classification retained
False-positive handling
Limited
Aggressive blocking can destroy crawl coverage
Confidence thresholds + domain/path overrides + explainable decisions
Graph model
Not really persistent
Relationship intelligence isn't available globally
Store relationships as normal records/edges
Live graph
Would be tempting to put in crawl path
Graph DB can become unnecessary hot-path complexity
No graph dependency in crawler hot path
Graph analytics
—
—
Scylla → Parquet → DuckDB/graph/ML as derived analytical layer
Media identity
Media largely represented as URL
Same content can appear under multiple URLs; same URL can change
Separate URL identity from media/content identity
Fingerprint architecture
Target-centric
Every new target can cause old media to be fingerprinted again
Media-centric representation registry
Media embeddings
Not necessarily persisted/reused
Expensive embeddings can be recomputed across campaigns
Persist media embedding + model/version
Target embeddings
Target-specific
Correctly target-specific, but tied too closely to processing flow
One reusable target representation per target/model version
Repeated media
Reprocessed
Same media discovered by multiple campaigns/sites costs again
`media_id` + content identity allows reuse
Media versioning
Weak
Same URL can point to different content later
`media_id + media_version/content_hash`
Fingerprint queue
Target + target version + media URL
Queue semantics force target-centric processing
`media_id + required_embedding_version + priority`
Matching
Closely attached to fingerprinting
Representation and comparison become one operation
Separate Media Encoder → Vector Index → Matcher
Vector search
Potentially brute-force
Historical media volume will become huge
Derived ANN/vector index
Embedding storage
Fingerprinter-local concept
Crawler and fingerprinter don't share reusable intelligence
Shared durable knowledge store
Crawler DB vs fingerprinter DB
Could become separate
Duplicate state and extra lookups
One shared persistent intelligence store, logically separated by tables/access layers
Bridge
Important runtime component
Can accidentally become authoritative state
Event/compatibility boundary; durable DB is authoritative
Crawler ↔ fingerprint coupling
Stronger
Crawler pipeline waits around expensive fingerprint work
Discovery and representation decoupled
Media discovery
Immediately target-driven fingerprinting
Expensive work triggered too early
Crawler registers media; encoder processes only missing representations
Target management
Target controls much of processing
Makes computation less reusable
Target Manager maintains target metadata/representations separately
Feedback loop
Mainly crawler ↔ fingerprint feedback
Limited global learning
Results feed source/page/media/fetch intelligence
Operational DB
Primarily crawler persistence
Doesn't represent the whole system
Shared durable knowledge database
Cold storage
Not clearly separated
Raw page evidence and hot operational data compete
Hot metadata in Scylla + compressed raw/archive/object storage
Raw page storage
Not central
Historical evidence/reconstruction is difficult
Compressed response/page snapshots when valuable
Storage architecture
One storage concept
Doesn't distinguish hot vs cold vs analytical access
Scylla hot durable knowledge + object storage/archive + Parquet analytical layer
Search
Basic crawler lookup
Large historical knowledge becomes difficult to query
Derived search/index layer
Data science
Not first-class
Hard to analyze years of crawling
Scylla → Parquet → DuckDB/ML/graph
Evidence
Results primarily indicate matches
Doesn't automatically create a defensible case package
Dedicated Evidence/Case Builder
Evidence collection
Not a formal subsystem
Investigator must manually reconstruct crawl history
Evidence collector stores pages, timestamps, URLs, screenshots/clips, logs and provenance
Target coverage statistics
Limited
Hard to communicate campaign scope
Number of URLs inspected, domains/sites, pages, media discoveries, matches, failures, etc.
Piracy-source statistics
Not formalized
Client reports require manual counting
Distinct domains/sources, source relationships and historical occurrences
Evidence timestamping
Crawl logs exist but aren't case-oriented
Harder to demonstrate when evidence was observed
Timestamped evidence events and crawl observations
Evidence provenance
Not a dedicated concern
Need to establish where/when/how evidence was obtained
URL + source + timestamp + crawl method + page/media identity + capture metadata
Case/report generation
Manual
Complaint/client preparation takes extra work
Evidence Finalizer produces structured case packages
V1 coexistence
N/A
—
Not a V2 architectural constraint; V1 can remain separately for reference/experiments
Future versions
V1 assumptions constrain evolution
Compatibility can freeze bad abstractions
V2 uses stable contracts/events/entities so V3 can replace individual subsystems
Extensibility
Crawler-centric
New intelligence features require modifying crawler
New intelligence modules can consume/produce durable events without rewriting crawler core
System philosophy
“Crawl target and fingerprint results”
Campaign-centric
“Continuously learn the web, then answer target-specific questions using accumulated knowledge.”
2. The fundamental architectural change
“The biggest V2 change isn't Scylla, Redis, Playwright, embeddings, or the evidence system.
It is changing the unit of thought.
V1

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
V2
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
3. V2's persistent intelligence model
Domain
domain
 ├── identity
 ├── source profile
 ├── CMS/template
 ├── reliability
 ├── historical productivity
 ├── fetch characteristics
 ├── ad/filter profile
 └── lifecycle state
Page
page
 ├── canonical URL
 ├── domain
 ├── page type
 ├── learned URL pattern
 ├── current state
 ├── crawl schedule
 └── historical versions
Page version
page_version
 ├── fetched_at
 ├── HTTP metadata
 ├── raw hash
 ├── normalized hash
 ├── text hash
 ├── link hash
 ├── media hash
 ├── structural hash
 ├── extracted entities
 └── archived response reference
Media
media
 ├── media_id
 ├── content identity
 ├── media versions
 ├── canonical URLs
 ├── source pages
 ├── embedding(s)
 └── embedding model/version
Target
target
 ├── target_id
 ├── target metadata
 ├── release/temporal metadata
 └── target embedding(s)
Relationship
page → page
page → media
domain → domain
domain → page
page → target
media → target
These are ordinary records in the operational database.
The graph is generated later.
4. V2's biggest fingerprinting improvement
This deserves special emphasis because it is one of the most important flaws we found.
V1
Suppose:
Campaign S1

A
B
C
D
E
You fingerprint them against S1.
Then:
Campaign S2

C
D
E
F
G
A target-centric implementation can end up doing:
S2
 ├── C → fingerprint again
 ├── D → fingerprint again
 ├── E → fingerprint again
 ├── F → fingerprint
 └── G → fingerprint
That is wasteful.
V2
Media A → embedding
Media B → embedding
Media C → embedding
Media D → embedding
Media E → embedding
S2 discovers:
C D E F G
The system checks:
C → embedding exists → reuse
D → embedding exists → reuse
E → embedding exists → reuse
F → missing → encode
G → missing → encode
Then matching happens separately.
> **Fingerprinting becomes a reusable infrastructure service rather than a per-target operation.**
This also means a piece of media can be encoded **before any particular target requires it**.
5. The new crawl intelligence layer
This is where V2 becomes substantially more intelligent than V1.
The intelligence system should answer three separate questions.
1. What should we crawl?
new movie category page
current-year archive
known movie page
newly discovered player page
2. When should we crawl it?
historical change rate
target relevance
source productivity
freshness
last crawl
last meaningful change
release timing
3. How should we crawl it?
/movie/*       → HTTP
/category/*    → async HTTP
/player/*      → Playwright
/legacy/*      → Selenium fallback
This separation is important.
A frontier should not be responsible for understanding all of this.
6. V2's ad/filter engine
I would explicitly make this a subsystem rather than a few crawler heuristics.
Request
   ↓
Filter Engine
   ↓
┌──────────────┬───────────────┐
│              │               │
ALLOW       CLASSIFY        BLOCK
│              │               │
▼              ▼               ▼
crawl      content/media    tracker/ad/etc.
With classifications such as:
CONTENT
MEDIA
PLAYER
NAVIGATION
AD
TRACKER
UNKNOWN
And each decision can carry:
classification
confidence
rule
rule_source
domain/path
resource_type
That gives you a much safer system than:
"50 redirects happened → block domain"
It also allows V2 to improve the filter engine without changing crawler architecture.
7. Evidence Finalizer — new major subsystem
I agree with adding this.
It should **not** be part of the crawler.
The crawler's job is to observe.
The Evidence Finalizer's job is to turn observations into a **case-ready evidence package**.
For a target/campaign, it could assemble:
Coverage
Target
Campaign ID
Campaign start/end
Total URLs discovered
Total URLs fetched
Total successful fetches
Total failed fetches
Total domains
Total pages
Total media discovered
Total unique media
Total confirmed matches
Source statistics
Distinct piracy domains
URLs per domain
Pages per domain
Media instances per domain
First observation
Last observation
Historical observations
Evidence records
Evidence ID
Target ID
Domain
URL
Canonical URL
Observed timestamp
Fetch timestamp
Page type
Crawler used
HTTP metadata
Content hash
Media identity
Match information
Confidence
Visual evidence
page screenshot
relevant page clip
relevant UI region
timestamp
source URL
capture metadata
Where legally/operationally appropriate:
The important thing is that the screenshot/clip is **not the source of truth**.
The underlying observation remains:
URL
+
timestamp
+
page version
+
content hash
+
media identity
+
crawl metadata
The visual capture is supporting evidence.
8. Evidence should be immutable
I would introduce an important distinction:
Operational data
Can change:
page priority
crawl schedule
fetch profile
source score
current state
Evidence data
Should be treated as immutable once finalized:
evidence_id
target
URL
timestamp
page/content identity
capture
provenance
That gives you a much cleaner chain:
Crawler observation
       ↓
Persistent observation
       ↓
Evidence candidate
       ↓
Evidence finalization
       ↓
Immutable case record
       ↓
Client / complaint package
This is much more defensible than generating a report directly from whatever the crawler database currently says.
9. V2 storage architecture
                         ┌───────────────────┐
                         │      Redis        │
                         │                   │
                         │ Frontier          │
                         │ Queues            │
                         │ Leases            │
                         │ Short-lived state │
                         └─────────┬─────────┘
                                   │
                                   │
                         ┌─────────▼─────────┐
                         │     ScyllaDB      │
                         │                   │
                         │ Persistent memory │
                         │ Domains           │
                         │ Pages             │
                         │ Versions          │
                         │ Links             │
                         │ Media             │
                         │ Targets           │
                         │ Embeddings        │
                         │ Events            │
                         │ Intelligence      │
                         │ Evidence metadata │
                         └─────────┬─────────┘
                                   │
                  ┌────────────────┼────────────────┐
                  │                │                │
                  ▼                ▼                ▼
             Object store     Vector index      Search index
             raw evidence      ANN index         text/search
                  │
                  ▼
             Parquet datasets
                  │
                  ▼
          DuckDB / ML / Graph
This is an architecture I would be comfortable extending into V3.
10. What V2 should deliberately NOT do
This is just as important as the features.
To leave room for V3, avoid baking specialized intelligence directly into the crawler.
The crawler should **not** know:
    • how target relevance is calculated
    • how source productivity is calculated
    • how media similarity works
    • how evidence reports are generated
    • how graph analysis works
    • how long-term recrawl schedules are learned
    • how source clustering works
    • how campaign statistics are generated
Instead:
Intelligence → decision
Crawler → execution
Database → memory
Redis → coordination
Encoder → representation
Matcher → comparison
Evidence Builder → case production
Analytics → offline learning
That separation is what makes V3 manageable.
11. Designing V2 for V3
The safest approach is to establish **stable contracts**, not stable implementations.
For example, a crawler shouldn't receive:
"Go crawl this because score = 0.873"
It should receive something closer to:
{
    url,
    fetch_profile,
    priority,
    deadline,
    crawl_context
}
Then V3 can completely replace the intelligence algorithm while the crawler doesn't care.
Similarly, the Media Encoder shouldn't care why a media item was discovered.
It receives:
media_id
content_identity
source_reference
required_representation_version
A future V3 encoder can then use a completely different model.
Likewise:
Evidence Finalizer
should consume durable observations rather than crawler internals.
That allows you to improve evidence generation independently.
12. The V2 closed loop
                    ┌──────────────────────────┐
                    │     Target Manager       │
                    └────────────┬─────────────┘
                                 │
                                 ▼
                       ┌───────────────────┐
                       │ Crawl Intelligence│
                       └─────────┬─────────┘
                                 │
                                 ▼
                           Redis Frontier
                                 │
                                 ▼
                         Crawler Worker Pool
                                 │
                                 ▼
                      Page / Media Discovery
                                 │
                ┌────────────────┴────────────────┐
                ▼                                 ▼
        Page Intelligence                  Media Registry
                │                                 │
                │                                 ▼
                │                           Media Encoder
                │                                 │
                │                                 ▼
                │                           Vector Index
                │                                 │
                │                                 ▼
                │                               Matcher
                │                                 │
                └────────────────┐                ▼
                                 │             Results
                                 │                │
                         ┌────────┴────────────────┼────────────────┐
                         ▼                         ▼                ▼
                  Source learning           Crawl learning     Evidence
                         │                         │                │
                         └─────────────────────────┴────────────────┘
                                                   │
                                                   ▼
                                          Persistent knowledge
                                                   │
                                                   └──────► next crawl
That is the actual **closed loop**.
V1's loop becomes useful as the historical reference:
V1:
crawl → fingerprint → feedback

V2:
observe → remember → learn → prioritize → crawl
   ↑                              │
   └──────────── results ─────────┘
13. V1 should now become the reference laboratory
I would **not modify V1 to make it look like V2**.
Keep V1 as:
> the known working baseline and experimental reference.
V2 becomes a clean implementation.
You can copy useful proven components:
    • Redis frontier mechanics
    • crawler HTTP implementation
    • async implementation
    • Playwright integration
    • Tor integration
    • Selenium fallback
    • parsing/extraction utilities
    • networking utilities
But they become **V2 implementations of V2 interfaces**, rather than V2 inheriting V1's architecture.
That is an important distinction.
14. Recommended high-level V2 repository/component structure
v2/
│
├── core/
│   ├── contracts/
│   ├── events/
│   ├── models/
│   └── configuration/
│
├── intelligence/
│   ├── crawl/
│   ├── source/
│   ├── temporal/
│   ├── url_patterns/
│   ├── fetch_profiles/
│   └── prioritization/
│
├── frontier/
│   └── redis/
│
├── crawlers/
│   ├── http/
│   ├── async/
│   ├── playwright/
│   ├── tor/
│   └── selenium/
│
├── extraction/
│   ├── pages/
│   ├── links/
│   ├── media/
│   └── metadata/
│
├── media/
│   ├── registry/
│   ├── encoder/
│   └── matcher/
│
├── filtering/
│   └── ad_tracker/
│
├── evidence/
│   ├── collector/
│   ├── finalizer/
│   └── exporters/
│
├── storage/
│   ├── scylla/
│   ├── object_store/
│   └── repositories/
│
├── analytics/
│   ├── parquet/
│   ├── duckdb/
│   └── graph/
│
└── orchestration/
    └── workers/
Not necessarily exactly this directory layout, but these **logical boundaries** are worth preserving.
15. The core V2 principle
If I had to reduce the entire redesign to one rule:
> **Do not make the system forget what it has learned just because the target changed.**
V1 effectively thinks:
Movie S1 → crawl → fingerprint → done
Movie S2 → crawl → fingerprint → done
Movie S3 → crawl → fingerprint → done
V2 should think:
                 ┌───────────────┐
                 │ Persistent    │
                 │ Web Knowledge │
                 └───────┬───────┘
                         │
             ┌───────────┼───────────┐
             ▼           ▼           ▼
            S1          S2          S3
             │           │           │
             └───────────┼───────────┘
                         ▼
                continuously improving
                   source intelligence
S1 teaches the system about the web.
S2 benefits from what S1 learned.
S3 benefits from S1 + S2.
And the media encoder doesn't care which campaign originally discovered a piece of media.
---
## Final V2 feature set
So the redesign isn't merely:
**V1 + ScyllaDB.**
1. Persistent web memory
2. Page/version/change history
3. Intelligent recrawling
4. Learned URL/path patterns
5. Temporal relevance
6. Persistent source intelligence
7. Negative knowledge
8. Source productivity/diminishing returns
9. Site/template clustering
10. Independent crawler worker pools
11. Learned fetch profiles
12. Cheap-first/progressive fetching
13. Dedicated ad/tracker filtering
14. Media-centric identity
15. Persistent media embeddings
16. Model/version-aware representations
17. Vector similarity infrastructure
18. Decoupled matching
19. Shared durable intelligence database
20. Redis as hot execution frontier
21. Cold/raw evidence storage
22. Analytical Parquet/DuckDB layer
23. Derived graph layer
24. Event/contract-based component boundaries
25. Dedicated evidence collection
26. Evidence finalization
27. Case/client reporting
28. Immutable evidence provenance
29. Historical campaign intelligence
30. Architecture designed so V3 can replace individual intelligence/ML components without redesigning the crawler
That is a much more appropriate scope for **Crawler V2 / Anti-Piracy Intelligence V2** than trying to preserve V1's internal architecture.
The next architectural step should therefore **not be coding yet**. We should turn this into a concrete V2 specification: **component boundaries → data entities/tables → event contracts → Redis queues → read/write access patterns → worker lifecycle → evidence schema → V1 components worth copying**. That will give us a stable foundation before we start rebuilding anything.