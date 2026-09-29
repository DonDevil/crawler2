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
    request_timeout_s: float = Field(default=10.0, gt=0)
    username: str | None = None
    password: SecretStr | None = None


class MinioSettings(BaseModel):
    endpoint: str = "minio:9000"
    secure: bool = False
    access_key: SecretStr | None = None
    secret_key: SecretStr | None = None
    bucket_raw: str = Field(default="crawler2-raw", pattern=r"^[a-z0-9][a-z0-9.-]{2,62}$")
    region: str = "us-east-1"
    request_timeout_s: float = Field(default=30.0, gt=0)
    max_object_bytes: PositiveInt = 256 * 1024 * 1024
    """Upper bound for one stored object; larger writes are refused, never truncated."""
    spool_memory_bytes: PositiveInt = 8 * 1024 * 1024
    """Writes up to this size are hashed in memory; larger ones spool to scratch_dir."""

    @property
    def base_url(self) -> str:
        return f"{'https' if self.secure else 'http'}://{self.endpoint}"


class EventSettings(BaseModel):
    """Event transport (Redis Streams, ADR-004) and outbox relay (ADR-013)."""

    stream_prefix: str = Field(default="events:", pattern=r"^[a-z0-9:._-]{0,32}$")
    """Stream key = prefix + ``<event_type>.v<major>``; shared with the fingerprinter."""
    stream_maxlen: PositiveInt = 100_000
    """Approximate per-stream cap (Redis is transport, not the system of record)."""
    relay_batch_size: PositiveInt = 500
    relay_settle_s: float = Field(default=600.0, gt=0)
    """A bucket older than this is assumed complete; the relay may move past it."""
    relay_poll_interval_s: float = Field(default=1.0, gt=0)


class ExecutionQueue(StrEnum):
    """Frontier execution classes (ADR-015): which worker pool runs a task.

    Distinct from the P1 ``FetchCapability`` (what a fetch can do); the
    frontier maps one onto the other. Adding a member adds a queue.
    """

    HTTP = "http"
    BROWSER = "browser"
    TOR = "tor"
    SELENIUM = "selenium"


_DEFAULT_MAX_DEPTH = {
    ExecutionQueue.HTTP: 200_000,
    ExecutionQueue.BROWSER: 50_000,
    ExecutionQueue.TOR: 50_000,
    ExecutionQueue.SELENIUM: 10_000,
}


class FrontierSettings(BaseModel):
    """Redis frontier mechanics (P3). Policy (what/when to crawl) is P6/P7."""

    default_interval_s: float = Field(default=1.0, ge=0)
    """Minimum time between two claims on one domain, across all queues."""
    lease_ttl_s: float = Field(default=90.0, gt=0)
    max_attempts: PositiveInt = 3
    """Attempts (claims) per task before it is exhausted; lease expiry counts."""
    base_backoff_s: float = Field(default=5.0, ge=0)
    max_backoff_s: float = Field(default=300.0, ge=0)
    defer_delay_s: float = Field(default=10.0, ge=0)
    promote_batch: PositiveInt = 256
    """Upper bound of scheduled tasks and of domain gates promoted per call."""
    recover_batch: PositiveInt = 200
    recovery_interval_s: float = Field(default=30.0, gt=0)
    dead_ttl_s: PositiveInt = 7 * 24 * 3600
    dead_max: PositiveInt = 100_000
    max_depth: dict[ExecutionQueue, PositiveInt] = Field(
        default_factory=lambda: dict(_DEFAULT_MAX_DEPTH)
    )
    """Admission limit per queue: scheduled + ready + leased tasks (ADR-016)."""

    @model_validator(mode="after")
    def _complete_depths(self) -> Self:
        for queue, limit in _DEFAULT_MAX_DEPTH.items():
            self.max_depth.setdefault(queue, limit)
        if self.max_backoff_s < self.base_backoff_s:
            raise ValueError("max_backoff_s must be >= base_backoff_s")
        return self


_KIB = 1024
_MIB = 1024 * 1024


class HttpFetchSettings(BaseModel):
    """One HTTP attempt (P4 design §11, §15, §18). Also used by the Tor pool."""

    user_agent: str = Field(default="crawler2/0.1", min_length=1, max_length=200)
    connect_timeout_s: float = Field(default=10.0, gt=0)
    """TCP connect and TLS handshake together (httpx has no separate TLS budget)."""
    read_timeout_s: float = Field(default=15.0, gt=0)
    """Inactivity between two received chunks."""
    total_timeout_s: float = Field(default=30.0, gt=0)
    """Whole attempt incl. redirects and body; defeats slowloris trickling."""
    max_redirects: int = Field(default=10, ge=0, le=30)
    max_body_bytes: PositiveInt = 5 * _MIB
    """Decoded page body limit; above it the attempt is ``too_large`` (nothing stored)."""
    max_manifest_bytes: PositiveInt = 1 * _MIB
    media_probe_bytes: PositiveInt = 64 * _KIB
    """Hard upper bound of body bytes read from a media response (D2)."""
    sniff_bytes: PositiveInt = 256 * _KIB
    """Prefix of an HTML body inspected for needs_js / captcha / block markers."""
    proxy: str | None = None
    """Outbound proxy URL (benchmarks' counting proxy); the Tor pool sets its own."""


