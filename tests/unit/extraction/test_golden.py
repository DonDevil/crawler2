"""Golden extraction results (P5 design §19).

The pages under ``tests/fixtures/p5_pages`` are small hand-written fixtures that
follow structures seen in the W691 capture (player iframes, JSON-LD, listing grids,
legacy charsets, broken markup). No captured page is committed (design §18).
Expected results pin links, media, metadata, every hash and the revision id; any
difference is either a regression or an intended change that must bump
``EXTRACTOR_VERSION`` or a hash tag. Regenerate with ``UPDATE_GOLDEN=1``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from antipiracy_contracts.models.web import UrlRef

from crawler2.extraction.extract import extract

FIXTURES = Path(__file__).parents[2] / "fixtures" / "p5_pages"
PAGES = {
    "streaming_watch": ("https://stream.example/watch/sample-movie-2026", "text/html"),
    "listing": ("https://catalog.example/movies/page/2", "text/html; charset=utf-8"),
    "article_dates": ("https://news.example/news/beispielserie-s02e05?from=feed", "text/html"),
    "latin1_legacy": ("http://legacy.example/legacy/cafe.htm", "text/html"),
    "malformed": ("https://broken.example/x", None),
}


def result(name: str) -> Any:
    url, content_type = PAGES[name]
    body = (FIXTURES / f"{name}.html").read_bytes()
    e = extract(body, page=UrlRef.of(url), content_type=content_type)
    return json.loads(e.model_dump_json())


@pytest.mark.parametrize("name", sorted(PAGES))
def test_golden(name: str) -> None:
    expected_path = FIXTURES / f"{name}.expected.json"
    actual = result(name)
    if os.environ.get("UPDATE_GOLDEN") == "1":
        expected_path.write_text(json.dumps(actual, indent=1, ensure_ascii=False) + "\n")
    assert actual == json.loads(expected_path.read_text())
