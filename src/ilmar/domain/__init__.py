# SPDX-License-Identifier: Apache-2.0
"""Provider-neutral domain types. Depends only on the standard library."""

from ilmar.domain.agent import AgentOutcome, AgentRequest, AgentResult
from ilmar.domain.changes import (
    ChangeRequest,
    ChangeRequestSpec,
    CheckResult,
    ChecksSummary,
    CheckState,
    RepositoryRef,
)
from ilmar.domain.execution import Execution, ExecutionState, Transition
from ilmar.domain.tasks import ClaimOutcome, Task, TaskRef
from ilmar.domain.workspace import (
    CommandResult,
    ContainerHandle,
    ContainerSpec,
    NetworkPolicy,
    ResourceLimits,
)

__all__ = [
    "AgentOutcome",
    "AgentRequest",
    "AgentResult",
    "ChangeRequest",
    "ChangeRequestSpec",
    "CheckResult",
    "CheckState",
    "ChecksSummary",
    "ClaimOutcome",
    "CommandResult",
    "ContainerHandle",
    "ContainerSpec",
    "Execution",
    "ExecutionState",
    "NetworkPolicy",
    "RepositoryRef",
    "ResourceLimits",
    "Task",
    "TaskRef",
    "Transition",
]