class BrowserSettings(BaseModel):
    """Playwright/Chromium pool (P4 design §12)."""

    contexts: PositiveInt = 2
    pages_per_context: PositiveInt = 1
    recycle_pages: PositiveInt = 50
    """Pages served by one context before it is replaced."""
    browser_recycle_pages: PositiveInt = 500
    """Pages served by one browser process before it is relaunched."""
    browser_rss_limit_mb: PositiveInt = 1200
    """Browser process-tree RSS that triggers a drain-and-relaunch."""
    navigation_timeout_s: float = Field(default=30.0, gt=0)
    """One deadline for goto + settle (V1 applied it twice)."""
    settle_s: float = Field(default=3.0, ge=0)
    """Max wait for network idle after DOMContentLoaded, inside the navigation deadline."""
    operation_timeout_s: float = Field(default=10.0, gt=0)
    launch_timeout_s: float = Field(default=30.0, gt=0)
    acquire_timeout_s: float = Field(default=30.0, gt=0)
    relaunch_attempts: PositiveInt = 3
    max_body_bytes: PositiveInt = 5 * _MIB
    blocked_resource_types: CsvList = Field(default_factory=lambda: ["image", "font", "media"])
    """Cost control, not filtering (P6 attaches through the interception hook)."""
    headless: bool = True
    proxy: str | None = None


class TorSettings(BaseModel):
    socks_proxy: str | None = None
    """Explicit ``socks5h://host:port``; else ``TOR_SOCKS_PROXY``/``TOR_SOCKS_PORT``/probe."""
    probe_ports: list[int] = Field(default_factory=lambda: [9050, 9150])
    health_interval_s: float = Field(default=30.0, gt=0)


class NetworkHealthSettings(BaseModel):
    """Per-process local-outage detection (V1 N1-N7, P4 design §16)."""

    enabled: bool = True
    trigger_threshold: PositiveInt = 10
    probe_timeout_s: float = Field(default=5.0, gt=0)
    probe_endpoints: CsvList = Field(
        default_factory=lambda: [
            "https://www.gstatic.com/generate_204",
            "https://www.msftconnecttest.com/connecttest.txt",
            "https://captive.apple.com/hotspot-detect.html",
        ]
    )
    confirm_delay_s: float = Field(default=5.0, ge=0)
    recovery_probe_interval_s: float = Field(default=15.0, gt=0)
    recovery_confirm_rounds: PositiveInt = 2

    @model_validator(mode="after")
    def _two_endpoints(self) -> Self:
        if self.enabled and len(self.probe_endpoints) < 2:
            raise ValueError("network health needs at least two independent probe_endpoints")
        return self


class PoolSettings(BaseModel):
    """Local capacity of one worker pool. Domain politeness is P3's, not this."""

    processes: PositiveInt = 1
    concurrency: PositiveInt = 8
    idle_poll_min_s: float = Field(default=0.2, gt=0)
    idle_poll_max_s: float = Field(default=2.0, gt=0)
    attempt_grace_s: float = Field(default=5.0, ge=0)
    """Added to the fetcher's own total deadline for the runtime's hard cap."""
    shutdown_grace_s: float = Field(default=20.0, ge=0)
    report_retry_s: float = Field(default=0.5, gt=0)
    """Pause between retries of an outcome report while the frontier is unavailable."""


class WorkersSettings(BaseModel):
    http: PoolSettings = Field(default_factory=lambda: PoolSettings(concurrency=32))
    browser: PoolSettings = Field(default_factory=lambda: PoolSettings(concurrency=2))
    tor: PoolSettings = Field(default_factory=lambda: PoolSettings(concurrency=8))
    fetch: HttpFetchSettings = Field(default_factory=HttpFetchSettings)
    browser_engine: BrowserSettings = Field(default_factory=BrowserSettings)
    tor_network: TorSettings = Field(default_factory=TorSettings)
    network_health: NetworkHealthSettings = Field(default_factory=NetworkHealthSettings)
    recover_every_s: float = Field(default=30.0, gt=0)
    """How often each worker process runs ``frontier.recover()`` (safe concurrently)."""


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
    events: EventSettings = Field(default_factory=EventSettings)
    frontier: FrontierSettings = Field(default_factory=FrontierSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    metrics: MetricsSettings = Field(default_factory=MetricsSettings)
    limits: ResourceLimits = Field(default_factory=ResourceLimits)
    workers: WorkersSettings = Field(default_factory=WorkersSettings)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if len(set(self.roles)) != len(self.roles):
            raise ValueError(f"duplicate roles: {[r.value for r in self.roles]}")
        if WorkerRole.ENCODER in self.roles and self.limits.gpu_vram_mb == 0:
            raise ValueError("role 'encoder' requires limits.gpu_vram_mb > 0 (GPU hosts only)")
        return self
