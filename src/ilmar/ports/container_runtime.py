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
    * A command that exceeds ``timeout_seconds`` is reported with
      ``timed_out=True``, and the whole container is stopped and removed: a
      runtime cannot always find every process a command started, and nothing
      may keep writing to the workspace afterwards (ADR-0014). The handle is
      stale from then on. If removal of the container, or of any resource the
      runtime created for it (such as an egress proxy or network), cannot be
      confirmed, ``run`` raises ``ContainmentError`` instead. The runtime then
      refuses to mount that workspace again, under any container name, until a
      later ``stop`` or ``start`` confirms every one of them is gone. ``stop``
      reports a partial teardown the same way.
    * ``NetworkPolicy.UNRESTRICTED`` is refused with ``PolicyViolationError``
      unless the runtime was explicitly configured to permit it (a trusted or
      development deployment). A ``ContainerSpec`` alone never grants it.
    * ``stop`` is idempotent and removes the container. ``is_running`` is
      ``False`` afterwards, and ``run`` raises ``ProviderError``.
    * Only ``spec.host_workspace`` is shared with the host, and only if it
      still has ``spec.workspace_identity``. Otherwise ``start`` raises
      ``OwnershipConflictError`` and starts nothing.
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

    def remove_owned(self, name: str, *, owner: ResourceOwner) -> bool:
        """Remove the sandbox named ``name`` and everything that belongs to it (ADR-0022).

        Works from durable data only (the name and the owner recorded in the
        ledger), so a restarted coordinator can clean up what a crashed one
        left. It removes the container *and* any companion resources the
        runtime created for it (for Podman: the egress proxy and the network).

        * Returns ``False`` if nothing with that name exists, ``True`` after
          removing something.
        * Fails closed before touching anything: if any of those resources
          carries another owner, or no complete owner, or a name matches more
          than one resource, ``OwnershipConflictError`` is raised and all are
          left as they are.
        * Raises ``ContainmentError`` if a removal cannot be confirmed.
        """
        ...

    def stop(self, handle: ContainerHandle, *, owner: ResourceOwner) -> None:
        """Stop and remove the container ``handle.id`` if it is owned by ``owner``.

        Idempotent if the container no longer exists. A different owner raises
        ``OwnershipConflictError`` and the container is left untouched.
        """
        ...
