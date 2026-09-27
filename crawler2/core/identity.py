"""Worker identity: ``{host_id}:{role}:{pid}:{instance_id}`` (plan B.4 #4).

Every claim, event and evidence record in later phases carries this value,
so it must be unique across hosts and restarts: ``pid`` alone repeats after
a restart and across hosts, hence the random ``instance_id``.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass

from crawler2.core.configuration.settings import HOST_ID_PATTERN, WorkerRole

_INSTANCE_ID = re.compile(r"^[0-9a-f]{32}$")
_HOST_ID = re.compile(HOST_ID_PATTERN)


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    host_id: str
    role: WorkerRole
    pid: int
    instance_id: str

    def __post_init__(self) -> None:
        if not _HOST_ID.fullmatch(self.host_id):
            raise ValueError(f"invalid host_id: {self.host_id!r}")
        if self.pid <= 0:
            raise ValueError(f"invalid pid: {self.pid}")
        if not _INSTANCE_ID.fullmatch(self.instance_id):
            raise ValueError(f"invalid instance_id: {self.instance_id!r}")

    @classmethod
    def create(
        cls,
        host_id: str,
        role: WorkerRole,
        *,
        pid: int | None = None,
        instance_id: str | None = None,
    ) -> WorkerIdentity:
        return cls(
            host_id=host_id,
            role=role,
            pid=os.getpid() if pid is None else pid,
            instance_id=uuid.uuid4().hex if instance_id is None else instance_id,
        )

    @classmethod
    def parse(cls, value: str) -> WorkerIdentity:
        parts = value.split(":")
        if len(parts) != 4:
            raise ValueError(f"worker identity must have 4 ':'-separated parts: {value!r}")
        host_id, role, pid, instance_id = parts
        if not pid.isdigit():
            raise ValueError(f"invalid pid in worker identity: {value!r}")
        return cls(host_id=host_id, role=WorkerRole(role), pid=int(pid), instance_id=instance_id)

    def __str__(self) -> str:
        return f"{self.host_id}:{self.role.value}:{self.pid}:{self.instance_id}"
