"""Run inside an app container (scripts/validate-stack.sh) against the live stack."""

import os

import pytest

from crawler2.core.configuration import Settings
from crawler2.diagnostics.connectivity import run_checks

pytestmark = pytest.mark.integration


def test_backends_reachable_by_service_name() -> None:
    settings = Settings()
    assert settings.redis.host not in {"localhost", "127.0.0.1"}
    assert not {"localhost", "127.0.0.1"} & set(settings.scylla.contact_points)
    assert not settings.minio.endpoint.startswith(("localhost", "127.0.0.1"))

    results = {r.backend: r for r in run_checks(settings)}

    assert set(results) == {"redis", "scylla", "minio"}
    failed = {name: r.detail for name, r in results.items() if not r.ok}
    assert not failed
    assert results["redis"].detail.startswith("redis_time=")


def test_host_identity_comes_from_configuration() -> None:
    settings = Settings()
    assert settings.host_id == os.environ["CRAWLER2_HOST_ID"]
    assert settings.roles, "compose must assign roles per host"
