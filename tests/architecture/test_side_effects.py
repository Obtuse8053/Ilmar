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
PORTS_DIR = CORE.parent / "ports"
STORE_PORT = "execution_store.py"
"""Its writes are fenced by the store itself (ADR-0020), not by the journal."""
READS = {
    # Port methods that change nothing outside Ilmar.
    "name", "find", "is_running", "list_eligible", "get", "branch_head",
    "load_candidate", "checks", "change_request_state",
    # ADR-0026 §6 (approved): the task's standing and this instance's held claims.
    "standing", "held_claims",
    # ADR-0027 §1 (approved): stray claim labels.
    "stray_labels",
    # ADR-0035 §3: StreamConsumer, which a runtime adapter feeds with the
    # command's stdout; validating it changes nothing outside Ilmar.
    "feed", "close",
    # ADR-0037 §1: the in-memory route of a GATEWAY sandbox.
    "model_access",
}  # fmt: skip


def _port_methods() -> dict[str, set[str]]:
    """Every method each port declares, read from ``ilmar.ports``."""
    methods: dict[str, set[str]] = {}
    for path in sorted(PORTS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        methods[path.name] = {
            f.name
            for c in tree.body
            if isinstance(c, ast.ClassDef)
            for f in c.body
            if isinstance(f, ast.FunctionDef) and not f.name.startswith("_")
        }
    return methods


MUTATING = {
    m
    for port, names in _port_methods().items()
    if port != STORE_PORT
    for m in names
    if m not in READS
}
"""Every port method that changes the outside world, derived from the ports so a new
one (like ``remove_owned``) cannot be forgotten."""


def test_the_mutating_set_is_derived_from_the_ports() -> None:
    expected = {
        "claim", "release", "post_update", "checkout", "freeze_candidate",
        "checkout_revision", "publish_revision", "upsert_change_request",
        "start", "stop", "run", "remove_owned",
        "release_claim", "restore_label",  # ADR-0026 §5: targeted, journaled writes
        "clear_label",  # ADR-0027 §1: targeted, journaled write
    }  # fmt: skip
    assert expected == MUTATING


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


JOURNALED = {"perform", "reconcile", "perform_instance", "intend_instance"}
"""The journal's entry points; each is journaled and fenced. ``perform_instance``
is the instance-scoped one (ADR-0026 §5, the ADR-0020 amendment): fenced by the
instance lock instead of an execution lease. ``intend_instance`` commits only
an instance-scoped intent, for an effect resumed in a later pass (ADR-0027 §1)."""


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
            and n.func.attr in JOURNALED
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


def _effect_closures(method: ast.FunctionDef) -> set[ast.AST]:
    """Nodes inside nested functions or lambdas: the effects handed to ``perform``."""
    inner: set[ast.AST] = set()
    for node in ast.walk(method):
        if node is not method and isinstance(node, ast.FunctionDef | ast.Lambda):
            inner.update(ast.walk(node))
    return inner


def _unjournaled_mutations(source: str) -> list[str]:
    tree = ast.parse(source)
    found = []
    for cls in tree.body:
        if not (isinstance(cls, ast.ClassDef) and cls.name == "Effects"):
            continue
        for method in cls.body:
            if not isinstance(method, ast.FunctionDef):
                continue
            inner = _effect_closures(method)
            for node in ast.walk(method):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in MUTATING
                    and _receiver_names(node.func.value) & PORTS
                    and node not in inner
                ):
                    found.append(f"Effects.{method.name}:{node.lineno} .{node.func.attr}()")
    return found


def test_every_mutating_port_call_in_effects_is_inside_a_journaled_effect() -> None:
    """A mutating call outside an effect closure would bypass the write-ahead intent,
    the ownership re-check and the lease check (ADR-0020), even in recovery cleanup."""
    assert _unjournaled_mutations((CORE / "effects.py").read_text()) == []


def test_the_closure_rule_detects_a_direct_call() -> None:
    sample = (
        "class Effects:\n"
        "    def settle(self):\n"
        "        self._c.container_runtime.remove_owned('x', owner=o)\n"
        "        def effect():\n"
        "            return self._c.container_runtime.remove_owned('y', owner=o)\n"
    )
    assert _unjournaled_mutations(sample) == ["Effects.settle:3 .remove_owned()"]
