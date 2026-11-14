# SPDX-License-Identifier: Apache-2.0
"""Only a person writes a gate record (ADR-0030 §2, ADR-0040).

No code path inside Ilmar builds a gate change except ``ilmar gate``
(``cli_gate.py``). The store also refuses any gate change whose actor is not a
person; this test keeps the coordinator from even trying.

Read statically with ``ast``, like the dependency rules.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "ilmar"
WRITERS = {SRC / "cli_gate.py"}
CHANGES = {"GateRecorded", "GateRevoked"}


def _builds_a_gate_change(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in CHANGES:
                lines.append(node.lineno)
        if isinstance(node, ast.keyword) and node.arg == "gate":
            lines.append(node.value.lineno)  # Commit(gate=...)
    return lines


def test_only_the_gate_command_writes_a_gate_record() -> None:
    found = {
        str(path.relative_to(SRC)): lines
        for path in sorted(SRC.rglob("*.py"))
        if path not in WRITERS and (lines := _builds_a_gate_change(path))
    }
    assert found == {}


def test_the_gate_command_does_write_one() -> None:
    assert _builds_a_gate_change(SRC / "cli_gate.py")
