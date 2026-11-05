# SPDX-License-Identifier: Apache-2.0
"""The provider-neutral set of components a coordinator runs with (ADR-0019).

``Components`` is what the adapter registry builds from ``ilmar.toml`` and
what the coordinator (#11) consumes. Everything in it is typed by a port or a
domain value, so the coordinator never names a concrete adapter.

``Components`` owns the lifecycle of what it holds: ``close()`` releases every
resource in reverse construction order, exactly once, and use as a context
manager closes on exit.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

from ilmar.core.layout import WorkspaceLayout
from ilmar.domain.changes import RepositoryRef
from ilmar.domain.errors import ProviderError
from ilmar.domain.verification import VerificationPolicy
from ilmar.domain.workspace import NetworkPolicy, ResourceLimits
from ilmar.ports.agent_runner import AgentRunner
from ilmar.ports.container_runtime import ContainerRuntime
from ilmar.ports.execution_store import ExecutionStore
from ilmar.ports.source_control import SourceControlProvider
from ilmar.ports.task_provider import TaskProvider


@dataclass(frozen=True, slots=True)
class AgentSandbox:
    """How the agent's sandbox is started: image, network and limits.

    The agent runner adapter derives the allowed hosts from what the agent
    needs; configuration cannot widen them. ``UNRESTRICTED`` is never allowed
    here (ADR-0011, ADR-0014).
    """

    image: str
    network: NetworkPolicy
    allowed_hosts: tuple[str, ...] = ()
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    def __post_init__(self) -> None:
        if not self.image:
            raise ValueError("the agent sandbox needs an image")
        if self.network is NetworkPolicy.UNRESTRICTED:
            raise ValueError("the agent sandbox never gets unrestricted network")
        if (self.network is NetworkPolicy.ALLOWLIST) != bool(self.allowed_hosts):
            raise ValueError("allowed_hosts must be given exactly when the network is ALLOWLIST")


@dataclass(frozen=True, slots=True)
class Target:
    """Where changes are made: the repository and the branch they start from."""

    repository: RepositoryRef
    base_branch: str


class Components:
    """The configured adapters and settings, with their lifecycle."""

    def __init__(
        self,
        *,
        instance_id: str,
        layout: WorkspaceLayout,
        task_provider: TaskProvider,
        source_control: SourceControlProvider,
        container_runtime: ContainerRuntime,
        agent_runner: AgentRunner,
        execution_store: ExecutionStore,
        agent_sandbox: AgentSandbox,
        target: Target,
        verification: VerificationPolicy,
        closers: Sequence[Callable[[], None]] = (),
    ) -> None:
        self.instance_id = instance_id
        self.layout = layout
        self.task_provider = task_provider
        self.source_control = source_control
        self.container_runtime = container_runtime
        self.agent_runner = agent_runner
        self.execution_store = execution_store
        self.agent_sandbox = agent_sandbox
        self.target = target
        self.verification = verification
        self._closers = list(closers)

    def __repr__(self) -> str:  # never shows adapter internals, which may hold credentials
        return f"Components(instance_id={self.instance_id!r}, target={self.target!r})"

    def close(self) -> None:
        """Release every resource, in reverse construction order, exactly once."""
        closers, self._closers = self._closers, []
        close_all(list(reversed(closers)))

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def close_all(closers: Sequence[Callable[[], None]]) -> None:
    """Call every closer even if some fail; then raise one error naming all failures."""
    failures: list[Exception] = []
    for close in closers:
        try:
            close()
        except Exception as failure:  # every resource must get its chance
            failures.append(failure)
    if failures:
        combined = ProviderError(f"{len(failures)} resource(s) could not be closed")
        for each in failures:
            combined.add_note(f"{type(each).__name__}: {each}")
        raise combined from failures[0]
