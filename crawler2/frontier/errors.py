"""Frontier failures. Redis trouble is raised, never reported as "no work"."""

from __future__ import annotations


class FrontierError(Exception):
    """Base class; a plain ``FrontierError`` indicates a bug, not an outage."""


class FrontierUnavailableError(FrontierError):
    """Redis could not be reached or refused service (connection, timeout,
    OOM, loading). The operation's effect is unknown to the caller; every
    operation may be retried with the same arguments (docs/phases/
    p03-frontier-scheduling §19)."""
