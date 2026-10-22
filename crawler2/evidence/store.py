"""Scylla tables owned by P12 (design §5; migration V007).

- E5 ``evidence_candidates``: the working candidate, upserted by the collector.
- E6 ``evidence_versions``: finalized versions, append-only; each version is
  claimed with ``INSERT ... IF NOT EXISTS`` so two finalizers cannot write
  different manifests under one (evidence, version).
- E7 ``evidence_audit``: append-only trail.
- E8/E9 campaigns.
- E4 (V001 ``evidence_by_target``) is upserted with every candidate, so
  unfinalized candidates are listed per target and month.

Authoritative rows (E6, E7, E8) are written at ``AUTHORITATIVE_WRITE``; E5 is
re-derivable from the match and the registry, but its ``collected_at`` and the
stored validation are not, so it is written at the same level.
"""

from __future__ import annotations

import random
import re
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.ids import EvidenceId, TargetId

from crawler2.evidence.model import CandidateStatus, EvidenceDossier, ValidationReport
from crawler2.storage.errors import StorageUnavailableError
from crawler2.storage.layout import MATCH_MONTH_SHARDS, month_bucket, shard_of
from crawler2.storage.scylla.session import Consistency, ScyllaSession, utc

CAMPAIGN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_INDEX_BUCKET = 0
_CAS_ATTEMPTS = 5
AUTH = Consistency.AUTHORITATIVE_WRITE
STRONG = Consistency.STRONG_READ
SERIAL = Consistency.SERIAL


@dataclass(frozen=True, slots=True)
class StoredCandidate:
    dossier: EvidenceDossier
    status: CandidateStatus
    revision: int
    updated_at: datetime
    validation: ValidationReport | None
    validated_at: datetime | None


@dataclass(frozen=True, slots=True)
class FinalizedVersion:
    evidence_id: EvidenceId
    version: int
    status: str
    """``complete`` or ``partial`` (the validation outcome it was finalized with)."""
    manifest_uri: str
    manifest_digest: ContentDigest
    manifest_size: int
    finalized_at: datetime
    finalized_by: str
    supersedes: int | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class AuditEntry:
    at: datetime
    action: str
    actor: str
    detail: str


@dataclass(frozen=True, slots=True)
class Campaign:
    campaign_id: str
    name: str
    since: datetime
    """First UTC month whose matches and evidence the case covers."""
    created_at: datetime
    target_ids: tuple[TargetId, ...]


class EvidenceStore(Protocol):
    def candidate(self, evidence_id: EvidenceId) -> StoredCandidate | None: ...

    def put_candidate(
        self,
        dossier: EvidenceDossier,
        *,
        status: CandidateStatus,
        revision: int,
        updated_at: datetime,
    ) -> None: ...

    def set_status(self, evidence_id: EvidenceId, status: CandidateStatus) -> None: ...

    def record_validation(
        self, evidence_id: EvidenceId, report: ValidationReport, *, at: datetime
    ) -> None: ...

    def versions(self, evidence_id: EvidenceId) -> list[FinalizedVersion]:
        """Newest first."""
        ...

    def claim_version(self, row: FinalizedVersion) -> tuple[bool, FinalizedVersion]:
        """(applied, the row now stored under (evidence, version))."""
        ...

    def audit(
        self, evidence_id: EvidenceId, action: str, *, actor: str, detail: str, at: datetime
    ) -> None: ...

    def audit_log(self, evidence_id: EvidenceId) -> list[AuditEntry]: ...

    def create_campaign(self, campaign: Campaign) -> None: ...

    def add_campaign_target(
        self, campaign_id: str, target_id: TargetId, *, at: datetime
    ) -> None: ...

    def campaign(self, campaign_id: str) -> Campaign | None: ...

    def campaigns(self) -> list[str]: ...


