"""ABP / uBlock Origin network-filter lists → P6 rules (design §8.2, audit §6).

Only the network-filter subset the project needs is supported. Every other
line is counted by category and skipped; nothing is approximated. The
parser is pure: downloading, hashing and storing happen in the CLI.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Final

from crawler2.filtering.model import (
    Classification,
    Party,
    Rule,
    RuleKind,
    RuleSource,
)
from crawler2.filtering.patterns import PatternError, compile_pattern, host_only

IMPORTER_VERSION: Final = "p6-abp/v1"
DEFAULT_CONFIDENCE: Final = 0.95
DEFAULT_CLASS: Final = {
    RuleSource.EASYLIST: Classification.AD,
    RuleSource.EASYPRIVACY: Classification.TRACKER,
    RuleSource.UBLOCK: Classification.AD,
}

_TYPE_OPTIONS: Final = {
    "script": "script",
    "image": "image",
    "stylesheet": "stylesheet",
    "css": "stylesheet",
    "xmlhttprequest": "xmlhttprequest",
    "xhr": "xmlhttprequest",
    "subdocument": "subdocument",
    "frame": "subdocument",
    "media": "media",
    "font": "font",
    "ping": "ping",
    "beacon": "ping",
    "websocket": "websocket",
    "other": "other",
    "object": "other",
    "document": "document",
    "doc": "document",
    "popup": "popup",
}
_PARTY_OPTIONS: Final = {
    "third-party": Party.THIRD,
    "3p": Party.THIRD,
    "~first-party": Party.THIRD,
    "~1p": Party.THIRD,
    "~third-party": Party.FIRST,
    "~3p": Party.FIRST,
    "first-party": Party.FIRST,
    "1p": Party.FIRST,
}
_HEADER_RE: Final = re.compile(r"^!\s*([A-Za-z][A-Za-z \-]*?)\s*:\s*(.+?)\s*$")
_HEADER_KEYS: Final = {
    "title",
    "version",
    "last modified",
    "homepage",
    "license",
    "licence",
    "expires",
}
_DOMAIN_RE: Final = re.compile(r"^[a-z0-9\-]+(\.[a-z0-9\-]+)+$")
_EXAMPLES: Final = 5


@dataclass
class ImportReport:
    source: str
    lines: int = 0
    comments: int = 0
    rules: int = 0
    exceptions: int = 0
    host_rules: int = 0
    pattern_rules: int = 0
    duplicates: int = 0
    unsupported: Counter[str] = field(default_factory=Counter)
    examples: dict[str, list[str]] = field(default_factory=dict)
    header: dict[str, str] = field(default_factory=dict)

    def skip(self, category: str, line_no: int, text: str) -> None:
        self.unsupported[category] += 1
        bucket = self.examples.setdefault(category, [])
        if len(bucket) < _EXAMPLES:
            bucket.append(f"line {line_no}: {text[:200]}")

    def to_doc(self) -> dict[str, object]:
        return {
            "importer": IMPORTER_VERSION,
            "source": self.source,
            "lines": self.lines,
            "comments": self.comments,
            "rules": self.rules,
            "exceptions": self.exceptions,
            "host_rules": self.host_rules,
            "pattern_rules": self.pattern_rules,
            "duplicates": self.duplicates,
            "unsupported": dict(sorted(self.unsupported.items())),
            "unsupported_total": sum(self.unsupported.values()),
            "examples": dict(sorted(self.examples.items())),
            "header": self.header,
        }


class _UnsupportedError(Exception):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def parse_list(
    text: str,
    source: RuleSource,
    *,
    classification: Classification | None = None,
    confidence: float = DEFAULT_CONFIDENCE,
) -> tuple[list[Rule], ImportReport]:
    """Parse a filter list; rules are unique by ``rule_id`` and sorted by it."""
    if source not in DEFAULT_CLASS:
        raise ValueError(f"{source} is not an ABP-format source")
    klass = classification or DEFAULT_CLASS[source]
    report = ImportReport(source=source.value)
    rules: dict[str, Rule] = {}
    for line_no, raw in enumerate(text.splitlines(), start=1):
        report.lines += 1
        line = raw.strip()
        if not line or line.startswith("!") or (line.startswith("[") and line.endswith("]")):
            report.comments += 1
            header = _HEADER_RE.match(line)
            if header and header.group(1).lower() in _HEADER_KEYS:
                report.header.setdefault(header.group(1).lower(), header.group(2))
            continue
        try:
            rule = _parse_line(line, source, klass, confidence, line_no)
        except _UnsupportedError as exc:
            report.skip(exc.category, line_no, line)
            continue
        if rule.rule_id in rules:
            report.duplicates += 1
            continue
        rules[rule.rule_id] = rule
        report.rules += 1
        report.exceptions += rule.exception
        if rule.kind is RuleKind.HOST:
            report.host_rules += 1
        else:
            report.pattern_rules += 1
    return sorted(rules.values(), key=lambda r: r.rule_id), report


def _parse_line(
    line: str, source: RuleSource, klass: Classification, confidence: float, line_no: int
) -> Rule:
    for marker, category in (
        ("#@#", "cosmetic"),
        ("#?#", "cosmetic_procedural"),
        ("#$#", "cosmetic_css_injection"),
        ("#%#", "scriptlet"),
        ("##+js", "scriptlet"),
        ("##^", "html_filter"),
        ("##", "cosmetic"),
    ):
        if marker in line:
            raise _UnsupportedError(category)
    exception = line.startswith("@@")
    body = line[2:] if exception else line
    options: list[str] = []
    dollar = body.rfind("$")
    if dollar != -1 and not (body.startswith("/") and body.endswith("/")):
        options = [o.strip() for o in body[dollar + 1 :].split(",") if o.strip()]
        body = body[:dollar]
    if body.startswith("/") and body.endswith("/") and len(body) > 2:
        raise _UnsupportedError("regex")

    types: set[str] = set()
    excluded: set[str] = set()
    party = Party.ANY
    domains: set[str] = set()
    not_domains: set[str] = set()
    important = False
    for option in options:
        name = option.lower()
        negated = name.startswith("~")
        bare = name[1:] if negated else name
        if name in _PARTY_OPTIONS:
            party = _PARTY_OPTIONS[name]
        elif bare in _TYPE_OPTIONS:
            (excluded if negated else types).add(_TYPE_OPTIONS[bare])
        elif bare.startswith(("domain=", "from=")) and not negated:
            for value in bare.split("=", 1)[1].split("|"):
                target = not_domains if value.startswith("~") else domains
                domain = value.lstrip("~")
                if not _DOMAIN_RE.match(domain):
                    raise _UnsupportedError("option:domain_entity_or_invalid")
                target.add(domain)
        elif name == "important":
            important = True
        else:
            raise _UnsupportedError(f"option:{bare.split('=', 1)[0]}")
    if (exception and types & {"document"}) or (exception and not types and not body):
        raise _UnsupportedError("page_exception")
    if "popup" in types and len(types) == 1:
        raise _UnsupportedError("popup_only")

    host = host_only(body.lower())
    if host is not None:
        kind, pattern = RuleKind.HOST, host
    else:
        try:
            compile_pattern(body)
        except PatternError as exc:
            category = "match_all" if "every URL" in str(exc) else "invalid_pattern"
            raise _UnsupportedError(category) from exc
        kind, pattern = RuleKind.URL_PATTERN, body.lower()
    return Rule(
        source=source,
        kind=kind,
        pattern=pattern,
        classification=klass,
        confidence=confidence,
        exception=exception,
        important=important and not exception,
        resource_types=frozenset(types),
        excluded_types=frozenset(excluded),
        party=party,
        source_domains=frozenset(domains),
        excluded_source_domains=frozenset(not_domains),
        reason=f"{source.value}_filter",
        origin=f"line {line_no}: {line[:500]}",
    )
