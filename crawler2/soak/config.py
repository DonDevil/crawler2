"""Soak configuration: what to run, what to watch and which limits the owner has approved.

Every numeric limit the plan does not define is ``None`` ("not decided"). A required
criterion whose limit is ``None`` can only end ``INCONCLUSIVE`` in production mode: the
harness never invents an approved threshold. Limits the plan *does* define (no lost
work, no unexpected duplicate effect, no evidence integrity failure, >= 168 h) are not
configurable here.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

REQUIRED_DURATION_H = 168.0
"""M6: a 7-day continuous soak (plan P14; docs/phases/p14-production-hardening/soak.md)."""

SECRET_KEY = re.compile(r"(pass(word)?|secret|token|api[_-]?key|credential|auth)", re.IGNORECASE)
REDACTED = "***redacted***"


class Mode(StrEnum):
    SYNTHETIC = "synthetic"
    """Generated workload and fake services. Never acceptance evidence."""
    PRODUCTION = "production"
    """The real pipeline on the real topology."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Approval(_Strict):
    """Who approved the limits below. Required before a production run starts."""

    approved_by: str | None = None
    approved_at: str | None = None
    reference: str | None = None
    """Where the decision is recorded (ADR, ticket, message)."""

    @property
    def complete(self) -> bool:
        return all((self.approved_by, self.approved_at, self.reference))


class GrowthLimit(_Strict):
    """A series must not keep growing: slope over the run and relative growth."""

    max_slope_per_h: float
    """Allowed linear-regression slope, in the series' own units per hour."""
    min_growth_ratio: float = Field(default=0.1, ge=0)
    """Ignore growth below this fraction of the series' median (noise)."""


class Thresholds(_Strict):
    max_interruptions: int | None = Field(default=None, ge=0)
    max_interruption_gap_s: float | None = Field(default=None, gt=0)
    min_series_coverage: float | None = Field(default=None, gt=0, le=1)
    max_dependency_outage_s: float | None = Field(default=None, gt=0)
    max_restarts_per_service: int | None = Field(default=None, ge=0)
    min_disk_free_fraction: float | None = Field(default=None, gt=0, lt=1)
    max_queue_age_s: float | None = Field(default=None, gt=0)
    max_event_lag_s: float | None = Field(default=None, gt=0)
    max_retry_ratio: float | None = Field(default=None, ge=0)
    """Retries (``*_retry*`` series deltas) per completed unit of work."""
    growth: dict[str, GrowthLimit] = Field(default_factory=dict)
    """Series name -> limit. Series named in ``Series.growth_watch`` without a limit here
    are measured and reported but make the growth criterion INCONCLUSIVE."""


class Series(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    required: bool = True
    growth_watch: bool = False
    kind: Literal["gauge", "counter"] = "gauge"
    unit: str = ""
    role: Literal[
        "resource", "queue_depth", "queue_age", "event_lag", "up", "disk_free", "retry", "work"
    ] = "resource"
    """How the evaluator treats it: ``up`` series (1/0) feed the outage criterion;
    ``disk_free`` series are fractions in [0, 1]; ``queue_*``/``event_lag`` feed their limits."""


class Collector(_Strict):
    """One data source. ``type`` selects the implementation (collectors.py)."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    type: Literal["host", "process", "gpu", "prometheus", "redis", "http_health", "synthetic"]
    options: dict[str, Any] = Field(default_factory=dict)
    series: list[Series] = Field(default_factory=list)
    """Series this collector must produce (names without the collector prefix)."""


class Edge(_Strict):
    """Reconciliation expectation: each ``from`` event leads to a ``to`` event with its key."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_.>-]{0,63}$")
    from_type: str
    to_types: list[str] = Field(min_length=1)
    from_key: str
    to_key: str
    to_key_by_type: dict[str, str] = Field(default_factory=dict)
    """Per-type override of ``to_key`` (event types with another payload shape)."""
    to_filters: dict[str, dict[str, str]] = Field(default_factory=dict)
    """Per-type ``{path: value}`` conditions (string compare); a ``to`` event counts if all hold,
    e.g. ``encode.failed`` counts as terminal only when ``payload.retryable`` is ``False``."""
    effect_path: str | None = None
    """Business effect of the ``to`` event. Two different effects for one key is a duplicate
    business effect; the same event delivered twice is only a redelivery."""
    grace_s: float | None = Field(default=None, gt=0)
    """How long a source may wait for its target before it is counted unaccounted-for."""
    required: bool = True
    """False: informational (reported, never fails)."""


