"""V1 ``domain_blacklist.txt`` → P6 rules through a reviewed manifest (design §8.1).

The V1 file has no provenance (entries were appended at runtime by
heuristics, audit F-1..F-5), so nothing is enabled without a review
verdict: hosts absent from the manifest are quarantined as ``unreviewed``.
Nothing is silently discarded; invalid lines are reported as rejected.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from crawler2.filtering.model import (
    ALL_CONTEXTS,
    Action,
    Classification,
    Context,
    Rule,
    RuleKind,
    RuleSource,
)

IMPORTER_VERSION: Final = "p6-v1-blacklist/v1"
DEFAULT_MANIFEST: Final = Path(__file__).with_name("data") / "v1_blacklist_review.toml"
_HOST_RE: Final = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)+$"
)


@dataclass(frozen=True, slots=True)
class Verdict:
    category: str
    migrate: bool
    classification: Classification
    confidence: float
    reason: str
    action: Action | None
    contexts: frozenset[Context]


@dataclass(frozen=True, slots=True)
class Manifest:
    verdicts: dict[str, Verdict]
    review: dict[str, str]
    digest: str

    @classmethod
    def load(cls, path: Path = DEFAULT_MANIFEST) -> Manifest:
        raw = path.read_bytes()
        doc = tomllib.loads(raw.decode())
        verdicts: dict[str, Verdict] = {}
        for name, cat in sorted(doc["categories"].items()):
            migrate = cat["verdict"] == "migrate"
            if cat["verdict"] not in ("migrate", "quarantine"):
                raise ValueError(f"category {name}: unknown verdict {cat['verdict']!r}")
            verdict = Verdict(
                category=name,
                migrate=migrate,
                classification=Classification(cat.get("classification", "unknown")),
                confidence=float(cat.get("confidence", 0.9)),
                reason=cat["reason"],
                action=Action(cat["action"]) if cat.get("action") else None,
                contexts=frozenset(Context(c) for c in cat["contexts"])
                if "contexts" in cat
                else ALL_CONTEXTS,
            )
            for host in cat["hosts"]:
                if host in verdicts:
                    raise ValueError(f"{host} is listed in two categories")
                verdicts[host] = verdict
        return cls(verdicts, dict(doc.get("review", {})), hashlib.sha256(raw).hexdigest())


@dataclass
class V1Report:
    lines: int = 0
    comments: int = 0
    blank: int = 0
    entries: int = 0
    imported: int = 0
    quarantined: int = 0
    rejected: int = 0
    transformed: int = 0
    duplicated: int = 0
    ambiguous: int = 0
    """Quarantined by an explicit review verdict (the reviewer could not accept it as-is)."""
    unreviewed: int = 0
    by_category: Counter[str] = field(default_factory=Counter)
    rejects: list[str] = field(default_factory=list)
    transforms: list[str] = field(default_factory=list)

    def to_doc(self) -> dict[str, Any]:
        return {
            "importer": IMPORTER_VERSION,
            "lines": self.lines,
            "comments": self.comments,
            "blank": self.blank,
            "entries": self.entries,
            "imported": self.imported,
            "quarantined": self.quarantined,
            "rejected": self.rejected,
            "transformed": self.transformed,
            "duplicated": self.duplicated,
            "ambiguous": self.ambiguous,
            "unreviewed": self.unreviewed,
            "by_category": dict(sorted(self.by_category.items())),
            "rejects": self.rejects,
            "transforms": self.transforms,
        }


def normalize_entry(entry: str) -> str | None:
    """A V1 line as a host (V1 accepted URLs too); None if it is not a host or IP."""
    text = entry.strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0]
    if text.count(":") == 1:
        text = text.split(":", 1)[0]
    text = text.strip(".")
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return text if _HOST_RE.match(text) else None
    return text


def parse_blacklist(text: str, manifest: Manifest) -> tuple[list[Rule], V1Report]:
    report = V1Report()
    rules: dict[str, Rule] = {}
    origins: dict[str, list[int]] = {}
    for line_no, raw in enumerate(text.splitlines(), start=1):
        report.lines += 1
        line = raw.strip()
        if not line:
            report.blank += 1
            continue
        if line.startswith("#"):
            report.comments += 1
            continue
        report.entries += 1
        host = normalize_entry(line)
        if host is None:
            report.rejected += 1
            report.rejects.append(f"line {line_no}: {line[:200]}")
            continue
        if host != line:
            report.transformed += 1
            report.transforms.append(f"line {line_no}: {line[:200]} -> {host}")
        if host in origins:
            report.duplicated += 1
            origins[host].append(line_no)
            continue
        origins[host] = [line_no]
    for host, lines in sorted(origins.items()):
        verdict = manifest.verdicts.get(host)
        origin = f"lines {','.join(map(str, lines))}: {host}"
        if verdict is None:
            report.quarantined += 1
            report.unreviewed += 1
            report.by_category["unreviewed"] += 1
            rule = _rule(host, Classification.UNKNOWN, 0.0, None, ALL_CONTEXTS, "unreviewed")
            rules[host] = _disabled(rule, origin)
            continue
        report.by_category[verdict.category] += 1
        rule = _rule(
            host,
            verdict.classification,
            verdict.confidence,
            verdict.action,
            verdict.contexts,
            verdict.reason,
        )
        if verdict.migrate:
            report.imported += 1
            rules[host] = Rule(**{**_fields(rule), "origin": origin})
        else:
            report.quarantined += 1
            report.ambiguous += 1
            rules[host] = _disabled(rule, origin)
    return sorted(rules.values(), key=lambda r: r.rule_id), report


def _rule(
    host: str,
    classification: Classification,
    confidence: float,
    action: Action | None,
    contexts: frozenset[Context],
    reason: str,
) -> Rule:
    return Rule(
        source=RuleSource.V1_BLACKLIST,
        kind=RuleKind.HOST,
        pattern=host,
        classification=classification,
        confidence=confidence,
        action=action,
        contexts=contexts,
        reason=reason,
    )


def _fields(rule: Rule) -> dict[str, Any]:
    return {name: getattr(rule, name) for name in rule.__dataclass_fields__ if name != "rule_id"}


def _disabled(rule: Rule, origin: str) -> Rule:
    return Rule(**{**_fields(rule), "enabled": False, "origin": origin})
