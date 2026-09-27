import json
from pathlib import Path
from typing import Any

from antipiracy_contracts.events import CATALOG
from antipiracy_contracts.schemas import SCHEMA_DIR, build_schemas, event_schema_path, stale_schemas


def test_committed_schemas_are_current() -> None:
    assert stale_schemas() == [], "run `make schemas` and commit the result"


def test_generation_is_deterministic() -> None:
    assert build_schemas() == build_schemas()


def test_stale_detection(tmp_path: Path) -> None:
    for relative, content in build_schemas().items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(content)
    assert stale_schemas(tmp_path) == []
    (tmp_path / "events" / "obsolete.v1.json").write_text("{}")
    (tmp_path / "catalog.json").write_text("{}")
    assert stale_schemas(tmp_path) == ["catalog.json", "events/obsolete.v1.json"]


def _objects(node: Any) -> list[dict[str, Any]]:
    # Any: arbitrary JSON Schema document structure.
    if isinstance(node, dict):
        found = [node] if node.get("type") == "object" else []
        return found + [o for value in node.values() for o in _objects(value)]
    if isinstance(node, list):
        return [o for item in node for o in _objects(item)]
    return []


def test_schemas_never_forbid_unknown_fields() -> None:
    # Forward compatibility: a consumer on minor N must accept documents from minor N+k.
    for spec in CATALOG:
        schema = json.loads(
            (SCHEMA_DIR / event_schema_path(spec.event_type, spec.major)).read_text()
        )
        for obj in _objects(schema):
            assert obj.get("additionalProperties", True) is not False, spec.event_type


def test_catalog_json_lists_every_event() -> None:
    catalog = json.loads((SCHEMA_DIR / "catalog.json").read_text())
    assert [e["event_type"] for e in catalog["events"]] == [s.event_type for s in CATALOG]
