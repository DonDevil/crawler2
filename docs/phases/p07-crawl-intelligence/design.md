# P7 Design — crawl intelligence

Status: **DRAFT for review (2026-10-07).** Increment 1 (§5) is implemented
per D-17. Everything else is design only.

Inputs: [audit.md](audit.md) (§5 reassessment),
[g5.md](g5.md) (G5 FAIL), [decisions.md](decisions.md) (D-12…D-17).

## 1. Principles from the evidence

1. **Optimize useful information per fetch, not changes.** 98.5 % of
   `page.changed` events on the seed set are not worth a revisit (G5), and
   about 2 % of 4-day revisits revealed anything new (audit §5.2).
2. **Most of the seed budget is wasted on sources that cannot answer.**
   39 of 50 seed domains are unreachable, moved, parked or empty. M1 also
   lost a quarter of all attempts to one 429-ing host and ~10 % to bulk
   files (P6 X-14, X-15). Negative knowledge is the largest measurable gain
   available now.
3. **The seed set barely uses the browser.** HOW learning for browsers is
   built and tested on off-target escalation evidence, labelled as such.
   It is not claimed as a seed-set result (G6).
4. **Deterministic, bounded, explainable, replayable.** No ML (plan P7).
   Every decision is a pure function of a bounded profile, the time, and a
   versioned policy.

## 2. Components and their evidence

| Component | Question | Evidence (M1) | Status |
|---|---|---|---|
| **Source health** (negative knowledge, lifecycle, reactivation) | Is this host worth fetching now? | 503 hosts' outcomes: unreachable, moved, parked, 4xx-only, 429 storms, 304s; 39/50 dead seeds | **Increment 1 — implemented** |
| **Fetch profile** (HOW) | Which capability first? | 5,112 `needs_js` → browser escalations on 20 **off-target** hosts (93 % browser ok); seed set: none | **Increment 1 — implemented**, browser evidence off-target |
| Page kind + URL templates (WHAT) | Hub or detail? Which template is the site's content? Spam? | 21,013 HTML pages on 11 live seed sites; G5 labels show the useful changes are on hubs and spam shares templates on 2 sites | design §6; needs its own labelled page sample |
| Recrawl (WHEN) | When to revisit a hub? | G5 FAIL: neither `page.changed` nor plain URL novelty works | **gated** (D-13, D-16); design §7 |
| Priority composition | One number for P3 | depends on the above | design §8 |
| Target relevance / release timing | — | no targets (G7) | deferred to P9 (D-7) |
| Tor profiles | — | no Tor daemon | deferred (D-7) |

## 3. Boundaries (checked for every increment)

| Boundary | Rule | Increment 1 |
|---|---|---|
| P1 contracts | frozen; P7 consumes `fetch.completed` (and later `page.observed`, `urls.discovered`) and produces `crawl.requested` | consumes the `FetchAttempt` model only; no contract change |
| P3 frontier | P7 never touches Redis keys; it admits through the P3 API (`admit_many` / `crawl.requested`) and feeds only near-due work (scheduled tasks count against `max_depth`); politeness overrides via `set_domain_interval` | no frontier call at all (a test enforces the import boundary) |
| P4 fetch | P4 keeps escalation (`needs_js` → browser inside one task); P7 only chooses the **starting** queue | advice only |
| P5 extraction | revisions/hashes are P5 facts; P7 does not re-parse pages at runtime | not used |
| P6 admission | filter/scope/revisit gate stay P6; P7 advice is applied at admission in increment 2 by an explicit, reviewed integration | not wired |
| Scylla ownership | P7-owned tables only (increment 2); profiles are derived and rebuildable from `fetch.completed` / the outbox / the fact export | none (pure) |
| Redis | hot frontier and transport only; P7 keeps no state in Redis | none |
| MinIO | evidence; P7 reads snapshots only for offline evaluation (G5) | none |
| B.5 #1 | the crawler runs with P7 stopped | P7 not on any runtime path |

