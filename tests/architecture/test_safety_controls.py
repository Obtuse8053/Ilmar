# SPDX-License-Identifier: Apache-2.0
"""ADR-0031 §1 and §5 (#92): the protected list covers all code that can
alter agent execution, credentials, sandbox policy, coordination, verification
or publication, and neither the list nor the verification policy has a
candidate-side source.

* **The whole package.** ``src/ilmar/`` is one protected entry, so every file
  in it, ``__init__.py`` and any new top-level module included, is protected
  by the list itself, not only by this test (maintainer decision, review of
  #118). There are no exemptions.
* **Explicit requirements.** ``REQUIRED`` names paths that must stay
  protected whatever the list's shape. It includes the omissions found in
  review on 2026-11-12, so they cannot recur.
* The list is constants in a module with no file, network or environment
  access. The policy is built in exactly one place, the configuration loader,
  from the running release's list and the operator's configuration.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from ilmar.domain.safety_controls import ILMAR_REPOSITORY, safety_controls, touched

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "ilmar"
LIST_MODULE = SRC / "domain" / "safety_controls.py"
PROTECTED = safety_controls(self_repository=True)

REQUIRED = (
    # Named by the maintainer in the review of #118 (2026-11-12).
    "src/ilmar/adapters/claude_code/runner.py",
    "src/ilmar/adapters/builtin.py",
    "src/ilmar/core/worker.py",
    "src/ilmar/core/coordinator.py",
    "src/ilmar/adapters/github/source_control.py",
    "AGENTS.md",
    "CLAUDE.md",
    "docs/AGENTS.md",
    "src/ilmar/core/CLAUDE.md",
    # Found in the audit that followed.
    "src/ilmar/adapters/claude_code/stream.py",  # progress and the result of a run
    "src/ilmar/adapters/git/trees.py",  # freezing the candidate
    "src/ilmar/adapters/git/runner.py",  # host-side git
    "src/ilmar/adapters/github/client.py",  # the forge token
    "src/ilmar/adapters/github/tasks.py",  # claims
    "src/ilmar/adapters/memory.py",  # a store
    "src/ilmar/core/components.py",  # wiring, the scanner
    "src/ilmar/core/cancellation.py",
    "src/ilmar/core/tracker.py",
    "src/ilmar/core/layout.py",  # what is mounted
    "src/ilmar/core/naming.py",  # owner labels and branch names
    "src/ilmar/domain/agent.py",  # run outcomes and their classification
    "src/ilmar/domain/errors.py",  # which failures are retried
    "src/ilmar/domain/changes.py",  # the candidate
    "src/ilmar/domain/events.py",
    "src/ilmar/domain/execution.py",
    "src/ilmar/ports/agent_runner.py",
    "src/ilmar/ports/source_control.py",
    "src/ilmar/ports/task_provider.py",
    "src/ilmar/cli.py",  # where unattended operation stays disabled
    "src/ilmar/cli_cancel.py",
    "src/ilmar/__main__.py",
    ".claude/settings.json",
    ".mcp.json",
    "tests/live_harness.py",  # runs with real credentials
    "tests/integration/test_claude_code_live.py",
    "tests/fixtures/claude_code_streams/recorded.json",  # gate evidence
    # The first list (part 1).
    ".github/workflows/ci.yml",
    "MANIFEST.md",
    "tests/architecture/test_safety_controls.py",
    "tests/contracts/source_control.py",
    "tests/conftest.py",
    "pyproject.toml",
    "requirements/live.txt",
    "scripts/live_ci.py",
    "images/claude-code/Containerfile",
    "deploy/systemd/ilmar.service",
    "src/ilmar/config.py",
    "src/ilmar/domain/verification.py",
    "src/ilmar/domain/safety_controls.py",
)


def test_every_required_path_is_protected() -> None:
    missing = [p for p in REQUIRED if not touched([p], PROTECTED)]
    assert missing == [], f"ADR-0031 §1: these must be protected: {missing}"


def test_the_whole_package_is_one_protected_entry() -> None:
    assert "src/ilmar/" in ILMAR_REPOSITORY


def test_every_existing_file_in_the_package_is_protected() -> None:
    files = sorted(str(p.relative_to(ROOT)) for p in SRC.rglob("*") if p.is_file())
    assert files
    assert [f for f in files if not touched([f], PROTECTED)] == []


@pytest.mark.parametrize(
    "path",
    [
        "src/ilmar/__init__.py",
        "src/ilmar/new_top_level.py",  # a candidate's new root-level module
        "src/ilmar/_hidden.py",
        "src/ilmar/sitecustomize.py",
        "src/ilmar/new_package/__init__.py",
        "src/ilmar/core/deeper/new.py",
        "src/ilmar/data.json",  # not only Python
        "src/ilmar/py.typed",
    ],
)
def test_a_new_file_anywhere_in_the_package_is_protected(path: str) -> None:
    assert touched([path], PROTECTED) == (path,)


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
