"""Deterministic JSON Schema export (ADR-010).

The pydantic models are the source of truth; the committed files under
``schema_json/`` are generated from them for non-Python consumers and for
review (a contract change shows up as a schema diff). A test fails when
the committed files are stale. Regenerate with ``make schemas``.

Usage: ``python -m antipiracy_contracts.schemas [--check] [DIRECTORY]``
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import JsonValue

from antipiracy_contracts.events.catalog import CATALOG
from antipiracy_contracts.events.envelope import ENVELOPE_VERSION, envelope_model

SCHEMA_DIR = Path(__file__).parent / "schema_json"


def _dump(document: JsonValue) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def event_schema_path(event_type: str, major: int) -> str:
    return f"events/{event_type}.v{major}.json"


def build_schemas() -> dict[str, str]:
    """Relative path -> file content for every generated schema file."""
    files: dict[str, str] = {}
    catalog_entries: list[JsonValue] = []
    for spec in CATALOG:
        path = event_schema_path(spec.event_type, spec.major)
        schema = envelope_model(spec.payload).model_json_schema(mode="validation")
        schema["title"] = f"{spec.event_type} v{spec.schema_version}"
        files[path] = _dump(schema)
        catalog_entries.append(
            {
                "event_type": spec.event_type,
                "schema_version": spec.schema_version,
                "producer": spec.producer.value,
                "producer_service": spec.producer.service.value,
                "consumers": [consumer.value for consumer in spec.consumers],
                "meaning": spec.meaning,
                "idempotency_key": list(spec.idempotency_key),
                "ordering": spec.ordering,
                "delivery": "at-least-once; duplicates allowed",
                "schema": path,
            }
        )
    files["catalog.json"] = _dump({"envelope_version": ENVELOPE_VERSION, "events": catalog_entries})
    return files


def write_schemas(directory: Path = SCHEMA_DIR) -> None:
    expected = build_schemas()
    for stale in _existing(directory) - expected.keys():
        (directory / stale).unlink()
    for relative, content in expected.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def stale_schemas(directory: Path = SCHEMA_DIR) -> list[str]:
    """Paths that are missing, different or no longer generated."""
    expected = build_schemas()
    problems = [
        relative
        for relative, content in expected.items()
        if not (directory / relative).is_file()
        or (directory / relative).read_text(encoding="utf-8") != content
    ]
    problems.extend(_existing(directory) - expected.keys())
    return sorted(problems)


def _existing(directory: Path) -> set[str]:
    return {path.relative_to(directory).as_posix() for path in directory.rglob("*.json")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="fail if files are stale")
    parser.add_argument("directory", nargs="?", type=Path, default=SCHEMA_DIR)
    args = parser.parse_args(argv)
    if args.check:
        stale = stale_schemas(args.directory)
        for path in stale:
            print(f"stale: {path}", file=sys.stderr)
        return 1 if stale else 0
    write_schemas(args.directory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