_CAND_COLS = "evidence_id, match_id, target_id, status, revision, collected_at, updated_at, doc"
_PUT_CAND = f"INSERT INTO {{ks}}.evidence_candidates ({_CAND_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
_BY_TARGET = (
    "UPDATE {ks}.evidence_by_target SET match_id = ?, collected_at = ? "
    "WHERE target_id = ? AND month = ? AND shard = ? AND evidence_id = ?"
)
_SET_STATUS = "UPDATE {ks}.evidence_candidates SET status = ? WHERE evidence_id = ?"
_SET_VALIDATION = (
    "UPDATE {ks}.evidence_candidates SET validation = ?, validated_at = ? WHERE evidence_id = ?"
)
_GET_CAND = (
    "SELECT status, revision, updated_at, doc, validation, validated_at "
    "FROM {ks}.evidence_candidates WHERE evidence_id = ?"
)
_VER_COLS = (
    "evidence_id, version, status, manifest_uri, manifest_digest, manifest_size, "
    "finalized_at, finalized_by, supersedes, reason"
)
_CLAIM_VER = (
    f"INSERT INTO {{ks}}.evidence_versions ({_VER_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "IF NOT EXISTS"
)
_GET_VERS = f"SELECT {_VER_COLS} FROM {{ks}}.evidence_versions WHERE evidence_id = ?"
_GET_VER = f"SELECT {_VER_COLS} FROM {{ks}}.evidence_versions WHERE evidence_id = ? AND version = ?"
_AUDIT = (
    "INSERT INTO {ks}.evidence_audit (evidence_id, at, seq, action, actor, detail) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
_AUDIT_LOG = "SELECT at, action, actor, detail FROM {ks}.evidence_audit WHERE evidence_id = ?"
_CAMPAIGN = (
    "INSERT INTO {ks}.evidence_campaigns (campaign_id, name, since, created_at) VALUES (?, ?, ?, ?)"
)
_CAMPAIGN_TARGET = (
    "INSERT INTO {ks}.evidence_campaigns (campaign_id, target_id, added_at) VALUES (?, ?, ?)"
)
_CAMPAIGN_INDEX = "INSERT INTO {ks}.evidence_campaign_index (bucket, campaign_id) VALUES (?, ?)"
_GET_CAMPAIGN = (
    "SELECT name, since, created_at, target_id FROM {ks}.evidence_campaigns WHERE campaign_id = ?"
)
_CAMPAIGNS = "SELECT campaign_id FROM {ks}.evidence_campaign_index WHERE bucket = ?"


def _version(r: Any) -> FinalizedVersion:
    return FinalizedVersion(
        evidence_id=EvidenceId.from_uuid(r.evidence_id),
        version=r.version,
        status=r.status,
        manifest_uri=r.manifest_uri,
        manifest_digest=ContentDigest(r.manifest_digest),
        manifest_size=r.manifest_size,
        finalized_at=utc(r.finalized_at),
        finalized_by=r.finalized_by,
        supersedes=r.supersedes,
        reason=r.reason,
    )


def _ms(value: datetime) -> datetime:
    """Scylla timestamps keep milliseconds; truncate so a read equals what was written."""
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


class ScyllaEvidenceStore:
    def __init__(self, session: ScyllaSession) -> None:
        self._s = session

    def candidate(self, evidence_id: EvidenceId) -> StoredCandidate | None:
        rows = self._s.execute(self._s.bind(_GET_CAND, STRONG, (evidence_id.uuid,)))
        if not rows or rows[0].doc is None:
            return None
        r = rows[0]
        return StoredCandidate(
            dossier=EvidenceDossier.model_validate_json(r.doc),
            status=CandidateStatus(r.status),
            revision=r.revision,
            updated_at=utc(r.updated_at),
            validation=ValidationReport.model_validate_json(r.validation) if r.validation else None,
            validated_at=utc(r.validated_at) if r.validated_at else None,
        )

    def put_candidate(
        self,
        dossier: EvidenceDossier,
        *,
        status: CandidateStatus,
        revision: int,
        updated_at: datetime,
    ) -> None:
        d = dossier
        evidence_id = EvidenceId(d.evidence_id)
        self._s.execute(
            self._s.bind(
                _PUT_CAND,
                AUTH,
                (
                    evidence_id.uuid,
                    d.match.match_id.uuid,
                    d.match.target.target_id.uuid,
                    status.value,
                    revision,
                    d.collected_at,
                    updated_at,
                    d.model_dump_json(by_alias=True),
                ),
            )
        )
        # E4 (V001), so a candidate is listed per target before it is finalized; the same
        # derived upsert E2 creation writes at finalization (identical values).
        self._s.execute(
            self._s.bind(
                _BY_TARGET,
                Consistency.DERIVED_WRITE,
                (
                    d.match.match_id.uuid,
                    d.collected_at,
                    d.match.target.target_id.uuid,
                    month_bucket(d.collected_at),
                    shard_of(evidence_id, MATCH_MONTH_SHARDS),
                    evidence_id.uuid,
                ),
            )
        )

    def set_status(self, evidence_id: EvidenceId, status: CandidateStatus) -> None:
        self._s.execute(self._s.bind(_SET_STATUS, AUTH, (status.value, evidence_id.uuid)))

    def record_validation(
        self, evidence_id: EvidenceId, report: ValidationReport, *, at: datetime
    ) -> None:
        self._s.execute(
            self._s.bind(_SET_VALIDATION, AUTH, (report.model_dump_json(), at, evidence_id.uuid))
        )

    def versions(self, evidence_id: EvidenceId) -> list[FinalizedVersion]:
        rows = self._serial(self._s.bind(_GET_VERS, SERIAL, (evidence_id.uuid,)))
        return [_version(r) for r in rows]

    def claim_version(self, row: FinalizedVersion) -> tuple[bool, FinalizedVersion]:
        stored = replace(row, finalized_at=_ms(row.finalized_at))
        rows = self._serial(
            self._s.bind(
                _CLAIM_VER,
                SERIAL,
                (
                    stored.evidence_id.uuid,
                    stored.version,
                    stored.status,
                    stored.manifest_uri,
                    str(stored.manifest_digest),
                    stored.manifest_size,
                    stored.finalized_at,
                    stored.finalized_by,
                    stored.supersedes,
                    stored.reason,
                ),
            )
        )
        if rows[0].applied:
            return True, stored
        existing = self._serial(
            self._s.bind(_GET_VER, SERIAL, (stored.evidence_id.uuid, stored.version))
        )
        return False, _version(existing[0])

    def audit(
        self, evidence_id: EvidenceId, action: str, *, actor: str, detail: str, at: datetime
    ) -> None:
        self._s.execute(
            self._s.bind(
                _AUDIT,
                AUTH,
                (evidence_id.uuid, at, uuid.uuid1(), action, actor, detail[:2000]),
            )
        )

    def audit_log(self, evidence_id: EvidenceId) -> list[AuditEntry]:
        rows = self._s.execute(self._s.bind(_AUDIT_LOG, STRONG, (evidence_id.uuid,)))
        return [AuditEntry(utc(r.at), r.action, r.actor, r.detail) for r in rows]

    def create_campaign(self, campaign: Campaign) -> None:
        c = campaign
        if not CAMPAIGN_ID.fullmatch(c.campaign_id):
            raise ValueError(f"campaign id must match {CAMPAIGN_ID.pattern}: {c.campaign_id!r}")
        self._s.execute_all(
            [
                self._s.bind(_CAMPAIGN, AUTH, (c.campaign_id, c.name, c.since, c.created_at)),
                self._s.bind(_CAMPAIGN_INDEX, AUTH, (_INDEX_BUCKET, c.campaign_id)),
                *(
                    self._s.bind(_CAMPAIGN_TARGET, AUTH, (c.campaign_id, t.uuid, c.created_at))
                    for t in c.target_ids
                ),
            ]
        )

    def add_campaign_target(self, campaign_id: str, target_id: TargetId, *, at: datetime) -> None:
        self._s.execute(self._s.bind(_CAMPAIGN_TARGET, AUTH, (campaign_id, target_id.uuid, at)))

    def campaign(self, campaign_id: str) -> Campaign | None:
        rows = self._s.execute(self._s.bind(_GET_CAMPAIGN, STRONG, (campaign_id,)))
        if not rows or rows[0].name is None:
            return None
        r = rows[0]
        return Campaign(
            campaign_id=campaign_id,
            name=r.name,
            since=utc(r.since),
            created_at=utc(r.created_at),
            target_ids=tuple(
                sorted(
                    (TargetId.from_uuid(x.target_id) for x in rows if x.target_id is not None),
                    key=str,
                )
            ),
        )

    def campaigns(self) -> list[str]:
        rows = self._s.execute(self._s.bind(_CAMPAIGNS, STRONG, (_INDEX_BUCKET,)))
        return sorted(r.campaign_id for r in rows)

    def _serial(self, statement: Any) -> list[Any]:
        """LWT with retry of contention timeouts (every CAS here is safe to repeat)."""
        for attempt in range(_CAS_ATTEMPTS):
            try:
                return self._s.execute(statement)
            except StorageUnavailableError:
                if attempt == _CAS_ATTEMPTS - 1:
                    raise
                time.sleep(random.uniform(0.05, 0.2) * 2**attempt)  # noqa: S311 — jitter
        raise AssertionError("unreachable")  # pragma: no cover
