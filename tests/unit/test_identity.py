import os

import pytest
from hypothesis import given
from hypothesis import strategies as st

from crawler2.core.configuration import WorkerRole
from crawler2.core.identity import WorkerIdentity

host_ids = st.from_regex(r"\A[a-z0-9][a-z0-9-]{0,62}\Z")
instance_ids = st.from_regex(r"\A[0-9a-f]{32}\Z")


def test_format_is_host_role_pid_instance() -> None:
    ident = WorkerIdentity.create("host-1", WorkerRole.HTTP, pid=42, instance_id="a" * 32)
    assert str(ident) == f"host-1:http:42:{'a' * 32}"


def test_create_defaults_to_current_pid_and_unique_instance() -> None:
    a = WorkerIdentity.create("host-1", WorkerRole.BROWSER)
    b = WorkerIdentity.create("host-1", WorkerRole.BROWSER)
    assert a.pid == os.getpid()
    assert a != b  # same host/role/pid, still distinct (restart/pid reuse safety)


@given(
    host_id=host_ids,
    role=st.sampled_from(WorkerRole),
    pid=st.integers(min_value=1, max_value=2**22),
    instance_id=instance_ids,
)
def test_parse_roundtrip(host_id: str, role: WorkerRole, pid: int, instance_id: str) -> None:
    ident = WorkerIdentity(host_id, role, pid, instance_id)
    assert WorkerIdentity.parse(str(ident)) == ident


@pytest.mark.parametrize(
    "value",
    [
        "host-1:http:42",
        f"host-1:http:42:{'a' * 32}:extra",
        f"host-1:nope:42:{'a' * 32}",
        f"host-1:http:-1:{'a' * 32}",
        f"host-1:http:0:{'a' * 32}",
        "host-1:http:42:not-hex",
        f"Host_1:http:42:{'a' * 32}",
    ],
)
def test_parse_rejects_malformed(value: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 — each case fails a different check
        WorkerIdentity.parse(value)
