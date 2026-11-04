# SPDX-License-Identifier: Apache-2.0
"""Run the ContainerRuntime contract against PodmanRuntime over a fake ``podman``.

This exercises the adapter's own logic offline. The same contract runs against
real Podman in ``tests/integration/test_podman_runtime.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from ilmar.adapters.podman import PodmanRuntime
from ilmar.adapters.podman.runtime import (
    LABEL_ATTEMPT,
    LABEL_EXECUTION,
    LABEL_INSTANCE,
    LABEL_TOKEN,
)
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.safe_fs import ensure_directory
from ilmar.domain.workspace import (
    ContainerHandle,
    ContainerSpec,
    DirectoryIdentity,
    ResourceOwner,
)
from ilmar.ports.container_runtime import ContainerRuntime

from contracts.container_runtime import ContainerRuntimeContract
from fake_podman import FakePodman


def owner_labels(owner: ResourceOwner | None) -> dict[str, str]:
    if owner is None:
        return {}
    return {
        LABEL_INSTANCE: owner.instance_id,
        LABEL_EXECUTION: owner.execution_id,
        LABEL_ATTEMPT: str(owner.attempt),
        LABEL_TOKEN: owner.token,
    }


class TestPodmanRuntimeOverFakePodman(ContainerRuntimeContract):
    _layout: WorkspaceLayout
    _podman: FakePodman

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        self._layout = WorkspaceLayout.under(tmp_path.resolve() / "state")
        self._layout.workspaces_root.mkdir(parents=True)
        self._podman = FakePodman()

    def make_runtime(self) -> ContainerRuntime:
        return PodmanRuntime(
            layout=self._layout,
            environ={},
            executor=self._podman,
            mount_probe=self._podman.mount_probe,
        )

    def make_workspace(self, tmp_path: Path) -> Path:
        path = self._layout.agent_workspace("exec-1", 1)
        ensure_directory(path, root=self._layout.workspaces_root)
        return path

    def make_spec(
        self, workspace: Path, name: str, owner: ResourceOwner, identity: DirectoryIdentity
    ) -> ContainerSpec:
        return ContainerSpec(
            image="localhost/ilmar-test:1",
            host_workspace=workspace,
            name=name,
            owner=owner,
            workspace_identity=identity,
        )

    def create_foreign(
        self, runtime: ContainerRuntime, spec: ContainerSpec, owner: ResourceOwner | None
    ) -> ContainerHandle:
        container_id = self._podman.create(spec.name, owner_labels(owner))
        return ContainerHandle(runtime=runtime.name, id=container_id, name=spec.name)

    def echo(self, text: str) -> Sequence[str]:
        return ["echo", text]

    def exit_with(self, code: int) -> Sequence[str]:
        return ["exit", str(code)]

    def sleep(self, seconds: float) -> Sequence[str]:
        return ["sleep", str(seconds)]

    def write_file(self, relative_path: str, content: str) -> Sequence[str]:
        return ["write", relative_path, content]
