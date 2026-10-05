# M1 run 2 — mid-run diagnostics

Computed from the live Redis streams during the run (UTC). The streams
retain only their newest entries (10,000, then 3,000 from 17:04 UTC), so
these breakdowns cannot be recomputed afterwards. The monitor's per-sample
tallies (`samples.jsonl.gz`, key `fetch`) have no host dimension.

## Blocked fetches by host (X-14)

Last 3,000 `fetch.completed` entries at 09:10 UTC (1.5 h):

| Host | blocked | of attempts | status |
|---|---:|---:|---|
| scholar.google.com | 526 | 526 | 429 |
| grants.nih.gov | 20 | 20 | 403 |
| nlmdirector.nlm.nih.gov | 4 | 146 | 403 |
| www.nih.gov | 4 | 4 | 403 |
| (total) | 563 | 3,000 | |

At 10:10 UTC (2.5 h), 4xx responses in the last 3,000 entries: 801, of which
`scholar.google.com` 429 ×737 (all of its 737 attempts = 25 % of fetches),
`collections.nlm.nih.gov` 403 ×41, `www.moviesda.business` 404 ×7,
`isaimini.com.in` 404 ×2, single 401/404/405 from NLM hosts.

## Fetch time by outcome and host (X-15)

Last 3,000 entries at 11:10 UTC (3.5 h), share of summed fetch duration:

| capability, outcome | fetches | seconds | share | mean s |
|---|---:|---:|---:|---:|
| http, response | 2,095 | 3,852 | 61 % | 1.8 |
| http, too_large | 174 | 1,008 | 16 % | 5.8 |
| http, blocked | 615 | 488 | 8 % | 0.8 |
| browser, response | 67 | 461 | 7 % | 6.9 |
| http, timeout | 22 | 333 | 5 % | 15.1 |
| http, tls_failure | 16 | 108 | 2 % | 6.7 |

`too_large` hosts: `ftp.ncbi.nlm.nih.gov` 168, `ftp.uniprot.org` 6.

Oldest vs newest 2,000 retained entries at 11:40 UTC:

| Entries | finished | mean fetch | top hosts by fetch time (s) |
|---|---|---:|---|
| oldest 2,000 | 10:12–10:26 | 1.43 s | ftp.ncbi 1,175 · scholar.google 377 · pubmed.ncbi 330 · www.ncbi 297 · datadiscovery.nlm 240 |
| newest 2,000 | 11:16–11:39 | 2.80 s | www.ncbi 2,468 · ftp.ncbi 2,273 · scholar.google 379 · pubmed.ncbi 236 · pmc.ncbi 108 |

Frontier at the same time: all 4 http slots leased, 5,081 → 5,541 eligible
domains over the first 4 h, iowait 9 % — throughput fell because fetches
got slower, not because anything was saturated.

## Redis memory (X-16)

At 15:46 UTC (Redis 757 MB of 768 MB, `noeviction`): stream sizes
(`MEMORY USAGE`): `urls.discovered` 10,000 entries 406.4 MB,
`page.observed` 10,000 33.2 MB, `fetch.completed` 10,002 25.3 MB,
`page.changed` 10,000 19.9 MB, `media.discovered` 359 1.4 MB; frontier
199,881 task hashes + 5,594 per-domain queues.

Extraction group after the replay: lag constant 1,858 from 17:40 UTC
(8 before the stall) = ~1,850 `page.observed` entries trimmed unread.

At 20:35 UTC (Redis 651 MB, cap 3,000): `urls.discovered` 3,000 entries
346.6 MB (~115 KB each). Newest 400 entries: 25 MB, of which
`account.ncbi.nlm.nih.gov` 5 pages 21 MB (~5,185 links per page),
`www.ncbi.nlm.nih.gov` 172 pages 3 MB (58 links/page),
`pmc.ncbi.nlm.nih.gov` 80 pages 1 MB, `pubmed.ncbi.nlm.nih.gov` 134 pages
(1 link/page), `www.uniprot.org` 2 pages (152 links/page). Redis peaked at
653.8 MB at 23:57 UTC and fell back as these entries rotated out.
