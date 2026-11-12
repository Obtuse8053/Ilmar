# SPDX-License-Identifier: Apache-2.0
"""The protected safety controls list and the touch rule (ADR-0031 §1-§2, #92)."""

from __future__ import annotations

import pytest

from ilmar.domain.safety_controls import (
    EVERY_TARGET,
    ILMAR_REPOSITORY,
    safety_controls,
    touched,
)
from ilmar.domain.verification import path_matches

SELF = safety_controls(self_repository=True)
OTHER = safety_controls(self_repository=False)


def example(pattern: str) -> str:
    """A concrete path the pattern protects."""
    path = pattern.replace("**/", "deep/er/").replace("*", "x")
    return path + "file.txt" if path.endswith("/") else path


# -- the list (ADR-0031 §1, #92's acceptance list) ------------------------------------

REQUIRED = [
    ".github/workflows/ci.yml",
    "AGENTS.md",
    "CLAUDE.md",
    "MANIFEST.md",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    "tests/architecture/test_dependencies.py",
    "tests/contracts/source_control.py",
    "tests/conftest.py",
    "src/x/conftest.py",
    "pyproject.toml",
    "requirements/live.txt",
    "scripts/live_ci.py",
    "images/claude-code/Containerfile",
    "deploy/systemd/ilmar.service",
    "src/ilmar/adapters/options.py",  # credentials
    "src/ilmar/adapters/claude_code/credential_route.py",
    "src/ilmar/core/secret_scan.py",
    "src/ilmar/adapters/podman/runtime.py",  # the sandbox
    "src/ilmar/ports/container_runtime.py",
    "src/ilmar/adapters/gateway/server.py",  # the gateway, once it exists
    "src/ilmar/core/ownership.py",  # fencing and ownership
    "src/ilmar/core/journal.py",
    "src/ilmar/core/effects.py",  # quarantine
    "src/ilmar/adapters/sqlite.py",
    "src/ilmar/domain/budget.py",  # budgets and limits
    "src/ilmar/core/suspension.py",
    "src/ilmar/core/clock.py",  # time
    "src/ilmar/core/starts.py",  # restarts
    "src/ilmar/config.py",  # configuration validation
    "src/ilmar/domain/verification.py",  # verification policy
    "src/ilmar/core/verification.py",
    "src/ilmar/core/gates.py",  # gate records
    "src/ilmar/domain/safety_controls.py",  # the list itself
]


@pytest.mark.parametrize("path", REQUIRED)
def test_each_required_control_is_protected_in_ilmar_itself(path: str) -> None:
    assert touched([path], SELF) == (path,)


@pytest.mark.parametrize("pattern", ILMAR_REPOSITORY)
def test_every_built_in_pattern_protects_a_path(pattern: str) -> None:
    path = example(pattern)
    assert path_matches(path, pattern)
    assert touched([path], SELF) == (path,)


@pytest.mark.parametrize("path", [".github/x.yml", ".gitlab-ci.yml", "a/.gitattributes",
                                  ".gitmodules", "Jenkinsfile"])  # fmt: skip
def test_ci_and_git_configuration_are_protected_in_every_target(path: str) -> None:
    assert touched([path], OTHER) == (path,)


def test_ilmars_own_controls_are_not_imposed_on_other_targets() -> None:
    assert touched(["src/ilmar/config.py", "pyproject.toml", "AGENTS.md"], OTHER) == ()


def test_operator_additions_extend_and_never_shorten_the_list() -> None:
    extended = safety_controls(self_repository=True, extra=("ops/", "AGENTS.md"))
    assert extended[: len(SELF)] == SELF
    assert "ops/" in extended
    assert len(extended) == len(SELF) + 1  # a repeated built-in is not duplicated
    assert set(EVERY_TARGET) <= set(safety_controls(self_repository=False, extra=()))


# -- what counts as touching (ADR-0031 §2) --------------------------------------------


def test_deletions_and_both_sides_of_a_rename_count() -> None:
    # A rename appears as the old path (deleted) and the new path (added).
    assert touched(["src/ilmar/config.py", "src/ilmar/settings.py"], SELF) == (
        "src/ilmar/config.py",
    )
    assert touched(["docs/a.md", "src/ilmar/core/journal.py"], SELF) == (
        "src/ilmar/core/journal.py",
    )


@pytest.mark.parametrize(
    ("link", "target"),
    [
        ("docs/policy", "../src/ilmar/config.py"),  # a protected file
        ("docs/wf", "../.github/workflows"),  # inside a protected directory
        ("tools/gh", "../.github"),  # the protected directory itself
        ("x/ilmar", "../src/ilmar"),  # a directory that holds protected paths
        ("root", "."),  # the whole repository
    ],
)
def test_a_link_to_a_protected_target_touches_it(link: str, target: str) -> None:
    assert touched([link], SELF, link_targets={link: target}) == (link,)


@pytest.mark.parametrize(
    ("link", "target"),
    [
        ("docs/readme", "../README.md"),
        ("docs/abs", "/etc/passwd"),  # outside: judged by the link's own path
        ("docs/out", "../../outside"),
    ],
)
def test_a_link_to_anything_else_does_not(link: str, target: str) -> None:
    assert touched([link], SELF, link_targets={link: target}) == ()


def test_only_changed_links_are_judged() -> None:
    assert touched(["docs/a.md"], SELF, link_targets={"old": "AGENTS.md"}) == ()
