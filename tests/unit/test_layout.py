# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

import pytest

from ilmar.core.layout import WorkspaceLayout
from ilmar.domain.changes import RepositoryRef

STATE = Path("/srv/ilmar/state")
LAYOUT = WorkspaceLayout.under(STATE)


def test_default_layout_has_disjoint_roots() -> None:
    assert LAYOUT.workspaces_root == STATE / "workspaces"
    assert LAYOUT.coordinator_root == STATE / "coordinator"


def test_attempt_layout() -> None:
    assert LAYOUT.attempt_dir("exec1", 2) == STATE / "workspaces/exec1/2"
    assert LAYOUT.agent_workspace("exec1", 2) == STATE / "workspaces/exec1/2/agent"
    assert LAYOUT.verification_checkout("exec1", 2, 3) == STATE / "workspaces/exec1/2/verify-3"


def test_each_verification_run_gets_a_fresh_directory() -> None:
    runs = {LAYOUT.verification_checkout("e", 1, run) for run in range(1, 5)}
    assert len(runs) == 4
    assert LAYOUT.agent_workspace("e", 1) not in runs


@pytest.mark.parametrize("execution_id", ["../escape", "/abs", "..", "a/b", "", ".hidden"])
def test_execution_ids_cannot_escape(execution_id: str) -> None:
    with pytest.raises(ValueError, match="execution id"):
        LAYOUT.attempt_dir(execution_id, 1)


@pytest.mark.parametrize(("attempt", "run"), [(0, 1), (1, 0), (-1, 1)])
def test_counters_must_be_positive(attempt: int, run: int) -> None:
    with pytest.raises(ValueError, match="starts at 1"):
        LAYOUT.verification_checkout("e", attempt, run)


def test_coordinator_repositories_live_outside_the_workspaces() -> None:
    hostile = RepositoryRef("github", "../../../../workspaces/e/1/agent")
    path = LAYOUT.coordinator_repository(hostile)
    assert path.is_relative_to(LAYOUT.coordinator_root)
    assert not path.is_relative_to(LAYOUT.workspaces_root)
    assert ".." not in path.parts
    other = LAYOUT.coordinator_repository(RepositoryRef("github", "owner/repo"))
    assert other != path
    with pytest.raises(ValueError, match="provider"):
        LAYOUT.coordinator_repository(RepositoryRef("../x", "owner/repo"))


def test_only_workspace_paths_are_mountable() -> None:
    agent = LAYOUT.agent_workspace("e", 1)
    assert LAYOUT.check_mountable(agent) == agent
    for bad in (
        LAYOUT.coordinator_repository(RepositoryRef("github", "o/r")),
        LAYOUT.coordinator_root,
        LAYOUT.workspaces_root,
        STATE,
        Path("/"),
        Path("/home/user/.ssh"),
        LAYOUT.workspaces_root / "e" / ".." / ".." / "coordinator",
        Path("relative/path"),
    ):
        with pytest.raises(ValueError, match=r"mount|mounted"):
            LAYOUT.check_mountable(bad)


@pytest.mark.parametrize(
    ("workspaces", "coordinator"),
    [
        ("/srv/w", "/srv/w/coordinator"),
        ("/srv/c/workspaces", "/srv/c"),
        ("/srv/x", "/srv/x"),
        ("relative", "/srv/c"),
        ("/srv/w/../c", "/srv/c2"),
    ],
)
def test_roots_must_be_absolute_and_disjoint(workspaces: str, coordinator: str) -> None:
    with pytest.raises(ValueError, match="root"):
        WorkspaceLayout(Path(workspaces), Path(coordinator))
