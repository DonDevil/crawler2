"""Finalization and supersession (design §6).

``finalize``: validate → build the canonical manifest → store its bytes in the
content-addressed object store (read back and verified) → claim version 1 in
E6 with ``IF NOT EXISTS`` → write the P1 ``EvidenceCandidate`` into E2 and seal
it with the manifest reference (E2's write-once LWT, ``evidence.finalized``
through the outbox) → mark the candidate finalized → audit.

The E6 row is the authority: every later step is idempotent and is completed
by a retry, so a crash or a transient storage error anywhere can be retried
without duplicating evidence. Re-finalizing identical content returns the
existing version; different content under an existing version is a
``ConflictError`` (audited). A correction is ``supersede``: version n+1 names
version n and a reason; nothing is overwritten. E2's seal stays the version-1
seal (the P1 contract is write-once and keyed by evidence id).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events import EventEnvelope, new_event
from antipiracy_contracts.events.evidence import EvidenceFinalized
from antipiracy_contracts.ids import EvidenceId, MediaId, ObservationId
from antipiracy_contracts.models.blobs import BlobRef
from antipiracy_contracts.models.evidence import (
    ArtifactRole as ContractRole,
)
from antipiracy_contracts.models.evidence import (
    EvidenceArtifact,
    EvidenceCandidate,
    EvidenceSeal,
)
from antipiracy_contracts.ownership import Component, Producer

from crawler2.evidence.collector import EvidenceMetrics, PageLookup
from crawler2.evidence.manifest import build_manifest, canonical_bytes, digest_of
from crawler2.evidence.model import (
    CandidateStatus,
    EvidenceDossier,
    ValidationOutcome,
    ValidationReport,
)
from crawler2.evidence.store import EvidenceStore, FinalizedVersion, StoredCandidate
from crawler2.evidence.validate import ArtifactReader, validate
from crawler2.storage.errors import ConflictError, StorageError
from crawler2.storage.objectstore.base import Payload
from crawler2.storage.repositories import EvidenceRecord

MANIFEST_MEDIA_TYPE = "application/json"


class FinalizationRefused(Exception):  # noqa: N818 -- a refusal, not a malfunction
    def __init__(self, message: str, report: ValidationReport | None = None) -> None:
        super().__init__(message)
        self.report = report


class NotFound(Exception):  # noqa: N818
    pass


class ManifestStore(ArtifactReader, Protocol):
    def put(
        self,
        data: Payload,
        *,
        media_type: str | None = None,
        expected_digest: ContentDigest | None = None,
    ) -> BlobRef: ...


class SealRepository(Protocol):
    """The E2 part of ``EvidenceRepository`` finalization uses (structural)."""

    def create_candidate(self, candidate: EvidenceCandidate) -> EvidenceRecord: ...

    def seal(
        self, seal: EvidenceSeal, *, event: EventEnvelope[EvidenceFinalized] | None = None
    ) -> EvidenceRecord: ...

    def get(self, evidence_id: EvidenceId) -> EvidenceRecord | None: ...


@dataclass(frozen=True, slots=True)
class Plan:
    """What finalization would write (``--dry-run`` shows this and writes nothing)."""

    evidence_id: EvidenceId
    report: ValidationReport
    version: int
    manifest: bytes
    digest: ContentDigest
    existing: FinalizedVersion | None
    """The stored version with this number, when one exists."""

    @property
    def idempotent(self) -> bool:
        return self.existing is not None and self.existing.manifest_digest == self.digest


@dataclass(frozen=True, slots=True)
class Finalized:
    result: str
    """``finalized``, ``superseded`` or ``idempotent``."""
    version: FinalizedVersion


class EvidenceFinalizer:
    def __init__(
        self,
        *,
        store: EvidenceStore,
        seals: SealRepository,
        objects: ManifestStore,
        pages: PageLookup,
        producer_instance: str,
        bucket: str | None,
        max_artifact_bytes: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        metrics: EvidenceMetrics | None = None,
    ) -> None:
        self._store = store
        self._seals = seals
        self._objects = objects
        self._pages = pages
        self._producer = Producer(
            service=Component.EVIDENCE_FINALIZER.service,
            component=Component.EVIDENCE_FINALIZER,
            instance=producer_instance,
        )
        self._bucket = bucket
        self._max = max_artifact_bytes
        self._clock = clock
        self._m = metrics or EvidenceMetrics(None)

    # --- reads ---------------------------------------------------------------------------

    def _candidate(self, evidence_id: EvidenceId) -> StoredCandidate:
        found = self._store.candidate(evidence_id)
        if found is None:
            raise NotFound(f"no evidence candidate {evidence_id}")
        return found

    def validate(self, evidence_id: EvidenceId, *, record: bool = True) -> ValidationReport:
        cand = self._candidate(evidence_id)
        report = validate(
            cand.dossier,
            objects=self._objects,
            pages=self._pages,
            bucket=self._bucket,
            max_artifact_bytes=self._max,
            metrics=self._m,
        )
        if record:
            now = self._clock()
            self._store.record_validation(evidence_id, report, at=now)
            if report.outcome is ValidationOutcome.REJECTED and cand.status not in (
                CandidateStatus.FINALIZED,
                CandidateStatus.REJECTED,
            ):
                self._store.set_status(evidence_id, CandidateStatus.REJECTED)
        return report

    def plan(
        self,
        evidence_id: EvidenceId,
        *,
        supersede: bool = False,
        reason: str | None = None,
        record: bool = False,
    ) -> Plan:
        cand = self._candidate(evidence_id)
        report = self.validate(evidence_id, record=record)
        versions = self._store.versions(evidence_id)
        latest = versions[0] if versions else None
        prior: tuple[int, ContentDigest] | None
        if report.outcome is ValidationOutcome.REJECTED:
            return Plan(evidence_id, report, 0, b"", digest_of(b""), None)
        if supersede:
            if latest is None:
                raise FinalizationRefused("nothing to supersede: no finalized version", report)
            version, prior = latest.version + 1, (latest.version, latest.manifest_digest)
            existing = None
        else:
            version = latest.version if latest else 1
            prior = _prior(versions, latest)
            reason = latest.reason if latest else None
            existing = latest
        manifest = canonical_bytes(
            build_manifest(cand.dossier, report, version=version, supersedes=prior, reason=reason)
        )
        return Plan(evidence_id, report, version, manifest, digest_of(manifest), existing)

    # --- writes --------------------------------------------------------------------------

    def finalize(
        self, evidence_id: EvidenceId, *, allow_partial: bool = False, actor: str
    ) -> Finalized:
        try:
            plan = self.plan(evidence_id, record=True)
            self._admit(plan, allow_partial=allow_partial, actor=actor, action="finalize")
            if plan.existing is not None:
                if not plan.idempotent:
                    self._audit(
                        evidence_id,
                        "finalize_conflict",
                        actor,
                        f"v{plan.existing.version} pins {plan.existing.manifest_digest}; "
                        f"attempt produced {plan.digest}",
                    )
                    self._m.finalization("conflict")
                    raise ConflictError(
                        f"{evidence_id} is finalized as v{plan.existing.version} with other "
                        "content; use supersede for a correction"
                    )
                row = plan.existing
                result = "idempotent"
            else:
                row = self._claim(plan, actor=actor, supersedes=None, reason=None)
                result = "finalized"
            self._complete(evidence_id, row, actor=actor, result=result)
        except ConflictError:
            raise
        except StorageError:
            self._m.finalization("failed")
            raise
        self._m.finalization(result)
        return Finalized(result, row)

    def supersede(
        self, evidence_id: EvidenceId, *, reason: str, allow_partial: bool = False, actor: str
    ) -> Finalized:
        if not reason.strip():
            raise FinalizationRefused("a supersession needs a reason")
        current = self.plan(evidence_id, record=True)
        if current.existing is not None and current.idempotent:
            self._audit(evidence_id, "supersede_refused", actor, "content unchanged")
            raise FinalizationRefused(
                f"v{current.existing.version} already holds this content; nothing to supersede",
                current.report,
            )
        plan = self.plan(evidence_id, supersede=True, reason=reason)
        self._admit(plan, allow_partial=allow_partial, actor=actor, action="supersede")
        assert plan.existing is None  # noqa: S101 -- supersede plans a new version
        latest = self._store.versions(evidence_id)[0]
        row = self._claim(plan, actor=actor, supersedes=latest.version, reason=reason)
        self._audit(
            evidence_id,
            "superseded",
            actor,
            f"v{row.version} supersedes v{latest.version} ({row.manifest_digest}): {reason}",
        )
        self._m.finalization("superseded")
        return Finalized("superseded", row)

    def _admit(self, plan: Plan, *, allow_partial: bool, actor: str, action: str) -> None:
        outcome = plan.report.outcome
        failed = ", ".join(c.check for c in plan.report.failed) or "none"
        if outcome is ValidationOutcome.REJECTED or (
            outcome is ValidationOutcome.PARTIAL and not allow_partial
        ):
            self._audit(
                plan.evidence_id,
                f"{action}_refused",
                actor,
                f"outcome={outcome.value} failed={failed}",
            )
            self._m.finalization("refused")
            hint = "" if outcome is ValidationOutcome.REJECTED else " (--allow-partial to accept)"
            raise FinalizationRefused(
                f"validation outcome is {outcome.value}; failed: {failed}{hint}", plan.report
            )

    def _claim(
        self, plan: Plan, *, actor: str, supersedes: int | None, reason: str | None
    ) -> FinalizedVersion:
        ref = self._objects.put(
            plan.manifest, media_type=MANIFEST_MEDIA_TYPE, expected_digest=plan.digest
        )
        self._objects.get(ref)  # read back: the pinned bytes are retrievable and intact
        wanted = FinalizedVersion(
            evidence_id=plan.evidence_id,
            version=plan.version,
            status=plan.report.outcome.value,
            manifest_uri=ref.uri,
            manifest_digest=plan.digest,
            manifest_size=ref.size_bytes,
            finalized_at=self._clock(),
            finalized_by=actor,
            supersedes=supersedes,
            reason=reason,
        )
        applied, row = self._store.claim_version(wanted)
        if not applied and row.manifest_digest != plan.digest:
            self._audit(
                plan.evidence_id,
                "finalize_conflict",
                actor,
                f"v{row.version} was claimed concurrently with {row.manifest_digest}",
            )
            self._m.finalization("conflict")
            raise ConflictError(f"{plan.evidence_id} v{row.version} holds another manifest")
        return row

    def _complete(
        self, evidence_id: EvidenceId, row: FinalizedVersion, *, actor: str, result: str
    ) -> None:
        """Version-1 side effects; each is idempotent, so a retry completes a crashed run."""
        if row.version == 1:
            cand = self._candidate(evidence_id)
            contract = _contract_candidate(cand.dossier)
            record = self._seals.get(evidence_id)
            if record is None:
                self._seals.create_candidate(contract)
            seal = EvidenceSeal(
                evidence_id=evidence_id,
                manifest=BlobRef(
                    uri=row.manifest_uri,
                    digest=row.manifest_digest,
                    size_bytes=row.manifest_size,
                    media_type=MANIFEST_MEDIA_TYPE,
                ),
                finalized_at=row.finalized_at,
            )
            if record is None or record.seal is None:
                self._seals.seal(
                    seal,
                    event=new_event(
                        EvidenceFinalized(seal=seal),
                        producer=self._producer,
                        occurred_at=row.finalized_at,
                    ),
                )
        self._store.set_status(evidence_id, CandidateStatus.FINALIZED)
        if result == "finalized":
            self._audit(
                evidence_id,
                "finalized",
                actor,
                f"v{row.version} status={row.status} manifest={row.manifest_digest}",
            )

    def _audit(self, evidence_id: EvidenceId, action: str, actor: str, detail: str) -> None:
        self._store.audit(evidence_id, action, actor=actor, detail=detail, at=self._clock())


def _prior(
    versions: list[FinalizedVersion], latest: FinalizedVersion | None
) -> tuple[int, ContentDigest] | None:
    if latest is None or latest.supersedes is None:
        return None
    for v in versions:
        if v.version == latest.supersedes:
            return v.version, v.manifest_digest
    return None


def _contract_candidate(dossier: EvidenceDossier) -> EvidenceCandidate:
    """The P1 contract form: only observed sources, sorted, with their snapshot artifacts."""
    usable = dossier.usable_sources
    snapshots = {str(s.snapshot.digest): s.snapshot for s in usable if s.snapshot is not None}
    return EvidenceCandidate(
        evidence_id=EvidenceId(dossier.evidence_id),
        match_id=dossier.match.match_id,
        target=dossier.match.target,
        content_id=dossier.match.content_id,
        media_ids=tuple(sorted({MediaId(s.media_id) for s in usable}, key=str)),
        page_observation_ids=tuple(
            sorted({ObservationId(s.page_observation_id) for s in usable}, key=str)
        ),
        artifacts=tuple(
            EvidenceArtifact(role=ContractRole.PAGE_SNAPSHOT, blob=snapshots[k])
            for k in sorted(snapshots)
        ),
        collected_at=dossier.collected_at,
    )
