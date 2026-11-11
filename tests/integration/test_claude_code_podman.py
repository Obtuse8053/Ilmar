# SPDX-License-Identifier: Apache-2.0
"""ClaudeCodeRunner on real Podman with a scripted ``claude`` (#8; ``pytest -m integration``).

The stand-in is a POSIX shell script in the workspace that speaks Claude Code's
documented JSON result, so argument passing, the per-command environment,
exit statuses and timeouts are exercised through a real container. Same
requirements as ``test_podman_runtime.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from ilmar.adapters.claude_code import ClaudeCodeRunner, ClaudeCodeSettings
from ilmar.adapters.podman import PodmanRuntime
from ilmar.core.layout import WorkspaceLayout
from ilmar.domain.agent import AgentOutcome
from ilmar.domain.errors import PolicyViolationError
from ilmar.domain.workspace import ContainerHandle, ContainerSpec, ResourceOwner
from ilmar.ports.agent_runner import AgentRunner
from ilmar.ports.container_runtime import ContainerRuntime

from claude_code_support import shell_script
from contracts.agent_runner import REQUEST, AgentRunnerContract, AgentScenario
from integration.podman_support import (
    IMAGE,
    PODMAN,
    identity_of,
    make_workspace,
    podman,
    remove_everything,
    require,
)

pytestmark = pytest.mark.integration

KEY = "sk-ant-integration-test-not-real"
OWNER = ResourceOwner(instance_id="it", execution_id="claude", attempt=1, token="tok-claude")
FAKE = ".ilmar-fake"
NAMES = tuple(f"ilmar-it-claude-{s.value.replace('_', '-')}" for s in AgentScenario)


@pytest.fixture(autouse=True)
def _require() -> Iterator[None]:
    require(("podman", PODMAN), ("ILMAR_TEST_PODMAN_IMAGE", IMAGE))
    remove_everything(*NAMES)
    yield
    remove_everything(*NAMES)


@pytest.fixture
def layout(tmp_path: Path) -> WorkspaceLayout:
    layout = WorkspaceLayout.under(tmp_path.resolve() / "state")
    layout.workspaces_root.mkdir(parents=True)
    return layout


def settings() -> ClaudeCodeSettings:
    return ClaudeCodeSettings(executable=f"/workspace/{FAKE}/claude", max_turns=7)


class TestClaudeCodeRunnerOnRealPodman(AgentRunnerContract):
    _layout: WorkspaceLayout
    _workspaces: dict[str, Path]

    @pytest.fixture(autouse=True)
    def _setup(self, layout: WorkspaceLayout) -> None:
        self._layout = layout
        self._workspaces = {}

    def make_runner(self) -> AgentRunner:
        return ClaudeCodeRunner(api_key=KEY, settings=settings())

    def arrange(self, scenario: AgentScenario) -> tuple[ContainerRuntime, ContainerHandle]:
        name = f"ilmar-it-claude-{scenario.value.replace('_', '-')}"
        remove_everything(name)
        workspace = make_workspace(self._layout, f"claude-{len(self._workspaces)}")
        fake = workspace / FAKE
        fake.mkdir()
        script = fake / "claude"
        script.write_text(shell_script(scenario, f"/workspace/{FAKE}/record"))
        script.chmod(0o755)
        runtime = PodmanRuntime(layout=self._layout)
        handle = runtime.start(
            ContainerSpec(
                image=IMAGE,
                host_workspace=workspace,
                name=name,
                owner=OWNER,
                workspace_identity=identity_of(workspace),
            )
        )
        self._workspaces[handle.id] = workspace
        return runtime, handle

    def commands_run_in(self, runtime: ContainerRuntime, container: ContainerHandle) -> int:
        record = self._workspaces[container.id] / FAKE / "record.argv"
        return 1 if record.exists() else 0


def test_arguments_and_credential_reach_only_the_agent_command(layout: WorkspaceLayout) -> None:
    contract = TestClaudeCodeRunnerOnRealPodman()
    contract._layout, contract._workspaces = layout, {}
    runtime, handle = contract.arrange(AgentScenario.COMPLETES)
    try:
        result = contract.make_runner().run(runtime, handle, REQUEST)
        assert result.outcome is AgentOutcome.COMPLETED
        record = contract._workspaces[handle.id] / FAKE / "record"
        argv = record.with_suffix(".argv").read_text().splitlines()
        assert argv[0] == "-p"
        assert argv[-6:] == [
            "--permission-mode", "bypassPermissions", "--no-session-persistence",
            "--strict-mcp-config", "--max-turns", "7",
        ]  # fmt: skip
        env = dict(
            line.split("=", 1)
            for line in record.with_suffix(".env").read_text().splitlines()
            if "=" in line
        )
        assert env["ANTHROPIC_API_KEY"] == KEY
        assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        # The container's own configuration and other commands never carry it.
        config = json.loads(podman("inspect", handle.id).stdout)[0]["Config"]
        assert not any(KEY in item for item in config.get("Env") or [])
        other = runtime.run(handle, ["env"], timeout_seconds=30)
        assert KEY not in other.stdout
    finally:
        runtime.stop(handle, owner=OWNER)


@pytest.mark.parametrize(
    ("env", "settings_file", "expected"),
    [
        ({"ANTHROPIC_AUTH_TOKEN": "not-a-real-token"}, None, "ANTHROPIC_AUTH_TOKEN"),
        ({"CLAUDE_CODE_USE_BEDROCK": "1"}, None, "CLAUDE_CODE_USE_BEDROCK"),
        ({}, '{"apiKeyHelper": "/bin/echo"}', ".claude/settings.json"),
    ],
    ids=["auth-token", "bedrock", "api-key-helper"],
)
def test_another_credential_route_refuses_before_claude_runs_on_real_podman(
    layout: WorkspaceLayout, env: dict[str, str], settings_file: str | None, expected: str
) -> None:
    """ADR-0029 §5: the real probe in a real sandbox finds a route set by the
    container's own environment or by project settings, and Claude Code never
    starts."""
    name = "ilmar-it-claude-route"
    remove_everything(name)
    workspace = make_workspace(layout, "claude-route")
    fake = workspace / FAKE
    fake.mkdir()
    script = fake / "claude"
    script.write_text(shell_script(AgentScenario.COMPLETES, f"/workspace/{FAKE}/record"))
    script.chmod(0o755)
    if settings_file is not None:
        (workspace / ".claude").mkdir()
        (workspace / ".claude" / "settings.json").write_text(settings_file)
    runtime = PodmanRuntime(layout=layout)
    handle = runtime.start(
        ContainerSpec(image=IMAGE, host_workspace=workspace, name=name, owner=OWNER,
                      workspace_identity=identity_of(workspace), env=env)
    )  # fmt: skip
    try:
        with pytest.raises(PolicyViolationError, match=expected) as refused:
            ClaudeCodeRunner(api_key=KEY, settings=settings()).run(runtime, handle, REQUEST)
        assert KEY not in str(refused.value)
        assert "not-a-real-token" not in str(refused.value)
        assert not (fake / "record.argv").exists()  # claude never ran
    finally:
        runtime.stop(handle, owner=OWNER)
        remove_everything(name)
