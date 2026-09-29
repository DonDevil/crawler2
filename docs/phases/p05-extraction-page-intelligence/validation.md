# P5 Validation

Status (2026-09-29): **IMPLEMENTED — all 16 exit criteria evaluated.**
The CPU gate **passed** (17.9 % of V1). Change-detection precision was
**checked**, as the plan requires, and turned out **low (1/23)** on the
real pair sample: one site's rotating in-content widget is not removed by
the baseline normalizer (§4). The plan sets no numeric precision
threshold, so this is reported as a finding and a known limitation owned
by P6/P7, not as a pass or fail number.

## 1. Exit criteria

| # | Criterion | Result | Evidence |
|---|---|---|---|
| 1 | single-parse extraction implemented and tested | met | `parse.py` is the only parser user; `test_one_parse_and_one_tree_feed_every_extractor`, `test_no_other_module_constructs_a_parser` |
| 2 | links, media, metadata, JS URLs from the shared tree | met | one walk in `extract.py`; `test_extract.py` |
| 3 | hash set raw/normalized/visible/link/media/structural | met | `hashing.py`; `test_hashing.py`, golden results |
| 4 | a revision only when normalized_hash changes | met (as `PageRevisionId`, ADR-020) | `test_raw_only_change_is_not_a_new_revision`, `test_meaningful_change_…` |
| 5 | unchanged observations update last_seen only | met | `test_same_content_updates_last_seen…`, stack loop test |
| 6 | no duplicate revisions under concurrency or replay | met | fakes: barrier-forced concurrent first sighting; real Scylla: 6 observations × 3 threads → 1 row; replay tests (unit + stack) |
| 7 | archival policy defined and implemented in P2 | met | `archival.py`, `snapshot_retention`; nothing deleted |
| 8 | P2 repositories/outbox used correctly | met | only repository calls; `page.changed`/`media.discovered` in the sighting's logged batch; `urls.discovered` via `LinkRepository` |
| 9 | event semantics documented, compatible with P1/P4 | met | ADR-020, P1 events.md, contract 1.1 additive, fixture + MANIFEST unchanged for released files |
| 10 | golden-file tests pass | met | 5/5 |
| 11 | hash stability tests pass | met | 12/12 |
| 12 | integration tests pass | met | §3 |
| 13 | CPU/page measured against V1 defensibly | met | [benchmarks.md](benchmarks.md) |
| 14 | ≤ 50 % gate reported | **PASSED: 0.179** | `results/20260929T133057Z/gate.json` |
| 15 | change-detection precision on a hand-labeled set | **evaluated: precision 0.043 (1/23)** | §4, `results/change_detection.json`, `labels.json` |
| 16 | documentation updated | met | §6 |

## 2. Unit and contract tests

`make check` (ruff, mypy strict, pytest unit + contract):
**437 passed, 107 skipped** (97 are integration tests that skip without
the stack; 10 are pre-existing P4 fetcher-contract cases that do not apply
to one fetcher). P5's own tests:

| File | Tests | Covers |
|---|---|---|
| `tests/unit/extraction/test_urls.py` | 45 | cleaning, schemes, RFC 3986 resolution, P1-only canonicalization (query order/tracking/`www` kept), media kinds; 2 hypothesis properties |
| `tests/unit/extraction/test_scripts.py` | 7 | the exact supported literal syntax, unsupported syntax, JSON-LD vocabulary |
| `tests/unit/extraction/test_extract.py` | 31 | link sources/relations, drops by reason, dedup/order, anchor text, base href, policy hook, caps, media sources/methods/precedence, P1 validity, metadata, charsets, malformed/huge/deep/invalid input, Unicode, determinism, **single parse** |
| `tests/unit/extraction/test_hashing.py` | 12 | stability under ad/nav/footer/script/hidden/whitespace/case/zero-width noise; sensitivity of each hash; injected rules and scheme identity; fixed digest vectors |
| `tests/unit/extraction/test_service.py` | 16 | first/same/raw-only/meaningful/sequential/revert/out-of-order, replay, crash between writes, concurrent first sighting, skips, contained extractor failure, archival rules |
| `tests/unit/extraction/test_golden.py` | 5 | pinned facts + hashes + revision ids of 5 hand-written fixtures |
| `tests/contract/test_page_revisions.py` | 3 | `PageRevisionId` derivation and `page.changed` validators |

## 3. Integration (real Scylla / MinIO / Redis)

`scripts/test-extraction.sh` (host, Scylla at its container IP): unit +
`tests/integration/storage` = **149 passed** (33 storage integration
tests, 4 of them new for P5: revision convergence under out-of-order and
replayed writes; extract/retention round trip; the full loop
`page.observed` → relay → Redis → `ExtractionLoop` → rows → relay →
`page.changed`/`urls.discovered`/`media.discovered` including a replayed
`page.observed`; concurrent consumers). The P4 crawler tier
(`scripts/test-crawlers.sh`) was rerun as a regression check: see §3a.

