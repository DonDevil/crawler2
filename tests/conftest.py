from collections.abc import Iterator

import pytest

from tests.fixtures.web import FixtureSite, Route, serve

BASIC_ROUTES = {
    "/": Route(
        body=b"<html><body><a href='/page'>page</a></body></html>",
        headers={"Content-Type": "text/html; charset=utf-8"},
    ),
    "/page": Route(
        body=b"<html><body>page</body></html>", headers={"Content-Type": "text/html; charset=utf-8"}
    ),
}


@pytest.fixture
def fixture_site() -> Iterator[FixtureSite]:
    with serve(BASIC_ROUTES) as site:
        yield site


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove ambient CRAWLER2_* variables so settings tests are hermetic."""
    import os

    for key in list(os.environ):
        if key.startswith("CRAWLER2_"):
            monkeypatch.delenv(key)
    return monkeypatch
