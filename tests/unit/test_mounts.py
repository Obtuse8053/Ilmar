# SPDX-License-Identifier: Apache-2.0
"""Mount authorization must not rely on lexical checks alone (PR #37 review)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ilmar.core.layout import WorkspaceLayout
from ilmar.core.mounts import authorize_mount
from ilmar.core.safe_fs import DirectoryIdentity, ensure_directory
from ilmar.domain.errors import OwnershipConflictError


@pytest.fixture
def layout(tmp_path: Path) -> WorkspaceLayout:
    state = Path(str(tmp_path)).resolve()
    layout = WorkspaceLayout.under(state)
    layout.workspaces_root.mkdir()
    (layout.coordinator_root / "repos" / "github" / "abc").mkdir(parents=True)
    (layout.coordinator_root / "repos" / "github" / "abc" / "token").write_text("secret\n")
    return layout


def _secret_dir(layout: WorkspaceLayout) -> Path:
    return layout.coordinator_root / "repos" / "github" / "abc"


def _identity(path: Path) -> DirectoryIdentity:
    info = path.stat()
    return DirectoryIdentity(info.st_dev, info.st_ino)


def test_recorded_workspace_is_authorized(layout: WorkspaceLayout) -> None:
    agent = layout.agent_workspace("e", 1)
    identity = ensure_directory(agent, root=layout.workspaces_root)
    assert authorize_mount(layout, agent, identity) == agent


def test_lexical_check_alone_would_pass_a_symlinked_workspace(layout: WorkspaceLayout) -> None:
    """Demonstrates why the lexical check is not the authorization gate."""
    agent = layout.agent_workspace("e", 1)
    agent.parent.mkdir(parents=True)
    agent.symlink_to(_secret_dir(layout))
    assert layout.check_mountable(agent) == agent  # lexically fine...
    with pytest.raises(OwnershipConflictError, match="symlink"):  # ...but refused
        authorize_mount(layout, agent, _identity(_secret_dir(layout)))


def test_symlinked_parent_targeting_coordinator_is_refused(layout: WorkspaceLayout) -> None:
    fake_execution = layout.coordinator_root / "repos"
    (fake_execution / "1" / "agent").mkdir(parents=True)
    (layout.workspaces_root / "e").symlink_to(fake_execution)
    agent = layout.agent_workspace("e", 1)
    with pytest.raises(OwnershipConflictError, match="symlink"):
        authorize_mount(layout, agent, _identity(fake_execution / "1" / "agent"))


def test_symlinked_workspaces_root_is_refused(tmp_path: Path) -> None:
    state = Path(str(tmp_path)).resolve()
    real = state / "elsewhere"
    (real / "e" / "1" / "agent").mkdir(parents=True)
    layout = WorkspaceLayout.under(state)
    layout.workspaces_root.symlink_to(real)
    agent = layout.agent_workspace("e", 1)
    with pytest.raises(OwnershipConflictError, match="canonical"):
        authorize_mount(layout, agent, _identity(real / "e" / "1" / "agent"))


def test_root_beneath_a_symlinked_ancestor_is_refused(tmp_path: Path) -> None:
    real_state = Path(str(tmp_path)).resolve() / "real-state"
    (real_state / "workspaces" / "e" / "1" / "agent").mkdir(parents=True)
    link_state = Path(str(tmp_path)).resolve() / "link-state"
    link_state.symlink_to(real_state)
    layout = WorkspaceLayout.under(link_state)
    agent = layout.agent_workspace("e", 1)
    with pytest.raises(OwnershipConflictError, match="canonical"):
        authorize_mount(layout, agent, _identity(agent))


def test_replaced_directory_is_refused(layout: WorkspaceLayout) -> None:
    agent = layout.agent_workspace("e", 1)
    recorded = ensure_directory(agent, root=layout.workspaces_root)
    agent.rename(agent.parent / "old")
    agent.mkdir()
    with pytest.raises(OwnershipConflictError, match="recorded"):
        authorize_mount(layout, agent, recorded)


def test_missing_directory_is_refused(layout: WorkspaceLayout) -> None:
    with pytest.raises(OwnershipConflictError, match="does not exist"):
        authorize_mount(layout, layout.agent_workspace("e", 1), DirectoryIdentity(0, 0))


def test_coordinator_paths_fail_the_lexical_check_first(layout: WorkspaceLayout) -> None:
    with pytest.raises(ValueError, match="mount"):
        authorize_mount(layout, _secret_dir(layout), _identity(_secret_dir(layout)))
