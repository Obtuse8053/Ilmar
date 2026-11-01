# SPDX-License-Identifier: Apache-2.0
"""Contract every ``ContainerRuntime`` adapter must satisfy.

The command hooks let each runtime express the same behaviour in its own
terms (a real runtime will use a shell; the fake uses a tiny DSL).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from ilmar.domain.errors import ProviderError
from ilmar.domain.workspace import ContainerHandle, ContainerSpec
from ilmar.ports.container_runtime import ContainerRuntime


class ContainerRuntimeContract:
    def make_runtime(self) -> ContainerRuntime:
        raise NotImplementedError

    def make_spec(self, workspace: Path) -> ContainerSpec:
        raise NotImplementedError

    def echo(self, text: str) -> Sequence[str]:
        raise NotImplementedError

    def exit_with(self, code: int) -> Sequence[str]:
        raise NotImplementedError

    def sleep(self, seconds: float) -> Sequence[str]:
        raise NotImplementedError

    def write_file(self, relative_path: str, content: str) -> Sequence[str]:
        raise NotImplementedError

    @pytest.fixture
    def runtime(self) -> ContainerRuntime:
        return self.make_runtime()

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        path = tmp_path / "workspace"
        path.mkdir()
        return path

    @pytest.fixture
    def container(self, runtime: ContainerRuntime, workspace: Path) -> Iterator[ContainerHandle]:
        handle = runtime.start(self.make_spec(workspace))
        yield handle
        runtime.stop(handle)

    def test_started_container_is_running(
        self, runtime: ContainerRuntime, container: ContainerHandle
    ) -> None:
        assert container.runtime == runtime.name
        assert runtime.is_running(container)

    def test_run_captures_output(
        self, runtime: ContainerRuntime, container: ContainerHandle
    ) -> None:
        result = runtime.run(container, self.echo("hello ilmar"), timeout_seconds=30)
        assert result.succeeded
        assert result.stdout.strip() == "hello ilmar"

    def test_non_zero_exit_is_a_result_not_an_error(
        self, runtime: ContainerRuntime, container: ContainerHandle
    ) -> None:
        result = runtime.run(container, self.exit_with(3), timeout_seconds=30)
        assert result.exit_code == 3
        assert not result.succeeded
        assert not result.timed_out

    def test_timeout_is_reported(
        self, runtime: ContainerRuntime, container: ContainerHandle
    ) -> None:
        result = runtime.run(container, self.sleep(30), timeout_seconds=1)
        assert result.timed_out
        assert not result.succeeded

    def test_workspace_is_shared_with_host(
        self, runtime: ContainerRuntime, container: ContainerHandle, workspace: Path
    ) -> None:
        result = runtime.run(container, self.write_file("out.txt", "done"), timeout_seconds=30)
        assert result.succeeded
        assert (workspace / "out.txt").read_text().strip() == "done"

    def test_stop_is_idempotent_and_final(
        self, runtime: ContainerRuntime, container: ContainerHandle
    ) -> None:
        runtime.stop(container)
        runtime.stop(container)
        assert not runtime.is_running(container)
        with pytest.raises(ProviderError):
            runtime.run(container, self.echo("too late"), timeout_seconds=30)
