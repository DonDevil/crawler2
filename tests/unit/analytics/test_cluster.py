"""P13 site/template clustering prototype (design §10).

Feature and clustering tests use hand-built SYNTHETIC ``PageFeatures``; the end-to-end
test runs on the SYNTHETIC export. No result here says anything about real sites.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from crawler2.analytics import cluster
from crawler2.analytics.cluster import (
    ClusterParams,
    PageFeatures,
    cluster_sites,
    jaccard,
    run_clustering,
    site_features,
    template_groups,
)
from crawler2.analytics.export import ExportError
from crawler2.analytics.query import ExportView
from tests.unit.analytics.conftest import Exported


def _pages(
    site: str,
    n: int,
    *,
    template: str = "watch",
    og: str | None = "video.movie",
    text: int | None = 900,
    kinds: tuple[str, ...] = ("video_file",),
    struct: str | None = "s1",
) -> list[PageFeatures]:
    return [
        PageFeatures(
            site=site,
            url=f"https://{site}/{template}/{i}",
            og_type=og,
            lang="en",
            text_chars=text,
            anchors=20,
            same_site_anchors=15,
            media_kinds=kinds,
            media_methods=("video_element",) if kinds else (),
            structural=struct,
        )
        for i in range(n)
    ]


P = ClusterParams(min_pages=3, min_similarity=0.6, min_samples=2)


def test_site_features_use_persisted_page_facts() -> None:
    f = site_features(_pages("a.example", 4), P)
    assert not f.insufficient
    assert f.pages == 4
    assert {
        "tpl:/{slug}/{num}",
        "og:video.movie",
        "lang:en",
        "mkind:video_file",
        "mmethod:video_element",
        "media_pages:q4",
        "internal_links:q4",
        "struct_reuse:q4",
    } <= f.tokens
    assert any(t.startswith("text:2^") for t in f.tokens)
    assert f.templates == {"/{slug}/{num}": 4}


def test_missing_features_do_not_crash_or_invent_tokens() -> None:
    f = site_features(_pages("a.example", 3, og=None, text=None, kinds=(), struct=None), P)
    assert not any(t.startswith(("og:", "mkind:", "mmethod:")) for t in f.tokens)
    assert "text:0" in f.tokens
    assert "struct_reuse:0" in f.tokens
    assert "media_pages:0" in f.tokens


def test_insufficient_data_is_never_clustered() -> None:
    pages = _pages("a.example", 4) + _pages("b.example", 4) + _pages("tiny.example", 2)
    _, assignments, clusters = cluster_sites(pages, P)
    tiny = next(a for a in assignments if a["site"] == "tiny.example")
    assert tiny["status"] == "insufficient_data"
    assert tiny["cluster_id"] is None
    assert all("tiny.example" not in c["members"] for c in clusters)


def test_similar_sites_cluster_and_unrelated_ones_stay_noise() -> None:
    pages = (
        _pages("a.example", 5)
        + _pages("b.example", 6)
        + _pages("c.example", 4)
        + _pages(
            "odd.example",
            5,
            template="forum/thread",
            og="article",
            text=50_000,
            kinds=(),
            struct=None,
        )
    )
    _, assignments, clusters = cluster_sites(pages, P)
    status = {a["site"]: (a["status"], a["cluster_id"]) for a in assignments}
    assert status["odd.example"] == ("noise", None)
    (c,) = clusters
    assert c["members"] == ["a.example", "b.example", "c.example"]
    assert c["cluster_id"].startswith("c-")
    assert status["a.example"] == ("assigned", c["cluster_id"])
    assert "tpl:/{slug}/{num}" in c["shared_features"]
    assert c["min_similarity"] is not None
    assert c["min_similarity"] >= P.min_similarity


def test_clustering_is_deterministic_and_ids_are_content_derived() -> None:
    pages = _pages("a.example", 5) + _pages("b.example", 5) + _pages("c.example", 5)
    first = cluster_sites(pages, P)
    second = cluster_sites(list(reversed(pages)), P)
    assert first[1] == second[1]
    assert first[2] == second[2]
    more = cluster_sites(pages + _pages("d.example", 5), P)[2]
    assert more[0]["cluster_id"] != first[2][0]["cluster_id"]  # different membership


def test_min_samples_prevents_pairs_from_forming_clusters() -> None:
    pages = _pages("a.example", 5) + _pages("b.example", 5)
    strict = ClusterParams(min_pages=3, min_similarity=0.6, min_samples=3)
    _, assignments, clusters = cluster_sites(pages, strict)
    assert clusters == []
    assert {a["status"] for a in assignments} == {"noise"}


def test_max_sites_refuses_instead_of_sampling() -> None:
    pages = [p for i in range(4) for p in _pages(f"s{i}.example", 3)]
    with pytest.raises(ExportError, match="max_sites"):
        cluster_sites(pages, ClusterParams(min_pages=3, max_sites=3))


def test_params_validation_and_jaccard() -> None:
    with pytest.raises(ValueError, match="min_samples"):
        ClusterParams(min_samples=1)
    with pytest.raises(ValueError, match="min_similarity"):
        ClusterParams(min_similarity=0)
    assert jaccard(frozenset(), frozenset()) == 0.0
    assert jaccard(frozenset({"a", "b"}), frozenset({"b", "c"})) == pytest.approx(1 / 3)


def test_template_groups_report_structural_agreement() -> None:
    pages = _pages("a.example", 3) + _pages("a.example", 1, template="zzq", struct="s2")
    groups = {g["url_template"]: g for g in template_groups(pages)}
    assert groups["/{slug}/{num}"]["pages"] == 4  # /zzq/0 shares the shape
    assert groups["/{slug}/{num}"]["distinct_structural_hashes"] == 2
    assert groups["/{slug}/{num}"]["dominant_structural_share"] == 0.75


def test_end_to_end_outputs_are_versioned_and_unreviewed(exported: Exported) -> None:
    params = ClusterParams(min_pages=1, min_similarity=0.5, min_samples=2)
    with ExportView(exported.revision_dir) as v:
        result = run_clustering(v, exported.root, params)
        assert run_clustering(v, exported.root, params)["outcome"] == "unchanged"
    out = Path(result["path"])
    assert out.name == f"cluster-v1-{params.digest}"
    assert out.parent.name == "rev-0001"
    assert exported.root in out.parents
    manifest = json.loads((out / "_manifest.json").read_text())
    assert manifest["feature_set"] == "site-features/v1"
    assert manifest["algorithm"] == "dbscan-jaccard/v1"
    assert manifest["params"]["min_pages"] == 1
    assert manifest["bootstrap_status"] == "unreviewed"
    assert manifest["export"]["content_id"] == exported.result.manifest["content_id"]
    assert "not evidence of common ownership" in manifest["disclaimer"]
    boot = json.loads((out / "bootstrap-candidates.json").read_text())
    assert boot["status"] == "unreviewed"
    assert all(
        c["review"] == {"decision": None, "reviewer": None, "note": None} for c in boot["clusters"]
    )
    names = {f["path"] for f in manifest["files"]}
    assert names == {
        "site_features.parquet",
        "site_clusters.parquet",
        "clusters.parquet",
        "template_groups.parquet",
        "bootstrap-candidates.json",
    }


def test_clustering_cannot_touch_p7_runtime_state() -> None:
    tree = ast.parse(Path(cluster.__file__).read_text())
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names}
    assert "crawler2.intelligence.what" in imported  # pure URL tokenizer only
    forbidden = (
        "crawler2.intelligence.store",
        "crawler2.intelligence.feedback",
        "crawler2.intelligence.advice",
        "crawler2.storage",
        "crawler2.frontier",
        "crawler2.discovery",
    )
    assert not {m for m in imported if m.startswith(forbidden)}
