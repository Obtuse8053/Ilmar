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
    StopSignal,
    StreamVerdict,
)


class StreamConsumer(Protocol):
    """Takes a command's standard output as it arrives (ADR-0035 §8).

    Called from one reader thread at a time, never concurrently. It must keep
    its own state bounded, whatever the stream's length.
    """

    def feed(self, chunk: bytes) -> StreamVerdict: ...

    def close(self) -> StreamVerdict:
        """The stream ended (the command exited or was ended)."""
        ...


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
    * A ``stop`` signal that is set before or while a command runs (checked at
      least once a second) is handled **exactly like a timeout**: only the
      runtime's own client process group is killed, the whole sandbox with its
      companion resources is removed by the id the handle holds, and the result
      has ``cancelled=True``. An unconfirmed removal raises ``ContainmentError``
      and quarantines the workspace, as on a timeout. A signal set before the
      command starts runs nothing. A signal that is never set changes nothing.
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
        stop: StopSignal | None = None,
        idle_timeout_seconds: float | None = None,
        stdout_consumer: StreamConsumer | None = None,
    ) -> CommandResult:
        """Run ``argv`` in the container with ``env`` added for this command only.

        ``stop``: see the contract above.

        ``stdout_consumer`` (ADR-0035 §4, §8): every byte of standard output is
        given to ``consumer.feed`` as it is read, and ``consumer.close`` is called
        once at its end. The runtime then keeps **no** standard-output text
        (``CommandResult.stdout`` is empty). A ``VIOLATION`` verdict ends the
        command at once, as a timeout does, and the result is ``rejected``.
        ``idle_timeout_seconds`` needs a consumer: when no ``PROGRESS`` verdict
        arrives for that long (counted from the start), the command is ended the
        same way and the result is ``stalled``. Whichever ending comes first wins.

        ``env`` is where a credential goes (the model key: ADR-0006). An adapter
        must keep it out of anything the runtime persists or shows: not the
        command line, not a file, not the runtime's own records of the command
        (ADR-0023). ``ContainerSpec.env``, by contrast, becomes part of the
        container's definition and never carries a secret.
        """
        ...

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
