"""``crawler2-filter``: import, publish, activate and explain P6 rules (design §8, §14).

Rules are written only by this operator tool, never by crawler processes.
Every import becomes an immutable source revision with its provenance;
``publish`` composes revisions into an immutable ruleset; ``activate`` and
``rollback`` move the one active pointer (compare-and-set). Running
processes pick up the new ruleset within ``filter.reload_interval_s``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from crawler2.core.configuration import Settings
from crawler2.core.observability import configure_logging
from crawler2.filtering import abp, builtin, v1import
from crawler2.filtering.engine import RulesetError
from crawler2.filtering.inputs import link_input, redirect_inputs, request_input
from crawler2.filtering.model import (
    ALL_CONTEXTS,
    Action,
    Classification,
    Context,
    FilterInput,
    Party,
    Policy,
    Rule,
    RuleKind,
    RuleSource,
)
from crawler2.filtering.store import SourceImport, load_engine, publish, store_source
from crawler2.storage.repositories import FilterRuleRepository

MIN_RULE_RATIO = 0.5
"""A new list revision with fewer than this share of the previous revision's rules is refused."""


def _now() -> datetime:
    return datetime.now(UTC)


def _print(doc: Any) -> None:
    print(json.dumps(doc, indent=2, sort_keys=True, default=str))


def _repo(settings: Settings) -> FilterRuleRepository:
    from crawler2.storage.scylla import ScyllaStorage

    return ScyllaStorage.open(settings.scylla, instance="crawler2-filter").filter_rules


def download(url: str, dest: Path, *, timeout_s: float = 900.0) -> bytes:
    """HTTPS only; the whole body or an error (httpx raises on a truncated transfer)."""
    if not url.startswith("https://"):
        raise SystemExit(f"refusing non-HTTPS list URL: {url}")
    with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        body = response.content
    declared = response.headers.get("content-length")
    identity = response.headers.get("content-encoding") in (None, "identity")
    if declared is not None and identity and int(declared) != len(body):
        raise SystemExit(f"truncated download: {len(body)} of {declared} bytes")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return body


def operator_rules(path: Path) -> list[Rule]:
    """Operator rule file (TOML ``[[rule]]`` tables); every rule needs a reason and a note."""
    doc = tomllib.loads(path.read_text("utf-8"))
    rules = []
    for index, item in enumerate(doc.get("rule", []), start=1):
        for key in ("kind", "pattern", "classification", "reason", "note"):
            if not item.get(key):
                raise SystemExit(f"{path}: rule {index} lacks {key!r}")
        rules.append(
            Rule(
                source=RuleSource.OPERATOR,
                kind=RuleKind(item["kind"]),
                pattern=str(item["pattern"]).lower()
                if item["kind"] != "selector"
                else item["pattern"],
                classification=Classification(item["classification"]),
                confidence=float(item.get("confidence", 1.0)),
                action=Action(item["action"]) if item.get("action") else None,
                exception=bool(item.get("exception", False)),
                resource_types=frozenset(item.get("types", [])),
                party=Party(item.get("party", "any")),
                contexts=frozenset(Context(c) for c in item["contexts"])
                if "contexts" in item
                else ALL_CONTEXTS,
                source_domains=frozenset(item.get("domains", [])),
                reason=item["reason"],
                note=item["note"],
                origin=f"{path.name} rule {index}",
            )
        )
    return rules


def _store(settings: Settings, item: SourceImport, report: dict[str, Any], args: Any) -> None:
    if args.dry_run:
        _print({"dry_run": True, "source": item.source.value, "report": report})
        return
    repo = _repo(settings)
    previous = repo.sources(item.source.value)
    enabled = sum(r.enabled for r in item.rules)
    if previous and not args.force and enabled < MIN_RULE_RATIO * previous[0].enabled_count:
        raise SystemExit(
            f"{item.source.value}: {enabled} enabled rules vs {previous[0].enabled_count} in the "
            "latest revision; refusing (use --force if intended)"
        )
    record = store_source(repo, item, by=args.by, at=_now())
    _print(
        {
            "source": record.source,
            "revision": record.revision,
            "rules": record.rule_count,
            "enabled": record.enabled_count,
            "report": report,
        }
    )


