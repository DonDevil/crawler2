"""P13 standard reports from Parquet alone (design §8), on the SYNTHETIC world."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from crawler2.analytics.query import DatasetUnavailableError, ExportView
from crawler2.analytics.reports import campaign_report, render_markdown, source_report
from crawler2.analytics.source import MemoryRowSource
from tests.unit.analytics.conftest import Exported, make_exporter
from tests.unit.analytics.world import BOUNDARY, build

CAMPAIGN = "synthetic-case"


def test_campaign_report_matches_the_hand_count(exported: Exported) -> None:
    with ExportView(exported.revision_dir) as v:
        r = campaign_report(v, CAMPAIGN)
    assert r["complete"]
    assert r["incomplete_reasons"] == []
    assert r["targets"] == {
        "in_campaign": 3,
        "in_projection": 2,
        "not_in_projection": 1,
        "retired": 0,
    }
    m = r["matches"]
    assert (m["reported"], m["confirmed_by_matcher"], m["by_verdict"]) == (
        2,
        1,
        {"confirmed": 1, "probable": 1},
    )
    assert m["by_day"] == [{"date": "2026-06-03", "reported": 2, "confirmed": 1}]
    assert r["media"] == {"carrying_matched_content": 2, "observations_of_matched_content": 4}
    assert r["sources"] == {
        "pages": 3,
        "sites": 2,
        "pages_per_site": {"mirror.example": 1, "site-a.example": 2},
        "matches_without_observed_content": 0,
    }
    e = r["evidence"]
    assert e["candidates"] == 2
    assert e["by_status"] == {"finalized": 1, "incomplete": 1}
    assert e["finalized_packages"] == 1
    assert e["latest_version_by_status"] == {"complete": 1}
    assert e["superseded_versions"] == 0
    assert e["matches_without_candidate"] == 0
    assert r["rates"]["candidate_coverage"] == {"numerator": 2, "denominator": 2, "value": 1.0}
    assert r["rates"]["finalized_share"] == {"numerator": 1, "denominator": 2, "value": 0.5}
    assert set(r["definitions"]) >= {"matches.reported", "matches.confirmed_by_matcher"}


def test_source_report_separates_lifecycle_states(exported: Exported) -> None:
    with ExportView(exported.revision_dir) as v:
        r = source_report(v)
    sites = {s["site"]: s for s in r["sites"]}
    a = sites["site-a.example"]
    assert (a["urls_discovered"], a["fetch_attempts"], a["observations"]) == (2, 3, 4)
    assert (a["observations_ok"], a["pages_fetched_ok"], a["pages_parsed"]) == (4, 2, 2)
    assert (a["media_discovered"], a["media_observed"], a["media_identified"]) == (1, 1, 1)
    assert a["matched_contents_seen"] == 1
    assert a["evidence_sources"] == {"content_verified": 2}
    assert a["rates"]["response_rate"] == {"numerator": 3, "denominator": 3, "value": 1.0}
    unknown = sites["(unknown site)"]
    assert unknown["urls_discovered"] == 1
    assert unknown["rates"]["ok_rate"]["value"] is None
    assert r["totals"]["urls_discovered"] == 8
    assert r["data_quality"]["page_observations"]["rejected"] == 1
    days = {(d["site"], d["date"]): d["observations"] for d in r["daily"]}
    assert days[("site-a.example", "2026-06-01")] == 2  # /watch/1 and /watch/2 on day 1
    one = source_report(ExportView(exported.revision_dir), site="site-b.example")
    assert [s["site"] for s in one["sites"]] == ["site-b.example"]


def test_reports_are_reproducible_across_independent_exports(tmp_path: Path) -> None:
    digests = []
    for name in ("a", "b"):
        world, _ = build()
        result = make_exporter(MemoryRowSource(world.tables), tmp_path / name).export(
            until=BOUNDARY
        )
        assert result.revision_dir is not None
        with ExportView(result.revision_dir) as v:
            digests.append(
                (campaign_report(v, CAMPAIGN)["report_digest"], source_report(v)["report_digest"])
            )
    assert digests[0] == digests[1]


def test_report_names_the_revision_it_used(exported: Exported) -> None:
    with ExportView(exported.revision_dir) as v:
        r = campaign_report(v, CAMPAIGN)
    ref = r["export"]
    assert ref["content_id"] == exported.result.manifest["content_id"]
    assert ref["revision"] == 1
    assert ref["datasets"]["matches"]["schema_version"] == 1
    assert "report_digest" in render_markdown(r)


def test_incomplete_input_is_reported_not_ignored(tmp_path: Path) -> None:
    world, _ = build()
    del world.tables["evidence_versions"]
    del world.tables["media_observations"]
    result = make_exporter(MemoryRowSource(world.tables), tmp_path).export(until=BOUNDARY)
    assert result.revision_dir is not None
    with ExportView(result.revision_dir) as v:
        r = campaign_report(v, CAMPAIGN)
        s = source_report(v)
    assert not r["complete"]
    assert any(x.startswith("evidence_versions:") for x in r["incomplete_reasons"])
    assert r["media"] is None
    assert r["sources"] is None
    assert "finalized_packages" not in r["evidence"]
    assert "finalized_share" not in r["rates"]
    assert not s["complete"]
    assert all(row["media_observed"] == 0 for row in s["sites"])


def test_missing_scope_dataset_or_campaign_fails_clearly(tmp_path: Path) -> None:
    world, _ = build()
    result = make_exporter(MemoryRowSource(world.tables), tmp_path).export(
        until=BOUNDARY, datasets=["urls"]
    )
    assert result.revision_dir is not None
    with ExportView(result.revision_dir) as v:
        with pytest.raises(DatasetUnavailableError, match="campaigns"):
            campaign_report(v, CAMPAIGN)
        assert not source_report(v)["complete"]
    with ExportView(result.revision_dir) as v, pytest.raises(LookupError):
        source_report(v, site="nowhere.example")


def test_unknown_campaign_is_a_lookup_error(exported: Exported) -> None:
    with ExportView(exported.revision_dir) as v, pytest.raises(LookupError, match="nope"):
        campaign_report(v, "nope")


def test_empty_export_gives_zero_counts_and_null_rates(tmp_path: Path) -> None:
    world, _ctx = build()
    for t in world.tables:
        if t != "evidence_campaigns":
            world.tables[t] = []
    result = make_exporter(MemoryRowSource(world.tables), tmp_path).export(until=BOUNDARY)
    assert result.revision_dir is not None
    with ExportView(result.revision_dir) as v:
        r = campaign_report(v, CAMPAIGN)
    assert r["complete"]
    assert r["matches"]["reported"] == 0
    assert r["rates"]["candidate_coverage"]["value"] is None
    assert r["rates"]["finalized_share"]["value"] is None


def test_reports_need_no_operational_store(exported: Exported) -> None:
    """Run in a fresh interpreter where the Scylla and Redis drivers cannot be imported."""
    code = textwrap.dedent(
        f"""
        import sys
        for mod in ("cassandra", "redis"):
            sys.modules[mod] = None
        from pathlib import Path
        from crawler2.analytics.query import ExportView
        from crawler2.analytics.reports import campaign_report, source_report
        with ExportView(Path({str(exported.revision_dir)!r})) as v:
            print(campaign_report(v, {CAMPAIGN!r})["complete"], len(source_report(v)["sites"]))
        """
    )
    out = subprocess.run(  # noqa: S603 -- our own interpreter and code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "True 5"