**Open item for increment 2 (needs an ADR before any P7 consumer runs).**
Under ADR-022 a consumer group protects its unread entries, and the relay
holds back while a grouped stream is full. A P7 group on `fetch.completed`
that falls behind would therefore throttle publication for the whole loop,
which violates B.5 #1. Proposal: groups named `intel-*` are
**best-effort**. Retention ignores them for the trim floor, the consumer
detects the gap, and it rebuilds its projection from facts. This changes
the P2 publisher and needs approval and its own ADR.

## 4. Source health and fetch profile — model (increment 1)

Code: `crawler2/intelligence/sources.py`. Policy: `SourcePolicy`
(versioned, `source-health/v1`).

### 4.1 Keys and facts

- **Key:** the P1 `domain_id` (host) of the requested URL. Registrable
  domains are used only to tell "moved" from "same site".
- **One fact per `fetch.completed`:** time (`finished_at`), capability, and
  outcome class:

| Class | From the attempt | Meaning |
|---|---|---|
| `ok` | response 2xx/304 and final URL on the same registrable domain | the source answers with content |
| `moved` | response whose final URL is on **another** registrable domain | mirror move / parking (target recorded) |
| `http_error` | response 4xx/5xx except 429 | reachable, page-level error |
| `blocked` | P1 `blocked` (429, challenge, captcha) | refuses us |
| `unreachable` | timeout, DNS, connection, TLS | does not answer |
| `needs_js` | http response with detail `outcome=needs_js` | content needs the browser |
| `other` | too_large, cancelled, invalid, media | not a source-health signal |

### 4.2 Profile state (bounded, order-independent)

Per host: one exponentially decayed weight per class (time constant
`tau_h` = 24 h), anchored at `ref_at`. Folding a fact at time *t*:

- if *t* > `ref_at`: every weight is multiplied by e^−(t−ref)/τ, then +1;
- otherwise: + e^−(ref−t)/τ.

The weighted sum Σ e^−(T−tᵢ)/τ is **commutative**, so any delivery order
gives the same state (up to float rounding), and duplicates are removed by
fact id before folding. The state also keeps:

- `first_seen` (min), `last_ok` (max), `last_attempt` (max),
  `last_http_attempt` (max);
- the moved target (latest);
- HOW weights with `tau_cap` = 7 d: `http_ok`, `http_needs_js`, `browser_ok`.

Size is fixed (~15 numbers per host), whatever the history length.

### 4.3 Lifecycle (derived at query time, never stored)

Evaluated in order. The weights are decayed to `now`.

| State | Condition | Advice |
|---|---|---|
| `unknown` | no facts | admit; `http` |
| `active` | `last_ok` within `active_window` (24 h) | admit |
| `moved` | moved ≥ `min_evidence` and ≥ 80 % of moved+ok+error+unreachable weight | probe only; target reported to the operator (not rooted, D-15) |
| `blocked` | blocked ≥ `min_evidence` and ≥ 80 % of all weight | probe only (429 back-off, X-14) |
| `unreachable` | unreachable ≥ `min_evidence` and ≥ 80 % of all weight | probe only |
| `error` | http_error ≥ `min_evidence` and ≥ 80 % of all weight | probe only |
| `degraded` | anything else (mixed evidence, old success) | admit |

**Probe only:** a fetch is allowed when
`now − last_attempt ≥ probe_interval`, where
`probe_interval = clamp(age / 4, 6 h, 7 d)` and *age* is the time since
`last_ok` (or since `first_seen` if the host never answered).

- **Reactivation:** a probe that returns `ok` makes the host `active` at
  once (`last_ok` = now).
- **No permanent death:** every non-active host is retried at least weekly.
- **Unbounded evidence ages out:** after ~3τ without new facts the weights
  fall below `min_evidence`, and the host returns to `degraded`, which
  admits normally. Under the probe-only regime, though, new facts keep
  arriving from the probes themselves.

### 4.4 Fetch profile (HOW)

