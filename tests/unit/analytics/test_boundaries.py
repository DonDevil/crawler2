"""P13 stays off the hot path: nothing imports it, and its store access is SELECT-only."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import crawler2
from crawler2.analytics.datasets import SOURCES
from crawler2.analytics.source import ScyllaRowSource

PACKAGE = Path(crawler2.__file__).parent


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
    return found


def test_no_runtime_component_imports_analytics() -> None:
    offenders = [
        str(p.relative_to(PACKAGE))
        for p in PACKAGE.rglob("*.py")
        if "analytics" not in p.parts
        and any(m.startswith("crawler2.analytics") for m in _imports(p))
    ]
    assert offenders == []


def test_analytics_never_imports_writers_or_brokers() -> None:
    allowed_storage = {
        "crawler2.storage.errors",
        "crawler2.storage.scylla.session",
        "crawler2.storage.scylla",
    }
    for p in (PACKAGE / "analytics").glob("*.py"):
        for m in _imports(p):
            assert not m.startswith(
                (
                    "redis",
                    "crawler2.frontier",
                    "crawler2.storage.events",
                    "crawler2.storage.objectstore",
                )
            ), (p.name, m)
            if m.startswith("crawler2.storage"):
                assert m in allowed_storage, (p.name, m)


class _RecordingSession:
    keyspace = "ks_test"

    def __init__(self) -> None:
        self.cql: list[str] = []

    def bind(self, cql: str, consistency: Any, values: Any) -> str:
        self.cql.append(cql)
        return cql

    def execute(self, statement: Any) -> list[Any]:
        return [(0,)]

    def stream(self, statement: Any, *, fetch_size: int) -> Any:
        return iter(())


def test_scylla_source_issues_only_selects() -> None:
    session = _RecordingSession()
    source = ScyllaRowSource(session)
    for table in SOURCES:
        source.available(table.table)
        list(source.scan(table, 0, 4))
        source.count(table, 3, 4)
    assert session.cql
    assert all(c.lstrip().upper().startswith("SELECT ") for c in session.cql)
    assert all("token(" in c for c in session.cql if "system_schema" not in c)
    assert source.label == "scylla:ks_test"