def cmd_import_v1(args: Any, settings: Settings) -> int:
    path = Path(args.path)
    raw = path.read_bytes()
    manifest = v1import.Manifest.load(
        Path(args.manifest) if args.manifest else v1import.DEFAULT_MANIFEST
    )
    rules, report = v1import.parse_blacklist(raw.decode("utf-8"), manifest)
    doc = report.to_doc() | {"manifest_sha256": manifest.digest, "review": manifest.review}
    item = SourceImport(
        source=RuleSource.V1_BLACKLIST,
        rules=rules,
        origin=str(path),
        input_sha256=hashlib.sha256(raw).hexdigest(),
        importer=v1import.IMPORTER_VERSION,
        report=doc,
    )
    _store(settings, item, doc, args)
    return 0


def cmd_import_abp(args: Any, settings: Settings) -> int:
    source = RuleSource(args.source)
    if args.url:
        raw = download(args.url, settings.scratch_dir / "filter-lists" / f"{source.value}.txt")
        origin = args.url
    else:
        raw = Path(args.file).read_bytes()
        origin = args.file
    rules, report = abp.parse_list(raw.decode("utf-8"), source)
    license_ = report.header.get("licence") or report.header.get("license") or args.license
    if not license_:
        raise SystemExit("the list states no licence; pass --license after checking it")
    doc = report.to_doc()
    item = SourceImport(
        source=source,
        rules=rules,
        origin=origin,
        input_sha256=hashlib.sha256(raw).hexdigest(),
        importer=abp.IMPORTER_VERSION,
        report=doc,
        license=license_,
        title=report.header.get("title"),
        list_version=report.header.get("version"),
    )
    _store(settings, item, doc, args)
    return 0


def cmd_import_builtin(args: Any, settings: Settings) -> int:
    rules = builtin.built_in_rules()
    code = Path(builtin.__file__).read_bytes()
    item = SourceImport(
        source=RuleSource.BUILT_IN,
        rules=rules,
        origin="crawler2/filtering/builtin.py",
        input_sha256=hashlib.sha256(code).hexdigest(),
        importer="p6-builtin/v1",
        report={"rules": len(rules)},
    )
    _store(settings, item, {"rules": len(rules)}, args)
    return 0


def cmd_import_operator(args: Any, settings: Settings) -> int:
    path = Path(args.path)
    rules = operator_rules(path)
    item = SourceImport(
        source=RuleSource.OPERATOR,
        rules=rules,
        origin=str(path),
        input_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        importer="p6-operator/v1",
        report={"rules": len(rules)},
    )
    _store(settings, item, {"rules": len(rules)}, args)
    return 0


def cmd_publish(args: Any, settings: Settings) -> int:
    repo = _repo(settings)
    pairs: list[tuple[str, str]] = []
    for spec in args.source:
        name, _, revision = spec.partition("@")
        if not revision or revision == "latest":
            revisions = repo.sources(name)
            if not revisions:
                raise SystemExit(f"no revision of {name}")
            revision = revisions[0].revision
        pairs.append((name, revision))
    policy = Policy(block_threshold=args.threshold)
    record, engine = publish(repo, pairs, policy, by=args.by, at=_now(), note=args.note)
    _print(
        {
            "ruleset": record.ruleset_id,
            "sources": record.sources,
            "rules": engine.rule_count,
            "kinds": engine.counts,
            "untokenized": engine.untokenized,
        }
    )
    return 0


def cmd_activate(args: Any, settings: Settings) -> int:
    repo = _repo(settings)
    try:
        load_engine(repo, args.ruleset)
    except RulesetError as exc:
        raise SystemExit(f"refusing to activate an invalid ruleset: {exc}") from exc
    current = repo.active(settings.filter.ruleset_name)
    expected = current.ruleset_id if current else None
    if not repo.activate(
        args.ruleset, expected=expected, by=args.by, at=_now(), name=settings.filter.ruleset_name
    ):
        raise SystemExit("the active ruleset changed concurrently; re-run")
    _print({"active": args.ruleset, "previous": expected})
    return 0


def cmd_rollback(args: Any, settings: Settings) -> int:
    repo = _repo(settings)
    current = repo.active(settings.filter.ruleset_name)
    if current is None or current.previous_ruleset_id is None:
        raise SystemExit("nothing to roll back to")
    args.ruleset = current.previous_ruleset_id
    return cmd_activate(args, settings)