Browser first when `http_needs_js ≥ min_evidence` (τ = `tau_cap`), `http_needs_js ≥ http_ok` and
`browser_ok ≥ 1` (weights with `tau_cap` = 7 d). Otherwise `http`.

**Re-test:** when the browser is recommended but no http attempt has run
for `http_retest` (7 d), recommend `http` once. P4 escalates again if it
still needs JS. As browser-only history accumulates, the http evidence
decays below the threshold, so http is re-tested in any case.

### 4.5 Parameter origins

| Parameter | Value | Origin |
|---|---|---|
| `tau_h` | 24 h | the M1 link revisit cadence (P6 D-3); evidence older than ~3 days weighs < 5 % |
| `min_evidence` | weight of **3 attempts within 12 h** = 1 + 2·e^(−12 h/τ) (2.21 for τ = 24 h; 2.86 for `tau_cap` = 7 d) | one P3 task's attempt budget (`max_attempts` = 3): a fully exhausted task is enough evidence, and a wrong call costs one probe interval. Expressed as a decayed weight, because a literal 3 is never reached by attempts spread over hours |
| dominance share | 0.8 | one success in five attempts keeps a host out of the negative states |
| `active_window` | 24 h | `tau_h` |
| probe interval | age/4, 6 h…7 d | 6 h = the M1 seed cadence; 7 d keeps every host retried weekly; age/4 is a backoff without stored counters (order-independent) |
| `tau_cap`, `http_retest` | 7 d | escalation evidence is rare (20 hosts in M1) and should last across days |

All values are fixed in `SourcePolicy` **before** the replay (§5.3), and
they are not tuned on it.

## 5. Increment 1 — scope and evaluation

### 5.1 Scope

- `crawler2/intelligence/sources.py`: facts, profile fold, lifecycle,
  advice, and adapters from P1 `FetchAttempt` and from export rows. Pure.
- Tests: unit, edge cases, order and duplicate independence (hypothesis),
  and the import boundary.
- `benchmarks/p7-intelligence/replay_sources.py`: offline replay over the
  M1 export.
- **Not in increment 1:** storage, consumers, admission wiring, WHAT, WHEN,
  priority.

### 5.2 Why this first

It is the component with the strongest evidence (§1.2). It needs no
signal that failed (G5) and no label set, its gains can be measured
offline on M1, and it is a prerequisite for priority (§8).

### 5.3 Offline replay (pre-registered)

- Replay every M1 attempt in `finished_at` order (ties by attempt id), with
  the policy fixed in §4.5.
- Before each attempt, ask for advice from the profile built from the
  attempts already **executed** in the simulation:
  - **probe-only and not due:** the attempt is *suppressed*; its outcome is
    not folded, because the policy would not have observed it;
  - **otherwise:** it is executed and folded.
- **Metrics,** reported separately for the seed set and for off-target
  hosts (labelled):
  - attempts executed and suppressed;
  - suppressed attempts by their actual outcome class — the **cost avoided**
    (unreachable/blocked/moved/error, and their seconds);
  - the **value lost**: suppressed attempts whose actual outcome was `ok`;
  - per-state counts;
  - HOW (off-target): http attempts that ended `needs_js` on hosts already
    browser-first (an http attempt saved), and attempts the profile would
    have started on the browser although http succeeded (an unnecessary
    browser fetch).
- **Limits:**
  - The replay cannot show what the freed budget would have discovered
    (audit A-7); that is the M2 live A/B.
  - The export keeps the final URL but not the redirect hops, so a move
    that ends in a failure counts as `unreachable` in the replay. The live
    adapter uses the hops.

## 6. WHAT (design; next increment after review)

