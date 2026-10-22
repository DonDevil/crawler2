"""P13 offline analytics (docs/phases/p13-analytics/design.md).

Durable store → bounded, validated Parquet exports → DuckDB reports, derived
graphs and the site/template clustering prototype. Nothing here runs in, or is
imported by, a crawler, extraction, media, matching, evidence or feedback path;
the only operational access is read-only scans of the crawler2 keyspace.
"""
