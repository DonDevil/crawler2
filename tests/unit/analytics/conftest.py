"""Fixtures for the P13 unit tests: the SYNTHETIC world (``world.py``) exported once
per test into a temporary analytics root. No store, no network."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from crawler2.analytics.export import Exporter, ExportOptions, ExportResult
from crawler2.analytics.source import MemoryRowSource
from tests.unit.analytics.world import BOUNDARY, World, build


@dataclass
class Exported:
    world: World
    ctx: dict[str, Any]
    source: MemoryRowSource
    exporter: Exporter
    result: ExportResult
    root: Path

    @property
    def revision_dir(self) -> Path:
        assert self.result.revision_dir is not None
        return self.result.revision_dir


def make_exporter(source: MemoryRowSource, root: Path, **options: Any) -> Exporter:
    opts = {"segments": 4, "retry_backoff_s": 0.0, "duckdb_threads": 1} | options
    return Exporter(source, root, options=ExportOptions(**opts), sleep=lambda _s: None)


@pytest.fixture
def exported(tmp_path: Path) -> Exported:
    world, ctx = build()
    source = MemoryRowSource(world.tables, page_size=2)
    exporter = make_exporter(source, tmp_path)
    result = exporter.export(until=BOUNDARY)
    return Exported(world, ctx, source, exporter, result, tmp_path)
