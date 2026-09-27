"""The contract package stays runtime-light and independent of any service (ADR-010)."""

import ast
import sys
from pathlib import Path

import antipiracy_contracts

ALLOWED_THIRD_PARTY = {"pydantic", "pydantic_core"}
PACKAGE_ROOT = Path(antipiracy_contracts.__file__).parent


def _imported_top_level_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_contract_package_imports_only_stdlib_and_pydantic() -> None:
    allowed = set(sys.stdlib_module_names) | ALLOWED_THIRD_PARTY | {"antipiracy_contracts"}
    for path in PACKAGE_ROOT.rglob("*.py"):
        forbidden = _imported_top_level_modules(path) - allowed
        assert not forbidden, f"{path.relative_to(PACKAGE_ROOT)} imports {sorted(forbidden)}"
