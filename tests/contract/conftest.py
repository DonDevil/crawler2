"""Shared helpers: realistic contract values come from the shipped golden fixtures."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from antipiracy_contracts.compat import event_fixtures
from antipiracy_contracts.events import EventPayload, decode_event
from pydantic import BaseModel


def payload_of(fixture_name: str) -> EventPayload:
    fixture = next(f for f in event_fixtures() if f.name == fixture_name)
    return decode_event(fixture.raw).payload


def revalidate[M: BaseModel](model: M, **changes: Any) -> M:
    """Re-parse ``model`` from JSON with top-level fields replaced (Any: arbitrary test input)."""
    data = model.model_dump(mode="json")
    data.update(changes)
    return type(model).model_validate_json(json.dumps(data))


@pytest.fixture
def payload() -> Callable[[str], EventPayload]:
    return payload_of
