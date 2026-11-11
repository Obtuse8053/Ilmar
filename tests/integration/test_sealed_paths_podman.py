# SPDX-License-Identifier: Apache-2.0
"""Claude Code's settings locations are sealed at the sandbox boundary (ADR-0036, #90).

In a real Podman sandbox, as the unprivileged sandbox user, nothing can
create, change, remove or rename a sealed settings location, so no
``apiKeyHelper`` or ``env`` setting can appear after the credential check, and
the repository's own ``.claude/`` is hidden and left unchanged on the host.
The credential probe accepts the sandbox, and refuses one without the seals.
Needs ``ILMAR_TEST_PODMAN_IMAGE``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ilmar.adapters.claude_code.credential_route import (
    SEALED_PATHS,
    parse_probe,
    probe_command,
    refusal,
)
from ilmar.adapters.podman import PodmanRuntime
from ilmar.core.layout import WorkspaceLayout
from ilmar.domain.workspace import ContainerHandle, ContainerSpec, ResourceOwner

from integration.podman_support import (
    IMAGE,
    PODMAN,
    identity_of,
    make_workspace,
    remove_everything,
    require,
    sh,
)

pytestmark = pytest.mark.integration

OWNER = ResourceOwner(instance_id="it", execution_id="sealed", attempt=1, token="tok-sealed")
NAMES = ("ilmar-it-sealed", "ilmar-it-unsealed")
REPO_SETTINGS = '{"permissions": {"allow": ["Bash"]}}\n'
HELPER = '{"apiKeyHelper": "/bin/echo sk-ant-other-key"}'


@pytest.fixture(autouse=True)
def _require() -> Iterator[None]:
    require(("podman", PODMAN), ("ILMAR_TEST_PODMAN_IMAGE", IMAGE))
    remove_everything(*NAMES)
    yield
    remove_everything(*NAMES)


@pytest.fixture
def sandbox(tmp_path: Path) -> Iterator[tuple[PodmanRuntime, ContainerHandle, Path]]:
    layout = WorkspaceLayout.under(tmp_path.resolve() / "state")
    layout.workspaces_root.mkdir(parents=True)
    workspace = make_workspace(layout, "sealed")
    (workspace / ".claude").mkdir()
    (workspace / ".claude" / "settings.json").write_text(REPO_SETTINGS)
    runtime = PodmanRuntime(layout=layout)
    handle = runtime.start(
        ContainerSpec(image=IMAGE, host_workspace=workspace, name=NAMES[0], owner=OWNER,
                      workspace_identity=identity_of(workspace), sealed_paths=SEALED_PATHS)
    )  # fmt: skip
    try:
        yield runtime, handle, workspace
    finally:
        runtime.stop(handle, owner=OWNER)


def fails(runtime: PodmanRuntime, handle: ContainerHandle, script: str) -> None:
    done = runtime.run(handle, sh(script), timeout_seconds=30)
    assert done.exit_code != 0, f"succeeded inside the sandbox: {script}"


ATTEMPTS = [
    # the project's settings directory
    f"printf '%s' '{HELPER}' > /workspace/.claude/settings.json",
    f"printf '%s' '{HELPER}' > /workspace/.claude/settings.local.json",
    "mkdir /workspace/.claude/x",
    "rm -rf /workspace/.claude",
    "rmdir /workspace/.claude",
    "mv /workspace/.claude /workspace/.claude-moved",
    # the user settings files
    f"printf '%s' '{HELPER}' > $HOME/.claude/settings.json",
    f"printf '%s' '{HELPER}' > $HOME/.claude/settings.local.json",
    "touch $HOME/.claude/settings.json/x",
    "rmdir $HOME/.claude/settings.json",
    "mv $HOME/.claude/settings.json $HOME/.claude/old",
    "mv $HOME/.claude/settings.local.json $HOME/.claude/old-local",
    # the user settings directory itself
    "mv $HOME/.claude $HOME/.claude-moved",
    "rm -rf $HOME/.claude",
    # and no way to lift a seal
    "umount /workspace/.claude",
    "umount $HOME/.claude/settings.json",
    "mount -o remount,rw /workspace/.claude",
]


@pytest.mark.parametrize("script", ATTEMPTS)
def test_no_sealed_settings_location_can_be_changed(
    sandbox: tuple[PodmanRuntime, ContainerHandle, Path], script: str
) -> None:
    runtime, handle, _ = sandbox
    fails(runtime, handle, script)


def test_seals_hide_repository_settings_and_leave_them_unchanged(
    sandbox: tuple[PodmanRuntime, ContainerHandle, Path],
) -> None:
    runtime, handle, workspace = sandbox
    listing = runtime.run(handle, sh("ls -A /workspace/.claude; echo END"), timeout_seconds=30)
    assert listing.exit_code == 0
    assert listing.stdout.strip() == "END"  # empty from inside
    for script in ATTEMPTS:
        runtime.run(handle, sh(script), timeout_seconds=30)
    assert (workspace / ".claude" / "settings.json").read_text() == REPO_SETTINGS
    assert sorted(p.name for p in (workspace / ".claude").iterdir()) == ["settings.json"]


def test_claude_code_state_stays_writable(
    sandbox: tuple[PodmanRuntime, ContainerHandle, Path],
) -> None:
    runtime, handle, _ = sandbox
    done = runtime.run(
        handle,
        sh("mkdir -p $HOME/.claude/projects && echo x > $HOME/.claude/projects/state "
           "&& echo '{}' > $HOME/.claude.json && cat $HOME/.claude/projects/state"),
        timeout_seconds=30,
    )  # fmt: skip
    assert (done.exit_code, done.stdout.strip()) == (0, "x"), done.stderr


def test_the_real_probe_accepts_the_sealed_sandbox(
    sandbox: tuple[PodmanRuntime, ContainerHandle, Path],
) -> None:
    runtime, handle, _ = sandbox
    env = {"ANTHROPIC_API_KEY": "sk-ant-integration-not-real"}
    done = runtime.run(handle, probe_command(), timeout_seconds=30, env=env)
    assert done.exit_code == 0, done.stdout + done.stderr
    report = parse_probe(done.stdout)
    assert report is not None, done.stdout
    assert refusal(report, gateway_configured=False, expected_variables=env) is None


def test_the_real_probe_refuses_an_unsealed_sandbox(tmp_path: Path) -> None:
    layout = WorkspaceLayout.under(tmp_path.resolve() / "state")
    layout.workspaces_root.mkdir(parents=True)
    workspace = make_workspace(layout, "unsealed")
    runtime = PodmanRuntime(layout=layout)
    handle = runtime.start(
        ContainerSpec(image=IMAGE, host_workspace=workspace, name=NAMES[1], owner=OWNER,
                      workspace_identity=identity_of(workspace))
    )  # fmt: skip
    try:
        done = runtime.run(handle, probe_command(), timeout_seconds=30)
        report = parse_probe(done.stdout)
        assert report is not None, done.stdout
        reason = refusal(report, gateway_configured=False)
        assert reason is not None
        assert "not sealed" in reason
    finally:
        runtime.stop(handle, owner=OWNER)
