# P6 Benchmarks

Raw results: `benchmarks/p6-filter/results/`, `benchmarks/p6-m1/results/`.
Scripts: `benchmarks/p6-filter/{throughput,fp_eval,corpus_build}.py`,
`benchmarks/p6-m1/{run.sh,monitor.py,report.py,check.py}`.

## 1. Gate D — filter throughput (≥ 100,000 decisions/s in-process)

**Method.** Ruleset = built_in (48) + V1 reviewed import (98 stored, 60
enabled) + EasyList + EasyPrivacy as downloaded on 2026-09-29 (versions
202609291623 / 202609291635), 106,098 compiled rules (93,651 host, 12,438
URL pattern, 9 host label; 2 patterns without an index token). Workload =
every link and every script/img/stylesheet/iframe/media reference of the
639 HTML pages of the W691 capture (`var/p5-corpus/capture-1`), each with
its page as first party: 60,086 inputs, 9,585 distinct URLs, shuffled with
a fixed seed. One process, **no decision cache**, Python 3.12.3,
i5-11400H, compose stack idle. "Warm" = repeated passes for 10 s; "cold" =
the first pass including input building with an empty PSL cache.

| Measure | Result |
|---|---|
| **warm decisions/s (gate metric)** | **127,985 — PASS** |
| warm URL → input → decision / s | 81,659 |
| cold first pass / s | 71,629 |
| latency p50 / p99 / max | 7.1 / 31.3 / ~170 µs |
| list parse (both lists) | 2.9 s |
| compile | 3.9 s |
| RSS after compile | 298 MB (from 36 MB) |
| decisions on the workload | 58,320 allow/unknown, 765 content block (out-of-scope), 694 tracker block, 307 ad block |

**Profile-driven optimisation (decisions.md X-2).** First measurement
33,151/s (synthetic mix) → short index tokens + per-bucket prefilter
regex → 77,175/s; on the real workload 94,610/s → reuse of the default
decision, C-level token/key intersection, skipped empty indexes →
121,925/s; final recorded run 127,985/s. Decision counts and ruleset id
were identical before and after every step.

The end-to-end figure (81.7k/s) includes URL parsing and the
registrable-domain lookup and is below 100k; the gate as defined
(in-process decisions) is met. In production the admission service builds
one input per link and the first-party host once per page.

## 2. Gate E — false-positive guard and precision/recall

**Corpus.** `tests/fixtures/filtering/labeled_corpus.json`, 346 items,
labels fixed before evaluation: 161 sampled (same-site links and observed
third-party requests of the W691 capture) + 185 hand-written (`synthetic`)
media, player, ad, tracker, redirect, social and adversarial URLs.

**First evaluation (importer p6-abp/v1): guard FAILED** — 2 violations:
`https://moviesda17.com/ad/leo-2023/` (EasyList `.com/ad/`) and
`https://adserver.hotshare.click/file/x.mp4` (EasyList `/adserver.`).
Cause: untyped URL patterns applied to page documents, contrary to ABP
semantics (X-3). **After the fix (p6-abp/v2): guard PASSED**, 0 violations.

| Class | Support | Precision | Recall | Blocked | Note |
|---|---:|---:|---:|---:|---|
| ad | 35 | 0.912 | 0.886 | 35 | 4 ad items classified tracker (still blocked) |
| tracker | 27 | 0.867 | 0.963 | 26 | |
| content | 196 | 1.000 | 0.051 | 10 | only the 10 out-of-scope social/reference links are asserted (explicit policy) |
| media | 30 | — | 0.000 | 0 | no positive rules by design; 2 labelled `ad` by the 0.6 host-label hint, not blocked |
| player | 24 | — | 0.000 | 0 | no positive rules by design |
| navigation | 19 | — | 0.000 | 0 | < 20 items: not meaningful |
| unknown | 15 | — | — | 1 | a first-party `track.js` blocked by EasyPrivacy (labelled ambiguous) |

Explicit-policy blocks of protected items: 10, all `out_of_scope`
(`built_in`, V1 F-1 list) with provenance. Recall for ad/tracker is likely
optimistic: the hand-written endpoints are well-known ones.

## 3. Gate F/G — M1

Run 1 (2026-09-29 17:26 → 2026-09-30 13:59 UTC): Gate F passed; the
Gate G window reached 16.7 of 24 h before a terminal crash stopped M1.
Full numbers, resource slopes and the incident timeline:
[validation.md](validation.md) §4.1–§4.2;
raw: `benchmarks/p6-m1/results/m1-run1.json`.

Run 2 (started 2026-10-04 07:39:53 UTC, detached, same configuration):
in progress; at 5 h no process exit after the first two minutes, 0 dead
letters, http RSS flat at ~205 MB since 3 h; throughput fell from 8.1k to
3.2k completions/h as two NCBI hosts took most fetch time (X-15) and
Scholar 429s were retried (X-14). [validation.md](validation.md) §4.3.
