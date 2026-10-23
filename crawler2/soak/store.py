"""Run directory: atomic state, append-only samples/events, single-runner lock.

Layout (``<run_root>/<run_id>/``)::

    config.json        redacted configuration as started (immutable)
    run.json           identity, environment, git/config fingerprints (written at start)
    checkpoint.json    {"data": ..., "sha256": ...}; previous generation in checkpoint.prev.json
    samples.jsonl      one line per sampling round
    events.jsonl       interruptions, restarts, anomalies, operator actions
    ledger.sqlite3     reconciliation ledger (reconcile.py)
    report.json/.md/.html   written by ``report``
    runner.lock        flock + heartbeat of the live runner

Nothing in a run directory is ever deleted by the harness.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import socket
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts, UTC).isoformat(
        timespec="seconds"
    )


def atomic_write(path: Path, data: bytes) -> None:
    """Write-then-rename with fsync of the file and its directory: all or nothing."""
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _dump(obj: Any) -> bytes:
    return json.dumps(obj, indent=2, sort_keys=True, default=str).encode()


class LockHeldError(RuntimeError):
    """Another live runner owns this run directory."""


@dataclass
class ReadResult:
    rows: list[dict[str, Any]]
    corrupt_lines: int


class RunStore:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        """Source of event timestamps; the runner sets its own so a simulated clock is coherent."""
        self._lock_fd: int | None = None

    # -- creation ------------------------------------------------------------
    @classmethod
    def create(cls, root: Path, run_name: str) -> RunStore:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{run_name}-{stamp}-{os.urandom(3).hex()}"
        path = root / run_id
        path.mkdir(parents=True, exist_ok=False)
        return cls(path)

    @property
    def run_id(self) -> str:
        return self.path.name

    # -- json ----------------------------------------------------------------
    def write_json(self, name: str, obj: Any) -> None:
        atomic_write(self.path / name, _dump(obj))

    def read_json(self, name: str) -> Any | None:
        try:
            return json.loads((self.path / name).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    # -- checkpoint (checksummed, two generations) ----------------------------
    def save_checkpoint(self, data: dict[str, Any]) -> None:
        current = self.path / "checkpoint.json"
        if current.exists():
            with contextlib.suppress(OSError):
                os.replace(current, self.path / "checkpoint.prev.json")
        body = json.dumps(data, sort_keys=True, default=str)
        digest = hashlib.sha256(body.encode()).hexdigest()
        atomic_write(current, _dump({"data": json.loads(body), "sha256": digest}))

    def load_checkpoint(self) -> tuple[dict[str, Any] | None, str]:
        """(state, source): source is ``current``, ``previous`` or ``none``/``corrupt``."""
        saw_corrupt = False
        for name, label in (("checkpoint.json", "current"), ("checkpoint.prev.json", "previous")):
            raw = self.read_json(name)
            if raw is None:
                saw_corrupt |= (self.path / name).exists()
                continue
            data = raw.get("data") if isinstance(raw, dict) else None
            body = json.dumps(data, sort_keys=True, default=str)
            if data is not None and raw.get("sha256") == hashlib.sha256(body.encode()).hexdigest():
                return data, label
            saw_corrupt = True
        return None, "corrupt" if saw_corrupt else "none"

    # -- jsonl ---------------------------------------------------------------
    def append(self, name: str, obj: dict[str, Any]) -> None:
        line = json.dumps(obj, sort_keys=True, default=str) + "\n"
        with open(self.path / name, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def read_jsonl(self, name: str) -> ReadResult:
        rows: list[dict[str, Any]] = []
        corrupt = 0
        try:
            lines = (self.path / name).read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return ReadResult([], 0)
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                corrupt += 1  # a torn last line after a crash, or damage: counted, never fatal
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                corrupt += 1
        return ReadResult(rows, corrupt)

    def event(self, kind: str, **fields: Any) -> dict[str, Any]:
        now = self.clock()
        row = {"ts": now, "at": utc_iso(now), "kind": kind, **fields}
        self.append("events.jsonl", row)
        return row

    # -- single-runner lock ---------------------------------------------------
    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.acquire()
        try:
            yield
        finally:
            self.release()

    def acquire(self) -> None:
        fd = os.open(self.path / "runner.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise LockHeldError(f"{self.run_id} is owned by a live runner") from exc
        self._lock_fd = fd
        self.heartbeat()

    def heartbeat(self) -> None:
        if self._lock_fd is None:
            return
        info = json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "ts": time.time()})
        os.ftruncate(self._lock_fd, 0)
        os.pwrite(self._lock_fd, info.encode(), 0)

    def release(self) -> None:
        if self._lock_fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            self._lock_fd = None

    def lock_info(self) -> dict[str, Any] | None:
        try:
            info: dict[str, Any] | None = json.loads(
                (self.path / "runner.lock").read_text() or "null"
            )
            return info
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def runner_alive(self) -> bool:
        """True if a live process holds the flock (probe without keeping it)."""
        fd = os.open(self.path / "runner.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)
