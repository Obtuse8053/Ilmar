# SPDX-License-Identifier: Apache-2.0
"""The tracker-reconciliation boundaries ADR-0026 fixes, read statically with ``ast``.

* Only the worker records tracker decisions and lost-claim observations: the
  events are built only in ``core/tracker.py``, always with the coordinator as
  actor, and its builders are called only from ``core/worker.py``.
* ``release_claim`` is called only inside ``Effects.release_orphan_claim``,
  journaled as ``release-orphan-claim`` through ``perform_instance``;
  ``restore_label`` only inside ``Effects.restore_label``, journaled as
  ``restore-claim-label``.
* ``release-orphan-claim`` is the only instance-scoped kind, and
  ``perform_instance`` is called only for it.
* Of the task provider, only ``standing``, ``held_claims``, ``get`` and
  ``list_eligible`` (and ``name``) are callable outside the journal.
* Nothing in ``core/tracker.py`` reads a timestamp (ADR-0022 §Time).
"""

from __future__ import annotations

import ast
from pathlib import Path

from architecture.test_side_effects import READS
from ilmar.domain.ledger import INSTANCE_SCOPED_KINDS, OperationKind

SRC = Path(__file__).resolve().parents[2] / "src" / "ilmar"
CORE = SRC / "core"
TRACKER = CORE / "tracker.py"
EFFECTS = CORE / "effects.py"
BUILDERS = {"claim_lost", "decision"}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _method(cls: str, name: str) -> ast.FunctionDef:
    (found,) = [
        f
        for c in _tree(EFFECTS).body
        if isinstance(c, ast.ClassDef) and c.name == cls
        for f in c.body
        if isinstance(f, ast.FunctionDef) and f.name == name
    ]
    return found


def _creates(path: Path, kind: str) -> list[ast.Call]:
    return [
        call
        for call in _calls(_tree(path))
        if _name(call) == "create"
        and any(isinstance(a, ast.Attribute) and a.attr == kind for a in call.args)
    ]


def test_only_the_tracker_module_builds_claim_lost_events() -> None:
    builders = {p.name for p in _sources() if _creates(p, "EXECUTION_CLAIM_LOST")}
    assert builders == {"tracker.py"}


def test_the_tracker_records_as_the_coordinator() -> None:
    text = TRACKER.read_text()
    assert "COORDINATOR = Actor(ActorKind.COORDINATOR)" in text
    creates = [c for c in _calls(_tree(TRACKER)) if _name(c) == "create"]
    assert creates
    for call in creates:
        actor = next(k.value for k in call.keywords if k.arg == "actor")
        assert isinstance(actor, ast.Name)
        assert actor.id == "COORDINATOR"


def test_only_the_worker_records_tracker_decisions() -> None:
    callers = set()
    for path in _sources():
        if path == TRACKER:
            continue
        for call in _calls(_tree(path)):
            func = call.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in BUILDERS
                and isinstance(func.value, ast.Name)
                and func.value.id == "tracker"
            ):
                callers.add(path.name)
    assert callers == {"worker.py"}


def test_targeted_claim_writes_are_journaled_by_their_own_kind() -> None:
    for method, port_call, entry, kind in (
        ("release_orphan_claim", "release_claim", "perform_instance", "RELEASE_ORPHAN_CLAIM"),
        ("restore_label", "restore_label", "perform", "RESTORE_CLAIM_LABEL"),
    ):
        node = _method("Effects", method)
        journal = [c for c in _calls(node) if _name(c) == entry]
        assert len(journal) == 1, method
        first = journal[0].args[0]
        assert isinstance(first, ast.Attribute)
        assert first.attr == kind
        inner = {
            n
            for f in ast.walk(node)
            if isinstance(f, ast.FunctionDef) and f is not node
            for n in ast.walk(f)
        }
        port = [c for c in _calls(node) if _name(c) == port_call and c.func is not None]
        port = [c for c in port if isinstance(c.func, ast.Attribute) and "task_provider" in
                ast.dump(c.func.value)]  # fmt: skip
        assert port
        assert all(c in inner for c in port), f"{method} calls {port_call} outside its effect"
    for path in _sources():
        if path == EFFECTS:
            continue
        for call in _calls(_tree(path)):
            if isinstance(call.func, ast.Attribute) and call.func.attr in (
                "release_claim",
                "restore_label",
            ):
                assert "task_provider" not in ast.dump(call.func.value), path.name


def test_release_orphan_claim_is_the_only_instance_scoped_kind() -> None:
    assert {OperationKind.RELEASE_ORPHAN_CLAIM} == INSTANCE_SCOPED_KINDS
    assert {k for k in OperationKind if k.instance_scoped} == {OperationKind.RELEASE_ORPHAN_CLAIM}
    sites = [
        path.name
        for path in _sources()
        for call in _calls(_tree(path))
        if _name(call) == "perform_instance" and path.name != "journal.py"
    ]
    assert sites == ["effects.py"]


def test_only_reads_of_the_task_provider_are_callable_outside_the_journal() -> None:
    provider = {
        f.name
        for c in _tree(SRC / "ports" / "task_provider.py").body
        if isinstance(c, ast.ClassDef)
        for f in c.body
        if isinstance(f, ast.FunctionDef)
    }
    assert provider & READS == {"name", "get", "list_eligible", "standing", "held_claims"}


def test_the_lost_claim_confirmation_reads_no_timestamp() -> None:
    """ADR-0022 §Time: order comes from ``seq`` and pass ids, never from a clock."""
    tree = _tree(TRACKER)
    functions = {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
    deciding = ("claim_write", "open_observation", "observed_in_pass", "may_observe",
                "confirms", "stale", "accepted_reason", "_seq")  # fmt: skip
    for name in deciding:
        used = {n.attr for n in ast.walk(functions[name]) if isinstance(n, ast.Attribute)}
        used |= {n.id for n in ast.walk(functions[name]) if isinstance(n, ast.Name)}
        assert not used & {"at", "created_at", "updated_at", "recorded_at", "now", "clock"}, name
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not imported & {"time", "datetime"}
    assert not modules & {"time", "datetime"}
