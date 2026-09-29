# P5 Benchmarks — parse/extraction CPU per page vs V1

Gate (plan P5): **parse CPU/page ≤ 50 % of V1.** Result: **PASSED — V2 =
17.9 % of V1** (median of 3 rounds, same bytes, same session).
Raw results: `benchmarks/p5-extraction/results/20260929T133057Z/`
(`gate.json`, per-round files with per-page samples keyed by body digest,
`environment.txt`). Scripts: `benchmarks/p5-extraction/`.

## 1. Workload

- **Source:** the HTML documents of the 691 W691 URLs (the fixed P4 gate
  workload, `benchmarks/p4-fetch/w691.txt`), captured on 2026-09-29 at
  12:52 UTC with the real P4 runtime (`capture.py`: P3 frontier, http pool
  50 slots + browser pool 2 pages, 0.3 s politeness, media probe-only).
  Bodies are in git-ignored `var/p5-corpus/capture-1/`, never committed.
- **Pages:** every 2xx response with an HTML content type: **639** (663
  bodies of any kind; 28 URLs gave no body).
- **Sizes:** median 164 KiB, p95 303 KiB, max 1 067 KiB.
- **Character:** portal, listing and article pages; heavy inline scripts
  (529/639 with JSON-LD); almost no player markup (V1 and V2 both find 0
  media references). Not selected or altered to favour either system.

## 2. What is timed

| | V1 | V2 |
|---|---|---|
| call | `HTMLLinkExtractor().extract_content(html, url)` — what every V1 crawler calls per page | `extract(body, page=, content_type=)` — the full P5 pipeline |
| work | BeautifulSoup(lxml) parse **twice**, links (with V1's URL cleaning/policy), media, text regex | charset decode, **one** selectolax parse, links, media, metadata, script/text literals, all six hashes |
| input | already-decoded `str` (decode not timed) | raw bytes (decode timed) |
| config | as deployed: blacklist enabled (a scratch copy, because `clean_url` writes to it); loguru sinks removed (no log I/O charged) | defaults |
| runtime | V1 venv: Python 3.12.3, bs4 4.15.0, lxml 6.1.2 | Python 3.12.3, selectolax 0.4.13 |

Excluded from both: network, storage, snapshot upload, database writes,
browser rendering. V2 does strictly more work than V1 here, so the
comparison is conservative for V2.

## 3. Method

`run.sh parse var/p5-corpus/capture-1 3`: three rounds, each running V1
then V2 in separate processes on the idle host. Per process: one warm-up
pass over all pages, then 5 measured passes; `time.process_time()` around
each call (CPU time, not wall time). Per-page value = mean over the 5
passes; aggregate = total CPU / (5 × pages). The gate uses the median over
rounds of V2/V1 aggregate CPU per page.

Hardware: Intel i5-11400H (6C/12T, 0.8–4.5 GHz, frequency scaling on),
15.7 GB RAM, Ubuntu 24.04 kernel 7.0.0-34, single-threaded measurement.

## 4. Results

| Round | V1 aggregate ms/page | V1 median / p95 | V2 aggregate ms/page | V2 median / p95 | V2/V1 aggregate | V2/V1 per-page median | worst page ratio |
|---|---|---|---|---|---|---|---|
| 1 | 53.17 | 41.71 / 108.74 | 9.72 | 7.75 / 19.23 | 0.183 | 0.197 | 0.686 |
| 2 | 53.31 | 42.10 / 109.49 | 9.52 | 7.61 / 18.94 | 0.179 | 0.193 | 0.676 |
| 3 | 53.03 | 41.45 / 110.30 | 9.47 | 7.61 / 18.69 | 0.179 | 0.193 | 0.676 |

**Gate: 0.179 ≤ 0.5 → PASSED.** No page is slower in V2 than in V1.

Informational: V2 decode + parse alone costs 0.98 ms/page; the rest is the
Python tree walk, URL canonicalization (P1 `canonicalize_url` + ID
derivation, ≈ 70 references per page) and hashing. V2 reports 45 236 links
against V1's 23 716 because V1 drops links by policy (relevance, external
budget of 10, blacklist, trap heuristics) while V2 records every valid link
as a fact (P6/P7 decide later). No optimization beyond the design was
needed.

## 5. Runs not used

A provisional 2-round run (V2/V1 0.185 and 0.162) overlapped a 46 s test
run on the same host and was discarded; the numbers above come from a
clean rerun. Both agree on the verdict.
