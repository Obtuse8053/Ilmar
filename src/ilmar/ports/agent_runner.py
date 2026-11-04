# SPDX-License-Identifier: Apache-2.0
"""Port for coding agents."""

from __future__ import annotations

from typing import Protocol

from ilmar.domain.agent import AgentRequest, AgentResult
from ilmar.domain.workspace import ContainerHandle
from ilmar.ports.container_runtime import ContainerRuntime


class AgentRunner(Protocol):
    """Invokes a coding agent inside an already running sandbox.

    The runner receives the runtime explicitly, so any agent adapter works with
    any container runtime adapter. The runner translates the agent's own
    output format into an ``AgentResult``; it never decides whether the work is
    acceptable (ADR-0010).

    Contract (see ``tests/contracts/agent_runner.py``): all commands run in the
    given container; ``COMPLETED`` requires both a clean exit and a structured
    result reporting success (the exit status alone is not trusted, F-021);
    exceeding the request's timeout yields ``TIMED_OUT``; runs are repeatable
    without hidden state.
    """

    @property
    def name(self) -> str: ...

    def run(
        self,
        runtime: ContainerRuntime,
        container: ContainerHandle,
        request: AgentRequest,
    ) -> AgentResult: ...
