# SPDX-License-Identifier: Apache-2.0
"""The protected safety controls (ADR-0031 §1, #92).

A candidate that touches any of these paths is never pushed: it fails with
reason code ``protected-path``, its evidence is kept and its task stays
claimed and blocked for the maintainer.

The list is code in the running release. It is never read from a candidate,
a workspace or a target repository. The operator may add patterns
(``[verification] extra_safety_controls``); nothing removes a built-in entry.
This module is itself on the list.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from posixpath import join as posix_join
from posixpath import normpath

from ilmar.domain.verification import DEFAULT_CI_CONFIG_PATHS, matching

EVERY_TARGET: tuple[str, ...] = (
    *DEFAULT_CI_CONFIG_PATHS,  # CI the provider runs by itself
    "**/.gitattributes",  # filters and diff drivers run on checkout
    "**/.gitmodules",  # fetches other repositories
)
"""Protected in every target repository."""

ILMAR_REPOSITORY: tuple[str, ...] = (
    # Governance, the founding documents, and every instruction file or setting
    # that steers a later agent session, at any depth.
    ".github/",
    "**/AGENTS.md",
    "**/CLAUDE.md",
    ".claude/",
    ".mcp.json",
    "MANIFEST.md",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    # The tests that hold the rules, what configures every test run, and the
    # code and recorded evidence of the live runs, which hold real credentials.
    "tests/architecture/",
    "tests/contracts/",
    "**/conftest.py",
    "tests/live_harness.py",
    "tests/integration/",
    "tests/fixtures/claude_code_streams/",
    # Dependencies, tool configuration that affects verification, CI helpers,
    # the agent image and the supervisor.
    "pyproject.toml",
    "requirements/",
    "scripts/",
    "images/",
    "deploy/",
    # Ilmar's code (audit of 2026-11-12, #118). Every package below can alter
    # agent execution, credentials, sandbox policy, coordination, verification
    # or publication, so each is protected whole: a new module in it is
    # protected from its first line.
    #   adapters/: the agent runner and its stream parser, the built-in wiring,
    #     git (freezing), GitHub (claims and publication), Podman (the
    #     sandbox), the stores, options and secrets.
    #   core/: the worker, coordinator, effects, journal, ownership,
    #     cancellation, tracker reconciliation, verification, layout, naming.
    #   domain/: states, errors and their classification, candidates, tasks,
    #     the ledger, events, workspace policy, verification policy, this list.
    #   ports/: the contracts every adapter is held to.
    "src/ilmar/adapters/",
    "src/ilmar/core/",
    "src/ilmar/domain/",
    "src/ilmar/ports/",
    # The entry points: run (where unattended operation stays disabled),
    # cancel, suspend and resume, the read-only audit view, configuration.
    "src/ilmar/__main__.py",
    "src/ilmar/cli.py",
    "src/ilmar/cli_*.py",
    "src/ilmar/config.py",
)
"""Also protected when the target is Ilmar's own repository (``self_repository``)."""


def safety_controls(*, self_repository: bool, extra: Iterable[str] = ()) -> tuple[str, ...]:
    """The patterns in force: the built-ins, then the operator's additions."""
    built_in = EVERY_TARGET + (ILMAR_REPOSITORY if self_repository else ())
    return tuple(dict.fromkeys((*built_in, *extra)))


def touched(
    changed: Iterable[str],
    patterns: Iterable[str],
    *,
    link_targets: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """The changed paths that touch a safety control, sorted.

    ``changed`` lists every added, modified or deleted path, so both sides of a
    rename and mode-only changes are in it. ``link_targets`` maps a changed
    symbolic link to its target. A link touches a control when its target,
    resolved inside the repository, is protected, lies in a protected
    directory, or is a directory that holds a protected path. An absolute
    target, or one that leaves the repository, is judged by the link's own
    path only.
    """
    pattern_list = tuple(patterns)
    paths = set(changed)
    found = set(matching(paths, pattern_list))
    for link, target in (link_targets or {}).items():
        if link in paths and _protected_target(link, target, pattern_list):
            found.add(link)
    return tuple(sorted(found))


def _protected_target(link: str, target: str, patterns: tuple[str, ...]) -> bool:
    if target.startswith("/"):
        return False
    resolved = normpath(posix_join(link.rpartition("/")[0], target))
    if resolved == ".." or resolved.startswith("../"):
        return False
    if resolved == ".":
        return True  # the whole repository
    if matching((resolved, resolved + "/x"), patterns):
        return True  # protected itself, or in a protected directory
    literal = (p for p in patterns if not any(c in p for c in "*?"))
    return any(p.startswith(resolved + "/") for p in literal)  # holds a protected path
