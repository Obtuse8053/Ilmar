# SPDX-License-Identifier: Apache-2.0
"""The AgentRunner contract against ClaudeCodeRunner with a scripted fake ``claude``.

The fake speaks Claude Code's documented ``--output-format json`` result. The
same contract runs against a real shell script in real Podman in
``tests/integration/test_claude_code_podman.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from ilmar.adapters.claude_code import ClaudeCodeRunner
from ilmar.domain.workspace import (
    CommandResult,
    ContainerHandle,
    ContainerSpec,
    ResourceOwner,
)
from ilmar.ports.agent_runner import AgentRunner
from ilmar.ports.container_runtime import ContainerRuntime

from claude_code_support import OUTPUTS
from contracts.agent_runner import AgentRunnerContract, AgentScenario
from contracts.container_runtime import identity_of
from fakes import FakeContainerRuntime

OWNER = ResourceOwner(instance_id="test", execution_id="agent", attempt=1, token="t")
API_KEY = "sk-ant-test-not-a-real-key"

_runtime_check: AgentRunner = ClaudeCodeRunner(api_key=API_KEY)


class TestClaudeCodeRunner(AgentRunnerContract):
    _tmp: Path = Path()

    @pytest.fixture(autouse=True)
    def _workspace_root(self, tmp_path: Path) -> None:
        self._tmp = tmp_path

    def make_runner(self) -> AgentRunner:
        return ClaudeCodeRunner(api_key=API_KEY)

    def arrange(self, scenario: AgentScenario) -> tuple[ContainerRuntime, ContainerHandle]:
        runtime = FakeContainerRuntime()

        def claude(argv: Sequence[str], timeout: float, env: Mapping[str, str]) -> CommandResult:
            assert argv[1] == "-p"
            if scenario is AgentScenario.HANGS:
                return CommandResult(-9, "", "", timeout, timed_out=True)
            code, out, err = OUTPUTS[scenario]
            return CommandResult(code, out, err, 1.0)

        runtime.register("claude", claude)
        workspace = self._tmp / scenario.value
        workspace.mkdir(parents=True, exist_ok=True)
        spec = ContainerSpec(
            image="fake:latest",
            host_workspace=workspace,
            name="agent",
            owner=OWNER,
            workspace_identity=identity_of(workspace),
        )
        return runtime, runtime.start(spec)

    def commands_run_in(self, runtime: ContainerRuntime, container: ContainerHandle) -> int:
        assert isinstance(runtime, FakeContainerRuntime)
        return sum(1 for container_id, _ in runtime.commands if container_id == container.id)
