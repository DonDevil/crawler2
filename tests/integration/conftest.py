import os

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get("RUN_INTEGRATION_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="needs the compose stack; set RUN_INTEGRATION_TESTS=1")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
