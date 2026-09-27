"""The single typed configuration schema for every crawler2 process.

Every value can be overridden from the environment with the ``CRAWLER2_``
prefix; nested fields use ``__`` (e.g. ``CRAWLER2_REDIS__HOST=redis``).
Hosts differ only by configuration (host_id, roles, limits) — never by
code path (docs/adr/ADR-006-multi-host-topology.md).
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, Field, PositiveInt, SecretStr, model_validator
from pydantic.functional_validators import BeforeValidator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# host_id is embedded in the colon-separated worker identity, so it must not
# contain ':' and must stay readable in logs and Redis keys.
HOST_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class WorkerRole(StrEnum):
    """Capabilities a host may run (plan B.4 #6). Workers arrive in P3+."""

    HTTP = "http"
    BROWSER = "browser"
    TOR = "tor"
    INTELLIGENCE = "intelligence"
    MEDIA_PROBE = "media_probe"
    FINALIZER = "finalizer"
    ENCODER = "encoder"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class ReplicationStrategy(StrEnum):
    SIMPLE = "SimpleStrategy"
    NETWORK_TOPOLOGY = "NetworkTopologyStrategy"


def _split_csv(value: object) -> object:
    """Accept ``"a,b"`` from env vars as well as real lists from code."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


CsvList = Annotated[list[str], NoDecode, BeforeValidator(_split_csv)]
RoleList = Annotated[list[WorkerRole], NoDecode, BeforeValidator(_split_csv)]


class RedisSettings(BaseModel):
    """Coordination store; also the authoritative clock (Redis TIME)."""

    host: str = "redis"
    port: int = Field(default=6379, ge=1, le=65535)
    db: int = Field(default=0, ge=0)
    password: SecretStr | None = None
    namespace: str = "crawler2"
    socket_timeout_s: float = Field(default=5.0, gt=0)


class ScyllaSettings(BaseModel):
    contact_points: CsvList = Field(default_factory=lambda: ["scylla"])
    port: int = Field(default=9042, ge=1, le=65535)
    keyspace: str = Field(default="crawler2", pattern=r"^[a-z][a-z0-9_]{0,47}$")
    local_dc: str = "datacenter1"
    replication_strategy: ReplicationStrategy = ReplicationStrategy.SIMPLE
    replication_factor: PositiveInt = 1
    connect_timeout_s: float = Field(default=10.0, gt=0)
    username: str | None = None
    password: SecretStr | None = None


class MinioSettings(BaseModel):
    endpoint: str = "minio:9000"
    secure: bool = False
    access_key: SecretStr | None = None
    secret_key: SecretStr | None = None
    bucket_raw: str = "crawler2-raw"
    region: str = "us-east-1"

    @property
    def base_url(self) -> str:
        return f"{'https' if self.secure else 'http'}://{self.endpoint}"


class LoggingSettings(BaseModel):
    level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    format: LogFormat = LogFormat.JSON


class MetricsSettings(BaseModel):
    enabled: bool = True
    namespace: str = Field(default="crawler2", pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    port: int = Field(default=9100, ge=1, le=65535)


class ResourceLimits(BaseModel):
    """Per-host budgets. Enforced by the worker pools that arrive in P4/P9."""

    max_memory_mb: PositiveInt = 512
    http_concurrency: PositiveInt = 64
    browser_contexts: PositiveInt = 2
    gpu_vram_mb: int = Field(default=0, ge=0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CRAWLER2_",
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
    )

    environment: Environment = Environment.DEV
    service: str = Field(default="crawler2", pattern=r"^[a-z][a-z0-9_]*$")
    host_id: str = Field(default="dev-1", pattern=HOST_ID_PATTERN)
    roles: RoleList = Field(default_factory=list)
    # Cache/scratch only (ADR-006: no authoritative local state).
    scratch_dir: Path = Path("/tmp/crawler2")  # noqa: S108 — scratch by definition

    redis: RedisSettings = Field(default_factory=RedisSettings)
    scylla: ScyllaSettings = Field(default_factory=ScyllaSettings)
    minio: MinioSettings = Field(default_factory=MinioSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    metrics: MetricsSettings = Field(default_factory=MetricsSettings)
    limits: ResourceLimits = Field(default_factory=ResourceLimits)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if len(set(self.roles)) != len(self.roles):
            raise ValueError(f"duplicate roles: {[r.value for r in self.roles]}")
        if WorkerRole.ENCODER in self.roles and self.limits.gpu_vram_mb == 0:
            raise ValueError("role 'encoder' requires limits.gpu_vram_mb > 0 (GPU hosts only)")
        return self
