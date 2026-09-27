# ADR-005 — Legal / compliance posture

Status: **OPEN** — no legal requirements have been supplied. Nothing below
is a legal requirement; it lists the questions that must be answered.

## Why it is open

Evidence capture, crawling policy and Tor usage have legal implications
that depend on jurisdiction and on how evidence will be used. These must
come from the project owner / counsel, not be invented by engineering.

## Questions to be answered **before P12 (evidence) design starts**

1. Jurisdictions in which evidence will be used; admissibility standards
   (chain of custody, timestamping, hashing, signing).
2. What evidence is captured: HTML, HTTP metadata, screenshots, video
   clips/segments, manifests; any limits on downloading copyrighted media.
3. Retention periods and deletion obligations for evidence and raw crawl
   data; object-lock / WORM requirements (affects ADR-003 bucket setup).
4. Crawling policy: robots.txt, rate limits, identification (User-Agent),
   authenticated/paywalled content.
5. Tor / dark-web crawling: permitted scope and operational constraints.
6. Personal data encountered in crawled content (e.g. GDPR/DPDP
   obligations).
7. Licensing obligations of infrastructure (e.g. MinIO AGPLv3, ADR-003).

## Engineering stance until closed

- Earlier phases retain the *hooks* P12 needs (plan B.5 #6: FetchResult
  metadata, redirect chains, raw snapshots, hashes, worker identity), so
  answers can be applied without re-crawling.
- No component may delete raw data irreversibly on a policy that has not
  been agreed here.
