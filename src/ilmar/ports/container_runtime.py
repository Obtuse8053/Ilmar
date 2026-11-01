# SPDX-License-Identifier: Apache-2.0
"""Port for isolated execution environments."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from ilmar.domain.workspace import CommandResult, ContainerHandle, ContainerSpec


class ContainerRuntime(Protocol):
    """Starts sandboxes and runs commands in them.

    Contract (see ``tests/contracts/container_runtime.py``):

    * ``run`` reports the command's exit code and output; a non-zero exit is a
      normal result, not an exception.
    * A command that exceeds ``timeout_seconds`` is killed and reported with
      ``timed_out=True``.
    * ``stop`` is idempotent and removes the container. ``is_running`` is
      ``False`` afterwards, and ``run`` raises ``ProviderError``.
    * Only ``spec.host_workspace`` is shared with the host.
    """

    @property
    def name(self) -> str: ...

    def start(self, spec: ContainerSpec) -> ContainerHandle: ...

    def run(
        self,
        handle: ContainerHandle,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...

    def is_running(self, handle: ContainerHandle) -> bool: ...

    def stop(self, handle: ContainerHandle) -> None: ...
