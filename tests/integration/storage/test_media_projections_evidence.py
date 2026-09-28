"""Media (M1-M6), projections (P1-P5) and evidence (E1-E4) against real Scylla."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events.fingerprinting import (
    EncodeFailed,
    EncodeFailure,
    EncodeRequested,
)
from antipiracy_contracts.events.targets import TargetRetired
from antipiracy_contracts.ids import EvidenceId
from antipiracy_contracts.models.blobs import BlobRef
from antipiracy_contracts.models.evidence import EvidenceSeal
from antipiracy_contracts.ownership import Component

from crawler2.storage.errors import ConflictError
from crawler2.storage.repositories import EvidenceState, RepresentationState
from crawler2.storage.scylla import ScyllaStorage
from crawler2.storage.scylla.projections import DEFAULT_SPEC_KEY, spec_key
from tests.fixtures.contracts import (
    SPEC,
    T0,
    content_key,
    event_of,
    evidence_candidate,
    match,
    media,
    media_observation,
    page_observation,
    representation,
    target,
    url,
)

pytestmark = pytest.mark.integration


def _tag() -> str:
    return uuid.uuid4().hex[:10]


def test_media_observations_and_read_models(storage: ScyllaStorage) -> None:
    tag = _tag()
    item = media(f"{tag}/movie.mp4")
    pages = [page_observation(url(f"{tag}/p{i}"), at=T0 + timedelta(days=i * 20)) for i in range(3)]
    first_content, second_content = (
        content_key(b"v1" + tag.encode()),
        content_key(b"v2" + tag.encode()),
    )
    observations = [
        media_observation(item, pages[0], content=first_content),
        media_observation(item, pages[1]),  # not probed
        media_observation(item, pages[2], content=second_content),  # bytes changed at the locator
    ]
    for o in [*observations, observations[0], observations[2]]:  # replays
        storage.media.record_observation(o)

    record = storage.media.get(item.media_id)
    assert record is not None
    assert record.media == item
    assert (record.first_seen, record.last_seen) == (pages[0].observed_at, pages[2].observed_at)
    assert storage.media.observation(pages[1].observation_id, item.media_id) == observations[1]
    assert storage.media.observations_on(pages[0].observation_id) == [observations[0]]

    sightings = storage.media.sightings(
        item.media_id, since=T0, until=T0 + timedelta(days=90), limit=10
    )
    assert [s.page_observation_id for s in sightings] == [p.observation_id for p in reversed(pages)]
    on_page = storage.media.on_page_version(pages[0].page_version_id)
    assert [(m.media_id, m.content_id) for m in on_page] == [
        (item.media_id, first_content.content_id)
    ]
    by_content = storage.media.by_content(first_content.content_id)
    assert [m.media_id for m in by_content] == [item.media_id]
    versions = storage.media.content_versions(
        item.media_id, since=T0, until=T0 + timedelta(days=90)
    )
    assert [v.content_id for v in versions] == [second_content.content_id, first_content.content_id]
    assert storage.media.rebuild_media(item.media_id, since=T0, until=T0 + timedelta(days=90)) == 3


def test_same_bytes_at_many_locators_map_back_to_every_media(storage: ScyllaStorage) -> None:
    tag = _tag()
    shared = content_key(b"same-bytes" + tag.encode())
    page = page_observation(url(f"{tag}/host-page"))
    mirrors = [media(f"{tag}/mirror-{i}.mp4", host=f"cdn{i}.example") for i in range(20)]
    for item in mirrors:
        storage.media.record_observation(media_observation(item, page, content=shared))
    assert {m.media_id for m in storage.media.by_content(shared.content_id)} == {
        m.media_id for m in mirrors
    }


def test_representation_status_converges_in_any_order(storage: ScyllaStorage) -> None:
    key = content_key(_tag().encode())
    content = key.content_id
    request = event_of(
        EncodeRequested(content=key, source=media("r.mp4"), required_spec=SPEC),
        Component.MEDIA_REGISTRY,
    )
    failed = [
        event_of(
            EncodeFailed(
                request_id=request.event_id,
                content_id=content,
                failure=EncodeFailure.TIMEOUT,
                retryable=True,
                attempt=n,
                spec=SPEC,
            ),
            Component.ENCODER,
            at=T0 + timedelta(minutes=n),
        )
        for n in (1, 2)
    ]
    storage.projections.mark_encode_requested(request)
    storage.projections.apply_encode_failed(failed[1])
    storage.projections.apply_encode_failed(failed[0])  # older attempt arrives late
    (status,) = storage.projections.representation_status(content)
    assert status.state is RepresentationState.FAILED
    assert status.failed_attempt == 2
    assert status.spec_key == spec_key(SPEC) != DEFAULT_SPEC_KEY

    ready = representation(content, at=T0 + timedelta(minutes=5))
    storage.projections.apply_representation_ready(ready)
    storage.projections.apply_representation_ready(ready)  # duplicate delivery
    storage.projections.apply_encode_failed(failed[1])  # redelivered failure
    (status,) = storage.projections.representation_status(content)
    assert status.state is RepresentationState.READY
    assert status.representation == ready
    assert status.request_event_id == request.event_id


def test_targets_keep_highest_version_and_retirement_is_terminal(storage: ScyllaStorage) -> None:
    tag = _tag()
    v1, v2 = target(tag, version=1), target(tag, version=2, at=T0 + timedelta(days=1))
    storage.projections.apply_target_registered(v2)
    storage.projections.apply_target_registered(v1)  # out of order
    state = storage.projections.target(v1.ref.target_id)
    assert state is not None
    assert state.target == v2
    assert state.active

    retired = TargetRetired(target_id=v1.ref.target_id, retired_at=T0 + timedelta(days=2))
    storage.projections.apply_target_retired(retired)
    storage.projections.apply_target_registered(target(tag, version=3))  # after retirement
    state = storage.projections.target(v1.ref.target_id)
    assert state is not None
    assert not state.active
    assert state.target.ref.version == 3
    assert v1.ref.target_id in {t.target.ref.target_id for t in storage.projections.targets()}


def test_matches_projections_and_replay(storage: ScyllaStorage) -> None:
    tag = _tag()
    work = target(tag)
    content = content_key(tag.encode()).content_id
    result = match(content, work.ref)
    mirrors = [media(f"{tag}/{i}.mp4") for i in range(3)]
    source = url(f"{tag}/page")
    storage.projections.record_match(
        result, media_ids=[m.media_id for m in mirrors[:2]], source_domains=[source.domain_id]
    )
    storage.projections.record_match(  # replay with a grown media set: union, no duplicate
        result, media_ids=[m.media_id for m in mirrors], source_domains=[source.domain_id]
    )
    assert storage.projections.matches_for_content(content) == [result]
    (by_target,) = storage.projections.matches_for_target(work.ref.target_id, T0)
    assert by_target.media_ids == {m.media_id for m in mirrors}
    (by_domain,) = storage.projections.matches_for_domain(source.domain_id, T0)
    assert by_domain.match_id == result.match_id


def test_evidence_lifecycle_is_compare_and_set(storage: ScyllaStorage) -> None:
    tag = _tag()
    work = target(tag)
    item = media(f"{tag}/e.mp4")
    page = page_observation(url(f"{tag}/evidence"))
    storage.pages.record(page)
    result = match(content_key(tag.encode()).content_id, work.ref)

    proposals = [EvidenceId.new() for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:  # racing collectors
        winners = set(
            pool.map(
                lambda e: storage.evidence.open_candidate(result.match_id, e, at=T0), proposals
            )
        )
    assert len(winners) == 1
    (evidence_id,) = winners

    loser = next(p for p in proposals if p != evidence_id)
    with pytest.raises(ConflictError, match="not the evidence item"):
        storage.evidence.create_candidate(evidence_candidate(result, loser, item, page))

    candidate = evidence_candidate(result, evidence_id, item, page)
    record = storage.evidence.create_candidate(candidate)
    assert record.state is EvidenceState.COLLECTING
    assert storage.evidence.create_candidate(candidate) == record  # idempotent replay
    changed = candidate.model_copy(update={"collected_at": T0 + timedelta(seconds=1)})
    with pytest.raises(ConflictError, match="different candidate"):
        storage.evidence.create_candidate(changed)

    manifest = BlobRef(
        uri="s3://crawler2-evidence/m", digest=ContentDigest.of_bytes(b"m"), size_bytes=1
    )
    seal = EvidenceSeal(
        evidence_id=evidence_id, manifest=manifest, finalized_at=T0 + timedelta(hours=1)
    )
    sealed = storage.evidence.seal(seal)
    assert sealed.state is EvidenceState.SEALED
    assert storage.evidence.seal(seal).seal == seal  # replay
    other = seal.model_copy(update={"finalized_at": T0 + timedelta(hours=2)})
    with pytest.raises(ConflictError, match="already sealed"):
        storage.evidence.seal(other)
    with pytest.raises(ConflictError, match="no candidate"):
        storage.evidence.seal(seal.model_copy(update={"evidence_id": EvidenceId.new()}))

    (summary,) = storage.evidence.for_target(work.ref.target_id, T0)
    assert summary.state is EvidenceState.SEALED
    provenance = storage.evidence.provenance(evidence_id)
    assert provenance is not None
    assert provenance.observations == (page,)
    assert provenance.missing_observations == ()
    assert storage.evidence.get(EvidenceId.new()) is None
