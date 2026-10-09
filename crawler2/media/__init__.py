"""P8 media registry & identity, levels 1-3 (docs/phases/p08-media-registry/design.md).

- ``resolver``: ``media.discovered`` (read from the outbox, ADR-024) →
  ``MediaObservation`` + ``media.observed``, decided once per observation;
- ``probe`` / ``hashing``: level-2 hints and the level-3 ``full-bytes/v1``
  content key, behind the SSRF guard (``ssrf``);
- ``encode``: at most one logical ``encode.requested`` per (content, spec),
  and the ``representation_status`` projection.

Nothing in the crawl loop depends on this package (B.5 #1).
"""
