# SPDX-License-Identifier: Apache-2.0
"""Only a person resolves a quarantine (ADR-0042).

No module but ``cli_quarantine.py`` builds a ``resource.resolved`` event, so the
coordinator, the loop and any scheduled path never resolve one. The store also
refuses the event from anyone but a person.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "ilmar"
WRITERS = {SRC / "cli_quarantine.py"}


def _names_the_event(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "RESOURCE_RESOLVED"
    ]


def test_only_the_quarantine_command_builds_a_resolution() -> None:
    # The event type's own definition, its store-side check, and the reader.
    readers = {SRC / "domain" / "events.py", SRC / "ports" / "event_log.py",
               SRC / "core" / "quarantine.py"}  # fmt: skip
    found = {
        str(path.relative_to(SRC)): lines
        for path in sorted(SRC.rglob("*.py"))
        if path not in WRITERS | readers and (lines := _names_the_event(path))
    }
    assert found == {}


def test_the_reader_only_reads() -> None:
    assert "Event.create(" not in (SRC / "core" / "quarantine.py").read_text()


def test_the_command_does_build_one() -> None:
    assert _names_the_event(SRC / "cli_quarantine.py")
