# SPDX-License-Identifier: Apache-2.0
"""Port for isolated execution environments."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from ilmar.domain.workspace import (
    CommandResult,
    ContainerHandle,
    ContainerInfo,
    ContainerSpec,
    ResourceOwner,
)


class ContainerRuntime(Protocol):
    """Starts sandboxes and runs commands in them.

    Contract (see ``tests/contracts/container_runtime.py``):

    * Containers carry their ``ResourceOwner``. Replacing or removing one
      requires presenting an equal owner; otherwise ``OwnershipConflictError``
      is raised and the container is left untouched (ADR-0012).
    * Operations act on ``ContainerHandle.id``, never on the name, so a
      container that was replaced in between is not affected.
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

    def start(self, spec: ContainerSpec) -> ContainerHandle:
        """Create and start ``spec.name`` owned by ``spec.owner``.

        If a container with that name exists and is owned by ``spec.owner``, it
        is removed and replaced. If it has any other owner, or none,
        ``OwnershipConflictError`` is raised and nothing changes.
        """
        ...

    def find(self, name: str) -> ContainerInfo | None:
        """Look up a container by name, running or not."""
        ...

    def run(
        self,
        handle: ContainerHandle,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...

    def is_running(self, handle: ContainerHandle) -> bool: ...

    def stop(self, handle: ContainerHandle, *, owner: ResourceOwner) -> None:
        """Stop and remove the container ``handle.id`` if it is owned by ``owner``.

        Idempotent if the container no longer exists. A different owner raises
        ``OwnershipConflictError`` and the container is left untouched.
        """
        ...
