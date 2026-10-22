"""P12 validation, artifacts and finalization (validation groups B and C)."""

from __future__ import annotations

import hashlib
import json

import pytest
from antipiracy_contracts.ids import EvidenceId
from antipiracy_contracts.models.matching import MatchVerdict

from crawler2.evidence.finalize import FinalizationRefused
from crawler2.evidence.model import (
    ArtifactRole,
    ArtifactState,
    CandidateStatus,
    CheckOutcome,
    ValidationOutcome,
)
from crawler2.evidence.validate import verify_blob
from crawler2.storage.errors import ConflictError, StorageUnavailableError
from crawler2.storage.repositories import EvidenceState
from tests.unit.evidence.fakes import World

DATA = b"matched-content-sample"


def _collected(**page: object) -> tuple[World, EvidenceId]:
    w = World()
    t = w.register("alpha")
    w.page("https://site-a.example/watch/1", "v/1.mp4", DATA, **page)  # type: ignore[arg-type]
    m = w.match(DATA, t)
    w.collector().collect(m)
    return w, w.repo.opened(m.match_id)[0]  # type: ignore[index]


def _snapshot_digest(w: World, eid: EvidenceId):  # type: ignore[no-untyped-def]
    cand = w.store.candidate(eid)
    assert cand is not None
    assert cand.dossier.sources[0].snapshot is not None
    return cand.dossier.sources[0].snapshot.digest


# --- B: artifacts -----------------------------------------------------------------------


def test_complete_candidate_validates_with_verified_snapshot_hash() -> None:
    w, eid = _collected()
    report = w.finalizer().validate(eid)
    assert report.outcome is ValidationOutcome.COMPLETE
    snaps = [a for a in report.artifacts if a.role is ArtifactRole.PAGE_SNAPSHOT]
    assert [a.state for a in snaps] == [ArtifactState.VERIFIED]
    stored = w.objects.blobs[snaps[0].digest.hex]  # type: ignore[union-attr]
    assert hashlib.sha256(stored).hexdigest() == snaps[0].digest.hex  # type: ignore[union-attr]
    # absent capture kinds are listed as not collected, never as verified
    assert {a.role: a.state for a in report.artifacts if a.subject is None} == {
        ArtifactRole.SCREENSHOT: ArtifactState.NOT_COLLECTED,
        ArtifactRole.MEDIA_SAMPLE: ArtifactState.NOT_COLLECTED,
    }


def test_missing_object_is_reported_and_blocks_complete() -> None:
    w, eid = _collected()
    w.objects.delete(_snapshot_digest(w, eid))
    report = w.finalizer().validate(eid)
    assert report.outcome is ValidationOutcome.PARTIAL
    (snap,) = [a for a in report.artifacts if a.role is ArtifactRole.PAGE_SNAPSHOT]
    assert snap.state is ArtifactState.MISSING
    assert {c.check: c.outcome for c in report.checks}["artifacts.page_snapshots"] is (
        CheckOutcome.FAIL
    )


def test_corrupted_bytes_are_an_integrity_failure_and_reject() -> None:
    w, eid = _collected()
    digest = _snapshot_digest(w, eid)
    original = w.objects.blobs[digest.hex]
    w.objects.corrupt(digest, b"X" * len(original))
    report = w.finalizer().validate(eid)
    assert report.outcome is ValidationOutcome.REJECTED
    (snap,) = [a for a in report.artifacts if a.role is ArtifactRole.PAGE_SNAPSHOT]
    assert snap.state is ArtifactState.CORRUPTED
    assert w.store.candidate(eid).status is CandidateStatus.REJECTED  # type: ignore[union-attr]
    with pytest.raises(FinalizationRefused):
        w.finalizer().finalize(eid, allow_partial=True, actor="t")
    assert "finalize_refused" in w.store.actions(eid)


def test_oversized_and_foreign_bucket_artifacts_are_not_claimed_verified() -> None:
    w, eid = _collected()
    ref = w.store.candidate(eid).dossier.sources[0].snapshot  # type: ignore[union-attr]
    assert ref is not None
    state, _ = verify_blob(w.objects, ref, bucket=w.objects.bucket, max_bytes=8)
    assert state is ArtifactState.OVERSIZED
    state, _ = verify_blob(w.objects, ref, bucket="other-bucket", max_bytes=10**9)
    assert state is ArtifactState.UNVERIFIABLE
    bad = ref.model_copy(update={"uri": "s3://crawler2-raw/../../etc/passwd"})
    assert verify_blob(w.objects, bad, bucket=None, max_bytes=10**9)[0] is ArtifactState.CORRUPTED


def test_changed_w4_row_is_detected_as_provenance_drift() -> None:
    w, eid = _collected()
    cand = w.store.candidate(eid)
    assert cand is not None
    oid = cand.dossier.sources[0].page_observation_id
    w.registry.pages[oid] = w.registry.pages[oid].model_copy(update={"http_status": 404})
    report = w.finalizer().validate(eid)
    assert report.outcome is ValidationOutcome.REJECTED
    assert {c.check: c.outcome for c in report.checks}["provenance.observations"] is (
        CheckOutcome.INTEGRITY_FAILURE
    )


# --- C: finalization --------------------------------------------------------------------


def test_complete_candidate_finalizes_into_sealed_version_one() -> None:
    w, eid = _collected()
    done = w.finalizer().finalize(eid, actor="reviewer")
    assert done.result == "finalized"
    assert done.version.version == 1
    assert done.version.status == "complete"
    manifest = w.objects.blobs[done.version.manifest_digest.hex]
    doc = json.loads(manifest)
    assert doc["evidence"]["evidence_id"] == str(eid)
    assert doc["evidence"]["status"] == "complete"
    record = w.repo.get(eid)
    assert record is not None
    assert record.state is EvidenceState.SEALED
    assert record.seal is not None
    assert record.seal.manifest.digest == done.version.manifest_digest
    assert len(w.repo.events) == 1  # evidence.finalized through the outbox
    assert w.store.candidate(eid).status is CandidateStatus.FINALIZED  # type: ignore[union-attr]
    assert "finalized" in w.store.actions(eid)


