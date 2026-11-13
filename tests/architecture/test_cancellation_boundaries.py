# SPDX-License-Identifier: Apache-2.0
"""The cancellation boundaries ADR-0025 fixes, read statically with ``ast``.

* The request inbox is reached only through ``open_inbox``, always under the
  layout's ``coordinator_root`` (never mountable into a sandbox).
* Only the worker's applier (``core/cancellation.py``) records
  ``execution.cancel_requested`` for a person, or moves or removes inbox
  entries, and only the worker calls it; the CLI only submits. The tracker's
  decisions (ADR-0026 §3) are built only in ``core/tracker.py`` and recorded
  only by the worker (``tests/architecture/test_tracker_boundaries.py``).
* The watcher calls no port method and no filesystem function: it reads
  through the validation routine it is given, and sets an event.
* Only ``cancel_inbox`` opens, lists or reads inbox entries: every other
  module goes through its validation routine.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "ilmar"
INBOX = SRC / "core" / "cancel_inbox.py"
APPLIER = SRC / "core" / "cancellation.py"
WATCHER = SRC / "core" / "cancel_watcher.py"
WORKER = SRC / "core" / "worker.py"
CLI = SRC / "cli_cancel.py"

MUTATORS = {"take", "remove", "set_aside"}
"""``cancel_inbox`` functions that move or remove entries."""
READERS = {"open", "listdir", "scandir", "read", "read_text", "read_bytes", "iterdir", "glob"}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _called(tree: ast.AST) -> list[tuple[str, ast.Call]]:
    """``(name, call)`` for every call: the attribute or function name called."""
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                calls.append((func.attr, node))
            elif isinstance(func, ast.Name):
                calls.append((func.id, node))
    return calls


def _uses_inbox(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "cancel_inbox" in node.module:
            return True
        if isinstance(node, ast.ImportFrom) and any(a.name == "cancel_inbox" for a in node.names):
            return True
    return False


def test_the_inbox_is_opened_only_under_the_coordinator_root() -> None:
    sites = []
    for path in _sources():
        for name, call in _called(_tree(path)):
            if name != "open_inbox" or path == INBOX:
                continue
            first = call.args[0] if call.args else None
            ok = isinstance(first, ast.Attribute) and first.attr in ("coordinator_root", "_root")
            sites.append((path.name, ok))
    assert sites, "open_inbox is used"
    assert [site for site in sites if not site[1]] == []
    applier = APPLIER.read_text()
    assert "self._root = layout.coordinator_root" in applier


def test_only_the_applier_records_a_cancel_request() -> None:
    writers = []
    for path in _sources():
        text = path.read_text()
        if "EXECUTION_CANCEL_REQUESTED" not in text:
            continue
        for name, call in _called(_tree(path)):
            if name == "create" and any(
                isinstance(a, ast.Attribute) and a.attr == "EXECUTION_CANCEL_REQUESTED"
                for a in call.args
            ):
                writers.append(path.name)
    assert writers == ["cancellation.py", "tracker.py"]  # a person; the tracker (ADR-0026)


def test_only_the_applier_moves_or_removes_inbox_entries() -> None:
    movers = set()
    for path in _sources():
        if path == INBOX:
            continue
        for name, _ in _called(_tree(path)):
            if name in MUTATORS and _uses_inbox(_tree(path)):
                movers.add(path.name)
    assert movers == {"cancellation.py"}
    applier_callers = {
        path.name
        for path in _sources()
        if path != APPLIER and any(name == "Cancellations" for name, _ in _called(_tree(path)))
    }
    assert applier_callers == {"worker.py"}


def test_the_cli_only_submits() -> None:
    names = {name for name, _ in _called(_tree(CLI))}
    assert "submit" in names
    assert not names & (MUTATORS | {"apply", "commit", "record", "remove", "unlink", "rename"})


def test_the_watcher_calls_no_port_method_and_no_filesystem_function() -> None:
    tree = _tree(WATCHER)
    allowed = {
        "Event", "Thread", "ValueError", "start", "set", "join", "wait", "_read",
        "_suspended",  # the injected read-only check below (ADR-0029 §3 amendment, #18)
    }  # fmt: skip
    called = {name for name, _ in _called(tree)}
    assert called <= allowed, called - allowed
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any(
        m.startswith(("ilmar.ports", "ilmar.adapters", "os", "pathlib")) for m in imported
    )
    assert not any(
        isinstance(node, ast.Import) and any(a.name in ("os", "shutil") for a in node.names)
        for node in ast.walk(tree)
    )


def test_only_the_validation_routine_reads_the_inbox() -> None:
    readers = []
    for path in _sources():
        if path == INBOX:
            continue
        tree = _tree(path)
        if not _uses_inbox(tree):
            continue
        for name, _ in _called(tree):
            if name in READERS:
                readers.append((path.name, name))
    assert readers == []


def test_the_watchers_suspension_check_only_reads() -> None:
    """ADR-0029 §3 (amended 2026-11-13): the worker hands the watcher
    ``suspension_reason``, which may only read the kill-switch file and the
    store's suspension; it writes nothing."""
    worker = _tree(WATCHER.parent / "worker.py")
    (method,) = [
        node for node in ast.walk(worker)
        if isinstance(node, ast.FunctionDef) and node.name == "suspension_reason"
    ]  # fmt: skip
    called = {name for name, _ in _called(method)}
    assert called <= {"kill_switch", "suspension"}, called