def cmd_status(args: Any, settings: Settings) -> int:
    repo = _repo(settings)
    current = repo.active(settings.filter.ruleset_name)
    ruleset = repo.ruleset(current.ruleset_id) if current else None
    sources = []
    for name, revision in ruleset.sources if ruleset else ():
        record = repo.source(name, revision)
        if record is not None:
            sources.append(
                {
                    "source": name,
                    "revision": revision,
                    "origin": record.origin,
                    "license": record.license,
                    "version": record.list_version,
                    "rules": record.rule_count,
                    "enabled": record.enabled_count,
                    "imported_at": record.revision_at,
                }
            )
    _print({"active": current, "ruleset": ruleset, "sources": sources})
    return 0


def _input(args: Any) -> FilterInput | None:
    context = Context(args.context)
    if context is Context.REQUEST:
        return request_input(args.url, args.type, args.source_url)
    if context is Context.REDIRECT:
        inputs = redirect_inputs(args.source_url or args.url, [], args.url)
        return inputs[0] if inputs else None
    return link_input(args.url, source_url=args.source_url)


def cmd_explain(args: Any, settings: Settings) -> int:
    repo = _repo(settings)
    ruleset = args.ruleset or (repo.active(settings.filter.ruleset_name) or None)
    ruleset_id = ruleset if isinstance(ruleset, str) else ruleset.ruleset_id if ruleset else None
    if ruleset_id is None:
        raise SystemExit("no active ruleset")
    engine = load_engine(repo, ruleset_id)
    inp = _input(args)
    if inp is None:
        raise SystemExit("not an http(s) URL")
    candidates = engine.candidates(inp)
    _print(
        {
            "input": {
                "context": inp.context,
                "url": inp.url,
                "host": inp.host,
                "resource_type": inp.resource_type,
                "first_party_host": inp.source_host,
                "third_party": inp.third_party,
            },
            "decision": engine.decide(inp).to_doc(),
            "candidates": [r.to_doc() for r in candidates],
        }
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crawler2-filter", description=__doc__)
    parser.add_argument("--by", default="operator", help="who performs the change (provenance)")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_store_flags(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--dry-run", action="store_true", help="report only, write nothing")
        p.add_argument("--force", action="store_true", help="accept a large rule-count drop")
        return p

    p = with_store_flags(sub.add_parser("import-v1", help="import V1 domain_blacklist.txt"))
    p.add_argument("path")
    p.add_argument("--manifest", default=None)
    p.set_defaults(func=cmd_import_v1)
    p = with_store_flags(sub.add_parser("import-abp", help="import an EasyList-format list"))
    p.add_argument("--source", required=True, choices=["easylist", "easyprivacy", "ublock"])
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--url")
    group.add_argument("--file")
    p.add_argument("--license", default=None)
    p.set_defaults(func=cmd_import_abp)
    p = with_store_flags(sub.add_parser("import-builtin", help="store the built-in rules"))
    p.set_defaults(func=cmd_import_builtin)
    p = with_store_flags(sub.add_parser("import-operator", help="import an operator rule file"))
    p.add_argument("path")
    p.set_defaults(func=cmd_import_operator)
    p = sub.add_parser("publish", help="compose source revisions into a ruleset")
    p.add_argument("--source", action="append", required=True, help="name[@revision|@latest]")
    p.add_argument("--threshold", type=float, default=0.9)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_publish)
    p = sub.add_parser("activate", help="make a ruleset active (compare-and-set)")
    p.add_argument("ruleset")
    p.set_defaults(func=cmd_activate)
    sub.add_parser("rollback", help="re-activate the previous ruleset").set_defaults(
        func=cmd_rollback
    )
    sub.add_parser("status", help="show the active ruleset and its sources").set_defaults(
        func=cmd_status
    )
    p = sub.add_parser("explain", help="explain the decision for a URL")
    p.add_argument("url")
    p.add_argument("--source-url", default=None, help="linking page / requesting page")
    p.add_argument("--context", choices=[c.value for c in Context], default="link")
    p.add_argument("--type", default="document", help="Playwright resource type (request)")
    p.add_argument("--ruleset", default=None)
    p.set_defaults(func=cmd_explain)
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings)
    result: int = args.func(args, settings)
    return result


if __name__ == "__main__":
    sys.exit(main())