class Redis(_Strict):
    host: str = "127.0.0.1"
    port: int = 6379
    db: int = 0
    namespace: str = ""
    username_env: str | None = None
    password_env: str | None = None
    """Names of environment variables holding credentials; values are never stored."""
    stream_prefix: str = "events:"


class EvidenceCheck(_Strict):
    enabled: bool = True
    command: list[str] = Field(
        default_factory=lambda: ["crawler2-evidence", "validate", "{id}", "--dry-run"]
    )
    sample: int = Field(default=200, ge=1)
    """Evidence items re-validated in the final reconciliation (newest first)."""
    timeout_s: float = Field(default=120, gt=0)


class SoakConfig(_Strict):
    run_name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,47}$")
    mode: Mode
    dry_run: bool = False
    """A short mechanics check. A dry run can pass but is never M6-eligible."""
    duration_h: float = Field(default=REQUIRED_DURATION_H, gt=0)
    sample_interval_s: float = Field(default=60.0, gt=0)
    heartbeat_stale_s: float = Field(default=300.0, gt=0)
    """A run whose heartbeat is older than this is considered dead (resume allowed)."""
    run_root: Path = Path("var/soak")
    topology: dict[str, str] = Field(default_factory=dict)
    """Host name -> role list; recorded verbatim and fingerprinted."""
    repos: list[str] = Field(default_factory=list)
    """Git repositories whose HEAD and working-tree state are fingerprinted at start and re-checked
    every round (a change invalidates the run)."""
    watch_paths: list[str] = Field(default_factory=list)
    """Files/directories whose content fingerprint must not change during the run
    (code revision is always checked)."""
    approval: Approval = Field(default_factory=Approval)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    collectors: list[Collector] = Field(default_factory=list)
    redis: Redis | None = None
    edges: list[Edge] = Field(default_factory=list)
    evidence: EvidenceCheck = Field(default_factory=EvidenceCheck)
    required_stages: list[str] = Field(default_factory=list)
    """Event types that must occur at least once in a production run (e.g. ``match.found``).
    A stage with zero events makes the pipeline criterion INCONCLUSIVE."""

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (
            self.mode is Mode.PRODUCTION
            and not self.dry_run
            and self.duration_h < REQUIRED_DURATION_H
        ):
            raise ValueError(
                f"a production run must last >= {REQUIRED_DURATION_H:g} h (set dry_run=true "
                "for a short mechanics check; it is never M6-eligible)"
            )
        names = [c.name for c in self.collectors]
        if len(set(names)) != len(names):
            raise ValueError("duplicate collector names")
        return self

    @property
    def m6_eligible(self) -> bool:
        return self.mode is Mode.PRODUCTION and not self.dry_run

    def series_names(self) -> list[str]:
        return [f"{c.name}.{s.name}" for c in self.collectors for s in c.series]

    def fingerprint(self) -> str:
        """Stable hash of the configuration (without secrets: there are none by design)."""
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()


def load_config(path: Path) -> SoakConfig:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return SoakConfig.model_validate(data)


Json = Annotated[Any, "json"]


def redact(value: Any) -> Any:
    """Copy of ``value`` with secret-looking keys blanked (for logs, run.json and reports)."""
    if isinstance(value, dict):
        return {
            k: (
                REDACTED if SECRET_KEY.search(str(k)) and not str(k).endswith("_env") else redact(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
