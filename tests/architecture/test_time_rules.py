# SPDX-License-Identifier: Apache-2.0
"""ADR-0028: a persisted time may only delay new work.

The only persisted time the code may compare with the clock is a closed list
of uses. Today that is one: a task's ``retry_not_before`` (#13). These tests
keep every other module, recovery and the tracker reconciliation included,
from reading it. Passing a new value to the state machine (a keyword argument
in a call) is a write, not a read, and is not counted.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "ilmar"

ALLOWED = {
    # read only where the field is defined, validated, stored and decided on
    "retry_not_before": {
        "domain/execution.py",
        "core/state_machine.py",
        "adapters/sqlite.py",
    },
    # the due check: only the worker, before a new attempt
    "attempt_wait_reason": {"core/state_machine.py", "core/worker.py"},
}


def _names(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.alias | ast.FunctionDef):
            found.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.add(node.target.id)
    return found


def _users(name: str) -> set[str]:
    users = set()
    for path in sorted(SRC.rglob("*.py")):
        if name in _names(ast.parse(path.read_text(encoding="utf-8"))):
            users.add(path.relative_to(SRC).as_posix())
    return users


def test_only_the_listed_modules_use_a_persisted_retry_time() -> None:
    for name, allowed in ALLOWED.items():
        assert _users(name) <= allowed, f"{name} used outside ADR-0028's list"


def test_the_listed_uses_exist() -> None:
    """The check is not vacuous: each name is where this test expects it."""
    for name, allowed in ALLOWED.items():
        assert _users(name) == allowed, name


def test_recovery_and_tracker_reconciliation_never_read_it() -> None:
    for module in ("core/coordinator.py", "core/tracker.py", "core/journal.py", "core/effects.py"):
        names = _names(ast.parse((SRC / module).read_text(encoding="utf-8")))
        assert not names & set(ALLOWED), module
