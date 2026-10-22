"""Case package export (design §10): directory (and optional ``.tar.gz``) for offline review.

Layout (every name is derived from a validated campaign id, an evidence id,
a version number or a hex digest; nothing comes from a URL or a page)::

    case.json                          canonical case manifest (sorted keys, no export time)
    report.html                        human-readable report generated from case.json
    evidence/<evidence_id>/manifest-v<N>.json   exact sealed manifest bytes (all versions)
    artifacts/sha256/<hex>             verified artifact bytes (content-addressed, deduplicated)
    SHA256SUMS                         digest of every other file

Only finalized versions are packaged; candidates are listed by status. Every
artifact is re-verified against its SHA-256 while copying. An artifact or
manifest that cannot be packaged is recorded in ``case.json``
(``export_failures``) and makes the export fail (non-zero exit); nothing is
silently dropped. Identical stored inputs give byte-identical packages.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.models.blobs import BlobRef

from crawler2.evidence.collector import EvidenceMetrics
from crawler2.evidence.manifest import DISCLAIMER, canonical_bytes, ts
from crawler2.evidence.model import ArtifactState
from crawler2.evidence.report import render_report
from crawler2.evidence.stats import CampaignData, campaign_stats
from crawler2.evidence.validate import ArtifactReader, verify_blob

CASE_SCHEMA = "crawler2.evidence.case/v1"
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ATTESTATION = {
    "trusted_timestamp": "not_configured",
    "signature": "not_configured",
    "note": "No RFC 3161 timestamp authority or signing key is configured. Integrity rests "
    "on SHA-256 digests and application-level write-once storage only; this is not an "
    "independent proof of time or of chain of custody.",
}


class UnsafePathError(ValueError):
    pass


def safe_relpath(path: str) -> PurePosixPath:
    """A portable relative path: safe segments only, no absolute, empty, ``.`` or ``..``."""
    rel = PurePosixPath(path)
    if rel.is_absolute() or not rel.parts or path != rel.as_posix():
        raise UnsafePathError(f"not a normalized relative path: {path!r}")
    for part in rel.parts:
        if part in (".", "..") or not _SAFE.fullmatch(part):
            raise UnsafePathError(f"unsafe path segment {part!r} in {path!r}")
    return rel


@dataclass
class _Package:
    root: Path
    max_bytes: int
    files: dict[str, bytes] = field(default_factory=dict)
    written: int = 0

    def add(self, path: str, data: bytes) -> str:
        rel = safe_relpath(path).as_posix()
        if rel in self.files:
            if self.files[rel] != data:
                raise UnsafePathError(f"two different files for {rel}")
            return rel
        self.written += len(data)
        if self.written > self.max_bytes:
            raise OverflowError(f"package exceeds max_package_bytes ({self.max_bytes})")
        self.files[rel] = data
        return rel


@dataclass(frozen=True, slots=True)
class ExportResult:
    directory: Path
    archive: Path | None
    case_digest: ContentDigest
    evidence_items: int
    files: int
    failures: tuple[str, ...]


def build_package(
    data: CampaignData,
    *,
    objects: ArtifactReader,
    bucket: str | None,
    include_artifacts: bool,
    max_artifact_bytes: int,
    max_package_bytes: int,
) -> tuple[dict[str, bytes], tuple[str, ...]]:
    """Every file of the package in memory (bounded by ``max_package_bytes``) + failures."""
    pkg = _Package(Path("."), max_package_bytes)
    failures: list[str] = []
    entries: list[dict[str, Any]] = []
    manifests: dict[str, dict[str, Any]] = {}
    artifacts: dict[str, dict[str, Any]] = {}
    not_finalized: list[dict[str, Any]] = []
    for item in data.evidence:
        eid = str(item.evidence_id)
        if item.latest is None:
            not_finalized.append(
                {
                    "evidence_id": eid,
                    "status": item.candidate.status.value if item.candidate else "unknown",
                }
            )
            continue
        versions = []
        for v in sorted(item.versions, key=lambda v: v.version):
            ref = BlobRef(
                uri=v.manifest_uri,
                digest=v.manifest_digest,
                size_bytes=v.manifest_size,
                media_type="application/json",
            )
            state, detail = verify_blob(objects, ref, bucket=bucket, max_bytes=max_artifact_bytes)
            path = f"evidence/{eid}/manifest-v{v.version}.json"
            if state is not ArtifactState.VERIFIED:
                failures.append(f"{eid} v{v.version} manifest {state.value}: {detail}")
                path_or_none = None
            else:
                raw = objects.get(ref)
                try:
                    path_or_none = pkg.add(path, raw)
                except OverflowError as exc:
                    failures.append(f"{eid} v{v.version} manifest: {exc}")
                    path_or_none = None
                if v is item.latest:
                    manifests[eid] = json.loads(raw)
            versions.append(
                {
                    "version": v.version,
                    "status": v.status,
                    "manifest_sha256": v.manifest_digest,
                    "manifest_size": v.manifest_size,
                    "path": path_or_none,
                    "finalized_at": ts(v.finalized_at),
                    "supersedes": v.supersedes,
                    "reason": v.reason,
                }
            )
        doc = manifests.get(eid)
        claims = doc["claims"] if doc else {}
        entries.append(
            {
                "evidence_id": eid,
                "latest_version": item.latest.version,
                "status": item.latest.status,
                "match_id": claims.get("match_id"),
                "verdict": claims.get("verdict"),
                "confidence": claims.get("confidence"),
                "decided_at": claims.get("decided_at"),
                "collected_at": doc["evidence"]["collected_at"] if doc else None,
                "target_id": claims.get("target_id"),
                "source_domains": doc["derived"]["source_domains"] if doc else [],
                "versions": versions,
            }
        )
        for a in doc["artifacts"] if doc else []:
            _collect_artifact(a, eid, artifacts)

    for digest, a in sorted(artifacts.items()):
        if a["recorded_state"] != ArtifactState.VERIFIED.value:
            a["export_state"] = "not_packaged"
            continue
        if not include_artifacts:
            a["export_state"] = "referenced"
            continue
        if a["size_bytes"] > max_artifact_bytes:
            a["export_state"] = "too_large_referenced"
            continue
        ref = BlobRef(uri=a["uri"], digest=ContentDigest(digest), size_bytes=a["size_bytes"])
        state, detail = verify_blob(objects, ref, bucket=bucket, max_bytes=max_artifact_bytes)
        if state is not ArtifactState.VERIFIED:
            a["export_state"] = f"failed_{state.value}"
            failures.append(f"artifact {digest} {state.value}: {detail}")
            continue
        try:
            a["path"] = pkg.add(f"artifacts/sha256/{ContentDigest(digest).hex}", objects.get(ref))
        except OverflowError as exc:
            a["export_state"] = "failed_package_limit"
            failures.append(f"artifact {digest}: {exc}")
            continue
        a["export_state"] = "included"

    targets = []
    for tid, tstate in sorted(data.targets.items()):
        t = tstate.target if tstate is not None else None
        targets.append(
            {
                "target_id": tid,
                "version": t.ref.version if t else None,
                "title": t.title if t else None,
                "kind": t.kind.value if t else None,
            }
        )
    case = {
        "schema": CASE_SCHEMA,
        "campaign": {
            "campaign_id": data.campaign.campaign_id,
            "name": data.campaign.name,
            "since": ts(data.campaign.since),
            "targets": targets,
        },
        "evidence": entries,
        "not_finalized": not_finalized,
        "artifacts": [a for _, a in sorted(artifacts.items())],
        "statistics": campaign_stats(data),
        "attestation": ATTESTATION,
        "export_failures": sorted(failures),
        "disclaimer": DISCLAIMER,
    }
    case_bytes = canonical_bytes(case)
    pkg.add("case.json", case_bytes)
    pkg.add("report.html", render_report(case, manifests).encode("utf-8"))
    sums = "".join(
        f"{hashlib.sha256(body).hexdigest()}  {name}\n" for name, body in sorted(pkg.files.items())
    )
    pkg.add("SHA256SUMS", sums.encode("ascii"))
    return pkg.files, tuple(sorted(failures))


def _collect_artifact(a: dict[str, Any], eid: str, into: dict[str, dict[str, Any]]) -> None:
    digest = a.get("sha256")
    key = digest or f"none:{a['role']}:{eid}:{a.get('subject') or ''}"
    entry = into.setdefault(
        key,
        {
            "sha256": digest,
            "role": a["role"],
            "uri": a.get("uri"),
            "size_bytes": a.get("size_bytes"),
            "media_type": a.get("media_type"),
            "recorded_state": a["state"],
            "path": None,
            "evidence_ids": [],
            "subjects": [],
        },
    )
    if eid not in entry["evidence_ids"]:
        entry["evidence_ids"] = sorted([*entry["evidence_ids"], eid])
    subject = a.get("subject")
    if subject and subject not in entry["subjects"]:
        entry["subjects"] = sorted([*entry["subjects"], subject])


def write_package(files: dict[str, bytes], out: Path, *, archive: bool) -> tuple[Path, Path | None]:
    """Write into ``out`` (must not exist, or be empty); optional deterministic tar.gz beside it."""
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise FileExistsError(f"{out} exists and is not an empty directory; refusing to overwrite")
    out.mkdir(parents=True, exist_ok=True)
    root = out.resolve()
    for name, body in sorted(files.items()):
        rel = safe_relpath(name)
        target = (root / rel).resolve()
        if root not in target.parents:
            raise UnsafePathError(f"{name} escapes the package directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    if not archive:
        return out, None
    tar_path = out.with_name(out.name + ".tar.gz")
    if tar_path.exists():
        raise FileExistsError(f"{tar_path} exists; refusing to overwrite")
    tar_path.write_bytes(deterministic_tar(files, prefix=out.name))
    return out, tar_path


def deterministic_tar(files: dict[str, bytes], *, prefix: str) -> bytes:
    """gzip(tar) with fixed mtimes, owners and modes, members sorted: same files → same bytes."""
    safe_relpath(prefix)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, body in sorted(files.items()):
            info = tarfile.TarInfo(f"{prefix}/{safe_relpath(name).as_posix()}")
            info.size, info.mtime, info.mode = len(body), 0, 0o444
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(body))
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def export_case(
    data: CampaignData,
    *,
    objects: ArtifactReader,
    bucket: str | None,
    out: Path,
    include_artifacts: bool,
    archive: bool,
    max_artifact_bytes: int,
    max_package_bytes: int,
    metrics: EvidenceMetrics | None = None,
) -> ExportResult:
    m = metrics or EvidenceMetrics(None)
    try:
        files, failures = build_package(
            data,
            objects=objects,
            bucket=bucket,
            include_artifacts=include_artifacts,
            max_artifact_bytes=max_artifact_bytes,
            max_package_bytes=max_package_bytes,
        )
        directory, tar_path = write_package(files, out, archive=archive)
    except Exception:
        m.export("error")
        raise
    m.export("failed" if failures else "ok")
    return ExportResult(
        directory=directory,
        archive=tar_path,
        case_digest=ContentDigest.of_bytes(files["case.json"]),
        evidence_items=sum(1 for e in data.evidence if e.latest is not None),
        files=len(files),
        failures=failures,
    )
