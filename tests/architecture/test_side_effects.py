# SPDX-License-Identifier: Apache-2.0
"""Every external side effect of the core goes through the journal (ADR-0020, ADR-0021).

* Inside ``ilmar.core``, only ``effects.py`` and ``verification.py`` (which
  runs inside the journaled verify step) may call a port method that changes
  the outside world.
* Every public method of ``Effects`` performs its effect through
  ``self._journal.perform``.

Read statically with ``ast``, like the dependency rules.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[2] / "src" / "ilmar" / "core"
ALLOWED = {"effects.py", "verification.py"}
MUTATING = {
    "claim",
    "release",
    "post_update",
    "checkout",
    "freeze_candidate",
    "checkout_revision",
    "publish_revision",
    "upsert_change_request",
    "start",
    "stop",
    "run",
}
PORTS = {
    "task_provider",
    "source_control",
    "container_runtime",
    "agent_runner",
    "runtime",
    "scm",
    "tasks",
}


def _receiver_names(node: ast.expr) -> set[str]:
    names: set[str] = set()
    while isinstance(node, ast.Attribute | ast.Call | ast.Subscript):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        else:
            node = node.value
    if isinstance(node, ast.Name):
        names.add(node.id)
    return names


def _port_mutations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in MUTATING
            and _receiver_names(node.func.value) & PORTS
        ):
            found.append(f"{path.name}:{node.lineno} .{node.func.attr}()")
    return found


@pytest.mark.parametrize(
    "path",
    [p for p in sorted(CORE.glob("*.py")) if p.name not in ALLOWED],
    ids=lambda p: p.name,
)
def test_only_the_effects_module_changes_the_outside_world(path: Path) -> None:
    assert _port_mutations(path) == []


def test_the_rule_detects_a_direct_call(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("def f(c):\n    c.source_control.publish_revision(r)\n")
    assert _port_mutations(sample) == ["sample.py:2 .publish_revision()"]


READ_ONLY = {"load_candidate"}
"""Public ``Effects`` methods that change nothing; they may not call a mutating method."""


def test_every_effect_goes_through_the_journal() -> None:
    """Each public ``Effects`` method acts through ``perform`` or settles through
    ``reconcile`` (both journaled and fenced), or is declared read-only."""
    tree = ast.parse((CORE / "effects.py").read_text())
    (effects,) = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Effects"]
    public = [
        n
        for n in effects.body
        if isinstance(n, ast.FunctionDef)
        and not n.name.startswith("_")
        and not any(isinstance(d, ast.Name) and d.id == "property" for d in n.decorator_list)
    ]
    assert public
    for method in public:
        journal_calls = {
            n.func.attr
            for n in ast.walk(method)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in {"perform", "reconcile"}
            and "_journal" in _receiver_names(n.func.value)
        }
        if method.name in READ_ONLY:
            mutating = [
                n.func.attr
                for n in ast.walk(method)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in MUTATING
                and _receiver_names(n.func.value) & PORTS
            ]
            assert not journal_calls, f"Effects.{method.name} is not read-only"
            assert not mutating, f"Effects.{method.name} is not read-only"
        else:
            assert journal_calls, f"Effects.{method.name} does not go through the journal"