§3a — P4 regression: `scripts/test-crawlers.sh` (fetcher contract +
crawler integration incl. browser tests) **85 passed, 22 skipped**
(skips = contract cases not applicable to a given fetcher, as in P4).

## 4. Change-detection evaluation

**Data.** The W691 HTML captured twice, 3 h apart (12:52 and 15:54 UTC,
2026-09-29, same runtime; bodies in git-ignored `var/`). Pairs = same
requested and final URL, 2xx HTML both times, bytes different: **475**
(of 647 HTML pages in capture 2). The rule (new revision ⇔ normalized hash
changed) calls **23** of them meaningful and **452** not.

**Sample and labels.** All 23 rule-positive pairs plus 30 seeded-random
rule-negative pairs (seed 20260929), shuffled, were written as review
files containing only a line diff of the page's whole visible text,
without the rule's verdict (`pairs.py review`). Each was labeled by the
reviewer (Claude) from the diff alone: *meaningful* = the page's own
primary content changed (subject text, an entry added to its primary
list, media); *non-meaningful* = identical visible text, rotating
site-wide widgets, randomized reshuffles, ads. Labels with one-line
reasons: `benchmarks/p5-extraction/labels.json` (no page text).
3 of 53 labels are marked borderline.

**Results** (`benchmarks/p5-extraction/results/change_detection.json`):

| | labeled meaningful | labeled non-meaningful |
|---|---|---|
| rule: changed | TP = 1 | FP = 22 |
| rule: unchanged | FN = 0 | TN = 30 |

- Precision **1/23 = 0.043**. Recall in the sample 1/1 (not meaningful as
  an estimate: 1 positive).
- Of 53 sampled pairs, 30 had *identical* visible text (markup/script
  noise only); the rule classified all of them correctly. Overall the rule
  suppressed a new revision for 452 of 475 byte-changed pairs (95 %).
- Borderline sensitivity: flipping the 3 borderline labels gives
  TP 2, FP 21, TN 30, FN 0 — precision 2/23 (0.087); the conclusion does
  not change.

**Root cause of the 22 false positives.** All are pages of one site
(isaimini.com.in). Every page embeds an Elementor "loop grid" of the
site's latest posts (casino/finance spam headlines) inside `<main>`, built
from plain `<div>`s without any landmark element or role. The baseline
normalizer removes only invisible content and landmark boilerplate
(design §9), so the rotation of one headline changes the normalized text
of every page on the site. The only true positive is that site's home
page, whose primary list gained an entry.

**What this means.** The rule does what it is defined to do; the baseline
definition of "boilerplate" is too narrow for template widgets that sit in
the main region. Fixing it requires either P6 selector rules injected as
`NormalizationRules(extra_selectors=…, scheme=…)` or site-template
learning (text blocks repeated across a site's pages) — both outside P5's
scope and not tuned here, because tuning the rule on the evaluation set
would overfit it. Until then, P7 should treat `page.changed` on such sites
with its diagnostic hashes (e.g. `link_set` and `media_set` unchanged).

**Limitations.** 53 pairs from one 3-hour interval; dominated by one site;
one reviewer; no statistical significance is claimed. The 3-hour window
contained almost no real content updates, so recall cannot be estimated.

## 5. Multi-host / architectural invariants (B.4, B.5)

No local state (captures are benchmark input in scratch); consumer group
shared across hosts with stale-entry reclaim; idempotent handlers;
derived-ID convergence; crawler runtime independent of intelligence;
target-independent storage; no cross-service reads; evidence hooks kept
(every snapshot still stored, decisions recorded).

## 6. Documentation updated

This phase directory (audit, design + deviations, implementation,
benchmarks, validation, decisions); ADR-020 and the ADR index; P1
`events.md`; P2 `schema.md`, `repositories.md`;
`architecture/extraction.md` and `architecture/README.md`;
`development.md`; `benchmarks.md`; `v2-phase-plan.md`.

## 7. Final validation run (2026-09-29)

| Suite | Result |
|---|---|
| `make check` (ruff, format, mypy strict, all tests; integration skipped without the stack) | 437 passed, 107 skipped |
| `scripts/test-extraction.sh` (P5 unit + storage integration, real stack) | 149 passed, three consecutive runs |
| `scripts/test-crawlers.sh` (P4 regression, incl. browser) | 85 passed, 22 skipped |

The first final run of the extraction tier had **1 failure**: the stack
loop test used the default 5 % archival sampling, so with random
observation IDs a re-sighting was occasionally retained as `sampled`. That
was a nondeterministic test, not a product defect; the test now pins
`sample_rate=0.0` and passed three times in a row.
