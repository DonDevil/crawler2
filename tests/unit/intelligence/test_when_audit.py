"""P7 WHEN readiness: the offline hub-revisit audit (when.md §4).

The audit is a benchmark tool, not a runtime component. These tests cover its
pure core: visit windows, site knowledge, novelty and role of new-to-hub
links, candidate events, crediting, the deterministic sample and the label
cards.
"""

from __future__ import annotations

import importlib.util
import random
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from crawler2.intelligence.what import (
    PageFacts,
    PageKind,
    PageKindResult,
    Relevance,
    RelevanceResult,
    SiteProfile,
    WhatResult,
)

ROOT = Path(__file__).parents[3]


def _load() -> ModuleType:
    path = ROOT / "benchmarks" / "p7-intelligence" / "when_audit.py"
    spec = importlib.util.spec_from_file_location("when_audit", path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["when_audit"] = mod
    spec.loader.exec_module(mod)
    return mod


W = _load()
HOST = "films.example"
B = f"https://{HOST}"
HUB = f"{B}/"
H = 3600.0


def profile(boilerplate: Iterable[str] = ()) -> SiteProfile:
    return SiteProfile(
        host=HOST,
        policy="what/v1",
        vocabulary="what-vocabulary/v1",
        pages=0,
        urls=0,
        templates=(),
        boilerplate_links=frozenset(boilerplate),
        title_boilerplate=frozenset(),
        script=None,
    )


def facts(url: str, links: Iterable[str]) -> PageFacts:
    return PageFacts(url=url, title="t", links=tuple((u, f"anchor {u[-12:]}") for u in links))


def analyzer(content: Iterable[str]) -> Any:
    """A WHAT stand-in: every link in ``content`` is a content link."""
    cset = set(content)

    def run(f: PageFacts) -> WhatResult:
        own = sorted({u for u, _ in f.links} & cset)
        return WhatResult(
            url=f.url,
            host=HOST,
            template=None,
            template_role=None,
            kind=PageKindResult(PageKind.HUB, ("many_page_links",), len(f.links), len(own)),
            relevance=RelevanceResult(Relevance.UNKNOWN, (), (), ()),
            content_links=tuple(own),
            policy="what/v1",
            vocabulary="what-vocabulary/v1",
        )

    return run


def obs(url: str, at_h: float, pgv: str, oid: str | None = None, requested: str = "") -> Any:
    return W.Obs(url, at_h * H, pgv, oid or f"{pgv}@{at_h}", requested or url)


def run_hub(
    hub_obs: list[Any],
    pages: dict[str, PageFacts],
    other_obs: Iterable[Any] = (),
    content: Iterable[str] = (),
    boilerplate: Iterable[str] = (),
    redirects: Iterable[str] = (),
    hub: str = HUB,
) -> list[Any]:
    links = {p: W.same_host_links(f) for p, f in pages.items()}
    known = W.first_known([*hub_obs, *other_obs], links)
    return list(
        W.hub_events(
            hub,
            hub_obs,
            pages,
            profile(boilerplate),
            known,
            W.first_known_keys(known),
            set(redirects),
            analyze=analyzer(content),
        )
    )


def film(n: int) -> str:
    return f"{B}/film-{n}-2026/"


# --------------------------------------------------------------------------- windows


def test_windows_merge_short_gaps_and_ignore_duplicates_and_order() -> None:
    o = [obs(HUB, t, f"v{t}") for t in (0, 1, 5.9, 6, 7, 30)]
    shuffled = [*o, o[2], o[0]]
    random.Random(7).shuffle(shuffled)  # noqa: S311 - deterministic test data
    wins = W.windows(shuffled)
    assert [[x.at / H for x in w] for w in wins] == [[0, 1, 5.9], [6, 7], [30]]
    assert W.windows([]) == []


def test_first_known_times_links_by_the_first_observation_of_their_page_version() -> None:
    a, b = obs(HUB, 10, "va"), obs(HUB, 2, "va", oid="early")
    other = obs(f"{B}/x/", 5, "vx")
    links = {"va": frozenset({film(1)}), "vx": frozenset({film(1), film(2)})}
    for order in ([a, b, other], [other, b, a]):
        known = W.first_known(order, links)
        assert known[film(1)] == 2 * H
        assert known[film(2)] == 5 * H
        assert known[HUB] == 2 * H


# --------------------------------------------------------------------------- events


def test_empty_and_single_window_hubs_have_no_events() -> None:
    assert run_hub([], {}) == []
    pages = {"v1": facts(HUB, [film(1)])}
    assert run_hub([obs(HUB, 0, "v1"), obs(HUB, 2, "v1", oid="dup-fetch")], pages) == []


def test_hub_with_no_new_links_is_a_non_candidate_event() -> None:
    pages = {"v1": facts(HUB, [film(1)]), "v2": facts(HUB, [film(1)])}
    (e,) = run_hub([obs(HUB, 0, "v1"), obs(HUB, 30, "v2")], pages, content=[film(1)])
    assert e.new_to_hub == []
    assert not e.candidate
    assert e.changed  # a new page version without a new link
    assert e.interval_h == 30


def test_only_duplicate_links_and_duplicate_observations_give_no_candidates() -> None:
    pages = {"v1": facts(HUB, [film(1), film(1) + "#top", film(2)])}
    hub_obs = [obs(HUB, 0, "v1"), obs(HUB, 0, "v1"), obs(HUB, 40, "v1", oid="o2")]
    (e,) = run_hub(hub_obs, pages, content=[film(1), film(2)])
    assert e.new_to_hub == []
    assert not e.changed


def test_multiple_new_links_are_classified_by_novelty_and_role() -> None:
    old_elsewhere, fresh_elsewhere, fresh_here = film(1), film(2), film(3)
    spam = f"{B}/casino-bonus/"
    pages = {
        "v1": facts(HUB, [film(9)]),
        "v2": facts(HUB, [film(9), old_elsewhere, fresh_elsewhere, fresh_here, spam]),
        "a": facts(f"{B}/a/", [old_elsewhere]),
        "b": facts(f"{B}/b/", [fresh_elsewhere]),
    }
    other = [obs(f"{B}/a/", -1, "a"), obs(f"{B}/b/", 20, "b")]
    (e,) = run_hub(
        [obs(HUB, 0, "v1"), obs(HUB, 30, "v2")],
        pages,
        other,
        content=[film(9), old_elsewhere, fresh_elsewhere, fresh_here],
    )
    assert e.novelty == {
        old_elsewhere: "known_before",
        fresh_elsewhere: "new_since_prev",
        fresh_here: "first_seen_here",
        spam: "first_seen_here",
    }
    assert e.role[spam] == "other:none"
    assert e.candidates == [fresh_elsewhere, fresh_here]
    assert e.first_seen_here == [fresh_here]
    assert (e.appeared, e.disappeared) == (4, 0)


def test_links_seen_in_a_short_gap_refetch_belong_to_the_next_window_not_the_history() -> None:
    pages = {"v1": facts(HUB, []), "v1b": facts(HUB, []), "v2": facts(HUB, [film(1)])}
    hub_obs = [obs(HUB, 0, "v1"), obs(HUB, 7, "v1b"), obs(HUB, 8, "v2")]
    (e,) = run_hub(hub_obs, pages, content=[film(1)])
    assert e.candidates == [film(1)]


def test_redirect_boilerplate_and_variant_links_are_never_candidates() -> None:
    moved, nav, variant = film(1), f"{B}/genre/", film(2)
    pages = {
        "v1": facts(HUB, []),
        "v2": facts(HUB, [moved, nav, variant]),
        "o": facts(f"{B}/o/", [variant.rstrip("/").upper().replace("HTTPS://FILMS.EXAMPLE", B)]),
    }
    other = [obs(f"{B}/o/", -1, "o"), obs(f"{B}/new/", 20, "n", requested=moved)]
    pages["n"] = facts(f"{B}/new/", [])
    (e,) = run_hub(
        [obs(HUB, 0, "v1"), obs(HUB, 30, "v2")],
        pages,
        other,
        content=[moved, nav, variant],
        boilerplate=[nav],
        redirects=[moved],
    )
    assert e.role[moved] == "redirect"
    assert e.role[nav] == "boilerplate"
    assert variant in e.variant
    assert e.candidates == []


def test_observations_without_facts_or_of_another_url_are_ignored() -> None:
    pages = {"v1": facts(HUB, []), "v2": facts(HUB, [film(1)])}
    hub_obs = [
        obs(HUB, 0, "v1"),
        obs(HUB, 10, "missing"),
        obs(f"{B}/other/", 12, "v2"),
        obs(HUB, 30, "v2"),
    ]
    (e,) = run_hub(hub_obs, pages, content=[film(1)])
    assert (e.index, e.prev_start, e.start) == (2, 0, 30 * H)
    assert e.candidates == [film(1)]


@settings(max_examples=40, deadline=None)
@given(st.randoms(use_true_random=False), st.integers(0, 3))
def test_events_are_independent_of_observation_order_and_duplicates(
    rnd: random.Random, dups: int
) -> None:
    pages = {f"v{i}": facts(HUB, [film(j) for j in range(i + 1)]) for i in range(5)}
    base = [obs(HUB, 10 * i, f"v{i}") for i in range(5)]
    variant = [*base, *rnd.sample(base, dups)]
    rnd.shuffle(variant)
    content = [film(j) for j in range(5)]

    def rows(o: list[Any]) -> list[dict[str, Any]]:
        return [W.event_row(e, {}) for e in run_hub(o, pages, content=content)]

    assert rows(variant) == rows(base)
    assert len(rows(base)) == 4


# --------------------------------------------------------------------------- crediting


def test_every_hub_linking_a_new_url_is_credited_and_the_first_is_recorded() -> None:
    label = f"{B}/label/tamil/"
    pages = {
        "h1": facts(HUB, []),
        "h2": facts(HUB, [film(1)]),
        "l1": facts(label, []),
        "l2": facts(label, [film(1)]),
    }
    hub_obs = [obs(HUB, 0, "h1"), obs(HUB, 31, "h2")]
    lab_obs = [obs(label, 0, "l1"), obs(label, 30, "l2")]
    links = {p: W.same_host_links(f) for p, f in pages.items()}
    known = W.first_known([*hub_obs, *lab_obs], links)
    keys = W.first_known_keys(known)
    an = analyzer([film(1)])
    events = [
        *W.hub_events(HUB, hub_obs, pages, profile(), known, keys, set(), analyze=an),
        *W.hub_events(label, lab_obs, pages, profile(), known, keys, set(), analyze=an),
    ]
    by_hub = {e.hub: e for e in events}
    assert by_hub[label].first_seen_here == [film(1)]
    assert by_hub[HUB].novelty[film(1)] == "new_since_prev"
    assert by_hub[HUB].candidates == [film(1)]  # not only the first discoverer
    credit = W.credit_table(reversed(events))
    assert credit[film(1)].hubs == {HUB, label}
    assert credit[film(1)].events == 2
    assert credit[film(1)].first_hub == label


# --------------------------------------------------------------------------- sample and cards


def _rows(n_sites: int, per_site: int) -> list[dict[str, Any]]:
    out = []
    for s in range(n_sites):
        for i in range(per_site):
            out.append(
                {
                    "id": f"e{s}-{i}",
                    "hub": f"https://s{s}.example/h{i}/",
                    "site": f"s{s}.example",
                    "candidate": i % 3 == 0,
                    "interval_bucket": "3-7d" if i % 2 else "6-24h",
                }
            )
    return out


def test_sample_is_deterministic_capped_per_domain_and_has_negatives() -> None:
    rows = _rows(5, 40)
    a = W.sample_events(rows, n_candidates=20, n_negatives=20)
    b = W.sample_events([*reversed(rows), rows[0]], n_candidates=20, n_negatives=20)
    assert a == b
    cands = [r for r in a if r["candidate"]]
    negs = [r for r in a if not r["candidate"]]
    assert (len(cands), len(negs)) == (20, 20)
    for side in (cands, negs):
        per = Counter(r["site"] for r in side)
        assert len(per) == 5
        assert max(per.values()) <= int(W.MAX_DOMAIN_SHARE * 20)
        assert {r["interval_bucket"] for r in side} == {"3-7d", "6-24h"}
    assert {r["split"] for r in a} == {"dev", "holdout"}


def test_one_dominant_domain_cannot_fill_the_sample() -> None:
    rows = [r | {"candidate": True} for r in _rows(1, 100)] + _rows(2, 3)
    a = W.sample_events(rows, n_candidates=50, n_negatives=10)
    cands = Counter(r["site"] for r in a if r["candidate"])
    assert cands["s0.example"] == int(W.MAX_DOMAIN_SHARE * 50)
    assert sum(cands.values()) < 50  # reported as insufficient, not padded


def test_sample_excludes_held_out_hubs_and_keeps_sparse_domains() -> None:
    rows = [*_rows(4, 30), *_rows(1, 1)]
    rows[-1] = rows[-1] | {"id": "sparse", "site": "sparse.example", "hub": "https://sp/"}
    excluded = frozenset(r["hub"] for r in rows if r["site"] == "s0.example")
    picked = W.sample_events(rows, exclude=excluded)
    assert all(r["site"] != "s0.example" for r in picked)
    assert any(r["id"] == "sparse" for r in picked)


def test_event_rows_are_bounded() -> None:
    many = [film(i) for i in range(500)]
    pages = {"v1": facts(HUB, []), "v2": facts(HUB, many)}
    (e,) = run_hub([obs(HUB, 0, "v1"), obs(HUB, 30, "v2")], pages, content=many)
    row = W.event_row(e, {})
    assert row["candidates"] == 500
    assert len(row["candidate_links"]) == W.MAX_EVENT_LINKS
    assert len(row["new_since_prev_links"]) == W.MAX_EVENT_LINKS


def test_card_hides_the_candidate_decision_and_what_roles() -> None:
    pages = {"v1": facts(HUB, []), "v2": facts(HUB, [film(1), f"{B}/casino/"])}
    (e,) = run_hub([obs(HUB, 0, "v1"), obs(HUB, 30, "v2")], pages, content=[film(1)])
    row = W.event_row(e, {film(1): "relevant"}, {film(1): "Film 1 (2026)"})
    text = W.card(row)
    assert film(1) in text
    assert "Film 1 (2026)" in text
    for leaked in ("candidate", "content", "relevant", "first_seen_here", "other:"):
        assert leaked not in text


@pytest.mark.parametrize(("hours", "bucket"), [(6, "6-24h"), (24, "24-72h"), (100, "3-7d")])
def test_interval_buckets(hours: float, bucket: str) -> None:
    assert W.interval_bucket(hours) == bucket
    assert W.interval_bucket(1e6) == ">=7d"
