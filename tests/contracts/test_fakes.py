# SPDX-License-Identifier: Apache-2.0
"""Run the provider contracts against the test fakes.

This keeps the fakes honest: tests that rely on them rely on behaviour real
adapters are also required to have.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ilmar.domain.tasks import Task
from ilmar.domain.workspace import ContainerSpec
from ilmar.ports.container_runtime import ContainerRuntime
from ilmar.ports.task_provider import TaskProvider

from contracts.container_runtime import ContainerRuntimeContract
from contracts.task_provider import TaskProviderContract
from fakes import FakeContainerRuntime, FakeTaskProvider
from helpers import task

_task_provider: TaskProvider = FakeTaskProvider()
_container_runtime: ContainerRuntime = FakeContainerRuntime()


class TestFakeTaskProvider(TaskProviderContract):
    def make_provider(self) -> TaskProvider:
        return FakeTaskProvider([task("1"), task("2"), task("3")])

    def claim_as_other(self, provider: TaskProvider, task: Task) -> None:
        assert isinstance(provider, FakeTaskProvider)
        provider.claim_as(task.ref, "someone-else")


class TestFakeContainerRuntime(ContainerRuntimeContract):
    def make_runtime(self) -> ContainerRuntime:
        return FakeContainerRuntime()

    def make_spec(self, workspace: Path) -> ContainerSpec:
        return ContainerSpec(image="fake:latest", host_workspace=workspace)

    def echo(self, text: str) -> Sequence[str]:
        return ["echo", text]

    def exit_with(self, code: int) -> Sequence[str]:
        return ["exit", str(code)]

    def sleep(self, seconds: float) -> Sequence[str]:
        return ["sleep", str(seconds)]

    def write_file(self, relative_path: str, content: str) -> Sequence[str]:
        return ["write", relative_path, content]
