"""Campaign (case) coverage and source statistics (design §9).

Derived on demand from authoritative and projected rows; nothing is stored, so
the numbers are always rebuildable. Deduplication rules:

- a match counts once per ``match_id`` (redelivered events upsert one row);
- an evidence item counts once per ``evidence_id`` (E1 gives one per match);
- a page counts once per canonical page URL, a domain once per host, however
  many URLs, observations or matches it has;
- an artifact counts once per (page observation, digest).

What cannot be scoped to a campaign from persisted data (pages examined: page
observations are target-independent by design; media discovered overall) is
reported as unavailable with the reason, never estimated.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from antipiracy_contracts.ids import EvidenceId, TargetId

from crawler2.evidence.manifest import ts
from crawler2.evidence.store import Campaign, EvidenceStore, FinalizedVersion, StoredCandidate
from crawler2.storage.repositories import EvidenceSummary, MatchSummary, TargetState

MAX_MONTHS = 36

UNAVAILABLE = {
    "pages_examined": "page observations are target-independent (P1 design); crawled pages "
    "cannot be attributed to a campaign, only pages that carried matched media",
    "media_discovered": "the media registry is not partitioned by target; only media carrying "
    "matched content is counted (matched_media)",
}


class TargetIndex(Protocol):
    """E4 (``EvidenceRepository.for_target``)."""

    def for_target(self, target_id: TargetId, month: datetime) -> list[EvidenceSummary]: ...


class CampaignProjections(Protocol):
    def target(self, target_id: TargetId) -> TargetState | None: ...

    def matches_for_target(self, target_id: TargetId, month: datetime) -> list[MatchSummary]: ...


def months(since: datetime, until: datetime) -> list[datetime]:
    """First instant of every UTC month from ``since`` to ``until`` (at most MAX_MONTHS)."""
    cur = datetime(since.year, since.month, 1, tzinfo=UTC)
    end = datetime(until.year, until.month, 1, tzinfo=UTC)
    found: list[datetime] = []
    while cur <= end and len(found) < MAX_MONTHS:
        found.append(cur)
        cur = datetime(cur.year + cur.month // 12, cur.month % 12 + 1, 1, tzinfo=UTC)
    return found


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    evidence_id: EvidenceId
    candidate: StoredCandidate | None
    versions: tuple[FinalizedVersion, ...]
    """Newest first."""

    @property
    def latest(self) -> FinalizedVersion | None:
        return self.versions[0] if self.versions else None


@dataclass(frozen=True, slots=True)
class CampaignData:
    campaign: Campaign
    window: tuple[datetime, ...]
    targets: dict[str, TargetState | None]
    matches: dict[str, MatchSummary]
    evidence: tuple[EvidenceItem, ...]
    """Sorted by evidence id."""


def load_campaign(
    campaign: Campaign,
    *,
    store: EvidenceStore,
    index: TargetIndex,
    projections: CampaignProjections,
    until: datetime,
) -> CampaignData:
    window = tuple(months(campaign.since, until))
    targets = {str(t): projections.target(t) for t in campaign.target_ids}
    matches: dict[str, MatchSummary] = {}
    evidence_ids: set[str] = set()
    for t in campaign.target_ids:
        for month in window:
            for m in projections.matches_for_target(t, month):
                matches[str(m.match_id)] = m
            for e in index.for_target(t, month):
                evidence_ids.add(str(e.evidence_id))
    items = []
    for raw in sorted(evidence_ids):
        eid = EvidenceId(raw)
        items.append(EvidenceItem(eid, store.candidate(eid), tuple(store.versions(eid))))
    return CampaignData(campaign, window, targets, dict(sorted(matches.items())), tuple(items))


def _counter(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def campaign_stats(data: CampaignData) -> dict[str, Any]:
    c = data.campaign
    candidates = [e.candidate for e in data.evidence if e.candidate is not None]
    sources = {
        (s.page_observation_id, s.media_id): s for cand in candidates for s in cand.dossier.sources
    }.values()
    pages = {s.page_url for s in sources}
    per_domain = Counter(s.domain for s in {s.page_url: s for s in sources}.values())
    snapshots = {(s.page_observation_id, str(s.snapshot.digest)) for s in sources if s.snapshot}
    observed = sorted(t for s in sources if (t := s.observed_at) is not None)
    collected = sorted(cand.dossier.collected_at for cand in candidates)
    item_gaps = Counter(g.code.value for cand in candidates for g in cand.dossier.gaps)
    validated = Counter(
        a.state.value
        for cand in candidates
        if cand.validation is not None
        for a in cand.validation.artifacts
    )
    latest = [e.latest for e in data.evidence if e.latest is not None]
    media = {str(m) for summary in data.matches.values() for m in summary.media_ids}
    return {
        "campaign_id": c.campaign_id,
        "window": {
            "from_month": data.window[0].strftime("%Y-%m"),
            "to_month": data.window[-1].strftime("%Y-%m"),
        }
        if data.window
        else None,
        "targets": {
            "count": len(data.targets),
            "registered": sum(1 for t in data.targets.values() if t is not None),
            "not_in_projection": sum(1 for t in data.targets.values() if t is None),
        },
        "matches": {
            "total": len(data.matches),
            "by_verdict": _counter(m.verdict.value for m in data.matches.values()),
        },
        "matched_media": len(media),
        "evidence": {
            "candidates": len(data.evidence),
            "by_status": _counter(cand.status.value for cand in candidates),
            "finalized": len(latest),
            "finalized_by_status": _counter(v.status for v in latest),
            "superseded_versions": sum(len(e.versions) - 1 for e in data.evidence if e.versions),
            "rejected": sum(1 for cand in candidates if cand.status.value == "rejected"),
            "matches_without_candidate": len(
                set(data.matches) - {str(cand.dossier.match.match_id) for cand in candidates}
            ),
        },
        "sources": {
            "source_records": len(sources),
            "pages": len(pages),
            "domains": len(per_domain),
            "pages_per_domain": dict(sorted(per_domain.items())),
            "by_basis": _counter(s.basis.value for s in sources),
        },
        "artifacts": {
            "page_snapshots_referenced": len(snapshots),
            "sources_without_snapshot": sum(1 for s in sources if s.snapshot is None),
            "screenshots_not_collected": item_gaps.get("screenshot_not_collected", 0),
            "media_samples_not_retained": item_gaps.get("media_sample_not_retained", 0),
            "last_validation_states": dict(sorted(validated.items())),
        },
        "gaps": dict(sorted(item_gaps.items())),
        "collection_window": {
            "first_observed_at": ts(observed[0]) if observed else None,
            "last_observed_at": ts(observed[-1]) if observed else None,
            "first_collected_at": ts(collected[0]) if collected else None,
            "last_collected_at": ts(collected[-1]) if collected else None,
        },
        "unavailable": dict(UNAVAILABLE),
    }