- **Page kind:** `hub` (home, listing, category, archive, search) vs
  `detail` vs `other`. Features, each with a reason:

  | Feature | Reason |
  |---|---|
  | same-site out-links | hubs link to many same-site content pages (moviesda.business listings: 35–150) |
  | share of out-links that follow one template | hubs repeat one detail template |
  | URL template token classes (`{year}`, `{num}`, `{slug}`, `{ext}`, `page/{n}`) | pagination and archives |
  | title/slug year token | film detail pages: `Lens (2017)`, `-2017/` |
  | `og:type` | `article` on most posts, useless alone (44k pages) |
  | media presence | 127 pages, one site |

  Path depth is **excluded** (audit §5.2).
- **URL templates:** per site, paths are tokenized with token classes and
  grouped by an identical class sequence. Templates with ≥ 20 members are
  kept. This is bounded, deterministic and explainable.
- **Relevance and spam (negative knowledge, page level):** a site's
  *content template* is the template whose members carry film signals
  (year tokens, "movie download" title patterns). Spam posts share the
  `/<slug>/` template on two sites, so page-level spam signals (no year,
  gambling/SEO vocabulary in titles) are needed. The vocabulary list must
  be **operator data**, not code.
- **Validation:** a new labelled sample of pages (kind, relevant or spam)
  drawn from the 21k live pages, labelled before the rules are frozen.
  This is not the G5 set.

## 7. WHEN (gated: D-13, D-16)

- **Candidate signal** (to be validated, not implemented): per hub page,
  the number of newly linked same-site URLs that match the site's content
  template and are not spam (§6). Credit goes to every hub that links the
  URL within the revisit, not only the first one fetched (the G5
  false-negative cause).
- **Estimator:** per hub, a Poisson rate from (new content links, exposure
  time), pooled per site template when the hub has little history; the
  interval is clamp(k/λ̂, min, max), with a conservative default for new
  hubs. Detail pages are not revisited for change: 0/31 useful in G5, and
  their recrawl stays the P6 static rule until evidence says otherwise.
- **Gate to implement:** the signal must reach precision ≥ 0.8 on a new
  labelled sample of hub revisits. The G5 threshold is kept, the G5 labels
  are not reused.
- **Data caveat:** run 2 revisited few hubs. If the new labelled sample
  cannot reach 50 hub revisits from existing data, that is reported, and a
  short hub-only experiment would be proposed then, not run.

## 8. Priority (design)

One function, `priority(advice, page_kind, hub_rate, provenance) → 0..100`,
used only at admission. It is introduced only when its inputs exist
(after WHAT). The P6 provenance priorities stay the default until then.
Source health acts first as a **gate** (probe-only states are not admitted
except on probes), not as a weight.

## 9. Storage and integration (increment 2, for review)

| Table (P7-owned) | Key | Access |
|---|---|---|
| `source_profiles` | `(domain_id)` | read at admission (one row per candidate host), written per `fetch.completed` with LWT on a version column (concurrent consumers) |
| `source_profile_moves` | `(domain_id)`, CK `observed_at` | operator report of moved/parked targets |

- **Consumer:** `crawler2-intel sources`, group `intel-sources` on
  `fetch.completed`, `IdempotentConsumer`, best-effort per §3.
- **Rebuild:** replay `fetch.completed` from the outbox or the fact export.
- **Admission:** the P6 `Admitter` consults the profile, through a
  read-only P7 interface with a cache, **after** the filter and scope. A
  probe-only host gets at most one admission per probe interval, and the
  starting queue comes from the fetch profile. With P7 absent, the advice
  is "admit, http", which is today's behaviour (B.5 #1).

## 10. M2 prerequisites (not started)

- **Seed population.** The current seed set gives 11 live domains and ~2 %
  new content per 4-day revisit (D-15). Seed refresh is an operator
  decision before any A/B.
- **HOW metric (D-8).** The seed set has no browser-requiring subset.
  Either the metric is defined on off-target escalation hosts, or it is
  recorded as not applicable to the seed set.
- **New-pages target X:** set from a power calculation on the base rate of
  the chosen population, before running. On the current base rate (30 new
  URLs per 4 days over 413 revisits), detecting +20 % needs on the order
  of hundreds of new URLs per arm, which this seed set cannot produce in
  days.
