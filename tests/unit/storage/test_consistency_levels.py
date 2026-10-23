"""ADR-012 §6 (TC-017): consistency levels are named choices, not ad hoc per repository.

This is a static guard: it cannot prove behaviour at RF=3 (no cluster exists, TC-010), only that
no repository can pick a level the ADR does not name.
"""

from __future__ import annotations

import re
from pathlib import Path

from cassandra import ConsistencyLevel

from crawler2.storage.scylla.session import Consistency

ROOT = Path(__file__).resolve().parents[3] / "crawler2"


def test_levels_match_adr_012() -> None:
    assert Consistency.AUTHORITATIVE_WRITE.value is ConsistencyLevel.LOCAL_QUORUM
    assert Consistency.STRONG_READ.value is ConsistencyLevel.LOCAL_QUORUM
    assert Consistency.DERIVED_WRITE.value is ConsistencyLevel.LOCAL_ONE
    assert Consistency.EVENTUAL_READ.value is ConsistencyLevel.LOCAL_ONE
    assert Consistency.SERIAL.value is ConsistencyLevel.LOCAL_SERIAL


def test_no_module_outside_the_session_chooses_a_driver_level() -> None:
    """The one deliberate exception: ``media/store.py`` sets ``LOCAL_SERIAL`` on a SELECT, the
    driver's way to ask for a serial (Paxos-completing) read of an LWT-written row."""
    uses: dict[str, set[str]] = {}
    for p in ROOT.rglob("*.py"):
        if p.name == "session.py":
            continue
        found = set(re.findall(r"ConsistencyLevel\.(\w+)", p.read_text()))
        if found or re.search(r"\bConsistencyLevel\b", p.read_text()):
            uses[str(p.relative_to(ROOT))] = found
    assert uses == {"media/store.py": {"LOCAL_SERIAL"}}
