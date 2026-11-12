# SPDX-License-Identifier: Apache-2.0
"""ADR-0031 §1 and §5 (#92): the protected list covers every safety-control
module, and neither the list nor the verification policy has a candidate-side
source.

* A new module whose name says it is a safety control (credentials, sandbox,
  gateway, fencing, ownership, quarantine, journal, budgets, limits,
  suspension, time, restarts, configuration, verification, gates) fails here
  until it is added to ``ILMAR_REPOSITORY``.
* The list is constants in a module with no file, network or environment
  access. The policy is built in exactly one place, the configuration loader,
  from the running release's list and the operator's configuration.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from ilmar.domain.safety_controls import ILMAR_REPOSITORY, safety_controls, touched

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "ilmar"
LIST_MODULE = SRC / "domain" / "safety_controls.py"
KINDS = re.compile(
    r"credential|secret|options|registry|sandbox|podman|container_runtime|mount|safe_fs|"
    r"lifetime|gateway|ownership|fenc|quarantine|journal|effects|ledger|sqlite|execution_store|"
    r"budget|limit|suspen|clock|time|start|restart|config|verification|gate|safety"
)


def modules() -> list[str]:
    return sorted(str(p.relative_to(ROOT)) for p in SRC.rglob("*.py"))


def test_every_safety_control_module_is_on_the_list() -> None:
    patterns = safety_controls(self_repository=True)
    missing = [m for m in modules() if KINDS.search(m) and not touched([m], patterns)]
    assert missing == [], f"add these to ILMAR_REPOSITORY (ADR-0031 §1): {missing}"


def test_the_list_protects_itself_and_the_policy() -> None:
    patterns = safety_controls(self_repository=True)
    for module in ("domain/safety_controls.py", "domain/verification.py", "config.py"):
        assert touched([f"src/ilmar/{module}"], patterns)
    assert "tests/architecture/" in ILMAR_REPOSITORY


def test_the_list_module_reads_nothing_from_outside() -> None:
    tree = ast.parse(LIST_MODULE.read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imported <= {"__future__", "collections", "posixpath", "ilmar"}, imported
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for forbidden in ("open", "Path", "environ", "getenv", "read_text", "read_bytes"):
        assert forbidden not in names | attributes, forbidden


def test_the_policy_and_the_list_are_built_only_by_the_configuration_loader() -> None:
    built: dict[str, list[str]] = {"VerificationPolicy(": [], "safety_controls(": []}
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        for call, found in built.items():
            if re.search(rf"(?<![\w.]){re.escape(call)}", text.replace(f"def {call}", "")):
                found.append(str(path.relative_to(SRC)))
    assert built["VerificationPolicy("] == ["config.py"]
    assert built["safety_controls("] == ["config.py"]


def test_the_configuration_loader_takes_no_candidate_or_workspace_path() -> None:
    """The loader reads the operator's configuration file only: no function in
    config.py names a workspace, a candidate or a checkout."""
    tree = ast.parse((SRC / "config.py").read_text())
    arguments = {
        a.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        for a in node.args.args + node.args.kwonlyargs
    }
    assert not {a for a in arguments if re.search(r"workspace|candidate|checkout", a)}
