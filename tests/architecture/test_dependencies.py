# SPDX-License-Identifier: Apache-2.0
"""Enforce the ports-and-adapters dependency rules (ADR-0002).

Imports are read statically with ``ast`` so that a forbidden import is caught
even if the module would fail to load.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "ilmar"

# Layer -> ilmar subpackages it may import. Standard-library imports are
# always allowed for these layers; third-party imports never are.
LAYER_RULES: dict[str, frozenset[str]] = {
    "domain": frozenset({"ilmar.domain"}),
    "ports": frozenset({"ilmar.domain", "ilmar.ports"}),
    "core": frozenset({"ilmar.domain", "ilmar.ports", "ilmar.core"}),
    "config": frozenset({"ilmar.domain"}),
}


def _modules(layer: str) -> list[Path]:
    package = PACKAGE_ROOT / layer
    if package.is_dir():
        return sorted(package.rglob("*.py"))
    return [PACKAGE_ROOT / f"{layer}.py"]


def _imports(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise AssertionError(f"{path}: relative imports are not allowed")
            if node.module:
                yield node.module


def _is_allowed(module: str, allowed: frozenset[str]) -> bool:
    top = module.split(".")[0]
    if top == "__future__" or (top in sys.stdlib_module_names and top != "ilmar"):
        return True
    return any(module == prefix or module.startswith(prefix + ".") for prefix in allowed)


@pytest.mark.parametrize(
    ("layer", "path"),
    [(layer, path) for layer in LAYER_RULES for path in _modules(layer)],
    ids=lambda value: value if isinstance(value, str) else value.name,
)
def test_layer_imports_respect_dependency_rule(layer: str, path: Path) -> None:
    forbidden = [m for m in _imports(path) if not _is_allowed(m, LAYER_RULES[layer])]
    assert not forbidden, (
        f"{path.relative_to(PACKAGE_ROOT)} ({layer}) imports {forbidden}; "
        f"allowed: standard library and {sorted(LAYER_RULES[layer])}"
    )


def test_every_layer_has_modules() -> None:
    for layer in LAYER_RULES:
        assert all(p.exists() for p in _modules(layer)), layer


def test_rule_detects_violations() -> None:
    """Guard against the checker silently allowing everything."""
    assert not _is_allowed("ilmar.adapters.sqlite", LAYER_RULES["core"])
    assert not _is_allowed("requests", LAYER_RULES["core"])
    assert not _is_allowed("ilmar.core", LAYER_RULES["domain"])
    assert _is_allowed("sqlite3", LAYER_RULES["domain"])
    assert _is_allowed("ilmar.domain.tasks", LAYER_RULES["ports"])


# -- the event log (ADR-0024 §9) -------------------------------------------------------

EVENT_MODULES = frozenset({"ilmar.domain.events", "ilmar.domain.event_history",
                           "ilmar.ports.event_log"})  # fmt: skip
STORE_ADAPTERS = frozenset({"sqlite.py", "memory.py"})
"""The store adapters write events in the same transaction as their rows. No
other adapter has store access, so no other adapter may touch events."""


def _uses_events(path: Path) -> list[str]:
    found = []
    for module in _imports(path):
        if module in EVENT_MODULES:
            found.append(module)
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("ilmar.domain", "ilmar.ports"):
            found += [f"{node.module}.{a.name}" for a in node.names
                      if f"{node.module}.{a.name}" in EVENT_MODULES]  # fmt: skip
    return found


@pytest.mark.parametrize(
    "path",
    sorted((PACKAGE_ROOT / "adapters").rglob("*.py")),
    ids=lambda p: str(p.relative_to(PACKAGE_ROOT / "adapters")),
)
def test_only_store_adapters_touch_events(path: Path) -> None:
    if path.parent == PACKAGE_ROOT / "adapters" and path.name in STORE_ADAPTERS:
        return
    assert _uses_events(path) == [], (
        f"{path.relative_to(PACKAGE_ROOT)} uses the event log; only the store adapters "
        f"{sorted(STORE_ADAPTERS)} write events (ADR-0024 §2)"
    )


def test_the_event_rule_detects_violations(tmp_path: Path) -> None:
    offender = tmp_path / "offender.py"
    offender.write_text("from ilmar.domain import events\nfrom ilmar.ports.event_log import x\n")
    assert _uses_events(offender) == ["ilmar.ports.event_log", "ilmar.domain.events"]
    assert all((PACKAGE_ROOT / "adapters" / name).exists() for name in STORE_ADAPTERS)
