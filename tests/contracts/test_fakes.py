# SPDX-License-Identifier: Apache-2.0
"""Run the provider contracts against the test fakes.

This keeps the fakes honest: tests that rely on them rely on behaviour real
adapters are also required to have.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from ilmar.domain.changes import RepositoryRef
from ilmar.domain.tasks import Task
from ilmar.domain.workspace import (
    CommandResult,
    ContainerHandle,
    ContainerSpec,
    DirectoryIdentity,
    ResourceOwner,
)
from ilmar.ports.agent_runner import AgentRunner
from ilmar.ports.container_runtime import ContainerRuntime
from ilmar.ports.source_control import SourceControlProvider
from ilmar.ports.task_provider import TaskProvider

from contracts.agent_runner import AgentRunnerContract, AgentScenario
from contracts.container_runtime import ContainerRuntimeContract, identity_of
from contracts.source_control import BASE_FILES, SourceControlContract
from contracts.task_provider import TaskProviderContract
from fakes import (
    FakeAgentRunner,
    FakeContainerRuntime,
    FakeSourceControl,
    FakeTaskProvider,
)
from helpers import task

# Static conformance checks: mypy fails if a fake drifts from its port.
_task_provider: TaskProvider = FakeTaskProvider()
_container_runtime: ContainerRuntime = FakeContainerRuntime()
_source_control: SourceControlProvider = FakeSourceControl()
_agent_runner: AgentRunner = FakeAgentRunner()

FAKE_OWNER = ResourceOwner(instance_id="test", execution_id="agent", attempt=1, token="t")


class TestFakeTaskProvider(TaskProviderContract):
    def make_provider(self) -> TaskProvider:
        return FakeTaskProvider([task("1"), task("2"), task("3")])

    def claim_as_other(self, provider: TaskProvider, task: Task) -> None:
        assert isinstance(provider, FakeTaskProvider)
        provider.claim_as(task.ref, "someone-else")


class TestFakeContainerRuntime(ContainerRuntimeContract):
    def make_runtime(self) -> ContainerRuntime:
        return FakeContainerRuntime()

    def make_spec(
        self, workspace: Path, name: str, owner: ResourceOwner, identity: DirectoryIdentity
    ) -> ContainerSpec:
        return ContainerSpec(
            image="fake:latest",
            host_workspace=workspace,
            name=name,
            owner=owner,
            workspace_identity=identity,
        )

    def create_foreign(
        self, runtime: ContainerRuntime, spec: ContainerSpec, owner: ResourceOwner | None
    ) -> ContainerHandle:
        assert isinstance(runtime, FakeContainerRuntime)
        return runtime.create_foreign(spec, owner)

    def echo(self, text: str) -> Sequence[str]:
        return ["echo", text]

    def exit_with(self, code: int) -> Sequence[str]:
        return ["exit", str(code)]

    def sleep(self, seconds: float) -> Sequence[str]:
        return ["sleep", str(seconds)]

    def write_file(self, relative_path: str, content: str) -> Sequence[str]:
        return ["write", relative_path, content]


class TestFakeSourceControl(SourceControlContract):
    def make_provider(self) -> SourceControlProvider:
        return FakeSourceControl()

    def make_repository(self, provider: SourceControlProvider) -> RepositoryRef:
        assert isinstance(provider, FakeSourceControl)
        repository = RepositoryRef(provider=provider.name, slug="owner/repo")
        provider.seed(repository, "main", BASE_FILES)
        return repository

    def push_external(
        self,
        provider: SourceControlProvider,
        repository: RepositoryRef,
        branch: str,
        files: Mapping[str, str],
    ) -> str:
        assert isinstance(provider, FakeSourceControl)
        return provider.push_external(repository, branch, files)

    def open_foreign_change_request(
        self, provider: SourceControlProvider, repository: RepositoryRef, source_branch: str
    ) -> None:
        assert isinstance(provider, FakeSourceControl)
        provider.open_foreign_change_request(repository, source_branch)


def _result_line(*, is_error: bool, summary: str) -> str:
    return json.dumps({"type": "result", "is_error": is_error, "summary": summary})


_SCENARIOS: dict[AgentScenario, CommandResult] = {
    AgentScenario.COMPLETES: CommandResult(
        0, "working...\n" + _result_line(is_error=False, summary="Fixed the bug.") + "\n", "", 1.0
    ),
    AgentScenario.REPORTS_ERROR_WITH_EXIT_ZERO: CommandResult(
        0, _result_line(is_error=True, summary="API error") + "\n", "", 1.0
    ),
    AgentScenario.CLAIMS_SUCCESS_WITH_NONZERO_EXIT: CommandResult(
        2, _result_line(is_error=False, summary="All done!") + "\n", "", 1.0
    ),
    AgentScenario.CRASHES: CommandResult(139, "", "segmentation fault\n", 1.0),
    AgentScenario.UNPARSEABLE_OUTPUT: CommandResult(0, "I think I finished?\n", "", 1.0),
}


class TestFakeAgentRunner(AgentRunnerContract):
    def make_runner(self) -> AgentRunner:
        return FakeAgentRunner()

    def arrange(self, scenario: AgentScenario) -> tuple[ContainerRuntime, ContainerHandle]:
        runtime = FakeContainerRuntime()

        def agent(argv: Sequence[str], timeout: float, env: Mapping[str, str]) -> CommandResult:
            if scenario is AgentScenario.HANGS:
                return CommandResult(-9, "", "", timeout, timed_out=True)
            return _SCENARIOS[scenario]

        runtime.register(FakeAgentRunner.PROGRAM, agent)
        workspace = self._tmp / scenario.value
        workspace.mkdir(parents=True, exist_ok=True)
        spec = ContainerSpec(
            image="fake:latest",
            host_workspace=workspace,
            name="agent",
            owner=FAKE_OWNER,
            workspace_identity=identity_of(workspace),
        )
        return runtime, runtime.start(spec)

    def commands_run_in(self, runtime: ContainerRuntime, container: ContainerHandle) -> int:
        assert isinstance(runtime, FakeContainerRuntime)
        return sum(1 for container_id, _ in runtime.commands if container_id == container.id)

    _tmp: Path = Path()

    @pytest.fixture(autouse=True)
    def _workspace_root(self, tmp_path: Path) -> None:
        self._tmp = tmp_path
