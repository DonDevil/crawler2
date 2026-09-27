import pytest

from crawler2.core.configuration import Settings
from crawler2.diagnostics.connectivity import run_checks


def _ok(_: Settings) -> str:
    return "fine"


def _down(_: Settings) -> str:
    raise ConnectionRefusedError("refused")


def _bug(_: Settings) -> str:
    raise KeyError("programming error")


def test_reports_each_backend(clean_env: pytest.MonkeyPatch) -> None:
    results = run_checks(Settings(), {"a": _ok, "b": _down})
    assert [(r.backend, r.ok) for r in results] == [("a", True), ("b", False)]
    assert results[1].detail == "ConnectionRefusedError: refused"


def test_unexpected_errors_propagate(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(KeyError):
        run_checks(Settings(), {"a": _bug})
