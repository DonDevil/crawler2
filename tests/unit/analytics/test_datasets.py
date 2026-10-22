"""P13 dataset catalog and mappers (design §3)."""

from __future__ import annotations

import itertools
import json
import uuid
from datetime import datetime

import pytest

from crawler2.analytics.datasets import (
    DATASETS,
    EXCLUDED,
    PAGE_OBSERVATIONS,
    SOURCES,
    URLS,
    Malformed,
    host_of,
    site_of,
    ts,
    wire,
)
from crawler2.analytics.source import MAX_TOKEN, MIN_TOKEN, token_ranges
from tests.unit.analytics.world import build


def test_every_mapper_returns_exactly_the_declared_columns() -> None:
    world, _ = build()
    seen = set()
    for source in SOURCES:
        for row in world.tables[source.table]:
            for d in source.datasets:
                try:
                    out = list(d.mapper({c: row.get(c) for c in source.columns}))
                except Malformed:
                    continue
                for r in out:
                    assert set(r) == set(d.column_names), d.name
                    assert all(r[k] is not None for k in d.key), d.name
                    seen.add(d.name)
    assert seen == set(DATASETS)


def test_catalog_is_consistent() -> None:
    for d in DATASETS.values():
        names = d.column_names
        assert len(set(names)) == len(names), d.name
        assert set(d.key) <= set(names), d.name
        assert set(d.sort) <= set(names), d.name
        assert d.time_column is None or d.time_column in names, d.name
        assert "date" not in names, "date is the partition column added by the exporter"
    tables = [s.table for s in SOURCES]
    assert len(set(tables)) == len(tables)
    excluded = {name.strip() for key in EXCLUDED for name in key.split(",")}
    assert not excluded & set(tables), "a table is both exported and excluded"


def test_wire_ids_and_timestamps() -> None:
    u = uuid.UUID("019e830e-1a00-712b-8710-f021bb485b63")
    assert wire("url", u) == "url_019e830e1a00712b8710f021bb485b63"
    assert wire("url", "url_019e830e1a00712b8710f021bb485b63") == wire("url", u)
    assert wire("url", None) is None
    with pytest.raises(Malformed):
        wire("url", "garbage")
    assert ts(datetime(2026, 6, 1, 12)) == "2026-06-01T12:00:00+00:00"  # driver: naive UTC
    assert ts("2026-06-01T12:00:00Z") == "2026-06-01T12:00:00+00:00"
    with pytest.raises(Malformed):
        ts("yesterday")


def test_host_and_site_normalization() -> None:
    assert host_of("https://WWW.Site-A.Example./watch/1") == "www.site-a.example"
    assert site_of("www.site-a.example") == "site-a.example"
    assert site_of("cdn.example.co.uk") == "example.co.uk"
    assert site_of("10.0.0.1") == "10.0.0.1"
    assert host_of("urn:synthetic:no-host") is None
    assert site_of(None) is None


def test_unknown_site_is_explicit_none() -> None:
    row = {
        "url_id": uuid.UUID(int=1),
        "url": "urn:x",
        "domain_id": uuid.UUID(int=2),
        "first_seen": datetime(2026, 6, 1),
        "last_observed_at": None,
        "last_status": None,
    }
    (out,) = URLS.mapper(row)
    assert out["host"] is None
    assert out["site"] is None
    assert out["url"] == "urn:x"


@pytest.mark.parametrize(
    "doc",
    ["{corrupt", "null", "[]", json.dumps({"observation_id": "obs_x"})],
)
def test_malformed_documents_are_rejected_not_repaired(doc: str) -> None:
    with pytest.raises(Malformed):
        list(PAGE_OBSERVATIONS.mapper({"observation_id": uuid.uuid4(), "doc": doc}))


def test_no_sensitive_fields_are_exported() -> None:
    names = {c for d in DATASETS.values() for c in d.column_names}
    for forbidden in ("body", "visible_text", "headers", "finalized_by", "reason", "actor"):
        assert forbidden not in names


def test_token_ranges_cover_the_ring_disjointly() -> None:
    for of in (1, 2, 3, 64, 4096):
        ranges = token_ranges(of)
        assert ranges[0][0] == MIN_TOKEN
        assert ranges[-1][1] == MAX_TOKEN
        assert all(a[1] + 1 == b[0] for a, b in itertools.pairwise(ranges))
        assert all(lo <= hi for lo, hi in ranges)
    with pytest.raises(ValueError, match="segments"):
        token_ranges(0)