def test_incomplete_candidate_is_refused_unless_partial_is_explicit() -> None:
    w = World()
    t = w.register("alpha")
    w.page("https://site-a.example/watch/1", "v/1.mp4", DATA)
    m = w.match(DATA, t, verdict=MatchVerdict.PROBABLE, confidence=0.7)
    w.collector().collect(m)
    eid = w.repo.opened(m.match_id)[0]  # type: ignore[index]
    with pytest.raises(FinalizationRefused, match="partial"):
        w.finalizer().finalize(eid, actor="t")
    assert w.store.versions(eid) == []
    assert w.store.candidate(eid).status is CandidateStatus.INCOMPLETE  # type: ignore[union-attr]

    done = w.finalizer().finalize(eid, allow_partial=True, actor="t")
    assert done.version.status == "partial"
    doc = json.loads(w.objects.blobs[done.version.manifest_digest.hex])
    assert doc["evidence"]["status"] == "partial"
    assert {c["check"]: c["outcome"] for c in doc["validation"]["checks"]}["match.confirmed"] == (
        "fail"
    )


def test_unattributed_candidate_can_never_be_finalized() -> None:
    w = World()
    t = w.register("alpha")
    m = w.match(b"carried-nowhere", t)
    w.collector().collect(m)
    eid = w.repo.opened(m.match_id)[0]  # type: ignore[index]
    with pytest.raises(FinalizationRefused, match="rejected"):
        w.finalizer().finalize(eid, allow_partial=True, actor="t")


def test_repeated_finalization_is_idempotent() -> None:
    w, eid = _collected()
    first = w.finalizer().finalize(eid, actor="t")
    puts = w.objects.puts
    again = w.finalizer().finalize(eid, actor="t")
    assert again.result == "idempotent"
    assert again.version == first.version
    assert len(w.store.versions(eid)) == 1
    assert len(w.repo.events) == 1
    assert w.objects.puts == puts


def test_conflicting_finalization_is_rejected_and_audited() -> None:
    w, eid = _collected()
    first = w.finalizer().finalize(eid, actor="t")
    # an artifact disappears: the same identity would now get different content
    w.objects.delete(_snapshot_digest(w, eid))
    with pytest.raises((ConflictError, FinalizationRefused)):
        w.finalizer().finalize(eid, allow_partial=True, actor="mallory")
    assert w.store.versions(eid) == [first.version]
    assert "finalize_conflict" in w.store.actions(eid)
    assert w.objects.blobs[first.version.manifest_digest.hex]  # v1 bytes untouched


def test_supersession_appends_a_version_and_preserves_the_original() -> None:
    w, eid = _collected()
    first = w.finalizer().finalize(eid, actor="t")
    with pytest.raises(FinalizationRefused, match="nothing to supersede"):
        w.finalizer().supersede(eid, reason="no change", actor="t")
    w.page("https://site-b.example/watch/2", "v/1.mp4", DATA)
    cand = w.store.candidate(eid)
    assert cand is not None
    assert w.collector().collect(cand.dossier.match, refresh=True)[0] == "updated"
    done = w.finalizer().supersede(eid, reason="second source found", actor="t")
    assert done.version.version == 2
    assert done.version.supersedes == 1
    v2 = json.loads(w.objects.blobs[done.version.manifest_digest.hex])
    assert v2["evidence"]["supersedes"] == {
        "version": 1,
        "manifest_sha256": str(first.version.manifest_digest),
    }
    assert len(v2["observed"]["sources"]) == 2
    assert [v.version for v in w.store.versions(eid)] == [2, 1]
    assert w.objects.blobs[first.version.manifest_digest.hex]  # original still stored
    record = w.repo.get(eid)
    assert record is not None
    assert record.seal is not None
    assert record.seal.manifest.digest == first.version.manifest_digest  # E2 stays v1
    assert "superseded" in w.store.actions(eid)
    # re-finalizing after a supersession is idempotent against the latest version
    assert w.finalizer().finalize(eid, actor="t").result == "idempotent"


@pytest.mark.parametrize("fail", ["object_put", "claim", "seal"])
def test_retry_after_transient_storage_error_does_not_duplicate(fail: str) -> None:
    w, eid = _collected()
    if fail == "object_put":
        w.objects.fail_puts = 1
    elif fail == "claim":
        w.store.fail_claims = 1
    else:
        w.repo.fail_seal = 1
    with pytest.raises(StorageUnavailableError):
        w.finalizer().finalize(eid, actor="t")
    done = w.finalizer().finalize(eid, actor="t")
    assert done.result in ("finalized", "idempotent")
    assert len(w.store.versions(eid)) == 1
    record = w.repo.get(eid)
    assert record is not None
    assert record.state is EvidenceState.SEALED
    assert len(w.repo.events) == 1
    assert w.store.candidate(eid).status is CandidateStatus.FINALIZED  # type: ignore[union-attr]


def test_dry_run_plan_writes_nothing() -> None:
    w, eid = _collected()
    plan = w.finalizer().plan(eid)
    assert plan.version == 1
    assert plan.existing is None
    assert plan.manifest
    assert w.store.versions(eid) == []
    assert w.repo.get(eid) is None
    assert w.store.candidate(eid).validation is None  # type: ignore[union-attr]
