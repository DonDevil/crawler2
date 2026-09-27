import pytest
from pydantic import ValidationError

from crawler2.core.configuration import (
    Environment,
    ReplicationStrategy,
    Settings,
    WorkerRole,
)


def test_defaults_target_compose_service_names(clean_env: pytest.MonkeyPatch) -> None:
    s = Settings()
    assert s.environment is Environment.DEV
    assert s.redis.host == "redis"
    assert s.scylla.contact_points == ["scylla"]
    assert s.minio.endpoint == "minio:9000"
    assert s.roles == []


def test_env_overrides_including_nested_and_csv(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("CRAWLER2_HOST_ID", "host-7")
    clean_env.setenv("CRAWLER2_ROLES", "http, browser,media_probe")
    clean_env.setenv("CRAWLER2_REDIS__PORT", "16379")
    clean_env.setenv("CRAWLER2_SCYLLA__CONTACT_POINTS", "s1,s2,s3")
    clean_env.setenv("CRAWLER2_SCYLLA__REPLICATION_STRATEGY", "NetworkTopologyStrategy")
    clean_env.setenv("CRAWLER2_SCYLLA__REPLICATION_FACTOR", "3")
    clean_env.setenv("CRAWLER2_LIMITS__HTTP_CONCURRENCY", "32")

    s = Settings()

    assert s.host_id == "host-7"
    assert s.roles == [WorkerRole.HTTP, WorkerRole.BROWSER, WorkerRole.MEDIA_PROBE]
    assert s.redis.port == 16379
    assert s.scylla.contact_points == ["s1", "s2", "s3"]
    assert s.scylla.replication_strategy is ReplicationStrategy.NETWORK_TOPOLOGY
    assert s.scylla.replication_factor == 3
    assert s.limits.http_concurrency == 32


@pytest.mark.parametrize("host_id", ["", "Host-1", "a:b", "-x", "x" * 64])
def test_rejects_invalid_host_id(clean_env: pytest.MonkeyPatch, host_id: str) -> None:
    with pytest.raises(ValidationError):
        Settings(host_id=host_id)


def test_rejects_unknown_role(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("CRAWLER2_ROLES", "http,crawler")
    with pytest.raises(ValidationError):
        Settings()


def test_rejects_duplicate_roles(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="duplicate roles"):
        Settings(roles=[WorkerRole.HTTP, WorkerRole.HTTP])


def test_encoder_role_requires_gpu_budget(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="gpu_vram_mb"):
        Settings(roles=[WorkerRole.ENCODER])
    clean_env.setenv("CRAWLER2_LIMITS__GPU_VRAM_MB", "3072")
    assert Settings(roles=[WorkerRole.ENCODER]).limits.gpu_vram_mb == 3072


def test_rejects_unknown_top_level_setting(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        Settings(unknown_field=1)  # type: ignore[call-arg]


def test_secrets_are_not_rendered(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("CRAWLER2_MINIO__SECRET_KEY", "super-secret-value")
    s = Settings()
    assert "super-secret-value" not in repr(s)
    assert "super-secret-value" not in s.model_dump_json()
    assert s.minio.secret_key is not None
    assert s.minio.secret_key.get_secret_value() == "super-secret-value"


def test_settings_are_immutable(clean_env: pytest.MonkeyPatch) -> None:
    s = Settings()
    with pytest.raises(ValidationError):
        s.host_id = "other"  # type: ignore[misc]
