"""P12 collector: match.found → candidate (validation group A)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from antipiracy_contracts.events import ContractError, encode_event
from antipiracy_contracts.events.fingerprinting import MatchFound
from antipiracy_contracts.ids import EvidenceId
from antipiracy_contracts.models.matching import MatchVerdict
from antipiracy_contracts.models.media import ProbeStatus
from antipiracy_contracts.ownership import Component
from pydantic import ValidationError

from crawler2.evidence.collector import EvidenceLoop
from crawler2.evidence.model import CandidateStatus, GapCode, SourceBasis
from crawler2.intelligence.feedback_service import MatchFeedback
from crawler2.storage.errors import StorageUnavailableError
from crawler2.storage.events.consumer import ConsumeOutcome, IdempotentConsumer, StreamEntry
from tests.fixtures.contracts import T0, event_of
from tests.unit.evidence.fakes import MediaView, World
from tests.unit.intelligence.feedback_fakes import MemoryFeedbackStore, MemoryProcessed

DATA = b"matched-content-sample"


def _world() -> tuple[World, object]:
    w = World()
    t = w.register("alpha")
    return w, t


def test_valid_match_creates_a_complete_candidate_with_provenance() -> None:
    w, t = _world()
    page, media_id = w.page(
        "https://site-a.example/watch/1", "v/1.mp4", DATA, redirect_from="http://site-a.example/w1"
    )
    m = w.match(DATA, t)  # type: ignore[arg-type]
    result, dossier = w.collector().collect(m)

    assert result == "created"
    assert dossier.status is CandidateStatus.COMPLETE
    assert dossier.match == m
    assert dossier.target is not None
    (src,) = dossier.sources
    assert src.basis is SourceBasis.CONTENT_VERIFIED
    assert src.page_observation_id == str(page.observation_id)
    assert src.media_id == str(media_id)
    assert src.requested_url == "http://site-a.example/w1"
    assert src.final_url == "https://site-a.example/watch/1"
    assert [(r.url, r.status) for r in src.redirects] == [("https://site-a.example/watch/1", 301)]
    assert src.capability == "browser"
    assert src.http_status == 200
    assert src.snapshot is not None
    assert src.snapshot.digest == page.body_digest
    assert src.worker is not None
    assert src.content_key_digest is not None
    # Declared, not fabricated: no screenshot, no media sample, no full headers.
    codes = {g.code for g in dossier.gaps}
    assert codes == {
        GapCode.SCREENSHOT_NOT_COLLECTED,
        GapCode.MEDIA_SAMPLE_NOT_RETAINED,
        GapCode.RESPONSE_HEADERS_NOT_RETAINED,
    }
    # collected_at is E1's opened_at (deterministic across replays and hosts)
    assert dossier.collected_at == w.repo.opened(m.match_id)[1]  # type: ignore[index]
    stored = w.store.candidate(w.repo.opened(m.match_id)[0])  # type: ignore[index]
    assert stored is not None
    assert stored.revision == 1
    assert w.store.actions(dossier.evidence_id) == ["candidate_created"]


def test_duplicate_delivery_is_idempotent() -> None:
    w, t = _world()
    w.page("https://site-a.example/watch/1", "v/1.mp4", DATA)
    m = w.match(DATA, t)  # type: ignore[arg-type]
    collector = w.collector()
    processed = MemoryProcessed()
    consumer = IdempotentConsumer("evidence-collector", MatchFound, collector.handle, processed)
    wire = encode_event(event_of(MatchFound(match=m), Component.MATCHER))
    # a producer retry: same fact, new event id
    retry = encode_event(event_of(MatchFound(match=m), Component.MATCHER))

    assert consumer.handle(wire) is ConsumeOutcome.APPLIED
    assert consumer.handle(wire) is ConsumeOutcome.DUPLICATE
    assert consumer.handle(retry) is ConsumeOutcome.DUPLICATE
    # even bypassing the marker (crash before mark), a replay writes nothing new
    assert collector.collect(m)[0] == "unchanged"
    assert len(w.store.rows) == 1
    assert w.store.candidate_writes == 1
    assert len(w.repo.opened_rows) == 1


def test_unattributed_match_is_explicitly_incomplete() -> None:
    w, t = _world()
    result, dossier = w.collector().collect(w.match(b"nothing-carries-this", t))  # type: ignore[arg-type]
    assert result == "created"
    assert dossier.status is CandidateStatus.UNATTRIBUTED
    assert dossier.sources == ()
    assert dossier.sources_total == 0
    assert GapCode.UNATTRIBUTED in {g.code for g in dossier.gaps}


def test_locator_only_sightings_are_ambiguous_and_select_no_source() -> None:
    w, t = _world()
    w.page("https://site-a.example/p/1", "v/1.mp4", DATA)  # probed here (M5 knows the media)
    m = w.match(DATA, t)  # type: ignore[arg-type]
    w.registry.observations[0] = w.registry.observations[0].model_copy(
        update={"probe_status": ProbeStatus.NOT_PROBED, "content": None}
    )
    # by_content needs one probed observation; add it on a page outside the window
    w.page("https://site-b.example/old", "v/1.mp4", DATA, at=T0 - timedelta(days=400))
    _, dossier = w.collector().collect(m)
    assert dossier.status is CandidateStatus.AMBIGUOUS
    assert [s.basis for s in dossier.sources] == [SourceBasis.LOCATOR_ONLY]
    assert GapCode.AMBIGUOUS_ATTRIBUTION in {g.code for g in dossier.gaps}


def test_sighting_that_probed_other_bytes_is_not_a_source() -> None:
    w, t = _world()
    w.page("https://site-a.example/p/1", "v/1.mp4", DATA)
    w.page("https://site-b.example/p/2", "v/1.mp4", b"other-bytes-at-this-locator")
    _, dossier = w.collector().collect(w.match(DATA, t))  # type: ignore[arg-type]
    assert [s.page_url for s in dossier.sources] == ["https://site-a.example/p/1"]


def test_missing_observation_snapshot_target_and_probable_verdict_are_gaps() -> None:
    w = World()
    t = w.register("alpha")
    w.projections.target_rows.clear()  # target.registered never reached crawler2
    w.page("https://site-a.example/p/1", "v/1.mp4", DATA, observation=False)
    w.page("https://site-b.example/p/2", "v/1.mp4", DATA, snapshot=False, attempt=False)
    m = w.match(DATA, t, verdict=MatchVerdict.PROBABLE, confidence=0.71)
    _, dossier = w.collector().collect(m)
    codes = {(g.code, g.subject is not None) for g in dossier.gaps}
    assert (GapCode.PAGE_OBSERVATION_MISSING, True) in codes
    assert (GapCode.SNAPSHOT_NOT_ARCHIVED, True) in codes
    assert (GapCode.FETCH_ATTEMPT_MISSING, True) in codes
    assert (GapCode.TARGET_UNKNOWN, True) in codes
    assert (GapCode.VERDICT_NOT_CONFIRMED, False) in codes
    assert dossier.status is CandidateStatus.INCOMPLETE
    assert dossier.target is None


def test_sources_are_bounded_and_truncation_is_declared() -> None:
    w, t = _world()
    w.settings = w.settings.model_copy(update={"max_sources": 2})
    for i in range(4):
        w.page(f"https://site-{i}.example/p", "v/1.mp4", DATA, at=T0 + timedelta(minutes=i))
    _, dossier = w.collector().collect(w.match(DATA, t))  # type: ignore[arg-type]
    assert len(dossier.sources) == 2
    assert dossier.sources_total == 4
    # newest verified sightings are kept, deterministically
    assert sorted(s.page_url for s in dossier.sources) == [
        "https://site-2.example/p",
        "https://site-3.example/p",
    ]
    assert GapCode.SOURCES_TRUNCATED in {g.code for g in dossier.gaps}


def test_new_provenance_updates_an_open_candidate_but_not_a_finalized_one() -> None:
    w, t = _world()
    w.page("https://site-a.example/p/1", "v/1.mp4", DATA)
    m = w.match(DATA, t)  # type: ignore[arg-type]
    c = w.collector()
    c.collect(m)
    w.page("https://site-b.example/p/2", "v/1.mp4", DATA)
    result, dossier = c.collect(m)
    assert result == "updated"
    assert len(dossier.sources) == 2
    assert w.store.candidate(w.repo.opened(m.match_id)[0]).revision == 2  # type: ignore[index,union-attr]

    w.finalizer().finalize(w.repo.opened(m.match_id)[0], actor="t")  # type: ignore[index]
    w.page("https://site-c.example/p/3", "v/1.mp4", DATA)
    result, frozen = c.collect(m)
    assert result == "frozen"
    assert len(frozen.sources) == 2


def test_match_projection_inputs_equal_p11_attribution() -> None:
    w, t = _world()
    w.page("https://site-a.example/p/1", "v/1.mp4", DATA)
    w.page("https://site-b.example/p/2", "v/2.mp4", DATA)
    m = w.match(DATA, t)  # type: ignore[arg-type]
    evidence_id = w.repo.open_candidate(m.match_id, EvidenceId.new(), at=T0)
    _, media_ids, domains = w.collector().resolve(m, evidence_id, T0)
    p11_media, rows = MatchFeedback(
        media=MediaView(w.registry),
        pages=w.registry,
        projections=w.projections,
        store=MemoryFeedbackStore(),
    ).attribute(m)
    assert sorted(map(str, media_ids)) == sorted(map(str, p11_media))
    assert sorted(map(str, domains)) == sorted({r.domain_id for r in rows})


class _Reader:
    def __init__(self, entries: list[StreamEntry]) -> None:
        self.entries = entries
        self.acked: list[str] = []

    def claim_stale(self, *, min_idle_ms: int, count: int = 100) -> list[StreamEntry]:
        return []

    def read(self, *, count: int = 100, block_ms: int | None = None) -> list[StreamEntry]:
        out, self.entries = self.entries, []
        return out

    def ack(self, entries: list[StreamEntry]) -> None:
        self.acked += [e.entry_id for e in entries]


def test_loop_acks_invalid_events_and_leaves_storage_failures_pending() -> None:
    w, t = _world()
    w.page("https://site-a.example/p/1", "v/1.mp4", DATA)
    m = w.match(DATA, t)  # type: ignore[arg-type]
    good = encode_event(event_of(MatchFound(match=m), Component.MATCHER))
    consumer = IdempotentConsumer("evidence-collector", MatchFound, w.collector().handle,
                                  MemoryProcessed())  # fmt: skip
    reader = _Reader([StreamEntry("1-0", b"{not json"), StreamEntry("2-0", good)])
    w.registry.fail = True
    loop = EvidenceLoop(reader, consumer, settings=w.settings)  # type: ignore[arg-type]
    assert loop.run_once() == 1
    assert reader.acked == ["1-0"]  # malformed: acknowledged; storage error: pending
    assert w.store.rows == {}

    w.registry.fail = False
    reader.entries = [StreamEntry("2-0", good)]  # redelivered after XAUTOCLAIM
    assert loop.run_once() == 1
    assert reader.acked == ["1-0", "2-0"]
    assert len(w.store.rows) == 1


def test_wrong_event_type_is_a_contract_error() -> None:
    w, _ = _world()
    consumer = IdempotentConsumer("evidence-collector", MatchFound, w.collector().handle,
                                  MemoryProcessed())  # fmt: skip
    with pytest.raises((ContractError, ValidationError)):
        consumer.handle(b'{"envelope_version": 1, "event_type": "match.found"}')


def test_storage_error_propagates_without_a_candidate() -> None:
    w, t = _world()
    w.page("https://site-a.example/p/1", "v/1.mp4", DATA)
    w.registry.fail = True
    with pytest.raises(StorageUnavailableError):
        w.collector().collect(w.match(DATA, t))  # type: ignore[arg-type]
    assert w.store.rows == {}
