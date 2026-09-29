"""Seed files → P3 admissions with provenance (design §16).

A seed file is one URL per line; blank lines and ``#`` comments are
skipped. Lines are canonicalized with P1 exactly as written: no scheme is
invented and nothing is rewritten, so an unusable line is reported, never
guessed at. Loading is idempotent (F9 rows are keyed by source and URL).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from antipiracy_contracts.models.web import UrlRef
from antipiracy_contracts.urls import InvalidUrlError

from crawler2.discovery.admission import Admitter, Candidate, Origin, Scope
from crawler2.filtering.inputs import host_of
from crawler2.storage.repositories import DiscoveryRepository, SeedRecord


@dataclass(frozen=True, slots=True)
class SeedLine:
    line: int
    original: str
    url: UrlRef | None
    error: str | None = None


def parse_seed_file(text: str) -> list[SeedLine]:
    """Every non-comment line, canonicalized; duplicates are kept (reported by the loader)."""
    lines = []
    for number, raw in enumerate(text.splitlines(), start=1):
        original = raw.strip()
        if not original or original.startswith("#"):
            continue
        if "://" not in original:
            lines.append(SeedLine(number, original, None, "no scheme (not invented)"))
            continue
        try:
            ref = UrlRef.of(original)
        except (InvalidUrlError, ValueError) as exc:
            lines.append(SeedLine(number, original, None, str(exc)[:200]))
            continue
        if host_of(ref.url) is None:
            lines.append(SeedLine(number, original, None, "not an http(s) URL"))
            continue
        lines.append(SeedLine(number, original, ref))
    return lines


@dataclass
class SeedReport:
    source: str
    file: str
    sha256: str
    lines: int = 0
    valid: int = 0
    duplicates: int = 0
    invalid: list[str] = field(default_factory=list)
    outcomes: dict[str, int] = field(default_factory=dict)


def load_seeds(
    path: Path,
    *,
    source: str,
    metadata: Mapping[str, str],
    admitter: Admitter,
    scope: Scope,
    repo: DiscoveryRepository,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SeedReport:
    """Record provenance (F9), root the seed sites (F11) and admit every valid seed."""
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    now = clock()
    parsed = parse_seed_file(raw.decode("utf-8"))
    report = SeedReport(source=source, file=str(path), sha256=digest, lines=len(parsed))
    seen: dict[str, SeedLine] = {}
    for item in parsed:
        if item.url is None:
            report.invalid.append(f"line {item.line}: {item.original[:200]} ({item.error})")
            continue
        key = str(item.url.url_id)
        if key in seen:
            report.duplicates += 1
            continue
        seen[key] = item
    valid = sorted(seen.values(), key=lambda s: str(s.url.url_id) if s.url else "")
    report.valid = len(valid)
    records = [
        SeedRecord(
            seed_source=source,
            url=s.url,
            original=s.original,
            file_path=str(path),
            file_sha256=digest,
            line=s.line,
            imported_at=now,
            metadata=dict(metadata),
        )
        for s in valid
        if s.url is not None
    ]
    repo.record_seeds(records)
    scope.add(
        (host_of(r.url.url) or "" for r in records),
        origin="seed",
        at=now,
        detail=f"{source}:{digest[:12]}",
    )
    outcomes = admitter.admit(
        [Candidate(r.url, Origin.SEED) for r in records],
        revisit_after_s=0.0,
    )
    report.outcomes = dict(sorted(Counter(o.value for o in outcomes.values()).items()))
    return report
