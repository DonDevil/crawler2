"""Structured (JSON) logging with a fixed set of context fields.

Every record carries: timestamp, level, service, host_id, role, component,
event and — when bound — correlation_id. Timestamps are wall-clock and for
humans only; coordination time comes from Redis TIME (ADR-006).
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO

import structlog
from structlog.typing import Processor

from crawler2.core.configuration.settings import LogFormat, Settings


def configure_logging(
    settings: Settings, *, role: str | None = None, stream: TextIO | None = None
) -> None:
    """Configure structlog for this process. Call once at process start."""
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if settings.logging.format is LogFormat.JSON
        else structlog.dev.ConsoleRenderer()
    )
    static = {
        "service": settings.service,
        "host_id": settings.host_id,
        "role": role,
    }

    def add_static(
        _: object, __: str, event_dict: structlog.typing.EventDict
    ) -> structlog.typing.EventDict:
        for key, value in static.items():
            event_dict.setdefault(key, value)
        return event_dict

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            add_static,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.logging.level]
        ),
        logger_factory=structlog.PrintLoggerFactory(stream or sys.stdout),
        cache_logger_on_first_use=False,
    )


def get_logger(component: str) -> structlog.typing.FilteringBoundLogger:
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger().bind(component=component)
    return logger


def new_correlation_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def bind_correlation_id(correlation_id: str | None = None) -> Iterator[str]:
    """Attach a correlation_id to every log record emitted inside the block."""
    cid = correlation_id or new_correlation_id()
    with structlog.contextvars.bound_contextvars(correlation_id=cid):
        yield cid
