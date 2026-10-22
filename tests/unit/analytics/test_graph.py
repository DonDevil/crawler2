"""P13 offline graph (design §9), on the SYNTHETIC world."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from crawler2.analytics.graph import build_graph
from crawler2.analytics.query import ExportView
from crawler2.analytics.source import MemoryRowSource
from tests.unit.analytics.conftest import Exported, make_exporter
from tests.unit.analytics.world import BOUNDARY, C1, D2_DAY, V1, build


def _edges(path: str) -> list[dict[str, Any]]:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    rel = con.sql(
        f"SELECT edge_id, edge_type, src, dst, directed, weight, status, confidence, "
        f"source_dataset, source_keys FROM read_parquet('{path}/edges.parquet')"
    )
    return [dict(zip(rel.columns, r, strict=True)) for r in rel.fetchall()]


def _build(e: Exported, root: Path | None = None) -> dict[str, Any]:
    with ExportView(e.revision_dir) as v:
        return build_graph(v, root or e.root)


def test_site_edges_have_typed_directed_semantics(exported: Exported) -> None:
    g = _build(exported)
    assert g["outcome"] == "built"
    assert not any(g["checks"].values())
    edges = _edges(g["path"])
    by = {(x["edge_type"], x["src"], x["dst"]): x for x in edges}
    redirect = by[("site_redirects_to", "site:mirror.example", "site:site-a.example")]
    assert redirect["directed"]
    assert redirect["weight"] == 1
    assert redirect["source_dataset"] == "page_observations"
    assert redirect["source_keys"][0].startswith("obs_")
    assert ("site_redirects_to", "site:site-a.example", "site:mirror.example") not in by
    links = by[("site_links_to", "site:site-a.example", "site:site-b.example")]
    assert links["weight"] == 2
    assert all("#" in k for k in links["source_keys"])
    assert by[("site_embeds_media_from", "site:site-a.example", "site:cdn.example")]["weight"] == 3


def test_page_media_target_chain_keeps_verdicts_and_provenance(exported: Exported) -> None:
    edges = _edges(_build(exported)["path"])
    m1 = exported.ctx["m1"]
    chain = {x["edge_type"]: x for x in edges if x["src"] == f"match:{m1.match_id}"}
    assert chain["match_for_target"]["dst"] == f"target:{m1.target.target_id}"
    assert chain["match_of_content"]["dst"] == f"content:{m1.content_id}"
    assert chain["match_for_target"]["status"] == "confirmed"
    assert chain["match_for_target"]["confidence"] == pytest.approx(0.97)
    m2 = exported.ctx["m2"]
    probable = next(x for x in edges if x["src"] == f"match:{m2.match_id}")
    assert probable["status"] == "probable"
    attributed = [x for x in edges if x["edge_type"] == "evidence_attributes_page"]
    assert len(attributed) == 2
    assert {x["status"] for x in attributed} == {"content_verified"}
    has_content = [x for x in edges if x["edge_type"] == "media_has_content"]
    assert {x["dst"] for x in has_content} == {
        f"content:{m1.content_id}",
        f"content:{m2.content_id}",
    }


def test_unobserved_match_gets_no_fabricated_edge(tmp_path: Path) -> None:
    world, ctx = build()
    world.match(b"never-observed-content", ctx["alpha"])
    result = make_exporter(MemoryRowSource(world.tables), tmp_path).export(until=BOUNDARY)
    assert result.revision_dir is not None
    with ExportView(result.revision_dir) as v:
        g = build_graph(v, tmp_path)
    assert g["stats"]["matches_without_observed_content"] == 1
    edges = _edges(g["path"])
    orphan = [x for x in edges if x["edge_type"] == "match_of_content"]
    contents = {x["dst"] for x in edges if x["edge_type"] == "media_has_content"}
    assert len(orphan) == 3
    assert len([x for x in orphan if x["dst"] not in contents]) == 1


def test_shared_content_is_one_undirected_edge(tmp_path: Path) -> None:
    world, _ = build()
    world.page(
        "https://site-b.example/watch/11",
        at=D2_DAY,
        media_urls=[V1],
        content={V1: C1},
    )
    result = make_exporter(MemoryRowSource(world.tables), tmp_path).export(until=BOUNDARY)
    assert result.revision_dir is not None
    with ExportView(result.revision_dir) as v:
        edges = _edges(build_graph(v, tmp_path)["path"])
    shared = [x for x in edges if x["edge_type"] == "site_shares_content"]
    assert len(shared) == 1
    (s,) = shared
    assert (s["src"], s["dst"], s["directed"]) == (
        "site:site-a.example",
        "site:site-b.example",
        False,
    )
    assert s["source_keys"][0].startswith("cnt_")


def test_ids_are_stable_and_rebuild_is_unchanged(exported: Exported, tmp_path: Path) -> None:
    first = _build(exported)
    assert _build(exported)["outcome"] == "unchanged"
    other_root = tmp_path / "elsewhere"
    second = _build(exported, other_root)
    assert first["content_digest"] == second["content_digest"]
    ids = sorted(x["edge_id"] for x in _edges(first["path"]))
    assert ids == sorted(x["edge_id"] for x in _edges(second["path"]))
    assert len(ids) == len(set(ids))


def test_repeated_records_aggregate_into_one_weighted_edge(tmp_path: Path) -> None:
    world, _ = build()
    world.page(  # another redirect observation of the same site pair
        "https://site-a.example/watch/1",
        at=D2_DAY + timedelta(hours=5),
        links=["https://site-a.example/watch/3"],
        redirected_from="https://mirror.example/go2",
    )
    result = make_exporter(MemoryRowSource(world.tables), tmp_path).export(until=BOUNDARY)
    assert result.revision_dir is not None
    with ExportView(result.revision_dir) as v:
        edges = _edges(build_graph(v, tmp_path)["path"])
    redirects = [x for x in edges if x["edge_type"] == "site_redirects_to"]
    assert len(redirects) == 1
    assert redirects[0]["weight"] == 2
    assert redirects[0]["source_keys"] == sorted(redirects[0]["source_keys"])


def test_missing_inputs_skip_edge_types_explicitly(tmp_path: Path) -> None:
    world, _ = build()
    result = make_exporter(MemoryRowSource(world.tables), tmp_path).export(
        until=BOUNDARY, datasets=["page_observations", "urls"]
    )
    assert result.revision_dir is not None
    with ExportView(result.revision_dir) as v:
        g = build_graph(v, tmp_path)
    assert set(g["stats"]["edges_by_type"]) == {"site_redirects_to"}
    assert "page_has_media" in g["skipped_edge_types"]
    assert "site_shares_content" in g["skipped_edge_types"]


def test_nodes_reference_their_dimension_records(exported: Exported) -> None:
    g = _build(exported)
    con = duckdb.connect()
    nodes = {
        r[0]: r
        for r in con.execute(
            f"SELECT node_id, node_type, natural_id, label, known "
            f"FROM read_parquet('{g['path']}/nodes.parquet')"
        ).fetchall()
    }
    alpha = exported.ctx["alpha"]
    t = nodes[f"target:{alpha.ref.target_id}"]
    assert t[1] == "target"
    assert t[2] == str(alpha.ref.target_id)
    assert t[3].startswith("Protected")
    assert nodes["site:cdn.example"][4] is True
    assert g["stats"]["unknown_nodes"] == sum(1 for n in nodes.values() if not n[4])
