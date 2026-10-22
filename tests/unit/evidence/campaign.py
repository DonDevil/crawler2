"""The deterministic P12 fixture campaign, with hand-counted expected statistics.

Synthetic pages on reserved ``.example`` hosts; nothing is fetched.

| match | target | verdict   | pages carrying the content                                   |
|-------|--------|-----------|--------------------------------------------------------------|
| m1    | alpha  | confirmed | site-a /watch/1 (observed twice), site-a /watch/2            |
| m2    | beta   | probable  | mirror /e/9 (no raw snapshot archived)                       |
| m3    | alpha  | confirmed | none (unattributed)                                          |
| m4    | delta  | confirmed | other /x — delta is NOT in the campaign                      |

``m1`` is delivered twice. ``gamma`` is in the campaign but its
``target.registered`` never reached crawler2. ``m1`` is finalized complete,
``m2`` partial (explicitly allowed), ``m3`` cannot be finalized.
"""

from __future__ import annotations

from datetime import timedelta

from antipiracy_contracts.ids import EvidenceId
from antipiracy_contracts.models.matching import MatchVerdict

from crawler2.evidence.stats import CampaignData, load_campaign
from crawler2.evidence.store import Campaign
from tests.fixtures.contracts import T0, target
from tests.unit.evidence.fakes import World

D1, D2, D3, D4 = b"content-one", b"content-two", b"content-three", b"content-four"

EXPECTED = {
    "targets": {"count": 3, "registered": 2, "not_in_projection": 1},
    "matches": {"total": 3, "by_verdict": {"confirmed": 2, "probable": 1}},
    "matched_media": 2,
    "evidence": {
        "candidates": 3,
        "by_status": {"finalized": 2, "unattributed": 1},
        "finalized": 2,
        "finalized_by_status": {"complete": 1, "partial": 1},
        "superseded_versions": 0,
        "rejected": 0,
        "matches_without_candidate": 0,
    },
    "sources": {
        "source_records": 4,
        "pages": 3,
        "domains": 2,
        "pages_per_domain": {"mirror.example": 1, "site-a.example": 2},
        "by_basis": {"content_verified": 4},
    },
    "artifacts": {
        "page_snapshots_referenced": 3,
        "sources_without_snapshot": 1,
        "screenshots_not_collected": 3,
        "media_samples_not_retained": 3,
    },
}


def build(w: World) -> tuple[Campaign, dict[str, EvidenceId]]:
    alpha, beta, delta = w.register("alpha"), w.register("beta"), w.register("delta")
    gamma = target("gamma")
    w.page("https://site-a.example/watch/1", "v/1.mp4", D1)
    w.page("https://site-a.example/watch/1", "v/1.mp4", D1, at=T0 + timedelta(minutes=30))
    w.page("https://site-a.example/watch/2", "v/1.mp4", D1, at=T0 + timedelta(minutes=5))
    w.page("https://mirror.example/e/9", "v/2.mp4", D2, cdn="cdn2.example", snapshot=False)
    w.page("https://other.example/x", "v/4.mp4", D4)
    matches = {
        "m1": w.match(D1, alpha),
        "m2": w.match(D2, beta, verdict=MatchVerdict.PROBABLE, confidence=0.74),
        "m3": w.match(D3, alpha),
        "m4": w.match(D4, delta),
    }
    collector = w.collector()
    for name in ("m1", "m2", "m3", "m1", "m4"):  # m1 delivered twice
        collector.collect(matches[name])
    ids = {n: w.repo.opened(m.match_id)[0] for n, m in matches.items()}  # type: ignore[index]
    finalizer = w.finalizer()
    finalizer.finalize(ids["m1"], actor="fixture")
    finalizer.finalize(ids["m2"], allow_partial=True, actor="fixture")
    campaign = Campaign(
        campaign_id="fixture-case",
        name="P12 fixture campaign (synthetic)",
        since=T0,
        created_at=T0,
        target_ids=tuple(
            sorted((alpha.ref.target_id, beta.ref.target_id, gamma.ref.target_id), key=str)
        ),
    )
    w.store.create_campaign(campaign)
    return campaign, ids


def data(w: World, campaign: Campaign) -> CampaignData:
    return load_campaign(
        campaign,
        store=w.store,
        index=w.repo,
        projections=w.projections,
        until=T0 + timedelta(days=2),
    )
